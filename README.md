# FeClaw — 你的 AI 学习搭档

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)]()

> 让 AI 的浪潮，惠及每一个学生。

---

## 一、面向学生/用户

### 这是什么？

你有没有过这样的体验——打开一个 AI 对话，每次都要重新自我介绍：「我是高一学生，选科物化生，数学比较弱……」隔天再聊，它又忘了。想让它帮你搜个东西，它只能凭训练数据猜。想持久化维护管理一个数字错题本，普通 AI 根本处理不了。

**FeClaw 在探索解决这些问题的可能性。** 它不是普通的聊天机器人——它能记住你的学习进度、自动搜索教材和网络、帮你分析错题、管理学习文件。像一个真正了解你的学习搭档。

### 它能做什么？

**🧠 有记忆，不用每次重新介绍自己**

聊过几次之后，FeClaw 会尝试提取你的偏好、学习进度、薄弱知识点。下次对话自动加载这些信息。

**🔍 会搜索，教材 + 联网双管齐下**

问一道物理题，它会自动去教材知识库里找相关概念和例题；问「2026 年高考政策有什么变化」，它会联网搜索最新信息。不需要你手动切换搜索模式。

**⚡ 快的时候秒回，难的时候深度思考**

简单聊天？不到十秒就能回复。需要数学推理、复杂分析？自动开启深度思考模式，仔细推理。你感知不到切换过程——只感觉它该快时快，该准时准。

**📝 错题分析，拍照就能搞定**

拍一张试卷或习题照片发给它，FeClaw Agent 能识别内容，把错题整理好存起来。之后随时可以回顾、重做。

**📂 文件管理，能看到 AI 的完整工作区**

每个 AI 有独立的工作区。AI 写了什么文件、存了什么笔记、生成了什么代码——你都能直接看到和下载。

### 怎么开始用？

可以联系 [lizidaren@firstentrance.net](mailto:lizidaren@firstentrance.net) 加入内测，或选择自行本地部署（见第二部分）。

---

## 二、面向开发者

### 设计理念

**All-in-Text** — 一切皆文本。Agent 的系统提示词、人格设定、用户画像、长期记忆、会话笔记，全部是 Markdown 文件，存放在 VFS（虚拟文件系统）中。Agent 通过读写文件来维护自身状态。这意味着：完全可审计、可编辑、可备份。

**大小模型分步混排** — 每条消息进入系统后，先经过 SmartRouter（智能路由层）用小模型做意图分类和预取决策。纯文本用 `qwen3.6-flash`（约 0.5s），带图片时自动切换 `qwen3.6-35b-a3b` 多模态模型（约 1.3s）。简单问题直接回复，复杂问题对同一模型开启深度思考模式。既省成本，又保体验。

**SmartRouter** — 核心路由层，负责四件事：
1. 判断是否需要深度思考（thinking）
2. 预取外部数据（prefetch：知识库/联网搜索/文件读取），省去主模型的一轮工具调用
3. 判断是否可以直接回复（direct_reply），跳过主模型
4. 注入规则提示（inject_rules），指导主模型行为

### 核心架构

消息流：`用户消息 → SmartRouter（小模型决策） → 预取数据（并行） → 向量搜索（并行） → 主模型（流式 SSE） → 工具调用循环 → 返回结果`

主模型在对话中自主调用工具（文件读写、联网搜索、代码执行、子 Agent 等），工具结果截断到 50KB 后注入上下文。上下文超过 110K tokens 时自动压缩。

认证流程：支持本地账号密码（JWT）、OAuth/OIDC SSO（对接 FirstEntrancePlatform）、以及 TOTP 验证码登录（用于安全分享 Agent 操作权限）。

### 技术栈

| 层 | 技术 |
|---|---|
| Web 框架 | FastAPI (Python 3.12+) |
| 数据库 | MySQL（强制，统一 SQLAlchemy ORM；不再支持 SQLite） |
| 文件存储 | 腾讯云 COS（通过 VFS 抽象层访问） |
| 消息协议 | SSE（流式对话）、WebSocket |
| 多模型兼容 | DeepSeek / 千问 / 智谱 GLM / 豆包 / 小米 MiMo / Kimi（OpenAI 兼容协议） |

### 适配的模型

| 模型名 | Provider | 能力 | 需配置 |
|--------|----------|------|--------|
| `deepseek-v4-flash` | DeepSeek | 文本 + 深度思考 | `DEEPSEEK_API_KEY` |
| `qwen3.6-flash` | 千问 | 文本（速度快） | `QWEN_API_KEY` |
| `qwen3.6-35b-a3b` | 千问 | **视觉**（图文理解） | `QWEN_API_KEY` |
| `text-embedding-v4` | 千问 | **嵌入向量化** | `QWEN_API_KEY` |
| `glm-4.7` / `glm-4.7-flash` | 智谱 GLM | 文本 | `ZHIPU_API_KEY` |
| `embedding-3` | 智谱 GLM | **嵌入向量化** | `ZHIPU_API_KEY` |
| `doubao-seed-2-0-lite-260215` | 豆包 | 视觉 | `DOUBAO_API_KEY` |
| `mimo-v2.5-pro` / `mimo-v2.5-pro-ultraspeed` | 小米 MiMo | 文本 + 深度思考 | `MIMO_API_KEY` |

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
