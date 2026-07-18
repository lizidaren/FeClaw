# FeClaw — 你的 AI 学习向导

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)]()

> 欢迎来到Agentic Learning新纪元。

---

## 一、面向学生/用户的介绍

### 背景

2026年的AI技术迅速发展，譬如，时下AI编程办公大火，《2025腾讯研发大数据报告》提到，腾讯去年**50%新代码**都由混元大模型写下，专业工作应用完全可行。

高考做题方面，AI同样不差，甚至可谓极强。羊城晚报教育发展研究院测评中，讯飞星火X2于2026广东物理类高考中，拿到了**708分的顶级屏蔽成绩**。

AI写代码、做试卷、处理工作的新闻铺天盖地，但当我将视线收回，在自己的手机上打开那些AI聊天软件，为什么感觉这阵新兴技术的暴风，刮到日常学习领域，却显得不温不火？

### 归根结底，我认为是以下两个原因：

1. 学习的意义，源于**思考**。
  - 工作的意义源于**产出交付物**，而谁进行生产没那么重要——无论是人还是AI。
  - 学习的意义源于思考。“做了多少题”并不重要，意义的来源是认知的过程中，**有知识流过大脑，有思想留下印记**。

2. 能做题，不等于**会做题**。
  - 纵然AI能在2026广东高考物理类拿到屏蔽级别的成绩，但这并不代表它会教学生。我承认AI高考已然是金榜题名，但它“师范”显然尚未毕业。
  - 我们需要有方法，不仅发挥AI会做题的优势，更要能教好学生。

直接使用AI问题，得到的结果大多与作业帮式的搜题软件差不多，**AI只不过是给答案装了一张嘴**。
我们需要更好的方式，去“打开AI的能力”。

### 破局

时下，2026年，**智能体技术发展迅猛**，我在这里，看到了新的可能性。
一个AI智能体：
- **有记忆**——它**记得**我对动量守恒定律不熟悉，**知道**我听讲解的时候，不喜欢直接上二级结论，而是先推一遍。
- **有方法**——它可以与我配合，通过**苏格拉底提问法**、**费曼学习法**等方式激发我的思考，让我在认知的过程中学到知识而不是记住答案。
- **有支持**——当我搞了很久没做出压轴题而烦躁，它**知道我很累，无心听长篇大论**，于是以“不着急，正着难算，那我们从待求的量出发倒推试试，好不好？”发问。

FeClaw是一个智能体平台，是我一次实践的尝试。它易于使用，同时功能丰富，不仅仅是有长期记忆的智能体，更有**完整的能力体系**，可以帮你以一册书为单位进行全面的知识总结梳理，可以帮你收集并管理错题，还可以帮你联网收集信息并分析，甚至做成PPT。

FeClaw可以是普通的智能体，但是我更希望它不只是智能体，而是**一位真正的向导，一位“学海引路者”**。

### 使用

欢迎联系我 [lizidaren@firstentrance.net](mailto:lizidaren@firstentrance.net) 加入内测，或者如果你有条件，可以自行部署FeClaw，详见下方。

---

## 二、面向开发者的介绍

### 这是什么？

FeClaw是一个智能体平台，但不局限于一个智能体平台，而是一个开放的Harness地基。

FeClaw实现了完整的VFS和沙箱运行环境，有向量存算方案支持RAG，有聊天记录自动提取异步生成记忆、用户画像……通过这些基础能力，加上“向导”的完整提示词模板，FeClaw可以支撑一位学习向导智能体的运行。

但是不止于此。FeClaw 提出的Gen 2 - IM Agent框架针对多智能体协作进行了原生优化——群聊中多个 Agent 可自主响应、交叉引用、协同产出。群内提供 VFS 挂载共享空间，Agent 之间通过 Buffer+Flush 原子化消息机制保证并发安全。

在此之上还有更多可能性。基于FeClaw强大的Harness基础能力，我正在规划并开发FeClaw Universe、FeClaw Work和FeClaw Zentrim，服务于PBL（项目式学习）全链路，以及个人无纸化学习等细分学习场景，详见 `future.md`

### Harness基础设施特色：Gen 2 - IM Agent

Gen 1 传统 Agent 虽轻量快速，但在复杂协作时，有 **TOCTOU（消息处理期间状态变化）竞态问题**导致混乱。为此，我在FeClaw中提出 <mark>Gen 2 - IM Agent</mark>，借鉴人类使用 IM 工作流程，将所有新消息以 IRQ 中断形式注入所有可介入节点（如工具调用结果），同时通过回复缓冲区（write/edit/flush）确保发送动作近乎原子化，从而彻底规避竞态。
全局仅单个 Agent 实例在线，天然实现跨单聊、群聊的上下文一致性，将 Classic Agent 升级为异步、连贯且状态安全的下一代 **"IM-Native" Agent**。

<details><summary>完整介绍</summary>

让我们先梳理传统Agent（这里称之为Gen 1 - Traditional Agent）的运作逻辑
- 单聊：输入问题 --> Agent 收到并思考，调用工具处理 --> Agent 输出写到聊天中
- “Heartbeat”：系统读取心跳机制提示词并注入上下文 --> 独立 Agent 会话处理 --> 输出写到特定消息渠道例如私聊
- Supervisor 式的群聊：用户输入问题 --> 管理员 Agent 分发任务 --> 所有 Agent 各自处理 --> 聚合结果输出给用户
- Swarm 式的群聊：用户输入问题 --> 所有 Agent 收到并说话 --> 一个 Agent 说话的内容会被广播到群内所有 Agent --> 讨论 --> 达成共识汇报用户

这个架构在轻度使用下非常好用，极简，并且速度快。但是面对复杂情况，例如大型 Agent 群协作，或者用户需要单聊和群聊处理同一问题，会导致严重混乱。
究其本质，Gen 1 传统 Agent 的问题在于，
1. 仅通过记忆文件实现跨渠道一致性，而不是上下文级
2. 群内消息处理存在 TOCTOU 竞态问题（收到消息-处理消息-回复消息时间太长）

因此，我在FeClaw设计中，提出**Gen 2 - IM Agent**，借鉴生活中人类处理IM消息和工作的形态解决问题。

我引入了硬件开发的IRQ（中断）和Coprocessor（协处理器）机制。

关于核心工作循环与协处理器：IM Agent 实例常驻后台在线，如果无任务就休眠（DORMANT），此时协处理器仍然运转。协处理器可以是代码层逻辑，例如定时器、消息接收器、WebHook、文件修改 Hook，也可以是任务固定、不携带Agent上下文、工具权限受限的子 Agent，负责稍复杂的语义级协处理任务，例如，定时执行联网搜索，监测广东省是否有新的高考模拟题出来，有则唤醒主 Agent，进行额外处理或通知用户。

关于IRQ：消息以 IRQ 形式进入处理队列，就像我们使用 IM 的时候看到有一条新消息的通知一样——当然不一定现在就要处理。具体而言，IRQ 会被插入到每一个可以注入上下文的位置，例如 Agent 执行了工具调用，那 FeClaw 会自动把 IRQ 通知注入到工具调用结果末尾，Agent 自行决定是否处理。如果需要回复消息，需要先调用 `reply_buffer_write` 工具写入回复内容到缓冲区，类比人类打字到输入框。工具调用请求生成完毕，发到系统，FeClaw 执行前会检查全局是否有新消息，如果有则注入此次 `reply_buffer_write` 调用的结果中使得 Agent 可以知道出现了TOCTOU的情况，Agent 自行决定是否使用 `reply_buffer_edit` 修改消息，或者不修改，直接使用 `reply_buffer_flush` 执行发送。由于 `reply_buffer_flush` 工具不需要传入消息内容，所以生成 `reply_buffer_flush` 速度很快，一定程度上可以认为是原子操作，因此避免TOCTOU。同时，由于时刻只有一个 Agent 实例在线，所以可以实现例如用户先在群内布置工作，然后私聊询问进度，就像与真实人类互动那样自然。

关于“Heartbeat”：对于Gen 2 - IM Agent，传统的“心跳机制”由协处理器定时器接管，如果定时器到时间但是 Agent 处于休眠状态，则唤醒；如果 Agent 正在活跃工作，则发送 IRQ，逻辑与收到消息相同。由于 Agent 会话不是定向某一个消息渠道的，Heartbeat 任务执行结果可以发向任何聊天，就像人类的闹钟响了，起床工作，工作过程与平常无异。

这套架构非常灵活，例如可以规划新功能：特别地，Agent 能够把一个群标记为免打扰并指定解除条件，此时该群内新消息不再 IRQ 通知，而是进入协处理器，通过小模型判断是否符合解除条件，例如“群内消息提到了化学相关内容”，如符合则发 IRQ，触发处理。
> *免打扰功能属于规划，目前还没有实现。*

</details>


### 基础设计理念

**All-in-Text** — 核心层仅文本，多模态在外围消化。
当 FeClaw Agent 收到一张图片，系统会自动触发预识别机制，发给 Agent 的消息除了图片路径之外，还有从 `场景、文字、风格` 多个维度的描述。这种处理方法可以应对绝大多数的图片处理场景，并且不会给 Agent 上下文引入额外噪声。

针对特殊情况，例如看图解题，Agent 可以唤起子 Agent使用多模态能力做题，**Agent 只负责将长篇大论的题目做法按照自己的人设讲给你听**。

对于复杂文件如含图PDF，FeClaw 提供万能文件解析工具（UniversalParser）（可以理解为针对特定处理任务的超级 SubAgent），Agent 调用时传入问题，如“做这套试卷的第1-5和第16-18题”，万能解析工具会自动识别文件内容并开启 8 个 LLM 并行解题，最终汇总结果为工具调用结果，发回。

所以，在 FeClaw 的核心层，不只 Agent 系统提示词、人格设定、长期记忆是文本，所有多模态内容都是文本。这不仅意味着任何文本模型都可以接入 FeClaw 作为 Agent 主模型，还代表着完全可审计、可编辑、可备份的数据主权。

**大小模型分步混排** — 不祈求一步登天的智能。
这是我对Gen 1 - Traditional Agent设计的优化。
“当局者迷，旁观者清”，主模型参数量大，速度慢，并且当上下文大量堆积容易飘。因此，每条新消息进入系统后，需先经过 SmartRouter（智能路由）用高速小模型进行分析。具体而言，SR 会得到一部分 Agent 的上下文，并负责判断问题是否：
- 可以直接回答？例如用户说“早上好”，则直接回复，无需 Agent 决策
- 需要外部信息？例如用户问“2026高考情况”，则 SR 预调用 `web_search` ，或预取向量知识库 `knowledge_search` 和文件读取 `file_read`，让 Agent 开始时就得到足够的信息回答问题，降低延迟。
- 可能理解出错？例如用户提到“我感觉不想再学数学了”，则 SR 应给 Agent 注入规则提示，例如“请注意关注用户情绪，给予一定鼓励，而不要忽略并继续讲题”。
- 需要深度思考？例如用户问“如何理解动能定理、势能定理和机械能守恒的关系”，SR 注意到这是一个偏复杂问题，则开启 Agent 主模型的深度思考。


### 核心架构

#### Agent层面

> FeClaw 同时支持创建 Gen 1 和 Gen 2 Agent。

Gen 1 - Traditional Agent 消息流：`用户消息 → SmartRouter（直答/预取/思考决策） → 主模型（流式） → 工具调用循环 → 回复消息`
主模型在对话中自主调用工具（文件读写、联网搜索、代码执行、子 Agent 等），工具结果截断到 50K 后注入上下文。

Gen 2 - IM Agent 工作流： `协处理器/IRQ唤醒 → WorkSession创建 → 处理+工具调用 → reply_buffer_write写入 → 系统TOCTOU检查 → reply_buffer_flush发送 → DORMANT（休眠）`
实现持续在线，并且深化环境感知能力

#### 安全层面

认证有两种方式：
1. FeClaw本地账号密码系统
2. 对接外部OAuth/OIDC Provider

此外用户可以通过有效期 10 分钟（±30s）的验证码单独分享自己某个 Agent 的权限给别人。

特别地，管理员用户可以查看metrics数据等。


### 技术栈

我使用Ubuntu 20.04进行开发，测试中表现稳定，但是注意需要添加 deadsnakes PPA 安装 Python 3.12+，并且避免破坏系统Python。

| 层 | 技术 |
|---|---|
| Web 框架 | FastAPI (Python 3.12+) |
| 数据库 | MySQL (SQLAlchemy ORM)、Redis |
| VFS后端 | 腾讯云 COS |
| RAG向量表 | 腾讯云 COS 向量存储 |
| 消息协议 | SSE（流式对话）、WebSocket |
| 多模型兼容 | DeepSeek / 千问 / 智谱 GLM / 豆包 / 小米 MiMo / Kimi（OpenAI 兼容协议） |

> 注：VFS后端也可配置为本地 `LocalStorage`（指存储在服务器磁盘，这里不指Web存储API的"LocalStorage"）；向量存储也可配置本地 `NumpyVec`

### 适配的模型

#### 文本模型

| 模型名 | Provider | 特性 | 需配置 |
|--------|----------|------|--------|
| deepseek-v4-flash | DeepSeek | 语言很自然友好 | `DEEPSEEK_API_KEY` |
| qwen3.6-flash | 阿里云百炼 | 速度非常快，质量中等 | `QWEN_API_KEY` |
| glm-4.7 / glm-4.7-flash | 智谱 GLM | flash是免费的 | `ZHIPU_API_KEY` |
| kimi-k2.5 | Kimi |  | `KIMI_API_KEY` |
| mimo-v2.5-pro / mimo-v2.5-pro-ultraspeed | 小米 MiMo | ultraspeed极快但要申请 | `MIMO_API_KEY` |

#### 视觉模型

| 模型名 | Provider | 特性 | 需配置 |
|--------|----------|------|--------|
| qwen3.6-35b-a3b | 阿里云百炼 | 高速的图片理解 | `QWEN_API_KEY` |
| doubao-seed-2-0-lite-260215 | 火山引擎 | 偏慢但准的图片理解 | `DOUBAO_API_KEY` |

#### 文生图

| 模型名 | Provider | 用途 | 需配置 |
|--------|----------|------|--------|
| doubao-seedream-5-0-260128 | 火山引擎 | 图片生成 | `DOUBAO_API_KEY` |

#### 嵌入 / 重排序

| 模型名 | Provider | 用途 | 需配置 |
|--------|----------|------|--------|
| text-embedding-v4 | 阿里云百炼 | 文本向量化 | `QWEN_API_KEY` |
| embedding-3 | 智谱 GLM | 文本向量化 | `ZHIPU_API_KEY` |
| qwen3-rerank | 阿里云百炼 | 搜索结果重排序 | `QWEN_API_KEY` |

#### TTS（语音合成）

| 模型名 | Provider | 特性 | 需配置 |
|--------|----------|:----:|--------|
| cosyvoice-v1 | 阿里云 CosyVoice | 稍有点呆 | `QWEN_API_KEY` |
| minimax-speech-02 | MiniMax | 还行 | `MINIMAX_API_KEY` |

### 快速开始

```bash
git clone https://github.com/lizidaren/FeClaw.git
cd FeClaw
pip install -r requirements.txt
cp .env.example .env

**系统依赖**（FUSE 文件系统挂载支持，可选但推荐）：

```bash
# Ubuntu / Debian
sudo apt install fuse3 libfuse3-dev

# macOS 已内置 FUSE 支持
# Windows WSL 需安装 fuse3 包（sudo apt install fuse3）
```
```

**最简起步 — 只配一个千问 API Key**（推荐用于快速体验）：

阿里云百炼同时提供文本模型、视觉模型、嵌入模型和搜索服务，一个 `QWEN_API_KEY` 即可覆盖全部核心功能。

```ini
JWT_SECRET=your-random-secret-here
QWEN_API_KEY=sk-xxx          # 文本 + 视觉 + 嵌入 + 搜索，all in
MAIN_TEXT_MODEL=qwen3.6-flash
MAIN_VISION_MODEL=qwen3.6-35b-a3b
```

> 注意：百炼的搜索服务通过 Qwen3.5-Flash 的联网搜索能力实现（`search_qwen`），无需额外 API Key。

**完整体验 — 添加 DeepSeek**（中文素养更佳）：

DeepSeek 的中文措辞和表达风格在同类模型中表现优秀，适合需要高质量中文交互的场景。

```ini
DEEPSEEK_API_KEY=sk-xxx
MAIN_TEXT_MODEL=deepseek-v4-flash       # 主模型切到 DeepSeek
```

> 所有三档配置（`MAIN_TEXT_MODEL`、`MAIN_VISION_MODEL`、`MAIN_EMBEDDING_MODEL`）均可独立覆盖或替换，不限制必须来自同一家平台。

此外还需在腾讯云创建 COS 存储桶（普通文件存储）和向量存储桶（知识库索引），后者需要在 `services/vector_search_service.py` 中配置桶名称和地址。

启动：

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

首次启动时系统自动创建 MySQL 数据库表和默认管理员账号（默认密码 `admin`，**强烈建议部署后立即修改**）。

打开 http://localhost:8080 ，用默认账号登录控制台，创建 Agent 后即可使用。系统会自动创建一个示例 Agent。

### 配置参考

完整配置见 `.env.example`。关键项：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `JWT_SECRET` | JWT 签名密钥（必填） | — |
| `DEEPSEEK_API_KEY` | DeepSeek | — |
| `ZHIPU_API_KEY` | 智谱 AI | — |
| `QWEN_API_KEY` | 通义千问（SmartRouter 小模型 + 多模态） | — |
| `DOUBAO_API_KEY` | 豆包/火山引擎（VLM 图片识别） | — |
| `KIMI_API_KEY` | Kimi（联网搜索） | — |
| `TENCENT_SEARCH_API_KEY` | 腾讯搜狗（轻量搜索） | — |
| `TENCENT_COS_SECRET_ID` | COS 密钥 ID（文件存储，必需） | — |
| `TENCENT_COS_SECRET_KEY` | COS 密钥 Key | — |
| `TENCENT_COS_BUCKET` | COS 存储桶名称（需在腾讯云创建） | — |
| `OAUTH_PROVIDER_URL` | OAuth Provider 地址（启用 SSO 时） | — |
| `FECLAW_PUBLIC_URL` | 部署域名 | — |
| `MIMO_API_KEY` | 小米 MiMo | — |
| `MAIN_TEXT_MODEL` | 默认文本模型 | `deepseek-v4-flash` |
| `MAIN_VISION_MODEL` | 默认视觉模型 | `qwen3.6-35b-a3b` |
| `MAIN_EMBEDDING_MODEL` | 默认嵌入模型 | `text-embedding-v4` |


## 🚀 部署指南（从零到一）

### 环境要求

| 依赖 | 版本 | 说明 |
|:----|:-----|:------|
| Python | 3.10+ | 3.12 推荐，3.10-3.11 可能缺少部分类型语法 |
| MySQL | 8.0+ | 硬依赖（不支持 SQLite） |
| pip | (最新) | Ubuntu 24.04 需 `apt install python3-pip python3-venv` |
| Docker | (可选) | 用于快速启动 MySQL 开发实例 |

### 快速启动（Ubuntu 24.04 示例）

```bash
# 1. 系统依赖
sudo apt update
sudo apt install -y python3-pip python3-venv git

# 2. 克隆
git clone https://github.com/lizidaren/FeClaw.git
cd FeClaw

# 3. 虚拟环境（Python 3.12+）
python3 -m venv venv
source venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 配置数据库
# 方案 A：使用 dev_init.sh（自动 Docker MySQL + 生成 .env）
sudo bash scripts/dev_init.sh   # ⚠️ Docker 需提前安装

# 方案 B：手动配置
cp .env.example .env
# 编辑 .env，填入 JWT_SECRET、DATABASE_URL、API Key
```

> **国内网络注意事项：**
> - `git clone` 可能因 GFW 超时，重试或使用代理
> - Docker Hub 被墙，配置镜像：`/etc/docker/daemon.json` → `{"registry-mirrors":["https://mirror.ccs.tencentyun.com"]}`
> - pip 使用腾讯云镜像：`pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple`

### 启动

```bash
source venv/bin/activate
python main.py
```

首次启动时控制台会打印冷启动地址（类似 `http://localhost:8080/setup?token=xxxx`），
打开浏览器完成配置向导（设置 admin 密码、选择 LLM 模型等）。

配置完成后重启服务：

```bash
# 停止：Ctrl+C
# 重新启动
python main.py
```

### 常见问题

| 问题 | 原因 | 解决 |
|:----|:-----|:------|
| `ModuleNotFoundError: No module named 'services'` | 从错误目录运行 | 确保在 `FeClaw/` 目录下执行 |
| `pip install` 报 PEP 668 错误 | Ubuntu 24.04 禁止系统 pip | 先 `python3 -m venv venv` 再 `source venv/bin/activate` |
| `PermissionError: .env` | `.env` 归 root | `sudo chown $(whoami) .env` |
| MySQL 连不上 | MySQL 未运行或密码不对 | 运行 `sudo bash scripts/dev_init.sh` 用 Docker 启动 |
| `pyfuse3` 安装失败 | 缺少 `fuse3 libfuse3-dev` | `apt install fuse3 libfuse3-dev` 或删除 `requirements-fuse.txt` |

### 项目结构

```
FeClaw/
├── main.py              # 应用入口，路由注册，生命周期管理
├── config.py            # Pydantic Settings 配置定义
├── .env.example         # 配置模板
├── requirements.txt
│
├── routers/             # HTTP 路由层
│   ├── feclaw_domain.py     # 核心路由：页面 + Agent API + 文件 API
│   ├── feclaw_chat.py       # 对话 API（SSE + WebSocket）
│   ├── oauth.py             # OAuth/OIDC 认证
│   ├── user.py              # 本地注册/登录
│   ├── agent_config.py      # Agent 配置 CRUD
│   ├── console.py           # 控制台管理
│   ├── wechat.py            # 微信消息接入
│   └── sandbox.py           # 沙箱执行环境
│
├── services/            # 业务逻辑层
│   ├── agent_executor.py     # Agent 执行引擎（LLM 调用 + 工具循环）
│   ├── smart_router.py       # 智能路由层（意图分类 + 预取决策）
│   ├── session_memory_service.py  # 会话记忆自动提取 + 蒸馏
│   ├── llm_service.py        # 多模型 LLM 调用封装
│   ├── virtual_filesystem.py # VFS 虚拟文件系统
│   ├── search_service.py     # 三级搜索（腾讯搜狗 / Kimi / 百度）
│   ├── vector_search_service.py  # 知识库向量检索
│   ├── tools/                # Agent 工具函数（web_search, file_*, bash 等）
│   └── ...
│
├── models/              # SQLAlchemy 数据模型（19 张表）
├── templates/           # Jinja2 页面模板
├── static/              # 前端静态资源
└── scripts/             # 运维/工具脚本
```

## 参与贡献

Issue 和 PR 欢迎提交。如需报告问题或讨论功能，请通过 [lizidaren@firstentrance.net](mailto:lizidaren@firstentrance.net) 联系。（我是高中生，查看消息可能不及时，请见谅 🙇）

技术文档见 `docs/` 目录。

## 许可

MIT © 2026 FirstEntrance / lizidaren
