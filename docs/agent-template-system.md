# Agent 模板系统设计

> v1 — 2026-07-16
> 将硬编码的 PERSONA_TEMPLATES 迁移到数据库，并为社区模板共享做准备。

---

## 1. 数据模型

### 1.1 `agent_templates` 表

```python
class AgentTemplate(Base):
    __tablename__ = "agent_templates"

    # ── 标识 ──
    id          = Column(String(32), primary_key=True)
    # 内置模板: "internal::guide" / "internal::default"
    # 社区模板: ULID（26 位大写字母+数字）
    # 判断: id.startswith("internal::") → 内置

    name        = Column(String(100), nullable=False)       # "AI 向导"
    description = Column(String(500))                       # "像向导一样陪你学习..."

    # ── 核心数据（JSON） ──
    definition  = Column(JSON, nullable=False)

    # ── 元信息 ──
    is_builtin  = Column(Boolean, default=False)    # 内置模板不可删除
    sort_order  = Column(Integer, default=0)

    # ── 创作信息 ──
    author_id   = Column(Integer, ForeignKey("users.id"), nullable=True)
    author_name = Column(String(100))

    # ── 版本与兼容 ──
    version          = Column(String(20), default="1.0.0")
    feclaw_version   = Column(String(20))

    # ── 分类与发现 ──
    category    = Column(String(50))         # "learning" / "coding" / "writing"
    tags        = Column(JSON)               # ["数学", "高中", "高考"]
    language    = Column(String(10), default="zh-CN")
    icon        = Column(String(50))         # emoji 或 URL

    # ── 共享许可 ──
    license     = Column(String(50), default="MIT")

    # ── 合规审查 ──
    compliance_category  = Column(String(50), default="education")
    compliance_status    = Column(String(20), default="pending")
    # pending → passed → rejected / suspended（管理员手动停用）
    compliance_reviewed_at = Column(DateTime, nullable=True)
    compliance_reason      = Column(String(500), nullable=True)

    # ── 时间 ──
    created_at  = Column(DateTime)
    updated_at  = Column(DateTime)
```

### 1.2 `agent_profiles` 追加字段

```python
template_id      = Column(String(32), nullable=True)    # 从哪个模板创建
template_version = Column(String(20), nullable=True)    # 创建时的模板版本
```

---

## 2. `definition` JSON 格式

这是模板最核心的字段。存什么、怎么用，全部定义在此。

### 2.1 完整结构

```json
{
  "persona": "Agent 系统提示词全文...",

  "tools": {
    "enabled": ["file_read", "file_write", "web_search", ...],
    "disabled": ["bash", "python_background"]
  },

  "config": {
    "max_tool_rounds": 30,
    "compression_ratio": 0.4,
    "sr_enabled": true
  },

  "default_files": {
    "workspace/agent/soul.md": "人格设定全文...",
    "workspace/agent/identity.md": "身份配置全文...",
    "workspace/agent/user.md": "用户画像引导模板..."
  }
}
```

### 2.2 字段说明

#### `persona`（字符串，必填）
- Agent 的系统提示词。写入 DB 的 `AgentConfig::{hash}/persona`。
- 同时作为初始化 VFS 时 `workspace/agent/soul.md` 的内容（除非 `default_files` 里有显式覆盖）。

#### `tools`（对象，可选）
- 创建 Agent 时用于覆盖默认的工具列表。
- 缺省时使用 `agent_init_service.py` 中的硬编码默认值。

| 子字段 | 类型 | 说明 |
|:-------|:-----|:------|
| `enabled` | string[] | 默认启用的工具 |
| `disabled` | string[] | 默认禁用的工具 |

#### `config`（对象，可选）
- Agent 运行时参数。
- 缺省时使用 `agent_init_service.py` 中的硬编码默认值。

| 子字段 | 类型 | 默认值 | 说明 |
|:-------|:-----|:-------|:------|
| `max_tool_rounds` | int | 50 | 单轮对话最多工具调用次数 |
| `compression_ratio` | float | 0.3 | 上下文压缩比率 |
| `sr_enabled` | bool | false | 是否启用 SmartRouter |

#### `default_files`（对象，可选）
- 初始化时写入 VFS `agents/{hash}/` 目录的文件。
- 键是 VFS 相对路径，值是文件内容。
- 缺省时使用 `agent_init_service.py` 中对应的 `DEFAULT_*` 常量。

**常用路径：**
| 路径 | 用途 | 备注 |
|:-----|:-----|:------|
| `workspace/agent/soul.md` | 人格设定 | 优先级高于 `.persona` 写入的 soul.md |
| `workspace/agent/identity.md` | 身份配置 | |
| `workspace/agent/user.md` | 用户画像 | 模板可提供引导模板，让 Agent 首次对话时主动询问用户信息 |
| `workspace/agent/memory.md` | 长期记忆 | 初始内容 |

---

## 3. 模板加载流程

```
服务启动
  ↓
init_db() 检查 agent_templates 表
  ├── 有数据 → 跳过
  └── 无数据 → INSERT 6 个内置模板（is_builtin=True）
        ├── internal::default   (sort_order=0)
        ├── internal::guide     (sort_order=1)
        ├── internal::learning  (sort_order=2)
        ├── internal::coding    (sort_order=3)
        ├── internal::writing   (sort_order=4)
        └── internal::creative  (sort_order=5)
```

---

## 4. API 端点

### `GET /api/console/templates`
返回所有 `compliance_status = "passed"` 的模板列表。

```json
{
  "status": "success",
  "templates": [
    {
      "id": "internal::guide",
      "name": "AI 向导",
      "description": "像向导一样陪你学习...",
      "icon": "🧭",
      "category": "learning",
      "tags": ["学习", "向导", "引导式教学"],
      "version": "1.0.0",
      "is_builtin": true,
      "sort_order": 1
    }
  ]
}
```

注意：**不返回 `definition` 内容**。模板的 persona 在创建 Agent 时由前端选择后传参。

### `GET /api/console/templates/{id}`（未来）
返回单个模板详情（含 definition，用于 Agent 设置页的模板预览/重新应用）。

### `POST /api/console/templates`（未来，社区）
创建社区模板。触发合规检测。

### `PUT /api/console/templates/{id}/suspend`（未来，管理员）
管理员手动停用模板。

---

## 5. 合规检测

### 触发时机
- 创建新模板时（未来社区功能）
- 修改现有模板时

### 检测流程

```
用户提交模板
  ↓
调 LLM 检查（prompt）：
  "以下模板 persona 是否属于『学习教育/知识问答/工作助手』等
   豁免类别？如否，请说明触犯红线（拟人化情感陪伴）的理由。"
  ↓
LLM 返回 { "compliant": true/false, "reason": "..." }
  ↓
compliant=true  → compliance_status = "passed"
compliant=false → compliance_status = "rejected"
                  compliance_reason = LLM 返回的理由
```

### 审核状态

| 状态 | 含义 | 前端展示 |
|:-----|:------|:---------|
| `pending` | 等待审核 | 不可用，显示"审核中" |
| `passed` | 合规通过 | 正常展示 |
| `rejected` | LLM 检测不通过 | 不可用，显示拒绝原因 |
| `suspended` | 管理员手动停用 | 不可用，显示"已被管理员停用" |

---

## 6. 内置模板清单

| ID | 名称 | category | sort_order | persona 要点 |
|:---|:-----|:---------|:----------|:-------------|
| `internal::default` | 默认助手 | general | 0 | 工具调用、记忆分层 |
| `internal::guide` | AI 向导 | learning | 1 | 三条铁律：先了解/不替做/先共情 |
| `internal::learning` | 学习助手 | learning | 2 | 循序渐进、耐心教学 |
| `internal::coding` | 编程助手 | coding | 3 | 代码规范、最佳实践 |
| `internal::writing` | 写作助手 | writing | 4 | 文档创作、内容优化 |
| `internal::creative` | 创意助手 | creative | 5 | 发散思维、灵感激发 |

---

## 7. 迁移计划

### 步骤
1. 建表 + 追加 `AgentProfile` 字段
2. 服务启动时 INSERT 内置模板（`init_db` 中）
3. 新建 `services/template_manager.py`
   - 提供 `list_templates()` / `get_template()` / `get_definition()` 方法
4. 改造 `agent_init_service.py`
   - 删除 `PERSONA_TEMPLATES`（180 行）
   - `get_persona_templates()` 改为调 `TemplateManager`
   - `initialize_agent()` 接受 `template_id` 参数，从 definition 读取 tools/config/default_files
5. 改造 `routers/console.py`
   - `/api/console/templates` 改为查 DB
6. 改造前端创建 Agent 流程
   - 创建时传 `template_id`
   - 初始化时传 `template_id`（服务端从 DB 拉 definition）

### 不涉及
- 现有 Agent 不受影响（`template_id` 字段默认为 NULL）
- 现有 API 响应格式尽量保持兼容
