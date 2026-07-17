# FeClaw 技术设计文档（TDD）

> 版本：v1 — 2026-07-09
> 状态：初稿
> 对应 PRD：`docs/prd.md`
> 引用文档：`docs/agent-universe-design.md`、`docs/agent-v2-daemon-design.md`、`docs/desktop-mode-architecture.md`

---

## 1. 架构总览

### 1.1 系统边界

```
┌──────────────────────────────────────────────────────────┐
│                    用户交互层                              │
│                                                          │
│  Browser  →  FeClaw Engine Web UI (Jinja2 → SPA)        │
│  Desktop  →  Tauri Rust + TS 前端                        │
│  Mobile   →  React Native + Skia 画布                    │
│  WeChat   →  第三方消息通道                               │
└──────────────────────────┬───────────────────────────────┘
                           │ HTTPS / WSS
                           ▼
┌──────────────────────────────────────────────────────────┐
│                  FeClaw Engine (FastAPI)                   │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ 渠道层   │  │ 路由层   │  │ 服务层   │  │ 数据层   │ │
│  │ Web      │  │ agent    │  │ chat_svc │  │ SQLite   │ │
│  │ WeChat   │  │ chat     │  │ llm_svc  │  │ COS      │ │
│  │ Desktop  │  │ zentrim    │  │ agent_ex │  │ FileStrg │ │
│  │ Mobile   │  │ universe │  │ search   │  │ VecStrg  │ │
│  │ WS       │  │ fehub    │  │ vfs      │  │          │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘ │
│                                                          │
│  共用基础设施：SmartRouter / IRQ / 工具系统 / 缓存         │
└──────────────────────────────────────────────────────────┘
```

### 1.2 数据流

**聊天消息流：**
```
用户 → 渠道 → ChatService → SmartRouter（并行预取）
     → 主 LLM（流式 SSE） → 工具调用循环 → 回复渠道
```

**Zentrim 捕获流：**
```
用户拍照 → 原始照片立即入库（entries + photo block，零等待）
    ↓ 后台 asyncio task
VLM 判断内容形态（印刷体/手写/混合）
    ↓
印刷体 → VLM→HTML（2 轮审核） → text block 入库
手写   → 智能二值化 → 净图 block 入库
混合   → 印刷体 HTML + 手写语义化（多个 block）

所有结果非阻塞 → 用户不等待处理完成
```

---

## 2. 仓库结构

```
lizidaren/FeClaw/                  ← Python 后端
├── services/                      ← 服务层
│   ├── chat_service.py
│   ├── llm_service.py
│   ├── agent_executor.py
│   ├── virtual_filesystem.py
│   ├── interrupt_controller.py
│   ├── search_service.py
│   ├── fehub_service.py
│   ├── moments_service.py
│   └── ...
├── routers/                       ← API 路由
│   ├── feclaw_chat.py
│   ├── feclaw_domain.py
│   ├── console.py
│   ├── dashboard.py
│   ├── apps_gateway.py
│   ├── fehub.py
│   └── ...
├── models/                        ← 数据模型
│   ├── database.py
│   ├── agent_profile.py
│   ├── chat.py
│   ├── group.py
│   ├── fehub.py
│   └── ...
├── docs/                          ← 架构文档（PRD + TDD 在此）
│   ├── prd.md
│   ├── tdd.md
│   ├── agent-universe-design.md
│   ├── agent-v2-daemon-design.md
│   └── desktop-mode-architecture.md
└── deploy/

lizidaren/FeClaw-Desktop/          ← Tauri 2.0 · Rust · Windows 原生
├── src-tauri/
│   └── src/
│       ├── engine.rs              ← 引擎进程管理
│       ├── ws.rs                  ← WS 隧道
│       ├── consent.rs             ← 原生弹窗确认
│       ├── tray.rs                ← 系统托盘
│       ├── fehub.rs               ← FeHub 浏览（已实现）
│       ├── moments.rs             ← 群广场（已实现）
│       ├── group.rs               ← 群管理（已实现）
│       ├── file_index.rs          ← 本地文件索引（已实现）
│       └── chat/                  ← TS 前端
│           ├── chat.ts
│           ├── store.ts
│           └── components/

lizidaren/FeClaw-Mobile/           ← React Native 跨平台移动端（待创建）
├── src/
│   ├── screens/                   ← 页面
│   ├── components/                ← 共用组件
│   ├── canvas/                    ← 画布组件（Skia / 原生降级）
│   └── store/                     ← 状态管理
└── android/ ios/ harmony/         ← 平台壳
```

---

## 3. 渠道体系

### 3.1 渠道类型

| 渠道 ID | 传输协议 | 客户端 | 离线能力 |
|:--------|:--------|:-------|:--------|
| `web` | HTTP SSE | 浏览器 | ❌ |
| `wechat` | iLink HTTP | 微信 | ❌ |
| `desktop` | WebSocket | Tauri | ✅ 本地模式 |
| `mobile` | HTTP WS | RN App | ✅ 本地数据层 |

### 3.2 统一上下文

所有渠道共享同一 ChatHistory 时间线。Agent 通过 `metadata.channel` 感知来源渠道、通过 `metadata.sender` 定位回复对象。

```python
# ChatHistory 表结构（已有）
class ChatHistory(Base):
    __tablename__ = "chat_history"
    id = Column(Integer, primary_key=True)
    agent_hash = Column(String(32), index=True)
    channel = Column(String(16))          # web / wechat / desktop / mobile
    session_id = Column(String(64))
    role = Column(String(16))             # user / assistant / tool
    content = Column(Text)
    tool_call_id = Column(String(64), nullable=True)   # 2026-07-03 新增
    tool_name = Column(String(64), nullable=True)
    tool_args = Column(JSON, nullable=True)
    created_at = Column(DateTime)
```

---

## 4. Zentrim 数据模型

### 4.1 存储架构

```
Zentrim（按 user_id 统一，跨 Agent）

数据分层：
┌─ 热存储 ──────────────────────────────┐
│  zentrim_entries 表（MySQL）            │
│    ← 条目元数据 + 四层扩展元数据          │
│  zentrim_blocks 表（MySQL）             │
│    ← 所有内容统一由 blocks 表达           │
│    ← text/ink/audio/photo/image/file   │
│  user_profile 表                        │
│    ← 用户画像                           │
└────────────────────────────────────────┘
┌─ 冷存储 ──────────────────────────────┐
│  COS / 本地文件系统                     │
│    ← 原始照片 / 录音 / 笔划原稿         │
│    ← VLM→HTML 产出                     │
│    ← 智能二值化净图                     │
└────────────────────────────────────────┘
┌─ 向量存储 ────────────────────────────┐
│  idx-zentrim-{uid}                     │
│    ← 用户所有 block 的向量索引           │
│    ← 每个 block 独立 embedding          │
│    ← 用于 Zentrim 注意到 + @引用推荐    │
└────────────────────────────────────────┘
```

### 4.2 zentrim_entries 表

```sql
-- zentrim_entries（精简）
CREATE TABLE zentrim_entries (
    id            VARCHAR(26) NOT NULL PRIMARY KEY,  -- ULID
    user_id       INTEGER NOT NULL,
    title         TEXT,
    tags          JSON,
    status        VARCHAR(16) DEFAULT 'active',
    metadata      JSON,                              -- 四层 + 扩展元数据
    created_at    DATETIME,
    updated_at    DATETIME,
    archived_at   DATETIME,
    INDEX idx_zentrim_user_id (user_id),
    INDEX idx_zentrim_created_at (created_at),
    INDEX idx_zentrim_status (user_id, status),
    CONSTRAINT ck_zentrim_status CHECK (status IN ('active', 'archived', 'processing'))
);
```

### 4.3 zentrim_blocks 表

```sql
-- zentrim_blocks（新增）
CREATE TABLE zentrim_blocks (
    id            VARCHAR(26) NOT NULL PRIMARY KEY,  -- ULID
    entry_id      VARCHAR(26) NOT NULL,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    type          VARCHAR(16) NOT NULL,              -- text/ink/audio/photo/image/file
    data          JSON,                              -- 类型专属数据
    text          TEXT,                              -- 搜索用文本
    model_name    VARCHAR(64),                       -- 向量模型名
    vector_id     VARCHAR(64),                       -- 向量索引 ID
    created_at    DATETIME,
    INDEX idx_block_entry (entry_id),
    INDEX idx_block_type (type),
    INDEX idx_block_entry_sort (entry_id, sort_order),
    FULLTEXT INDEX idx_block_text (text)
);
```

**block 类型说明：**

| type | data 内容 | text 来源 |
|:-----|:---------|:---------|
| `text` | `{ content: "markdown..." }` | 用户输入的纯文本 |
| `ink` | `{ strokes: [...], page_id: "..." }` | VLM OCR 提取 |
| `audio` | `{ url: "...", duration: 4512, asr: "..." }` | ASR 转写文本 |
| `photo` | `{ url: "...", mime: "image/jpeg" }` | VLM OCR + 标题 |
| `image` | `{ url: "...", source: "canvas" }` | VLM 语义描述 |
| `file` | `{ url: "...", filename: "...", mime: "..." }` | 文本提取 |

### 4.4 COS / 本地文件路径规划

| 内容类型 | 路径（COS key 或本地路径） |
|:--------|:-------------------------|
| 原始照片 | `zentrim/user_{uid}/attachments/{entry_id}_original.jpg` |
| 智能二值化图 | `zentrim/user_{uid}/attachments/{entry_id}_clean.webp` |
| VLM→HTML | `zentrim/user_{uid}/attachments/{entry_id}_render.html` |
| 录音 | `zentrim/user_{uid}/attachments/{entry_id}_audio.mp3` |
| 画布 strokes | `zentrim/user_{uid}/pages/{page_id}.json` |
| 画布缩略图 | `zentrim/user_{uid}/pages/{page_id}_thumb.webp` |
| block 附件 | `zentrim/user_{uid}/blocks/{block_id}_{filename}` |

### 4.5 向量索引

```
索引名称：idx-zentrim-{uid}
文档内容：每个 block 独立 embedding（block.text + block.data 中的文本）
元数据：  { entry_id, block_id, type, created_at }
模型追踪：block.model_name 记录每个 block 使用的向量模型
路由模型：Qwen 3.6 Flash（与公共知识搜索共用，但限搜 idx-zentrim-{uid}）
```

---

## 5. Zentrim 画布数据模型

### 5.1 坐标系

| 项 | 值 |
|:---|:----|
| 坐标类型 | **Int32**（±21 亿） |
| 锚定策略 | **首次保存时固化锚定** |
| 锚定网格 | 设备分辨率 × 4（文档创建时） |
| 最大缩放 | **400%（4x）** — R1 修复，超出后 UI 禁用缩放按钮 |
| 最小缩放 | 25%（0.25x） |
| Y 轴方向 | **向下**（与屏幕/SVG 一致） |
| 颜色空间 | sRGB（默认），metadata 可指定 P3 |

**跨设备一致性：**
- 坐标是绝对的，不随设备变化
- 同一文档在不同设备上 100% zoom 看到不同内容量（取决于屏幕物理像素）——正常行为
- `device_resolution` 只用于锚定，不用于运行时坐标变换
- 横竖屏切换：画布坐标系不变，视口旋转变为"看的位置变了"，笔划位置不动

### 5.2 page schema

```json
{
  "version": 1,
  "id": "01JARQ3GJ3E5K7Y6Z8D0N2X4V0",
  "strokes": [],
  "images": [],
  "widgets": [],
  "metadata": {
    "first_stroke_at": 1783609000000,
    "last_modified_at": 1783609000123,
    "content_bbox": { "x_min": 0, "y_min": 0, "x_max": 15360, "y_max": 8640 },
    "device_resolution": { "width": 3840, "height": 2160 },
    "thumbnail": "base64_webp_data"
  }
}
```

### 5.3 stroke 数据模型

```json
{
  "id": "01JARQ3GJ3E5K7Y6Z8D0N2X4V1",
  "type": "ink",
  "ts": 0,
  "curves": [
    {
      "p0": [100, 200],
      "p1": [150, 220],
      "p2": [180, 250],
      "p3": [200, 300],
      "samples": [
        { "t": 0.0, "pressure": 0.3 },
        { "t": 0.5, "pressure": 0.8 },
        { "t": 1.0, "pressure": 0.1 }
      ]
    }
  ],
  "style": {
    "color": "#1a1a1a",
    "width": 5.0
  }
}
```

| 字段 | 类型 | 说明 |
|:-----|:-----|:-----|
| `id` | ULID（26 字符） | 稳定唯一标识，用于撤销栈寻址、命中测试、协同（R5） |
| `type` | `"ink" \| "eraser"` | 闭集但可扩展；读到未知 type 时跳过 |
| `ts` | Int64 (ms) | 相对首笔偏移：`stroke_ts - metadata.first_stroke_at` |
| `curves[].p0..p3` | Int32[2] | 三次贝塞尔控制点（绝对坐标） |
| `curves[].samples[].t` | Float32 [0.0, 1.0] | 曲线参数位置（R8：per-curve samples，消除映射歧义） |
| `curves[].samples[].pressure` | Float32 [0.0, 1.0] | 归一化压力（跨设备标准化） |
| `style.color` | String | Hex RGB（#rrggbb） |
| `style.width` | Float32 | 标称笔尖宽度（px），压力在此基础上缩放（范围 0.1x-1.0x） |

**Eraser 特例：**
```json
{
  "id": "01JARQ3GJ3E5K7Y6Z8D0N2X4V2",
  "type": "eraser",
  "ts": 5000,
  "curves": [...],
  "eraser_width": 40
}
```

### 5.4 压感渲染：轮廓填充法

不是逐段 `lineWidth`，而是将贝塞尔曲线等距细分（每段 ~2-4px），按压力值沿法线方向左右偏移顶点，闭合为多边形后一次 `fill()`。

```
细分 → 法线偏移 → 闭合多边形 → Canvas.fill("evenodd")
```

**关键约束：**
- 细分使用 de Casteljau 等距细分（不用 t 参数直接插值，避免弧长不均）
- 法向量使用 parallel transport frame（防止曲率为 0 处的 flip）
- 端点 cap：默认 tapered（起笔/收笔按压力梯度渐变到 minWidth=10%）
- 自相交：默认 `fill("evenodd")` 防止 8 字回环填充错误
- 压力量化：`effective_width = style.width × (0.1 + 0.9 × pressure)`

### 5.5 矢量擦除：增量合成

**运行时（增量更新）：**
- 每画一笔 ink → `source-over` 画上去（O(1)）
- 每画一笔 eraser → `destination-out` 挖掉（O(1)）
- **不遍历历史，只对当前画布做一步操作**
- 关闭 App 时画布内容丢失（GPU 纹理不持久化）

**冷启动（R3 修复）：**
```
① 铺缩略图（page.metadata.thumbnail，WebP base64，~20-50KB）
② 用户可立即书写 → 新笔画画在 overlay canvas（独立透明层）
③ 后台 backbuffer 从 strokes[0] 回放到 strokes[N-1]
④ backbuffer 就绪后替换缩略图，overlay 叠在上面
⑤ 用户不等待、不阻塞、不感知两阶段
```

**缩略图更新策略：** 每次用户保存时增量更新（每 5s 防抖），不做全量重绘。

### 5.6 SVG 导出

**改用 evenodd 单 path（R2 修复），不导出 mask 层。**

```svg
<!-- 一个圆环：外圈 ink，内圈 eraser -->
<path d="
  M 50,20 A 30,30 0 1,1 50,80 A 30,30 0 1,1 50,20
  M 50,30 A 10,10 0 1,0 50,50 A 10,10 0 1,0 50,30
" fill-rule="evenodd" fill="#1a1a1a" />
```

**算法：** 遍历 strokes，按 `ts` 排序，ink 写外路径，eraser 写内路径（子路径）。所有笔划合并为一个 `<path>`。

### 5.7 撤销/重做（R4）

**模型：Command Pattern（运行时内存维护，退出清空）**

```typescript
type Command = {
  type: "add_stroke";
  stroke: Stroke;
  undo: () => void;      // 从 strokes[] 中移除 stroke
  redo: () => void;      // 重新添加 stroke
};
```

- 仅支持按时间倒序撤销（LIFO），对齐市面 APP
- 通过 `stroke.id` 精确寻址
- eraser 的 undo 天然正确：删掉 eraser stroke → 被挖掉的 ink 重新可见
- 撤销栈上限：最近 100 个命令（防止内存溢出）

### 5.8 命中测试（R9 — 未来优化）

MVP 不做。上线后评估：
- N < 1000 笔：暴力遍历距离计算
- N >= 1000 笔：四叉树空间索引

### 5.9 存储格式（R10 — 未来优化）

MVP 用 JSON（`page.json`），后续可升级至 CBOR（换序列化库，不改数据模型）。

---

## 6. Zentrim 拍照入库管线

### 6.1 管线总览

```
拍照 → entry 创建 → 图片存 COS
                    ↓
(后台 asyncio task)
Qwen3.6 Flash 判断：这是文档内容还是随手拍？
         ↓
┌────────────────────┴────────────────────┐
❌ 随手拍（风景/生活照）                     ✅ 文档内容（课本/笔记/屏幕/白板……）
↓                                            ↓
放弃，保持原图                               ├── ✅ 必跑智能二值化 → 存净图（兜底）
                                             │
                                             └── 额外判断："规整度够跑 HTML 吗？"
                                                   ├── ✅ 是 → VLM→HTML → 存 HTML
                                                   └── ❌ 否 → 只有二值化结果
```

**决策依据（2026-07-13）：**
- 第一层分类：是否是"文档内容"（而不是随手拍），用 Qwen3.6 Flash
- 文档内容必跑智能二值化（VLM 迭代调阈值，见 §6.3）
- 规整的内容额外跑 VLM→HTML（豆包 Seed 2.0 Lite）
- HTML 转化失败或被判定为不够规整 → 二值化结果兜底，用户至少有净图
- MVP 不做 VLM 审核轮次

### 6.2 状态机

| 状态 | 时间线标签 | 说明 |
|:----|:----------|:-----|
| `processing` | `⏳ 转换中` | 后台 VLM 处理中，短暂存在 |
| `rendered` | `📐 HTML 已生成` | VLM→HTML 成功 |
| `active` | （无标签） | 原始照片，无需额外处理（含放弃转换的情况）|
| `archived` | （不显示） | 用户归档 |

失败场景：不生成任何标签，用户只能看到原始照片。不加"处理失败"字样。

### 6.3 智能二值化（手写笔记专用）

**流程：**

```
Step 0: 形态判断结果为"手写" → 确认需要智能二值化
                    ↓
1. VLM 识别主要颜色（黑色、蓝色、红色……）
                    ↓
2. 对每种颜色，并行启动迭代优化：
   a. 选定一个居中的该颜色阈值，对图片做二值化处理
   b. 输入 VLM，让它评估结果并调整阈值（变大或变小）
   c. 用新阈值重新处理图片
   d. 重复 b-c 直到收敛（上限 5 轮）
                    ↓
3. 各颜色优化层合成 → 纯色分层超净图
```

**关键设计：**
- 迭代由 VLM 驱动：不靠数学公式算阈值，让 VLM "看"结果好不好，然后调阈值
- 每色独立跑：黑色笔墨、蓝色批注、红色批改——各自的阈值不同
- 并行执行：n 色 × 至多 5 轮，墙钟时间 ≈ 最慢的那色的耗时
- 原始彩色照片始终保留，二值化结果是计算层，可丢弃可重算

---

## 7. Zentrim 注意到算法

### 7.1 触发时机

用户新增一条 Zentrim 条目后，后台异步触发（非阻塞）。

### 7.2 算法流程

```
输入：新条目的文本内容（ASR/OCR/自填）
   ↓
① 向量搜索 idx-zentrim-{uid} → Top 10（语义匹配，按 block 独立 embedding）
② MySQL FULLTEXT 搜索 blocks.text → Top 5（字面匹配，EXISTS 子查询）
③ qwen3-rerank 精排 15 条 → 选 Top 3-5
   ↓
④ 注入 LLM prompt:
   请对比用户的新内容与之前的关联记录，
   找出新内容中用户可能还没关注到的差异点。
   如果没有 → 回复 NONE
   如果有 → 用一句话描述事实
   ↓
⑤ 结果处理
   结果 = NONE → 不生成"注意到"元素
   结果有意义 → 缓存在该条目元数据下（不入用户画像）
```

### 7.3 展示位置

- 最浅：主页底部"你知道吗"一行
- 中等：条目详情页底部"Zentrim 注意到了..."一句话
- 深入：点"了解更多" → 跳转 Agent 对话

### 7.4 关键原则

- 每次独立调用，不依赖上次结果（无级联失败风险）
- 不写长期状态到用户画像
- 用户删除条目 → "注意到"一同删除
- 开关可关闭"允许 Zentrim 访问聊天记录"（默认关）

---

## 8. 全局用户画像

### 8.1 schema

```sql
CREATE TABLE user_profiles (
    user_id    INTEGER PRIMARY KEY,
    self_reported JSON DEFAULT '{}',
    -- { grade: "高一", subjects: ["数学","物理"], goals: "备战期末" }

    observed_by_zentrim JSON DEFAULT '{}',
    -- { active_subjects: ["化学","英语"], recent_terms: [...], content_types: ["photo","note"] }

    observed_by_agent JSON DEFAULT '{}',
    -- { topics_discussed: [...], tools_requested: [...] }

    system JSON DEFAULT '{}',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 8.2 读写边界

| 写入方 | 写入什么 | 用户可删除？ |
|:-------|:---------|:-----------:|
| Zentrim | `observed_by_zentrim` | ✅ |
| Agent | `observed_by_agent` | ✅ |
| 用户 | `self_reported` | ✅ |

**红线：** 不存掌握度、不存性格分析、不存主观评价。

---

## 9. 搜索架构（引用已有实现）

详见 `services/search_service.py` 和 `services/agent_tools_service.py`。

**索引分层：**

| 索引 | 内容 | 范围 |
|:-----|:-----|:-----|
| `idx-{subject}-textbook` | 教材知识库 | 公共 |
| `idx-gaokao` | 高考真题/趋势 | 公共 |
| `idx-{hash}-kb` | Agent AIGC 知识库 | Agent 私有 |
| `idx-zentrim-{uid}` | Zentrim 条目 | 用户私有，跨 Agent |

**搜索流程：**
```
用户消息 → SmartRouter（并行预取判断）
         → search_public_with_quality()
           → Qwen 3.6 Flash 学科路由 + budget 分配
           → 并行查索引（3s 超时）
           → qwen3-rerank 精排
           → 格式化注入【相关知识库】
```

---

## 10. Agent 引擎核心（引用已有文档）

详见 `docs/agent-v2-daemon-design.md`：

| 模块 | 文件 | 说明 |
|:-----|:-----|:-----|
| IRQ 中断系统 | `services/interrupt_controller.py` | 消息/定时器/文件变更/Watchdog |
| WorkSession | 运行时内存对象 | 12 小时超时自动休眠 |
| Buffer+Flush | `models/agent_buffer.py` | TOCTOU 防护，原子写入 |
| 协处理器 | 独立 asyncio task | Cron/文件监测/兴趣话题/Webhook |
| Watchdog | 函数 | API 断连后告警 + TODO 恢复 |

---

## 11. PBL Universe（引用已有文档）

详见 `docs/agent-universe-design.md`：

| 模块 | 说明 |
|:-----|:-----|
| 种子系统 | JSON schema（title/language/seed/constraints） |
| NPC 层级 | 主线角（永远存在）/ 支线角（首次触发后持久化）/ 一次性（用完即弃） |
| 场景持久化 | Minecraft 区块模型：加载/卸载/状态保存 |
| 时间系统 | 真实时间（UTC+8）/ 虚拟时间（用户控制） |
| 三层错误防御 | 检查点 / 反思蒸馏 / 事实-叙事分离 |

---

## 12. API 契约（关键端点）

### 12.1 聊天

```http
POST /api/chat
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "agent_hash": "abcd",
  "message": "帮我解这道题",
  "channel": "mobile",
  "attachments": ["zentrim://entry_xxx.jpg"]
}

Response: SSE stream
event: text
data: {"content": "让我们来看...", "delta": true}

event: tool_call
data: {"tool": "knowledge_search", "args": {...}}

event: done
data: {"final": "所以答案是 42"}
```

### 12.2 Zentrim CRUD

```http
POST /api/zentrim/entries
{
  "title": "...",
  "tags": ["化学", "氧化还原"],
  "blocks": [
    { "type": "photo", "data": { "mime": "image/jpeg", "url": "..." } }
  ]
}
→ { "id": "ulid", "status": "processing", "created_at": "..." }

GET /api/zentrim/entries?user_id=1&limit=20&before=timestamp
→ { "entries": [{ "id", "title", "tags", "blocks": [...] }], "has_more": true }

POST /api/zentrim/entries/{id}/archive
→ { "status": "archived" }

POST /api/zentrim/entries/{id}/blocks
{
  "type": "text",
  "data": { "content": "..." },
  "text": "..."
}
→ { "block_id": "ulid", "sort_order": 1 }

GET /api/zentrim/blocks/{block_id}
→ { "block": {...} }
```

### 12.3 画布同步（文件级）

```http
PUT /api/zentrim/pages/{page_id}
Content-Type: application/json

{ "strokes": [...], "metadata": {...} }

GET /api/zentrim/pages/{page_id}
→ { "page": {...}, "thumbnail": "base64..." }
```

### 12.4 Desktop WS 消息

```json
// Desktop → Engine（注册）
{ "type": "register", "mode": "local" }

// Engine → Desktop（执行命令）
{ "type": "command_exec_request", "id": "uuid", "payload": { "command": "ls" } }

// Desktop → Engine（执行结果）
{ "type": "command_exec_response", "id": "uuid", "status": "ok", "payload": { "stdout": "...", "exit_code": 0 } }

// Desktop → Engine（弹窗确认 pending——高风险操作）
{ "type": "consent_pending", "payload": { "command": "rm -rf C:\\", "risk_level": "L3" } }
```

---

## 13. 离线同步策略

### 13.1 策略：LWW（Last-Writer-Wins）+ 全量覆盖

```
设备 A：在 T1 保存 page.json（version=5）
设备 B：在 T2 保存 page.json（version=6）
  ↓
云端：以 T2 为准（version=6 覆盖 version=5）
  ↓
设备 A 下次同步时：
  - 云端 version=6 > 本地 version=5
  - 下载云端 version=6，覆盖本地
  - 如果本地有未同步的修改 → 冲突提示
```

### 13.2 冲突提示

```json
{
  "conflict": true,
  "local_version": 5,
  "cloud_version": 6,
  "local_modified_at": "2026-07-09T10:00:00Z",
  "cloud_modified_at": "2026-07-09T10:05:00Z",
  "message": "你在这页画布上有未同步的修改。云端有一份更新的版本。要覆盖还是保留本地的？"
}
```

用户选择：**覆盖**（本地被云端覆盖）或者 **保留**（本地修改优先，下次同步再提醒）。

### 13.3 升级路径

- V1：LWW + 全量覆盖（当前）
- V2：增量同步（仅同步变化的 strokes）
- V3：CRDT 自动合并（如有真实多人编辑需求）

---

## 14. 未定项清单

| # | 事项 | 待决策 | 建议 |
|:--|:-----|:------|:-----|
| 1 | Mobile RX vs Flutter | 画布方案最终定夺 | PRD 选 RN（兼顾鸿蒙） |
| 2 | 鸿蒙原生画布 | react-native-skia 能否跑通 | 先走 RN Skia，不行再降级 |
| 3 | CRDT 协同 | 是否引入 Yjs / Automerge | MVP 不引入，LWW 足够 |
| 4 | 二进制存储格式 | CBOR vs FlatBuffers vs 保持 JSON | 先 JSON，等性能瓶颈再换 |
| 5 | hit-testing 实现 | 暴力遍历 vs 四叉树 | MVP 不做 |
| 6 | VLM 型号选型 | 轻量化多模态模型 | 已决：形态判断用 Qwen3.6 Flash，VLM→HTML 用豆包 Seed 2.0 Lite |
| 7 | Padless 硬件 | 三层堆叠概念 | 存档 |
| 8 | 3D 世界引擎 | 混元3D vs Marble vs 不搞 | MVP 不进 3D |

---

## 15. Build Plan

### MVP

| 周期 | 内容 |
|:----|:-----|
| Week 1-2 | Agent 聊天现有功能稳定 + Zentrim 条目 CRUD + 时间线 |
| Week 3-4 | Zentrim 画布 v1（缩放 2x、<5000 笔、JSON 存储、缩略图冷启动、evenodd 导出） |
| Week 5-6 | Zentrim 拍照入库管线（印刷体 → VLM→HTML） |
| Week 7-8 | Mobile RN 壳 + 聊天 + 简单画布 |
| Week 9-10 | 离线 LWW 同步 + 冲突提示 |
| 持续 | 测试 + bug 修复 |

### V2

| 范围 | 内容 |
|:-----|:-----|
| Zentrim 录音入库 | ASR + 关键提取 |
| Zentrim @引用 | 双向引用 + 推荐 |
| Zentrim 注意到 | 混合搜索 + LLM |
| 智能二值化 | 手写笔记 → 净图 |
| Universe MVP | 枫叶镇英语（1 种子，3-5 NPC） |
| 全局用户画像 | 三层读写 |

### V3

| 范围 | 内容 |
|:-----|:-----|
| Universe V2 | 澜汐小镇理科 + God Mode |
| Work 群广场 | Agent 群聊 + 动态发布 |
| FeHub | 小程序发布/浏览 |
| Desktop 云模式 | WS 隧道中继远程命令 |
| HarmonyOS RN 适配 | 兼容性测试 |
| 性能优化 | 二进制存储 / 画布 LOD |

---

## 16. 扩展性考量 & 已知风险

### 16.1 已知风险

| 风险 | 等级 | 缓解 |
|:-----|:----:|:-----|
| LLM token 成本随 DAU 线性增长 | 🔴 | SmartRouter 预分类 + 小模型兜底 + 缓存 |
| SQLite 单写瓶颈 | 🟡 MVP 无问题 | 已预留 MySQL 切换路径 |
| IRQ WorkSession 重启丢失 | 🟡 | MVP 容忍，V2 加 Redis |
| per-Agent 协程数膨胀 | 🟡 | MVP 小规模没问题，V2 换 Celery |
| react-native-skia 鸿蒙兼容 | 🟡 | 备选：原生画布组件 |
| 画布 JSON 存储体积增长 | 🟢 | MVP 可接受，换 CBOR 是单步迁移 |

### 16.2 扩展性承诺

| 能力 | 100 DAU | 10K DAU | 100K DAU |
|:-----|:-------:|:-------:|:--------:|
| LLM 调用 | ✅ 单实例 | ✅ 水平扩展 | ✅ 水平扩展 |
| SQLite | ✅ | 🟡 切 MySQL | ✅ MySQL |
| 向量搜索 | ✅ | ✅ | ✅ |
| FastAPI 路由 | ✅ | ✅ | ✅ |
| 画布存储 | ✅ JSON | ✅ JSON/CBOR | ✅ CBOR |
| 渠道统一 | ✅ | ✅ | ✅ |
| 工具系统 | ✅ | ✅ | ✅ |

---

> **变更记录**
>
> v1 — 2026-07-09：初稿，含画布审计修复（R1-R3）和全局架构决策
