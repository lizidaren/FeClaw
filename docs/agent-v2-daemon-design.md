# Agent V2: 自驱自主模式设计文档

> **日期**: 2026-07-01
> **状态**: 设计稿（待审）
> **对应范式**: 从"一问一答"到"Agent 始终在线"

---

## 1. 核心目标

将 Agent 从**被动响应者**（收到消息才能动）升级为**自驱自主个体**（像真人一样持续在线、自己决定该做什么）。

### 基础范式对比

```
V1（Classic Agent）：               V2（IM Agent）：

消息 → dispatch → Agent 回复       Agent 创建 = 开始"上班"
    回复完 = 死                  有自己的 Inbox + TODO + 协处理器
                                  收到消息只是中断之一
                                  自己决定：回消息 / 继续干活 / 忽略
```

---

## 2. Agent 两代设计

Agent 创建时选择模式，创建后不可更改。

| 维度 | Classic Agent | IM Agent |
|:----|:-------------|:---------|
| 交互方式 | 一问一答 | 自驱自主 |
| 响应机制 | Push dispatch | 中断驱动 |
| 后台状态 | 无（回复完即休） | 有协处理器常驻 |
| 群聊适合度 | 弱（需 dispatch） | 强（自主决策） |
| TODOs | 可选 | 核心组件 |
| Buffer + Flush | 不需要 | 必须 |
| 共享空间 `{group_id}` | 临时挂载，仅当前群可见 | 长期挂载，不隔离渠道 |

**数据层设计：**

```python
class AgentProfile(Base):
    # ... 已有字段
    agent_mode: str = "classic"  # "classic" | "im"
```

---

## 3. 状态机

### Agent 三层状态

```
DORMANT ─────────── 中断来了 ──────────────────→ WORKING
  ↑                                                  │
  │  WorkSession 关闭                                │
  │  (TODO 全 skip / 无 pending / Watchdog 放行)     │
  │                                                  │
  └──────────────────────────────────────────────────┘
```

### DORMANT

Agent 的自然状态。不占用任何资源：
- 无进程、无 LLM 调用、无 token 消耗
- 存在形式 = DB 一行记录 + COS 若干文件
- 不跑协处理器（协处理器是与 Agent 状态**无关**的独立服务）

### WORKING

Agent 收到中断后进入工作状态，表现是一个 **WorkSession**。

#### WorkSession 定义

```python
class WorkSession:
    id: str                           # uuid
    agent_hash: str
    channel: Channel                   # 触发渠道（group / direct / cron...）
    group_id: Optional[str]            # 如果是群聊
    started_at: datetime
    last_activity: datetime
    interrupt_queue: List[Interrupt]   # 排队的中断（先进先出）
    buffer: Optional[ReplyBuffer]      # 当前缓冲区
    interrupted_count: int             # 本会话中断次数
```

#### WorkSession 子状态

```
PROCESSING     — 正在调 LLM API（等待返回）
TOOL_CALL      — LLM 返回 tool_call，系统正在执行本地代码
INTERRUPTED    — 工具调用返回时发现有新中断，正在注入
FLUSHING       — Agent 调了 flush()，系统执行 TOCTOU 检查 + 发送
WATCHDOG       — LLM API 断了，Watchdog 正在分析
```

#### WorkSession 最长时限

**12 小时**。超时后自动休眠，TODO 保留。
Agent 可以主动宣告完成提前销毁 WorkSession。

---

## 4. 中断系统

### 中断类型（Interrupt Vector Table）

| IRQ | 触发源 | 优先级 | 说明 |
|:---:|:------|:-----:|:----|
| **IRQ_MESSAGE** | 新消息 | 高 | 用户在群里@你，或普通消息 |
| **IRQ_CRON** | 定时器 | 中 | 协处理器 cron 到期 |
| **IRQ_WEBHOOK** | HTTP 回调 | 中 | 外部系统触发 |
| **IRQ_FILE_CHANGE** | 文件变更 | 低 | VFS 文件/日志变化 |
| **IRQ_SEMANTIC_ALERT** | 兴趣话题 | 低 | 协处理器监测到相关话题 |
| **IRQ_BG_TASK_DONE** | 后台任务 | 低 | SubAgent/Bash 执行完成 |
| **IRQ_WATCHDOG** | 系统 | 高 | Agent 干一半断了 |

### InterruptController

一个共享服务，不 per-Agent，不 per-WorkSession。

```python
class Interrupt:
    irq_type: str           # IRQ_MESSAGE / IRQ_CRON / ...
    agent_hash: str
    priority: str           # high / medium / low
    payload: dict           # 携带的具体数据
    created_at: datetime
```

```python
class InterruptController:
    """
    单 worker 模式：直接创建 WorkSession 或入队。
    多 worker 模式（未来）：dispatch() 通过 Redis PubSub 推送。
    """
    
    def dispatch(self, interrupt: Interrupt):
        ws = self._get_active_session(interrupt.agent_hash)
        if ws:
            # Agent 正在 WORKING → 入中断队列
            ws.interrupt_queue.append(interrupt)
        else:
            # Agent DORMANT → 创建 WorkSession
            self._create_work_session(interrupt)
```

### 中断注入时机

**关键设计：每次工具调用（tool_call）返回时是天然的中断注入点。**

高优先级中断（如用户@消息）不会打断当前 LLM 调用——**等当前 LLM 返回后**，在工具调用结果中一并注入。

```
LLM 返回 tool_call
    │
    ▼
系统执行工具（本地代码）
    │
    ▼
系统检查 interrupt_queue 是否有中断
    │
    ├── 有 → 注入到工具调用结果中：
    │        "工具执行成功。系统通知：群内有 2 条新消息..."
    │
    └── 无 → 正常返回
    │
    ▼
LLM 收到结果，继续输出
```

> **未来可扩展**: 等待期间前端可显示"Agent 未读消息数"（已读指示器）。

---

## 5. Buffer + Flush 消息系统

### 为什么需要 Buffer

1. **提供注入点**: 纯回复不走工具调用就没有注入点。Buffer 工具强制提供注入点。
2. **防 TOCTOU**: Agent 写回复的过程中可能有新消息到来——flush 前最后一次检查防止发过时消息。
3. **多媒体附件**: 消息和附件一起写入 buffer，防"回消息、附文件"两张皮。

### Buffer 数据结构

**Buffer 是唯一的**（不是 per 消息渠道的），只能有一个 pending buffer。
**发送到哪里**由 `flush()` 的参数指定。

```python
@dataclass
class ReplyBufferItem:
    content: str
    attachments: List[Attachment]
    created_at: datetime
    version: int

@dataclass
class Attachment:
    type: str                   # "file" | "image"（首批，后续扩展）
    path: str                   # VFS 路径
    caption: Optional[str]

class ReplyBuffer:
    is_dirty: bool              # 是否有未 flush 的内容
    item: Optional[ReplyBufferItem]
```

```python
# MySQL 持久化（防 Agent 看历史记录里自己写过但找不到文件 → 幻觉）
class AgentBuffer(Base):
    __tablename__ = "agent_buffers"
    id = Column(Integer, primary_key=True)
    agent_hash = Column(String(32), unique=True)  # 一个 Agent 只有一个 buffer
    content = Column(Text)
    attachments = Column(JSON)
    version = Column(Integer, default=0)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
```

### Buffer 工具

| 工具 | 功能 |
|:----|:-----|
| `reply_buffer_write(content, attachments)` | 写入缓冲区（覆盖写，结果提示"已覆盖"） |
| `reply_buffer_flush(channel, group_id, to)` | 指定发送渠道后确认发送，执行 TOCTOU 检查 |
| `reply_buffer_cancel()` | 取消待发送的消息 |
| `reply_buffer_update(content, version)` | 修改缓冲区内已存在的消息（类似 edit，不是覆盖） |
| `reply_buffer_stash()` | 暂存当前 buffer，清空 |
| `reply_buffer_pop()` | 恢复最近的一个 stash |

### Flush 流程（TOCTOU 防护）

```
Agent 调用 reply_buffer_flush(channel="group", group_id="...", to="所有人")
               │
               ▼
系统检查：自 Agent 写入缓冲区后，目标渠道内
是否有新消息？
               │
       ┌───────┴────────┐
      有               没有
       │                │
       ▼                ▼
注入新消息到           执行 flush
flush 结果中           │
       │               ✅ 发送消息
       ▼               │
LLM 收到结果           Agent 继续
       │
重新决策：
  ├─ 修改回复 → reply_buffer_update() → 再 flush
  ├─ 取消发送 → reply_buffer_cancel()
  └─ 照发 → 直接 flush（第二次不再检查 TOCTOU）
```

> ⚠️ **Q6：两个 Agent 几乎同时 flush 怎么办？**
> 小概率事件，无法完全避免。多发 1-2 条重复消息是可接受的边界情况。```

**为什么第二次不再检查？** Agent 已经看过新消息并作出了修改决策，此时再检查可能陷入无限循环。相信 Agent 的最后一轮判断。

---

## 6. 群共享空间

### 路径设计

| 路径 | 映射到 COS | 可见性 |
|:----|:----------|:-------|
| `/mnt/group/{group_id}/` | `feclaw/groups/{group_id}/` | 群成员可读写 |
| `/workspace/` | `feclaw/agents/{hash}/workspace/` | 仅 Agent 自己（不变） |

### IM Agent vs Classic Agent

| | IM Agent | Classic Agent |
|:--|:---------|:--------------|
| 挂载方式 | 长期挂载（创建即挂） | 临时挂载（仅当前群上下文中可见） |
| 渠道隔离 | 不隔离，所有群 `/mnt/group/{id}/` 都可见 | 隔离，非当前群/渠道不可见 |
| 单聊时 | 不适用（IM Agent 不纯单聊） | `/mnt/group/` 只读，可回答群相关问题 |
| 实现 | WorkSession 建立时自动挂载 | 工具执行时按群上下文解析 |

### Session Memory 渠道隔离

- **Classic Agent**: 同文件不同 Markdown 章节（如 `## Web` / `## WeChat` / `## Group {id}`）。
  读取时全文件加载不过滤，通过提示词告知当前渠道上下文。兼容存量 `session_memory.md`。
- **IM Agent**: 不隔离，混写。IM Agent 的架构已经从底层解决了渠道感知问题。

### 路径解析（工具层，非 VFS）

`VFS` 不认识 `/mnt/group/`。工具层（`file_write` / `file_read` / `file_list` 等）收到 `group_id` 后自行解析：

```python
def _resolve_path(path: str, agent_hash: str, group_id: Optional[str]) -> str:
    if path.startswith("/mnt/group/"):
        # /mnt/group/{group_id}/xxx → feclaw/groups/{group_id}/xxx
        parts = path.split("/")
        gid = parts[3]
        rest = "/".join(parts[4:])
        return f"feclaw/groups/{gid}/{rest}"
    elif path.startswith("/workspace/"):
        return f"feclaw/agents/{agent_hash}/workspace/{path[11:]}"
    else:
        return f"feclaw/agents/{agent_hash}/{path.lstrip('/')}"
```

---

## 7. 协处理器

### 架构

**协处理器是独立于 WorkSession 的后台 task**，Agent 创建（IM 模式）时启动，删除时销毁。

```
┌─ 协处理器（per IM Agent, 独立 task）────┐
│                                        │
│  asyncio.create_task()                 │
│  → 一直存在，Agent DORMANT 也不停       │
│  → 定时检查/事件监听                    │
│  → 触发条件满足时 → dispatch IRQ       │
│                                        │
│  与 WorkSession 无关                   │
│  Agent 可以"关掉闹钟"（忽略 IRQ）       │
│  但闹钟定了就一定会响                   │
└────────────────────────────────────────┘
```

### 闹钟类型

| 种类 | 触发机制 | 实现方式 | 创建者 |
|:----|:--------|:--------|:------|
| **Cron** | 时间到 | asyncio 定时器，到期 dispatch `IRQ_CRON` | 用户可控 / Agent 可改自己的 |
| **文件变化监测** | 单个文件 CRD（Create/Update/Delete） | 每 10s 轮询 `mtime`，如 `/mnt/group/xxx/plan.md`，可用 pattern 过滤内容 | Agent |
| **兴趣话题监测** | 群消息涉及相关话题 | 轻量 LLM 分析最近 `人数×2` 条消息，发现相关话题则发 `IRQ_SEMANTIC_ALERT` | 系统 |
| **Webhook** | 外部 HTTP 调用 | 注册 HTTP 端点，收到请求后 dispatch `IRQ_WEBHOOK` | 用户/Agent |
| **后台任务完成** | SubAgent/Bash 结束 | 任务结束时回调协处理器，dispatch `IRQ_BG_TASK_DONE` | Agent |

> **Agent 动态改闹钟**: Agent 可以在工作中调用工具修改自己的 cron/heartbeat 间隔。
> 但用户创建的 cron 只读，Agent 不可修改。

### 协处理器注册（工具 CRUD + VFS JSON 配置）

配置存于 VFS 文件 `coprocessor_config.json`（实际后端为 DB，见第 10 节）。

Agent 通过以下工具 CRUD 协处理器配置：

| 工具 | 功能 |
|:----|:-----|
| `coprocessor_add_cron(schedule, task_desc)` | 添加定时任务 |
| `coprocessor_add_file_watch(path, pattern)` | 添加文件监控 |
| `coprocessor_add_topic_watch(group_id, keywords)` | 添加兴趣话题 |
| `coprocessor_list()` | 列出所有闹钟 |
| `coprocessor_remove(id)` | 删除闹钟 |

工具内部步骤：
1. 读 DB 中 `coprocessor_config.json`
2. 修改配置
3. 写回 DB
4. **同时更新运行时协处理器**

```python
async def coprocessor_add_cron(agent_hash, schedule, task_desc):
    configs = _load_config(agent_hash)  # 从 DB 加载
    configs["crons"].append({"id": uuid, "schedule": schedule, ...})
    _save_config(agent_hash, configs)
    # 同步更新运行时
    await CoprocessorManager.reload(agent_hash)
```

**重启恢复**: 服务器重启后，所有 IM Agent 的协处理器从 DB 加载配置，全部重启。用户无感。

免打扰群不发 `IRQ_MESSAGE`，但协处理器持续监测。

```
协处理器每 10 分钟（可配置）：
  拉取该群最近 N 条消息（N = 群人数 × 2）
  调轻量 LLM（qwen3.6-flash 等，无上下文，~500 tokens）：
    prompt: "群消息内容：... 该 Agent 定位：技术负责人。
             是否有与其相关的话题？是/否"
  如果发现相关 → dispatch IRQ_SEMANTIC_ALERT
```

---

## 8. Watchdog 机制

### 触发条件

LLM API 意外中断（超时/网络断/500），Agent 干到一半停了。

### 流程

```
API 断了
    │
    ▼
Watchdog 检查 TODO
    │
    ├── 有 pending messages 或高优先级任务未完成
    │     └── 提示 Agent: "todos: ... 是否继续？"
    │           ├── 继续 → 重启 WorkSession
    │           ├── 暂停 → 标记 skip_check
    │           └── 全部 skip → 放行休眠
    │
    └── 无 pending / 全部 skip_check
          └── 放行休眠 ✅
```

### 限制

| 限制项 | 值 |
|:------|:--:|
| 硬上限 | 10 次连续 Watchdog |
| 每次分析模型 | 超轻量（qwen3.6-flash，无上下文） |
| 分析逻辑 | 最后一句 LLM 输出是啥？ |
| | - 工具调用准备执行 → 重启干活 |
| | - 总结性文本/已宣布完成 → 放行休眠 |
| 超限 | 10 次后自动休眠，TODO 保留 |

---

## 9. Classic + IM 混群

同一个群里可以同时有 Classic Agent 和 IM Agent：

```
用户发消息
    │
    ├── Classic Agent → dispatch 依然工作（与现在一致）
    │
    └── IM Agent → 不 dispatch
          → 协处理器/消息中断自己决定要不要回
```

两种 Agent 在群里的消息格式完全兼容，区别只是"收到消息的方式不同"。

---

## 10. 性能与可扩展性

### IM Agent 数量

不设硬上限。通过实际性能指标（CPU/内存/API 延迟）决定是否扩充服务器资源。

### DB 劫持文件（性能优化）

以下"VFS 文件"实际存储在 DB（SQLite/MySQL）而非 COS，Agent 无感：

| VFS 路径 | 后端 | 原因 |
|:---------|:-----|:-----|
| `todos.json` | DB | 频繁读写，毫秒级 vs COS 数十毫秒 |
| `coprocessor_config.json` | DB | 同上 |
| 其他文件 | COS / LocalStorage | 不变 |

VFS 层检测到这些路径时自动路由到 DB。

### 协处理器开销估计

| 项目 | 估算 |
|:----|:----:|
| 20 个协处理器 asyncio.Task | ~200KB 内存 |
| 文件变化监测轮询（10s/Agent） | 轻微，Python asyncio 几乎无成本 |
| 兴趣话题 LLM 调用（10min/Agent） | ~500 tokens × 6次/h × 20 = ~60K tokens/h |

### 多 Worker 兼容性（未来设计）

现在的 `InterruptController.dispatch()` 是直接方法调用。未来多 Worker 场景：

```python
class InterruptController:
    def dispatch(self, interrupt: Interrupt):
        if MULTI_WORKER_MODE:
            # 通过 Redis PubSub 推送到所有 Worker
            redis.publish("agent:interrupt", interrupt.to_json())
        else:
            # 单进程模式：直接创建/入队
            self._add_interrupt(interrupt)
```

WorkSession 是运行时内存对象，不持久化。Worker 挂了 WorkSession 消失，Agent 回到 DORMANT——TODO 等状态持久化在 COS 上，不受影响。

---

## 11. 施行计划（分期）

| 阶段 | 内容 | 前置 |
|:----|:-----|:-----|
| **P0（近期）** | 清理旧群垃圾消息 | — |
| | 群共享空间 VFS 路径解析 `/mnt/group/` | — |
| | ChatService 复用 + 双写 + 渠道 Session Memory | — |
| **P1** | AgentProfile 加 `agent_mode` 字段 + 创建选择器 | P0 |
| | ReplyBuffer + Flush 工具 | P0 |
| | InterruptController + WorkSession 骨架 | P1 |
| **P2** | 协处理器（Cron 优先） | P1 |
| | Watchdog 机制 | P1 |
| **P3** | 兴趣话题监测（LLM 协处理器） | P2 |
| | Webhook + 后台任务完成通知 | P2 |
| | 文件变化监测 | P2 |
| **P4** | 多 Worker 兼容 | P3 |
