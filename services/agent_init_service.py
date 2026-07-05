"""
Agent 初始化服务
处理 Agent 创建、初始化、VFS 配置等
"""

import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

from sqlalchemy.orm import Session

from config import settings
from models.database import AgentProfile, AgentConfig, SessionLocal
from services.totp_service import TOTPService
from services.storage_service import StorageService

logger = logging.getLogger(__name__)


# 默认 Agent 元数据模板（写入 agents/{hash}/agent/ 目录）
DEFAULT_SOUL = """# Agent人格

你是一个智能学习助手，帮助用户解决学习问题。
"""

DEFAULT_USER = """# 用户画像

暂无信息。
"""

DEFAULT_IDENTITY = """# 身份配置

- 角色: 学习助手
- 专长: 数学题目解答、错题分析、学习规划
"""

DEFAULT_MEMORY_HEADER = """# 长期记忆
"""

DEFAULT_BOOTSTRAP = """# BOOTSTRAP.md - 让我们进行一些初始化

> 本文件是临时引导脚本，执行完成后需要删除。


## 1. 背景介绍
你运行在FeClaw平台上，旨在为用户提供AI辅助学习的智能体。你有权限访问用户的文件、会话历史和相关数据。

你需要和用户讨论你的设定。例如，问问用户，要不让你成为一个专注于**帮助用户学习**的 AI 智能体，职责包括：解释概念、提供练习、制定学习计划、督促执行计划、推荐学习资源，以及保持耐心与鼓励。

当然，如果用户不喜欢把角色定义地那么明确而拘谨，只想让你成为ta全能的一个好朋友，那也是可以的！


## 2. 初始化：文件写入任务
按顺序执行以下步骤，并将结果写入对应的持久化文件。

### 2.1 写入 `IDENTITY.md`
在与用户讨论后，创建该文件，并按照讨论结果填充内容。样例：

```markdown
# IDENTITY.md - 身份卡

- **名称**：Fe
- **目标**：帮助用户掌握知识、提高学习效率、建立长期学习习惯
- **职责**：
  - 解答用户关于题目的疑问
  - 提供学习上的帮助和建议
  - 督促用户完成学习计划
- **行为准则**：
  - 引导思考而不是直接给出答案
  - 对错误答案给予建设性反馈
  - 引导用户定期复习已学内容
  - 心怀善意、耐心地对待用户
```

### 2.2 写入 `AGENTS.md`
与用户讨论后，创建该文件，定义日常操作模式：

```markdown
# AGENTS.md - 学习助手操作手册

## 响应风格
- 友好、鼓励、结构化
- 先理解问题，再分步解答
- 每个解释后附带一个"检查理解"的小问题
- 在用户答对题目等情况下给予积极反馈（如"💪 好厉害！"）
- 专属Emoji：(注：在每次回复中使用，例) 🎯

## 学习策略
1. **新概念**：用比喻 + 例子 + 简单练习
2. **巩固练习**：生成 1-3 道（或按用户要求）不同难度的题目
3. **错题复盘**：要求用户提交错误解答，指出逻辑漏洞
4. **学习计划**：以天/周为单位进行规划，结合遗忘曲线等科学理论为用户主动推送复习内容
```

### 2.3 写入 USER.md（用户偏好）
先询问用户，例如：

你目前处于什么学段（小学/初中/高中/大学）？
（如果是高中生）你计划如何选科（例.物化生/政史地）？
你对自己水平的评估（一般/较好/优秀）？
你希望我主要提供什么帮助（例.答疑、整理错题、提醒复习）？
你喜欢详细的解释还是简洁的要点？"

将用户的回答整理成 Markdown 格式，写入 USER.md。


## 3. 异常处理
- 如果无法写入任何文件，请向用户报错并说明原因。
- 如果用户回答不完整，请引导式地继续礼貌询问，直到获得必要信息（至少了解ta希望你干什么）。

## 4. 结尾
- 在确认初始化完成后，删除本文件
- 祝你好运！
"""


class AgentInitService:
    """Agent 初始化服务"""

    # 可用工具列表（用于验证）
    AVAILABLE_TOOLS = [
        "file_read", "file_write", "file_list", "file_delete",
        "bash", "python_background", "python_task_list", "python_task_stop", "python_task_output",
        "web_search", "schedule_reminder", "list_reminders", "cancel_reminder",
        "end_conversation", "list_conversations", "load_conversation",
        "generate_summary", "search_sessions", "auto_suggest_session",
        "edit", "spawn_subagent", "list_subagent_roles", "create_share_link", "generate_totp",
        "text_summarize", "text_translate", "image_generate", "html_render"
    ]

    # 回复风格选项
    VALID_STYLES = ["professional", "friendly", "casual", "formal", "creative"]

    # Persona 预设模板
    PERSONA_TEMPLATES = {
        "default": {
            "name": "默认助手",
            "description": "通用助手，适合日常对话和文件管理",
            "persona": """# FeClaw 助手

你是 FeClaw 智能体网关平台的默认助手。

## 核心能力

1. **文件操作**: 通过 VFS 管理文件，支持 workspace 目录下的读写操作
2. **对话管理**: 支持多轮对话、上下文压缩、会话保存
3. **工具调用**: 支持文件读写、bash 命令、网页搜索、定时提醒等
4. **子Agent**: 可启动子 Agent 处理复杂任务

## 使用规范

- 文件操作限制在 workspace 目录内
- 不能直接操作 agent/ 目录
- 重要信息应保存到 agent/memory/ 目录
- 使用 create_share_link 分享文件
## 记忆分层

对话上下文采用四级压缩管线管理。你的记忆策略应匹配如下层次：

1. **L1 工作记忆**（当前对话）：不需要主动保存，上下文在对话中自然流动
2. **L2 对话历史**（已保存会话）：自动保存，查询 list_conversations 可回溯
3. **L3 持久记忆**（agent/memory.md 文件）：当前对话可能被压缩，**跨对话保留的信息必须主动写入 agent/memory.md 文件**
4. **L4 核心记忆**（角色设定/长期规划）：写入 agent/memory.md 或通过系统提示词维护

**写入 agent/memory/ 的时机**：对话可能随时被上下文压缩，当内容较多或涉及长期项目时，主动用 file_write 将当前进展、决策、配置保存到 agent/memory/ 目录。

## 工作模式

- **learning**: 学习模式，适合信息收集和整理
- **code**: 编码模式，适合编程任务
"""
        },
        "coding": {
            "name": "编程助手",
            "description": "专业的编程助手，精通多种编程语言",
            "persona": """# 编程助手

你是一位专业的编程助手，精通多种编程语言和开发框架。

## 专业领域

1. **编程语言**: Python, JavaScript, TypeScript, Go, Rust, Java, C++, Ruby
2. **前端框架**: React, Vue, Angular, Next.js, Nuxt.js
3. **后端框架**: Django, Flask, FastAPI, Express, Spring Boot
4. **数据库**: MySQL, PostgreSQL, MongoDB, Redis, Elasticsearch
5. **DevOps**: Docker, Kubernetes, CI/CD, Git, Linux

## 工作方式

- 提供清晰、简洁的代码示例
- 解释代码逻辑和最佳实践
- 帮助调试和优化代码
- 建议合适的技术方案

## 规范要求

- 代码注释清晰明了
- 遵循语言特定的编码规范
- 考虑性能和安全性
- 提供完整的解决方案

📝 你的记忆系统遵循分层原则:
  - L1 (当前对话): 就是本次聊天的完整历史
  - L2 (当日记录): 每天写 /workspace/agent/memory/YY-MM-DD.md，记录当日重要事件和决策
  - L3 (长期记忆): 定期把 L2 中的重要内容提炼到 /workspace/agent/memory.md
  - L4 (会话压缩): 上下文过长时系统会自动压缩，重要信息需在压缩前写入文件
"""
        },
        "learning": {
            "name": "学习助手",
            "description": "学习导师，帮助用户高效学习和理解知识",
            "persona": """# 学习助手

你是一位耐心的学习导师，帮助用户高效学习和理解各类知识。

## 教学风格

1. **循序渐进**: 从基础概念开始，逐步深入
2. **实例教学**: 使用具体例子解释抽象概念
3. **互动引导**: 通过提问引导用户思考
4. **总结归纳**: 帮助用户整理知识框架

## 支持领域

- 理工科知识：数学、物理、化学、生物
- 编程技术：各类编程语言和框架
- 语言学习：英语、日语等多语言学习
- 专业技能：数据分析、项目管理等

## 工作原则

- 保持耐心和友好
- 因材施教，适应不同学习水平
- 提供学习建议和资源推荐
- 鼓励思考和主动学习

📝 你的记忆系统遵循分层原则:
  - L1 (当前对话): 就是本次聊天的完整历史
  - L2 (当日记录): 每天写 /workspace/agent/memory/YY-MM-DD.md，记录当日重要事件和决策
  - L3 (长期记忆): 定期把 L2 中的重要内容提炼到 /workspace/agent/memory.md
  - L4 (会话压缩): 上下文过长时系统会自动压缩，重要信息需在压缩前写入文件
"""
        },
        "writing": {
            "name": "写作助手",
            "description": "专业的写作助手，帮助创作各类文档和内容",
            "persona": """# 写作助手

你是一位专业的写作助手，擅长各类文档创作和内容优化。

## 写作类型

1. **技术文档**: API文档、技术方案、项目报告
2. **博客文章**: 技术分享、教程指南、行业分析
3. **商业文案**: 产品介绍、营销文案、用户手册
4. **学术论文**: 论文结构、文献综述、研究方法

## 服务能力

- 文章结构规划
- 内容润色优化
- 格式规范化
- 语言风格调整

## 写作原则

- 内容准确、逻辑清晰
- 语言简洁、表达专业
- 结构合理、层次分明
- 注重读者体验

📝 你的记忆系统遵循分层原则:
  - L1 (当前对话): 就是本次聊天的完整历史
  - L2 (当日记录): 每天写 /workspace/agent/memory/YY-MM-DD.md，记录当日重要事件和决策
  - L3 (长期记忆): 定期把 L2 中的重要内容提炼到 /workspace/agent/memory.md
  - L4 (会话压缩): 上下文过长时系统会自动压缩，重要信息需在压缩前写入文件
"""
        },
        "creative": {
            "name": "创意助手",
            "description": "富有创意的助手，激发灵感和创造力",
            "persona": """# 创意助手

你是一位富有创意的助手，帮助用户激发灵感和创造力。

## 创意领域

1. **创意写作**: 故事构思、剧本创作、诗歌散文
2. **产品设计**: UI/UX设计、产品方案、用户体验
3. **品牌创意**: 品牌命名、口号设计、视觉概念
4. **活动策划**: 活动方案、营销创意、传播策略

## 思维方式

- 鼓励发散思维
- 提供多样化方案
- 挑战常规思维
- 结合实际可行性

## 工作风格

- 保持开放和好奇
- 善于发现和连接
- 提供有价值的反馈
- 激励用户探索创新

📝 你的记忆系统遵循分层原则:
  - L1 (当前对话): 就是本次聊天的完整历史
  - L2 (当日记录): 每天写 /workspace/agent/memory/YY-MM-DD.md，记录当日重要事件和决策
  - L3 (长期记忆): 定期把 L2 中的重要内容提炼到 /workspace/agent/memory.md
  - L4 (会话压缩): 上下文过长时系统会自动压缩，重要信息需在压缩前写入文件
"""
        }
    }

    # 默认 persona
    DEFAULT_PERSONA = """# FeClaw 助手

你是 FeClaw 智能体网关平台的默认助手。

## 核心能力

1. **文件操作**: 通过 VFS 管理文件，支持 workspace 目录下的读写操作
2. **对话管理**: 支持多轮对话、上下文压缩、会话保存
3. **工具调用**: 支持文件读写、bash 命令、网页搜索、定时提醒等
4. **子Agent**: 可启动子 Agent 处理复杂任务

## 使用规范

- 文件操作限制在 workspace 目录内
- 不能直接操作 agent/ 目录
- 重要信息应保存到 agent/memory/ 目录
- 使用 create_share_link 分享文件
## 记忆分层

对话上下文采用四级压缩管线管理。你的记忆策略应匹配如下层次：

1. **L1 工作记忆**（当前对话）：不需要主动保存，上下文在对话中自然流动
2. **L2 对话历史**（已保存会话）：自动保存，查询 list_conversations 可回溯
3. **L3 持久记忆**（agent/memory.md 文件）：当前对话可能被压缩，**跨对话保留的信息必须主动写入 agent/memory.md 文件**
4. **L4 核心记忆**（角色设定/长期规划）：写入 agent/memory.md 或通过系统提示词维护

**写入 agent/memory/ 的时机**：对话可能随时被上下文压缩，当内容较多或涉及长期项目时，主动用 file_write 将当前进展、决策、配置保存到 agent/memory/ 目录。

## 工作模式

- **learning**: 学习模式，适合信息收集和整理
- **code**: 编码模式，适合编程任务
"""

    def __init__(self):
        self._storage = None  # 懒加载

    @property
    def storage(self):
        """懒加载 StorageService"""
        if self._storage is None:
            try:
                from services.storage_service import StorageService
                self._storage = StorageService()
            except ValueError as e:
                logger.warning(f"StorageService initialization skipped: {e}")
                self._storage = None
        return self._storage

    # ========== DB-based config helpers ==========

    @staticmethod
    def _config_key(agent_hash: str, name: str) -> str:
        """构建 AgentConfig 表中的唯一 key（新格式：agents/{hash}/{name}）"""
        return f"agents/{agent_hash}/{name}"

    @staticmethod
    def _read_config_db(agent_hash: str, name: str, db: Session = None) -> Optional[str]:
        """从 AgentConfig 表读取配置值"""
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        try:
            key = AgentInitService._config_key(agent_hash, name)
            config = db.query(AgentConfig).filter(
                AgentConfig.key == key,
                AgentConfig.agent_hash == agent_hash
            ).first()
            if config:
                return config.value
            return None
        except Exception as e:
            logger.warning(f"Failed to read config {name} for agent {agent_hash}: {e}")
            return None
        finally:
            if close_db:
                db.close()

    @staticmethod
    def _write_config_db(agent_hash: str, name: str, value: str, db: Session = None):
        """写入配置到 AgentConfig 表"""
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        try:
            key = AgentInitService._config_key(agent_hash, name)
            config = db.query(AgentConfig).filter(
                AgentConfig.key == key,
                AgentConfig.agent_hash == agent_hash
            ).first()
            if config:
                config.value = value
                config.updated_at = datetime.utcnow()
            else:
                config = AgentConfig(
                    key=key,
                    value=value,
                    agent_hash=agent_hash,
                    updated_at=datetime.utcnow()
                )
                db.add(config)
            db.commit()
        except Exception as e:
            logger.error(f"Failed to write config {name} for agent {agent_hash}: {e}")
            db.rollback()
            raise  # Let caller handle it
        finally:
            if close_db:
                db.close()

    @staticmethod
    def _config_exists(agent_hash: str, name: str) -> bool:
        """检查配置是否存在"""
        return AgentInitService._read_config_db(agent_hash, name) is not None

    def create_agent(
        self,
        db: Session,
        user_id: int,
        name: str = "",
        description: str = None,
        hash_value: str = None,
        agent_mode: str = "classic",
    ) -> AgentProfile:
        """
        创建新 Agent

        Args:
            db: 数据库会话
            user_id: 用户 ID
            name: Agent 名称
            description: Agent 描述
            hash_value: 指定的 4 位 hash（可选）
            agent_mode: V2 Agent 模式 "classic" | "im"（默认 classic）

        Returns:
            AgentProfile 实例
        """
        import secrets
        from sqlalchemy import func

        # 检查 Agent 数量限制（最多 100 个）
        agent_count = db.query(func.count(AgentProfile.id)).filter(
            AgentProfile.user_id == user_id
        ).scalar() or 0
        if agent_count >= 100:
            raise ValueError("每个用户最多创建 50 个 Agent")

        # 生成唯一的 4 位 hash
        if not hash_value:
            def generate_hash():
                return secrets.token_hex(2)  # 4 位十六进制，匹配 DB String(4)

            hash_value = generate_hash()
            max_attempts = 100
            attempts = 0

            while db.query(AgentProfile).filter(AgentProfile.hash == hash_value).first():
                hash_value = generate_hash()
                attempts += 1
                if attempts >= max_attempts:
                    raise ValueError("Failed to generate unique hash")

        # 生成 TOTP secret
        totp_secret = TOTPService.generate_secret()

        # 创建 Agent
        agent = AgentProfile(
            user_id=user_id,
            hash=hash_value,
            totp_secret=totp_secret,
            name=name or f"Agent-{hash_value}",
            description=description,
            status="pending",
            permissions="chat,upload,session",
            agent_mode=agent_mode if agent_mode in ("classic", "im") else "classic",
            parallel_sandbox=False,
            lock_behavior="wait_3s",
            created_at=datetime.utcnow()
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)

        logger.info(f"Created new agent: hash={agent.hash}, user_id={user_id}")

        # Agent V2: 如果是 IM Agent，自动启动协处理器
        if agent.agent_mode == "im":
            try:
                import asyncio
                from services.interrupt_controller import CoprocessorService
                # 创建协处理器（fire-and-forget，create_agent 是同步方法）
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.ensure_future(CoprocessorService.start(agent.hash))
                    else:
                        loop.run_until_complete(CoprocessorService.start(agent.hash))
                except RuntimeError:
                    # 没有事件循环 —— 跳过启动（由 restart_all 在 lifespan 阶段补启）
                    logger.info(f"[Coprocessor] create_agent 阶段无可用事件循环，Agent {agent.hash} 的协处理器将在 restart_all 时启动")
            except Exception as e:
                logger.warning(f"[Coprocessor] 启动失败 agent={agent.hash}: {e}")

        return agent

    def initialize_agent(
        self,
        db: Session,
        agent: AgentProfile,
        persona: str = None,
        tools_config: Dict = None,
        agent_config: Dict = None
    ) -> Dict[str, Any]:
        """
        初始化 Agent

        包括：
        - 保存 Agent 配置到数据库
        - 创建 VFS 目录结构（在 COS 上）
        - 加载默认工具
        - 设置 Agent persona

        Args:
            db: 数据库会话
            agent: AgentProfile 实例
            persona: 自定义 persona（可选）
            tools_config: 工具配置（可选）
            agent_config: Agent 配置（可选）

        Returns:
            初始化结果
        """
        agent_hash = agent.hash
        user_id = agent.user_id

        # 1. 保存 persona 到数据库
        persona_content = persona or self.DEFAULT_PERSONA
        self._write_config_db(agent_hash, "persona", persona_content)

        # 2. 保存 tools 配置到数据库（仅当不存在时写入默认值）
        # IM Agent 默认启用 get_group_history（IM 模式主要在群聊中工作，需要读群历史）
        default_tools = {
            "enabled": [
                "file_read", "file_write", "file_list", "file_delete",
                "bash", "web_search", "spawn_subagent",
                "create_share_link", "edit", "list_conversations",
                "load_conversation", "schedule_reminder", "generate_totp",
                "python_background", "python_task_list", "python_task_output",
                "get_group_history",
            ],
            "disabled": []
        }
        tools_content = tools_config or default_tools
        existing_tools = self._read_config_db(agent_hash, "tools", db=db)
        if not existing_tools:
            self._write_config_db(agent_hash, "tools", json.dumps(tools_content), db=db)

        # 3. 保存 agent 配置到数据库（仅当不存在时写入默认值）
        from services.model_registry import resolve as _ais_resolve
        _ais_main_info = _ais_resolve(settings.MAIN_TEXT_MODEL)
        default_config = {
            "llm_provider": _ais_main_info["provider"],
            "llm_model": settings.MAIN_TEXT_MODEL,
            "max_context_tokens": 110000,
            "compression_ratio": 0.3,
            "max_tool_rounds": 50
        }
        config_content = agent_config or default_config
        existing_config = self._read_config_db(agent_hash, "config", db=db)
        if not existing_config:
            self._write_config_db(agent_hash, "config", json.dumps(config_content), db=db)

        # 3.5 预填默认配置
        from services.agent_tools_service import DEFAULT_CONFIG
        for key, value in DEFAULT_CONFIG.items():
            config_key = f"agents/{agent_hash}/{key}"
            existing = db.query(AgentConfig).filter(
                AgentConfig.key == config_key,
                AgentConfig.agent_hash == agent_hash,
            ).first()
            if not existing:
                db.add(AgentConfig(
                    key=config_key,
                    value=str(value),
                    agent_hash=agent_hash,
                    permission="readwrite",
                    description=f"{key}",
                ))
        db.commit()

        vfs_base = self._get_vfs_base_path(agent.user_id, agent_hash)
        vfs_dirs_created = []

        # 4. 创建 Agent 元数据文件（agents/{hash}/agent/）
        if self.storage:
            agent_files = {
                "workspace/agent/BOOTSTRAP.md": DEFAULT_BOOTSTRAP,
                "workspace/agent/soul.md": DEFAULT_SOUL,
                "workspace/agent/identity.md": DEFAULT_IDENTITY,
                "workspace/agent/user.md": DEFAULT_USER,
                "workspace/agent/memory.md": DEFAULT_MEMORY_HEADER,
            }
            for rel_path, content in agent_files.items():
                file_key = f"{vfs_base}{rel_path}"
                try:
                    self.storage.put_object(file_key, content.encode("utf-8"))
                    logger.info(f"Created agent metadata file: {file_key}")
                    # 记录目录（去重）
                    dir_name = rel_path.split("/")[0]
                    if dir_name not in vfs_dirs_created:
                        vfs_dirs_created.append(dir_name)
                except Exception as e:
                    logger.warning(f"Failed to create agent metadata file {file_key}: {e}")

        # Note: soul/identity/user 不再写入 AgentConfig。
        # Agent 读取自 COS (workspace/agent/*.md)，配置页面也改为从 COS 读取。
        # 见 routers/agent_config.py

        # 5. 创建默认 memory.md（memory/memory.md）
        if self.storage:
            memory_key = f"{vfs_base}memory/memory.md"
            default_memory = f"# Agent {agent_hash} Memory\n\nCreated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n"
            try:
                self.storage.put_object(memory_key, default_memory.encode("utf-8"))
                if "memory" not in vfs_dirs_created:
                    vfs_dirs_created.append("memory")
            except Exception as e:
                logger.warning(f"Failed to create memory.md: {e}")

        # 6. 更新 Agent 状态
        agent.status = "initialized"
        agent.initialized_at = datetime.utcnow()
        db.commit()
        db.refresh(agent)

        logger.info(f"Agent {agent_hash} initialized successfully")

        # 7. 创建向量索引（kb + conv），确保搜索功能就绪
        try:
            from services.vector_search_service import VectorSearchService
            vs = VectorSearchService(agent_hash=agent_hash)
            vs.ensure_index(vs._get_index_name("kb"))
            vs.ensure_index(vs._get_index_name("conv"))
            logger.info(f"Vector indexes created for agent {agent_hash}")
        except Exception as e:
            logger.warning(f"Failed to create vector indexes for {agent_hash}: {e}")

        return {
            "status": "success",
            "agent_hash": agent_hash,
            "vfs_base": vfs_base,
            "directories_created": vfs_dirs_created
        }

    def get_agent_status(self, agent: AgentProfile) -> Dict[str, Any]:
        """
        获取 Agent 状态

        Args:
            agent: AgentProfile 实例

        Returns:
            Agent 状态信息
        """
        agent_hash = agent.hash

        # 检查 profile 配置（从 AgentConfig 表）
        persona_exists = self._config_exists(agent_hash, "persona")
        tools_exists = self._config_exists(agent_hash, "tools")
        config_exists = self._config_exists(agent_hash, "config")

        # 检查 VFS 目录
        vfs_base = self._get_vfs_base_path(agent.user_id, agent_hash)
        vfs_dirs = []
        if self.storage:
            for dir_name in ["workspace", "public", "memory"]:
                dir_key = f"{vfs_base}{dir_name}/.directory"
                try:
                    content = self.storage.get_file_content(dir_key)
                    if content is not None:
                        vfs_dirs.append(dir_name)
                except Exception:
                    pass

        return {
            "agent_hash": agent_hash,
            "status": agent.status,
            "profile_files": {
                "persona": persona_exists,
                "tools": tools_exists,
                "config": config_exists
            },
            "vfs_directories": vfs_dirs,
            "initialized_at": agent.initialized_at.isoformat() if agent.initialized_at else None
        }

    def load_agent_persona(self, agent_hash: str) -> Optional[str]:
        """
        加载 Agent persona

        Args:
            agent_hash: Agent 4 位 hash

        Returns:
            persona 内容或 None
        """
        return self._read_config_db(agent_hash, "persona")

    def load_agent_tools(self, agent_hash: str) -> Optional[Dict]:
        """
        加载 Agent 工具配置

        Args:
            agent_hash: Agent 4 位 hash

        Returns:
            工具配置或 None
        """
        value = self._read_config_db(agent_hash, "tools")
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return None
        return None

    def load_agent_config(self, agent_hash: str) -> Optional[Dict]:
        """
        加载 Agent 配置

        Args:
            agent_hash: Agent 4 位 hash

        Returns:
            配置或 None
        """
        value = self._read_config_db(agent_hash, "config")
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return None
        return None

    def get_available_tools(self) -> List[str]:
        """获取可用工具列表"""
        return self.AVAILABLE_TOOLS

    def get_persona_templates(self) -> Dict[str, Dict[str, str]]:
        """
        获取 persona 预设模板列表

        Returns:
            模板字典，包含每个模板的 name, description, persona
        """
        return self.PERSONA_TEMPLATES

    def validate_tools_config(self, tools_config: Dict) -> Tuple[bool, Optional[str]]:
        """
        验证工具配置

        Args:
            tools_config: 工具配置，格式为 {"enabled": [...], "disabled": [...]}

        Returns:
            (是否有效, 错误消息)
        """
        if not isinstance(tools_config, dict):
            return False, "tools_config must be a dictionary"

        enabled = tools_config.get("enabled", [])
        disabled = tools_config.get("disabled", [])

        if not isinstance(enabled, list) or not isinstance(disabled, list):
            return False, "enabled and disabled must be lists"

        # 检查工具名是否有效
        for tool in enabled + disabled:
            if tool not in self.AVAILABLE_TOOLS:
                return False, f"Invalid tool name: {tool}"

        # 检查是否有重复
        if set(enabled) & set(disabled):
            return False, "Tools cannot be both enabled and disabled"

        return True, None

    def validate_style(self, style: str) -> Tuple[bool, Optional[str]]:
        """验证回复风格"""
        if style not in self.VALID_STYLES:
            return False, f"Invalid style: {style}. Valid options: {self.VALID_STYLES}"
        return True, None

    def save_agent_persona(self, agent_hash: str, content: str) -> bool:
        """
        保存 Agent persona

        Args:
            agent_hash: Agent 4 位 hash
            content: persona 内容

        Returns:
            是否成功
        """
        if not content or not content.strip():
            return False

        self._write_config_db(agent_hash, "persona", content)

        logger.info(f"Saved persona for agent {agent_hash}")
        return True

    def save_agent_tools(self, agent_hash: str, tools_config: Dict) -> Tuple[bool, Optional[str]]:
        """
        保存 Agent 工具配置

        Args:
            agent_hash: Agent 4 位 hash
            tools_config: 工具配置

        Returns:
            (是否成功, 错误消息)
        """
        # 验证配置
        valid, error = self.validate_tools_config(tools_config)
        if not valid:
            return False, error

        self._write_config_db(agent_hash, "tools", json.dumps(tools_config))

        logger.info(f"Saved tools config for agent {agent_hash}")
        return True, None

    def save_agent_config(self, agent_hash: str, config: Dict) -> Tuple[bool, Optional[str]]:
        """
        保存 Agent 配置（包括 style）

        Args:
            agent_hash: Agent 4 位 hash
            config: 配置字典，可包含 style 字段

        Returns:
            (是否成功, 错误消息)
        """
        # 验证 style（如果提供）
        if "style" in config:
            valid, error = self.validate_style(config["style"])
            if not valid:
                return False, error

        # 加载现有配置并合并
        existing_config = self.load_agent_config(agent_hash) or {}
        merged_config = {**existing_config, **config}

        self._write_config_db(agent_hash, "config", json.dumps(merged_config))

        logger.info(f"Saved config for agent {agent_hash}")
        return True, None

    def reload_agent_config(self, agent_hash: str) -> Dict[str, Any]:
        """
        重新加载 Agent 配置（返回所有配置）

        Args:
            agent_hash: Agent 4 位 hash

        Returns:
            配置信息
        """
        persona = self.load_agent_persona(agent_hash)
        tools = self.load_agent_tools(agent_hash)
        config = self.load_agent_config(agent_hash)

        return {
            "persona": persona,
            "tools": tools,
            "config": config,
            "style": config.get("style", "professional") if config else "professional"
        }

    def _get_vfs_base_path(self, user_id: int, agent_hash: str) -> str:
        """获取 Agent VFS base path"""
        return f"{settings.TENCENT_COS_PREFIX}agents/{agent_hash}/"


# 全局实例
agent_init_service = AgentInitService()


def ensure_default_agent_5178():
    """
    确保默认 Agent 5178 存在
    在应用启动时调用

    TODO: "5178" 硬编码，建议移到 settings.DEFAULT_AGENT_HASH
    """
    db = SessionLocal()
    try:
        # 查找 admin 用户
        from models.database import User
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            logger.warning("Admin user not found, skipping Agent 5178 creation")
            return None

        # 查找 Agent 5178
        agent = db.query(AgentProfile).filter(AgentProfile.hash == "5178").first()

        if agent:
            logger.info("Agent 5178 already exists")
            return agent

        # 创建 Agent 5178
        agent = agent_init_service.create_agent(
            db=db,
            user_id=admin.id,
            hash_value="5178",
            name="FeClaw 助手",
            description="默认智能体"
        )

        # 初始化 Agent
        agent_init_service.initialize_agent(db, agent)

        logger.info("Agent 5178 created and initialized")
        return agent

    except Exception as e:
        logger.error(f"Failed to create Agent 5178: {e}")
        return None
    finally:
        db.close()
