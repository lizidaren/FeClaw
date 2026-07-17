# Zentrim（格物所）设计文档

> 版本：v1.0.0
> 最后更新：2026-07-10
> 状态：初稿
> 对应 PRD：`docs/v1/01-prd.md`
> 对应 TDD：`docs/v1/06-tdd.md`

---

## 1. 产品概述

### 1.1 一句话定位

Zentrim（格物所）是 FeClaw 用户的个人学习数据沉淀、整理、回顾系统。所有用户在 FeClaw 中产生的、主动存入的内容，最终汇入 Zentrim 的时间线。

### 1.2 哲学红线

| # | 原则 | 含义 |
|:-:|:-----|:-----|
| 1 | **输入即整理** | flomo 式交互，写时顺手打标签 |
| 2 | **不整理 > 整理** | 不强迫分类，纯时间线 |
| 3 | **回顾 > 记录** | 前瞻关联：放入内容后 AI 主动做延伸 |
| 4 | **克制边界** | 只做整理与扩展，不做写作/搜索/生成 |
| 5 | **不评价不说看法** | 客观陈列差异，不做价值判断 |
| 6 | **可信任的主动性** | 用户放入前不监听，放入后处理完全可见 |

### 1.3 三端分工

| 功能 | 手机 | Pad | Desktop |
|:-----|:----:|:---:|:-------:|
| Zentrim 主页浏览 | ✅ 双 Tab | ✅ 居中布局 | ✅ |
| 打字速记 | ✅ flomo 输入框 | ✅ 输入框 | ✅ |
| 拍照入库 | ✅ 原生相机 | ✅ 相机 | ❌ |
| 录音三件套 | 🟡 仅录音 | ✅ 录音+手写+拍照全过程 | ❌ |
| 画布手写 | ❌ 无笔 | ✅ 核心 | ❌ |
| 草稿纸 | ❌ | ✅ 左下角专用按钮 | ❌ |
| 时间线搜索 | ✅ | ✅ | ✅ |

---

## 2. Zentrim 主页（手机 & Pad 共用布局）

### 2.1 手机端

```
┌──────────────────────────────┐
│  ☀️ 早上好，今天有物理课      │  ← 文字问候语（每日更新）
│                               │
│  💡 你有一段录音提到了        │  ← Zentrim 注意到（一行小字）
│     Kimi K2.7                 │      点击展开弹窗
│                               │
│  📋 TODO    📈 完成度  📅    │  ← 三张轻量卡片并排
│  3未完成    2待回顾   全部   │     点 📅 进入完整时间线
│                               │
│                       ＋     │  ← 唯一入口，点击进入画布
└──────────────────────────────┘
```

**说明**：主页不放输入框，不放「写点随笔」「画布」按钮——只剩一个 ＋ 浮窗按钮。
点 ＋ → 直接进入画布；画布内打字产生 `text` block，手写笔产生 `ink` block，两种 block 共存。

### 2.2 Pad 端

- 排版更居中，不把输入框摁到底部
- 四周留边距
- ＋ 按钮位置更醒目（右下角悬浮，或顶部居中）
- 默认进入 Zentrim Tab 时，如果是首次使用则显示主页，否则可直接进入画布

### 2.3 画布内的双模态共存

进入画布（点 ＋ 后）：

```
┌─────────────────────────────────────────┐
│ ← Untitled                       [⋮]    │
│                                          │
│   ┌─ text block ────────────────┐       │
│   │ 这里的笔记会落成 text block  │       │  ← canvas-editor（键盘）
│   └─────────────────────────────┘       │
│                                          │
│   ┌─ ink block ─────────────────┐       │
│   │   ✏️ 手写区域                │       │  ← Skia（笔）
│   │   （与 text 共存，可拖动）   │       │
│   └─────────────────────────────┘       │
│                                          │
└─────────────────────────────────────────┘
```

- **打字 → text block**（canvas-editor，markdown 渲染）
- **手写 → ink block**（Skia，压感笔划）
- 两者并存，按 sort_order 排列，可拖动重排
- 退出画布 → 自动落成一条 entry（包含若干 blocks）

### 2.4 冷启动态（零条目）

- 显示使用引导 + 一条示例 entry（含 text/ink/photo 各类 block 示例）
- 点 ＋ 直接进入画布体验双模态
- 三天后还是零条 → 温和提醒一次
- 一周后零条 → 不再打扰

---

## 3. Zentrim 注意到

### 3.1 触发

用户新增一条 Zentrim 条目后，后台异步触发（非阻塞）。

### 3.2 算法

```
输入：新条目的文本内容（ASR/OCR/自填）
   ↓
① 向量搜索 idx-zentrim-{uid} → Top 10（语义匹配）
② MySQL FULLTEXT 搜索 → Top 5（字面匹配）
③ qwen3-rerank 精排 15 条 → 选 Top 3-5
   ↓
④ LLM prompt：
   对比用户的新内容与之前的关联记录，
   找出新内容中用户可能还没关注到的差异点。
   如果没有 → 回复 NONE
   如果有 → 用一句话描述事实
   ↓
⑤ 结果处理
   结果 = NONE → 不生成
   有意义 → 缓存在该条目元数据下
```

### 3.3 UI 行为

**主页底部一行小字：**
```
💡 你有一段录音提到了 Kimi K2.7，之前你没关注过
```

**点击后弹窗：**

```
┌─ 💡 Zentrim 注意到 ─────────────────┐
│                                    │
│  "2026.7.1的录音提到了             │
│   Kimi K2.7-coding，也许感兴趣？"  │
│                                    │
│  🎙️ AI 模型调研 · 07-01  ▸        │  ← 来源卡片，可点击
│                                    │
│  [ 展开看看 ]                      │  ← 展开预缓存的客观介绍
│                                    │    不评价用户内容
│                                    │    不反驳用户观点
│                                    │    无人格，非对话形态
│                                    │
│  [加入 07-01 录音的附录]           │  ← 存到计算层，不改原始层
│                                    │    支持多媒体附件
│                                    │
│  [不感兴趣]            [OK]        │  ← 反馈入口
│                                    │    展开与否做兴趣信号
└────────────────────────────────────┘
```

**关键约束：**
- 不追问（无输入框）
- 不保存到 Zentrim（除非用户手动点「加入附录」）
- 不评价用户内容（录音里骂 Kimi 差，不反驳）
- 展开行为作为隐式兴趣信号

---

## 4. 时间线 & 子时间线

### 4.1 主时间线

按时间倒序排列，所有类型混排（不设分类 Tab）。

```
📅 2026-07-09 星期四
├── 📷 化学试卷批改        ✓ 已处理       ← 状态标签
├── 🎙️ 英语课堂录音        ⏳ 转换中
├── 📝 氧化还原笔记                        ← 无标签=原始态
└── 🔗 熵增的文章           📐 HTML已生成

📅 2026-07-08 星期三
├── 📝 三角函数总结
├── 📷 物理试卷              🎨 已增强
└── 🎙️ 码头调研录音          ✓ 已处理
```

**状态标签：**

| 标签 | 含义 |
|:----|:-----|
| ⏳ 转换中 | VLM/ASR 后台处理中 |
| 🎨 已增强 | 智能二值化完成 |
| 📐 HTML已生成 | VLM→HTML 完成 |
| ✓ 已处理 | 处理完成 |
| （无标签） | 原始内容，无需处理 |

### 4.2 子时间线（标签式筛选）

- 本质是标签，一个条目可属于多个子时间线（多对多）
- 不代替主时间线——主时间线永远完整可用
- 创建方式：长按/右键条目 → 「加入子时间线」→ 选已有或新建，体验类似相册
- 主页进入：点 📅 全部卡片 → 进入完整时间线视图 → 可见子时间线列表

```
📁 我的子时间线
├── 🧪 化学期末复习（5 条）
├── 📐 三角函数专题（3 条）
├── 🏖️ 码头调研项目（12 条）
└── ＋ 新建
```

---

## 5. 搜索

### 5.1 入口

Zentrim 主页顶部搜索栏 🔍，点开进入搜索覆盖层（不离开 Zentrim 上下文）。

### 5.2 搜索范围

搜索单位是 **block**（不是 entry），每个 block 独立被搜：

| Block 类型 | 搜索内容（block.text） | 来源 |
|:----------|:---------------------|:-----|
| `text` | 用户输入的 markdown 文本 | 用户输入 |
| `ink` | VLM OCR 提取的手写文本 | 计算层（VLM） |
| `audio` | ASR 转写全文 | 计算层（ASR） |
| `photo` | VLM 标题 + OCR 文本 | 计算层（VLM） |
| `image` | VLM 语义描述 | 计算层（VLM） |
| `file` | 文件内文本提取 | 计算层（提取） |
| 子时间线 | 名称 + 描述 | 用户命名 |

### 5.3 搜索算法

```
用户打字 → 300ms 防抖 → 并行执行：
  ① 向量搜索 idx-zentrim-{uid}
       → 每个 block 独立 embedding → 命中 block 的 entry + block_type
  ② FULLTEXT 搜索 blocks.text
       → MySQL FULLTEXT（INDEX idx_block_text）
       → 命中 block 的 entry + block_type
  → 合并去重 → qwen3-rerank 精排 Top 20
  → 返回 UI（每条结果展示其所在 entry + 高亮 block 摘要）
```

**关键变化**：旧版搜 `entry.content`（一条文本），新版搜 `blocks.text`（多条文本，用 `EXISTS` 子查询）。
每个 block 独立 embedding、独立 `model_name` 追踪，便于多模型并存与重算。

### 5.4 结果展示

每条结果：类型图标 + 所属 entry 标题 + 高亮匹配的 block 摘要 + 日期 + 所属子时间线。
录音 block 附加时间戳跳转。画布 block 展示所在画布的缩略图。

### 5.5 手写画布搜索（ink block）

**方案 C（OCR + VLM 双通道，写入 ink block.text）：**

```
用户保存画布 →
  ① 分块：画布切 1024×1024 瓦片（10% 重叠）
  ② 每瓦片跑 VLM（豆包 Seed 2.0 Lite）
  ③ 一个 LLM 聚合所有瓦片结果为全文
  ④ 写入 ink block.text（供 FULLTEXT 搜索）
  ⑤ 同时 VLM 输出语义描述（也写入 block.text 或单独 semantic_summary 字段）
  ⑥ 每个 ink block 独立 embedding → 写入 idx-zentrim-{uid}

备选：全模态 embedding 向量化瓦片（效果不会太好）
```

**成本：** VLM 调用次数 = 瓦片数 × 保存次数。MVP 阶段画布页数少时可接受。

---

## 6. 条目详情页

### 6.1 纯文本笔记

显示标题 + 全文 Markdown 渲染。

### 6.2 照片条目

```
详情页默认展示：
  印刷体 → VLM→HTML 渲染页
  纯手写 → 智能二值化净图
  混合   → VLM→HTML 正文 + 底部 👁️ 切原图

处理失败 → 不展示任何标签，保留原始照片
```

### 6.3 录音条目（Pad 横屏）

```
┌─────────────────────────────────────────┐
│  🎧 ▸ 45:12 ⋮ [📝ASR] [🔊音轨] [📷]  │  ← 顶部菜单
│                                          │    不放底部（防全面屏手势误触）
│                                          │
│        画布（全屏，主体）                  │
│                                          │
│  📷 照片1（⏱️ 18:30）   手写笔记...      │
│                                          │
│         📷 照片2                         │
│                                          │
└─────────────────────────────────────────┘
```

**录音气泡行为：**
- **录音态：** 左上角小气泡，只显示 🎧 符号 + 时间码。无红色无闪烁。
- **回放态：** 拉长占满顶部横向空间。显示 [📝ASR] [🔊音轨] [📷]。
- **首次录音**时提示用户。
- **转写完成前**不显示 ASR 和音轨按钮。
- **多音轨**时显示 🔊 按钮，单音轨不显示。

**图片双向锚定：**
- 画布上照片右下角 ⏱️ 18:30 标签 → 点它跳到录音对应时间播放
- ASR 覆盖层里 📷 [缩略图] → 点它画布滚动到照片位置

**多段录音（同一条目）：**
```
┌────────────────────────────────────┐
│ 🎙️ (1/3) 45:12 📷3  → 播放 / 下载 │  ← 各段独立
│ 🎙️ (2/3) 30:05 📷1  → 播放 / 下载 │
│ 🎙️ (3/3) 12:00 📷0  → 播放 / 下载 │
├────────────────────────────────────┤
│          共用画布                    │
├────────────────────────────────────┤
│  ASR 全文                           │
│  第一段转写...                       │
│  ─── 录音间隙 10 分钟 ───          │
│  第二段转写...                       │
└────────────────────────────────────┘
```

**多设备关联（捕获会话）：**
- Pad 点「开始三件套」→ 创建捕获会话
- 手机检测到同账号活跃会话 → 询问「是否并入」
- 首次手动确认，后续可加自动关联
- 不自动推荐主音轨

### 6.4 画布条目

纯手写的画布条目，详情页直接进入画布编辑模式。

### 6.5 详情页底部

- 「你可能也关注」：LLM 驱动关联推荐（基于条目全文+同子时间线上下文，异步）
- 图片/录音/画布条目标注「被？条引用」（来自 @引用）
- 点 @引用 跳转到引用方条目

---

## 7. 画布

### 7.1 坐标系

| 项 | 值 |
|:---|:----|
| 坐标类型 | Int32（±21 亿） |
| 锚定策略 | 首次保存时固化：设备分辨率 × 4 |
| 最大缩放 | 400%（4x），超出禁用 |
| 最小缩放 | 25%（0.25x） |
| Y 轴方向 | 向下（与 SVG 一致） |

### 7.2 stroke 数据

```json
{
  "version": 1,
  "id": "01JARQ3GJ3E5K7Y6Z8D0N2X4V0",
  "strokes": [],
  "images": [],
  "widgets": [],
  "metadata": {
    "first_stroke_at": 1783609000000,
    "content_bbox": {"x_min": 0, "y_min": 0, "x_max": 15360, "y_max": 8640},
    "device_resolution": {"width": 3840, "height": 2160},
    "thumbnail": "base64_webp_data"
  }
}
```

### 7.3 画布 UI

```
┌─────────────────────────────────────────┐
│ ← 📝 讲座录音·23:15             [⋮]   │
│                                          │
│                                          │
│         画布                              │
│                                          │
│                                          │
│  ✏️                                     │
│  🧹                                     │  ← 左侧竖排 4 个
│  📷                                     │     笔/橡皮/拍照/撤销
│  ↩️                                     │
│                                          │
│                               🗒         │  ← 左下角草稿按钮
└─────────────────────────────────────────┘
```

**左侧工具栏：**
- ✏️ 笔 — 点一下切到笔模式；已在笔模式再点 → 打开颜色面板
- 🧹 橡皮 — 硬边，eraser_width 可选
- 📷 拍照 — 一键拍摄，插入画布 + 录音时间轴锚点
- ↩️ 撤销 — 按 stroke 倒序撤消（LIFO）

**右上角 ⋮ 菜单：**
- 画布信息（创建时间、创建设备、查看次数、编辑时长）
- 子时间线归属/更改
- 导出
- 插入文件

**左下角 🗒 按钮：** 单击切换草稿模式（底色灰网格，笔划半透明，默认不保存）。

**工具栏镜像：** 设置中可选镜像到右侧，适配左撇子。

### 7.4 触摸交互

| 手势 | 行为 |
|:-----|:-----|
| **双指滑动** | 平移画布（类似触控板） |
| **单指滑动** | 无操作（防误触） |
| **长按图片** | 选中（加载圈动画）→ 可拖拽/缩放 |
| **选中后** | 弹出菜单：删除 |
| **笔在画布** | 写字/画图（与触摸事件分离） |

### 7.5 打字 & 手写混合

- 画布 ⋮ 菜单 →「插入文本」→ 插入文本框（类似 PPT 文本框）
- 现有文本条目想加手写批注 → 文本转为文档格式 → 以 PDF 背景方式在画布中打开 → 复用 PDF 批注逻辑
- 两个方向都支持，不分主次

### 7.6 性能（R1-R3 审计修复）

| 问题 | 修复 |
|:-----|:-----|
| R1 缩放超 Canvas 上限 | 限制最大 400% |
| R2 SVG 1001 层 mask 崩溃 | 改用 evenodd 单 path |
| R3 destination-out 性能 | 运行时增量更新（O(1)），冷启动用缩略图+overlay+backbuffer |

**冷启动序列：**
1. 铺缩略图（page.metadata.thumbnail，WebP base64，~20-50KB），用户立即可见
2. 用户可立即书写 → 新笔画画在 overlay canvas 上
3. 后台 backbuffer 从 strokes[0] 回放至 strokes[N-1]
4. backbuffer 就绪后替换缩略图，overlay 叠在上面
5. 用户不等待、不阻塞、不感知两阶段

**缩略图更新：** 每次保存时增量更新（5s 防抖）。

### 7.7 SVG 导出

改用 evenodd 单 path：遍历 strokes 按 ts 排序，ink 写外路径，eraser 写内路径（子路径），合并为一个 `<path fill-rule="evenodd">`。

### 7.8 撤销

Command Pattern。仅运行时内存维护，退出清空。LIFO 顺序，上限 100 步。eraser 的 undo 天然正确（删掉 eraser stroke 即可）。

### 7.9 交互协议（文字模式 vs 绘画模式）

画布有两种互斥的交互模式，通过工具栏切换：

| | 文字模式（笔不选中）| 绘画模式（笔选中）|
|:--|:------------------|:----------------|
| 默认态 | ✅ 进入画布时默认 | 点击笔工具后 |
| 键盘 | 弹出 | 收起 |
| 点文字 | 出光标，可编辑 | 无反应 |
| 点墨迹 | 无反应（预览态）| 在上面继续画 |
| 点图片 | 全屏预览/轻量操作 | 在上面画批注 |
| 点空白 | 光标移到最近文字 | 画墨迹 |
| 切换方式 | 点击已选中的笔工具取消选择 | 点击笔工具 |

**核心原则：当前选中什么工具，就只能交互对应的内容类型。** 没选中的工具对应的内容显示为预览态，不可编辑。这跟 Apple Notes 的逻辑一致——文字模式下编辑文字/预览图片，笔模式下在一切之上画批注。选笔划/移动/删除属于选框工具（未来实现），不是笔工具的职责。

## 8. PDF/Word 导入

### 8.1 以文件导入（从 Zentrim 文件列表打开）

Zentrim 作为 PDF 查看器，PDF 转成上下接长的长页面作为画布背景。支持写字、录音，不翻页。

### 8.2 拖入已有画布

弹出对话框选意图：

| 选项 | 行为 |
|:-----|:-----|
| 📎 放个链接 | 画布上出现文件引用卡片，点开跳转 |
| 🖼️ 展开到画布 | 弹出页面选择器（缩略图预览，可点看大图）→ 多选/全选 → 选中的页面合并为长图，作为 image 元素插入画布 |

不设卡片 widget 渲染 PDF。选页 → 贴到画布 → 用户在上面自由写字/批注。

---

## 9. AI Pipeline

### 9.1 管道总览

```
用户操作
  │
  ├─ 拍照 → Step 1a：拍照入库管线 → photo block
  ├─ 录音 → Step 1b：ASR 转写 + 关键提取 → audio block
  ├─ 提交文本 → Step 1c：轻量处理（已有内容无需 VLM） → text block
  ├─ 保存画布 → Step 1d：OCR + VLM 分块语义提取 → ink block
  └─ 导入文件 → Step 1e：文本提取 → file block
          │
          ▼
    Step 2：内容入库（原始层 blocks.data，用户可见，立刻出现在时间线）
          │
          ▼
    Step 3：内容理解（后台）
        ├─ 印刷体照片 → VLM→HTML（豆包 Seed 2.0 Lite，最多 2 轮审核）
        ├─ 手写照片 → 智能二值化净图
        ├─ 混合照片 → VLM→HTML 正文 + 手写批注语义化
        ├─ 录音 → ASR 转写全文
        ├─ 画布 → VLM 分块瓦片聚合语义描述
        └─ 文件 → 文本提取
          │
          ▼
    Step 4：结构化入库（写计算层 blocks.text + 向量化索引，按 block 独立 embedding）
          │
          ▼
    Step 5：Zentrim 注意到（依赖 Step 4 向量索引就绪）
          │
          ▼
    Step 6：更新主页「Zentrim 注意到」行（如有发现）
```

### 9.2 VLM 型号

| 用途 | 模型 |
|:-----|:-----|
| 内容形态判断 | 豆包 Seed 2.0 Lite |
| VLM→HTML 生成 | 豆包 Seed 2.0 Lite |
| 智能二值化判断 | 豆包 Seed 2.0 Lite |
| 画布瓦片语义提取 | 豆包 Seed 2.0 Lite |
| 瓦片聚合 LLM | qwen3-flash（轻量） |

### 9.3 画布 VLM 分块策略

```
画布保存 → 切成 1024×1024 瓦片（10% 重叠防止切断文字）
  → 每瓦片独立跑 VLM → 输出瓦片内文本 + 语义
  → 一个 LLM 聚合所有瓦片结果 → 输出完整全文 + 语义描述
  → 写入 ink block.text（供 FULLTEXT 搜索）
  → ink block 独立 embedding → 写入 idx-zentrim-{uid}，model_name 记录所用模型

备选：全模态 embedding 向量化瓦片（效果可能不太好）
```

---

## 10. 数据分层

### 10.1 四层统一结构 + blocks 表达

所有内容统一由 **blocks** 表达；entry 是「一条时间线记录」，block 是「entry 内的一个内容单元」。

```
entry（一条时间线记录）
  ├── blocks[]（按 sort_order 排序）
  │     ├── type: text/ink/audio/photo/image/file
  │     ├── data: JSON（类型专属，与 text 解耦）
  │     └── text: TEXT（搜索用，由 data 派生）
  └── metadata（按四层结构组织扩展元数据）
```

**四层 + metadata 字段映射：**

| 层 | 表达方式 | 可编辑 |
|:---|:---------|:------|
| 原始层 | blocks.data 中的原始 payload（用户笔划、原始照片二进制路径、ASR 原文等） | ❌ 不可变 |
| 计算层 | blocks.text（OCR/ASR/VLM 语义）+ 计算层附加资源（二值化图、HTML 等） | ✅ 可删除、可重算 |
| 关联层 | entry.metadata.references + zentrim_references 表（@引用关系） | ✅ 可删除 |
| 批注层 | entry.metadata.annotations（Agent 补充资料） | ✅ 可删除 |

**metadata 字段约定（zentrim_entries.metadata JSON）：**

```json
{
  "raw": { /* 原始层扩展元数据 */ },
  "computed": { /* 计算层扩展元数据 */ },
  "references": [{ "source": "block_id", "target": "block_id" }],
  "annotations": [{ "by": "agent", "content": "...", "added_at": "..." }],
  "noted_by_zentrim": "..." /* 注意到缓存 */
}
```

### 10.2 存储路径

**Entry 级（封面/概览）：**

| 内容 | 路径 |
|:-----|:-----|
| Entry 封面缩略图 | `zentrim/user_{uid}/entries/{entry_id}_thumb.webp` |

**Block 级（具体内容单元，以 block_id 为单位）：**

| Block 类型 | 路径 |
|:-----------|:-----|
| photo 原始 | `zentrim/user_{uid}/blocks/{block_id}_original.jpg` |
| photo 智能二值化 | `zentrim/user_{uid}/blocks/{block_id}_clean.webp` |
| photo VLM→HTML | `zentrim/user_{uid}/blocks/{block_id}_render.html` |
| ink 画布 strokes | `zentrim/user_{uid}/blocks/{block_id}_strokes.json` |
| ink 画布缩略图 | `zentrim/user_{uid}/blocks/{block_id}_thumb.webp` |
| audio 录音 | `zentrim/user_{uid}/blocks/{block_id}_audio.mp3` |
| image | `zentrim/user_{uid}/blocks/{block_id}.{ext}` |
| file | `zentrim/user_{uid}/blocks/{block_id}_{filename}` |

**注意**：路径以 `block_id` 而非 `entry_id` 为单位，因为同一 entry 可包含多张照片、多段录音、多份文件，各自独立成 block。

---

## 11. 版本管理

### 11.1 Entry 模型

```
一条时间线记录（entry）= 一个独立的 Git 仓库
├── block A = repo 里的文件 A（有自己独立的历史）
├── block B = repo 里的文件 B
└── Version 3 = commit 3（指向 A_v3 + B_v2）
```

**三条核心原则：**
1. Entry 是一个完整的 repo，跟其他 entry 完全隔离
2. 每个 block 的版本独立管理
3. 每次版本（edit）记录当前所有 block 版本的指向关系

### 11.2 数据模型

#### 新增表：zentrim_entry_versions

```python
class ZentrimEntryVersion(Base):
    """Entry 级别的版本记录（类似 Git commit）"""
    __tablename__ = "zentrim_entry_versions"
    id = Column(Integer, primary_key=True)
    entry_id = Column(String(26), ForeignKey("zentrim_entries.id"), nullable=False)
    version = Column(Integer, nullable=False)          # 自增版本号
    block_tree = Column(JSON)                          # {b_id: v, ...} block 版本映射树
    created_at = Column(DateTime, default=func.now())
    delta_summary = Column(JSON, nullable=True)        # 变化摘要
```

#### 新增表：zentrim_block_versions

```python
class ZentrimBlockVersion(Base):
    """Block 级别的版本数据"""
    __tablename__ = "zentrim_block_versions"
    id = Column(Integer, primary_key=True)
    block_id = Column(String(26), nullable=False)
    entry_id = Column(String(26), nullable=False)
    version = Column(Integer, nullable=False)          # 该 block 的版本号
    data = Column(JSON)                                # 该版本的 block.data 快照
    text = Column(Text, nullable=True)                 # 该版本的计算层文本
    created_at = Column(DateTime, default=func.now())
```

#### zentrim_blocks = HEAD（工作副本）

当前最新版本的数据始终在 zentrim_blocks 表。读最新数据不走版本表，零查询成本：
- 读最新：`zentrim_blocks WHERE entry_id = ?` → 直接查当前表
- 读历史：`zentrim_block_versions WHERE entry_id = ? AND version = ?` → 查版本表
- 回滚：从 block_versions 读目标版本的 data，写回 blocks 表

### 11.3 保存与版本落地

**无保存按钮：**
- 写一个字 → 自动暂存到 zentrim_blocks（当前工作副本）
- 离开页面（返回/切换Tab/息屏）→ 自动持久化
- 用户不需要手动"保存"

**版本落地（防抖）：**
- 连续编辑中不产生新 entry_version，仅更新 zentrim_blocks
- 用户停止操作 **1 小时**后，当前工作副本落地为一个新的 entry_version + 逐个 block 的 block_version
- 防抖时间可配置（设置页面，必须提供用户编辑入口）
- ⚠️ 此项必须在设置页面提供用户可编辑选项

**什么会产生版本：**

| 变更 | 产生版本？ | 说明 |
|:-----|:---------:|:------|
| 新增/修改/删除 block | ✅ | 核心变化 |
| 标题变更 | ✅ | metadata 变化 |
| 标签变更 | ✅ | 产生编辑事件 |
| 画布布局变化 | ✅ | layout 进 metadata |
| 管线处理结果 | ✅ | 计算层与原始层绑定 |
| 纯浏览/不编辑 | ❌ | 无变化，不产生版本 |

### 11.4 变化摘要（delta_summary）

每个 entry_version 记录本次发生了什么变化。**不使用 LLM 生成**——由系统根据 block_tree diff 推导确定性摘要：

```json
{
    "changed_blocks": [
        {"block_id": "b4", "type": "text", "chars_added": 42, "chars_removed": 5},
        {"block_id": "b6", "type": "ink", "strokes_added": 3, "strokes_removed": 1}
    ],
    "tags_changed": {"added": ["todo"], "removed": []},
    "layout_changed": false
}
```

用户界面不显示机器生成的文字描述，而是**直接渲染差异**：
- 文本：左右并排对比，差异处高亮
- 墨迹：新增笔划蓝色，擦除笔划红色
- 照片/文件：标签说明（"新添加了一张照片"）

### 11.5 时间线展现

```
📅 今天 13:45
├── 📝 氧化还原笔记
├── ✏️ 氧化还原笔记 · 添加了标签"todo"     ← 灰色编辑事件
│
📅 今天 12:00
├── ✏️ 氧化还原笔记 · 新增 3 笔墨迹         ← 灰色编辑事件
│
📅 昨天 19:00
├── 📝 氧化还原笔记                           ← 原始创建
```

- 原始条目按 `created_at` 固定排序，不随编辑移动
- 编辑事件按 `entry_version.created_at` 在时间线对应位置插入
- 编辑事件：灰色小字，显示 delta_summary 的简明描述
- 点击编辑事件 → 跳转到条目 + 打开版本历史面板
- 编辑事件自动继承原条目的子时间线归属

### 11.6 回滚（硬回滚）

- 点「恢复到此版本」→ 确认弹窗
- 弹窗提示：「将删除 v{N} 及之后的所有版本，恢复到 v{X} 时的状态」
- 确认后：
  1. 从 `zentrim_block_versions` 读取目标版本的 block data
  2. 写入 `zentrim_blocks`（替换当前工作副本）
  3. 删除目标版本之后的所有 `entry_version` + `block_version` 记录
  4. 时间线上对应被删除的编辑事件同步消失
- **按 block 粒度处理**：只重建实际变化的 block 的计算层，未变动的 block 保留原计算层
- 干净、简单、不可撤销（用户确认时已有明确提示）

### 11.7 计算层与版本绑定

**计算层（blocks.text + 管线产物）参与版本管理：**

```
V1: raw_1 → pipeline → output_1  → 版本 1 快照
V2: raw_2 → pipeline → output_2  → 版本 2 快照
V3: raw_3 → pipeline → output_3  → 版本 3 快照
```

每一层都是版本的一部分。查看 V2 时，用户看到的是 V2 的原始内容 + V2 当时的管线输出。

**增量处理（避免不必要的重算）：**

```
用户编辑（V2 → V3）改了 ink_block，photo_block 没动：
├── ink_block.data → 变更 → block_version+1 → 管线标记"待重新处理"
├── photo_block.data → 不变 → block_version 不变 → 计算层保留
└── pipeline 后台仅处理 ink_block，跳过 photo_block
```

回滚也是一样：只重建变化的 block 的计算层。

### 11.8 用户交互

- 退出条目 → 自动保存当前工作副本
- 防抖到期 → 自动落地为新的 entry_version
- ⋮ 菜单 →「版本历史」→ 面板列出所有版本（时间倒序）
- 每个版本显示：版本号 + 时间 + delta_summary 渲染的差异预览
- 点「查看版本」→ 只读展示该版本的 blocks（含当时的计算层产物）
- 点「恢复到此版本」→ 确认弹窗 → 硬回滚
- 版本对比视图：左右并排，差异处高亮，墨迹增量直接渲染

---

## 12. 开放问题

| # | 事项 | 状态 |
|:--|:-----|:-----|
| 1 | VLM→HTML 输出 schema | ✅ 已清除 — HTML 不需要 schema，直接输出 HTML 字符串 |
| 2 | VLM Step 0 形态判断 prompt | ✅ 已清除 — 形态判断用 Qwen3.6 Flash 快速模型，不走 VLM 分类 prompt |
| 3 | 画布渲染引擎（RN Skia 实现方案） | ✅ 已决 — 双模态架构：文字层 canvas-editor（键盘输入）+ 墨迹层 Skia（手写输入），两 block 共存 |
| 4 | canvas-editor 集成 | 待写 spec — 文字层编辑器的 API 边界、与 ink block 的 sort_order 协调 |
| 5 | blocks 迁移完成 | ✅ 已决 — zentrim_blocks 表上线，旧 entry.content 已迁移到对应 text block |
| 6 | 墨迹跟随文字（stroke anchor） | 待写 spec — 文字修改时墨迹块的位置/锚点策略 |
| 7 | Work 转换桥（JSON→HTML→DOCX） | 待写 spec — 把 entry（含 blocks）导出为可分享的 HTML / DOCX |
| 8 | Zentrim 前端 UI 组件化 | 待写 spec |
| 9 | 数据导出流程 | 未设计 |
