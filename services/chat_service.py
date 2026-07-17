"""
聊天业务服务 - 渠道无关的 AI 对话核心逻辑

职责：
- 构建系统提示词（从 VFS 读取 soul/identity/user/memory）
- 管理 AI 对话流程（流式响应、工具调用）
- 处理消息压缩和历史管理
- 会话管理（创建/恢复/保存）

使用方式：
    chat = ChatService(user_id="2", channel="wechat")
    async for event in chat.chat("你好"):
        if event.type == ChatEventType.TEXT:
            print(event.content)
        elif event.type == ChatEventType.DONE:
            print("对话结束")
"""

import asyncio
import json
import logging
import time
import warnings
from typing import AsyncGenerator, Optional, List, Dict, Any, Callable, Awaitable
from datetime import datetime
from contextlib import contextmanager

from models.chat import ChatEvent, ChatEventType, ChatContext
from models.chat_input import ChatInput, Attachment
from config import settings
from services.virtual_filesystem import VirtualFileSystem
from services.agent_tools_service import AgentToolsService, Step
from services.agent_executor import AgentExecutor
from services.point_service import PointService
from services.message_compactor import estimate_tokens
from services.workspace_service import get_agent_workspace_root
from services.active_tracker import track_start, track_end
from models.database import SessionLocal, AgentProfile, ChatHistory

logger = logging.getLogger(__name__)

# VFS 文件内容缓存 { path: (content, expiry_timestamp) }
# 避免每次对话都从 COS 重新读取静态文件
_VFS_FILE_CACHE: Dict[str, tuple] = {}  # key="{agent_hash}:{path}"
_VFS_CACHE_TTL = 60  # 缓存存活秒数


@contextmanager
def get_session():
    """SessionLocal 的上下文管理器"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

# Agent 初始化提示模板
AGENT_PENDING_PROMPT = """Agent 尚未初始化

此 Agent 正处于待初始化状态，需要完成初始化后才能正常使用。

请通过管理控制台完成 Agent 初始化配置。

初始化完成后，Agent 将能够：
- 读取人格设定（soul.md）
- 配置身份信息（identity.md）
- 存储用户偏好（user.md）
- 管理长期记忆（memory.md）

如果您是管理员，请尽快完成初始化配置。
"""

# 最大历史消息对数
MAX_HISTORY_MESSAGES = 20


def _has_meaningful_content(text: str) -> bool:
    """判断 memory.md 是否有实际内容，跳过纯标题/占位符的空模板。"""
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if not lines:
        return False
    # 排除纯标题行
    content_lines = [l for l in lines if not l.startswith('#')]
    # 排除仅占位符行
    placeholders = {'暂无记录', '暂无信息', ''}
    content_lines = [l for l in content_lines if l not in placeholders]
    return len(content_lines) > 0


class NoReplyError(Exception):
    """群聊模式专用：Agent 决定本轮不回复（NO_REPLY）

    由 GroupDispatchService 捕获以跳过本轮。
    """
    def __init__(self, agent_hash: str, group_id: str):
        self.agent_hash = agent_hash
        self.group_id = group_id
        super().__init__(f"Agent {agent_hash} returned NO_REPLY in group {group_id}")


class ChatService:
    """聊天业务服务 - 渠道无关"""

    def __init__(
        self,
        agent_hash: str,
        channel: str = "api",
        session_id: Optional[str] = None,
        session_reset_at: Optional[datetime] = None,
        pre_process_hook: Optional[Callable[[str, Dict, str], Awaitable[Optional[str]]]] = None,
        group_id: Optional[str] = None,
    ):
        """
        初始化聊天服务

        Args:
            agent_hash: Agent hash（必需）
            channel: 渠道标识（wechat, api, web 等）
            session_id: 会话 ID（wechat_main / web_sess_abc）
            session_reset_at: 会话切割时间（WeChat 新会话用）
            pre_process_hook: 前置钩子 async (channel, meta, text) -> str|None
                              None=继续, ""=静默终止, "文本"=回复并跳过LLM
            group_id: 群组 ID（V2 群聊模式；设置后从 GroupMessage 拉上下文，回复双写）
        """
        self.agent_hash = agent_hash
        self.channel = channel
        self.session_id = session_id
        self.session_reset_at = session_reset_at
        self.pre_process_hook = pre_process_hook
        self.group_id = group_id
        self._agent_status = None
        self._user_id = None

        # 内部组件（group_id 注入到 tools 以支持 /mnt/group/ 路径解析）
        if group_id:
            self.tools = AgentToolsService(agent_hash=agent_hash, group_id=group_id)
        else:
            self.tools = AgentToolsService(agent_hash=agent_hash)
        self.executor = AgentExecutor(agent_hash, self.tools)
        self.workspace_root = get_agent_workspace_root(agent_hash)

        self.vfs = VirtualFileSystem(agent_hash=agent_hash)
        self.context = ChatContext(user_id=self.user_id, channel=channel)
        self._history_loaded_from_session = False  # 由 WebChannelService 设置

        # Session Memory 提取状态追踪
        self._session_memory_lock = asyncio.Lock()
        self._session_memory_initialized: bool = False
        self._session_memory_last_extract_idx: int = 0
        self._session_memory_tool_calls_since: int = 0

        # Reflection 事实核查状态
        self._pending_correction = None  # {short_desc, topic, msg_count, consumed}

        # User Profile 提取状态追踪
        self._user_profile_lock = asyncio.Lock()
        self._user_profile_initialized: bool = False
        self._user_profile_last_extract_idx: int = 0
        self._user_profile_last_extract_time: float = 0
        self._user_profile_tool_calls_since: int = 0

    @property
    def is_group_mode(self) -> bool:
        """是否群聊模式（V2）"""
        return bool(self.group_id)

    def _check_agent_status(self) -> str:
        """检查 Agent 状态（懒加载）"""
        if self._agent_status is None:
            with get_session() as db:
                agent = db.query(AgentProfile).filter(AgentProfile.hash == self.agent_hash).first()
                if agent:
                    self._agent_status = agent.status
                else:
                    self._agent_status = "unknown"
        return self._agent_status

    @property
    def user_id(self) -> str:
        """获取所属用户 ID（懒加载）"""
        if self._user_id is None:
            with get_session() as db:
                agent = db.query(AgentProfile).filter(AgentProfile.hash == self.agent_hash).first()
                if agent:
                    self._user_id = str(agent.user_id)
                else:
                    raise ValueError(f"Agent {self.agent_hash} not found")
        return self._user_id

    async def chat(
        self,
        user_input: str = None,
        image_url: Optional[str] = None,
        skip_history: bool = False,
        input: Optional[ChatInput] = None,
    ) -> AsyncGenerator[ChatEvent, None]:
        """
        核心 AI 对话方法

        新签名（推荐）：
            chat(input=ChatInput(text=..., attachments=..., meta=...))
        旧签名（兼容，发出 deprecation warning）：
            chat(user_input, image_url, skip_history)

        Yields:
            ChatEvent: 对话事件流
        """
        # 构建 ChatInput（兼容旧签名）
        if input is not None:
            actual = input
        else:
            warnings.warn(
                "ChatService.chat(user_input, image_url, skip_history) is deprecated. "
                "Use chat(input=ChatInput(...)) instead.",
                DeprecationWarning, stacklevel=2
            )
            attachments = []
            if image_url:
                attachments.append(Attachment(type="image", url=image_url))
            actual = ChatInput(
                text=user_input or "",
                attachments=attachments,
                meta={}
            )

        _req_id = None
        try:
            _chat_t0 = time.time()
            _req_id = track_start(self.agent_hash, self.channel)
            logger.warning(f"[TIMING] chat() start, agent={self.agent_hash}, channel={self.channel}, user_input_len={len(actual.text) if actual.text else 0}")

            # ① 前置钩子
            if self.pre_process_hook:
                result = await self.pre_process_hook(self.channel, actual.meta, actual.text)
                if result is not None:
                    if result == "":
                        return  # 静默终止
                    yield ChatEvent(
                        type=ChatEventType.TEXT,
                        content=result
                    )
                    yield ChatEvent(
                        type=ChatEventType.DONE,
                        content=result
                    )
                    return

            # ② 检查 Agent 状态
            agent_status = self._check_agent_status()

            # 立即给前端反馈，避免等待 VFS 读取
            yield ChatEvent(type=ChatEventType.PIPELINE, content="🤔 正在准备...")

            if agent_status == "pending":
                yield ChatEvent(
                    type=ChatEventType.TEXT,
                    content=AGENT_PENDING_PROMPT
                )
                yield ChatEvent(
                    type=ChatEventType.DONE,
                    content=AGENT_PENDING_PROMPT,
                    metadata={"channel": self.channel, "agent_status": "pending"}
                )
                return

            # ③ 检查每日配额
            if not PointService.try_deduct(self.user_id):
                yield ChatEvent(
                    type=ChatEventType.ERROR,
                    error_message="今日对话次数已达上限"
                )
                return

            # ④ 构建系统提示词
            _t_build_sp = time.time()
            system_prompt = await self.build_system_prompt()
            logger.warning(f"[TIMING] build_system_prompt: {time.time()-_t_build_sp:.1f}s")

            # ⑤ 加载历史消息
            _t_load_hist = time.time()
            if not skip_history and not self._history_loaded_from_session:
                await self._load_history()
                logger.warning(f"[TIMING] load_history: {time.time()-_t_load_hist:.1f}s, history_len={len(self.context.history)}")
                yield ChatEvent(
                    type=ChatEventType.HISTORY_LOADED,
                    metadata={"count": len(self.context.history)}
                )

            # ⑥ 自动解析引用令牌 [reference:xxx]
            _resolved = await self._resolve_references(actual.text)
            if _resolved != actual.text:
                actual.text = _resolved

            # ⑦ 构建消息列表
            messages = self._build_messages(system_prompt, actual.text, image_url)

            # ⑧ 调用 AI 进行对话
            _t_ai = time.time()
            full_response = ""
            # 结构化事件列表（用于 ChatHistory 持久化，区分纯文本 vs 工具调用 vs 工具结果）
            # 每项: {"role": "assistant"|"tool", "content": str, "tool_call_id"?, "tool_name"?, "tool_args"?}
            structured_events: List[Dict[str, Any]] = []
            async for event in self._stream_ai_response(messages, structured_events):
                if event.type == ChatEventType.TEXT:
                    full_response += event.content
                yield event
            logger.warning(f"[TIMING] AI response: {time.time()-_t_ai:.1f}s, response_len={len(full_response)}")

            # ⑧ 保存对话历史
            _t_save = time.time()
            self._save_conversation(
                actual.text, full_response,
                structured_events=structured_events,
                meta=actual.meta if actual.meta else None,
                attachments=actual.attachments if actual.attachments else None,
            )
            logger.warning(f"[TIMING] save_conversation: {time.time()-_t_save:.1f}s")

            # ⑨ 会话记忆后台提取（非阻塞）
            # IM Agent 关闭 session memory（会自我强化："辩论已结束"写入后又读到）
            is_im_agent = (
                self.channel == "group" and not self.group_id
            )
            if not is_im_agent and settings.SESSION_MEMORY_ENABLED:
                asyncio.create_task(self._maybe_extract_session_memory())

            # ⑩ 用户画像后台提取（非阻塞）
            if getattr(settings, 'USER_PROFILE_ENABLED', True):
                asyncio.create_task(self._maybe_extract_user_profile())

            # ⑪ 发送完成事件
            yield ChatEvent(
                type=ChatEventType.DONE,
                content=full_response,
                metadata={"channel": self.channel}
            )

        except Exception as e:
            track_end(_req_id)
            logger.error(f"[ChatService] chat error: {e}", exc_info=True)
            yield ChatEvent(
                type=ChatEventType.ERROR,
                error_message=str(e)
            )
        finally:
            track_end(_req_id)

    async def chat_to_completion(
        self,
        user_input: str = "",
        input: Optional[ChatInput] = None,
        no_reply_check: bool = True,
    ) -> str:
        """
        V2 群聊模式专用：消费 chat() 事件流，返回最终纯文本。

        - 在 group_mode 下：检测 NO_REPLY → raise NoReplyError
        - 否则返回完整回复文本（含 SILENT 由调用方自行判断）
        - 群聊模式下 user_input 通常是触发本轮的"最新消息"；如果不传，
          ChatService 会自己从 GroupMessage 拉历史。
        """
        from models.chat_input import Attachment as _A
        if input is None and user_input:
            input = ChatInput(text=user_input, attachments=[], meta={"group_id": self.group_id} if self.group_id else {})
        elif input is None and not user_input:
            # 群聊模式：可能从历史自动取最近一条作为触发输入
            latest = self._get_latest_group_message()
            if latest:
                input = ChatInput(text=latest, attachments=[], meta={"group_id": self.group_id})

        full_text = ""
        error_msg = None
        try:
            async for event in self.chat(input=input):
                if event.type == ChatEventType.TEXT:
                    full_text += event.content or ""
                elif event.type == ChatEventType.ERROR:
                    error_msg = event.error_message
                elif event.type == ChatEventType.DONE:
                    # DONE 事件携带完整文本
                    if event.content:
                        full_text = event.content
        except NoReplyError:
            raise
        except Exception as e:
            logger.warning(f"[ChatService] chat_to_completion error: {e}", exc_info=True)
            return ""

        if error_msg:
            logger.warning(f"[ChatService] chat_to_completion got ERROR event: {error_msg}")
            return ""

        full_text = (full_text or "").strip()

        # 群聊模式：检测 NO_REPLY
        if no_reply_check and self.is_group_mode:
            from services.group_service import NO_REPLY_MAGIC
            if full_text == NO_REPLY_MAGIC:
                raise NoReplyError(self.agent_hash, self.group_id or "")

        return full_text

    def _get_latest_group_message(self) -> str:
        """V2 群聊：取最新一条非本 Agent 的群消息作为本轮触发输入"""
        try:
            from models.group import GroupMessage
            with get_session() as db:
                msg = (
                    db.query(GroupMessage)
                    .filter(GroupMessage.group_id == self.group_id)
                    .order_by(GroupMessage.created_at.desc())
                    .first()
                )
                if msg:
                    return msg.content or ""
        except Exception as e:
            logger.debug(f"[ChatService] _get_latest_group_message failed: {e}")
        return ""
    async def build_system_prompt(self) -> str:
        """构建系统提示词，优先使用 AgentProfile.system_prompt，否则从 VFS 读取"""

        # 0. 当前时间信息（用 Python 标准库，不手算）
        import zoneinfo as _zi
        from datetime import datetime as _dt
        from utils.lunar_date import LunarDate as _LunarDate

        _tz = _zi.ZoneInfo("Asia/Shanghai")
        _now = _dt.now(_tz)
        _naive = _now.replace(tzinfo=None)
        _lc = _LunarDate.from_datetime(_naive)
        _wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][_now.weekday()]
        _cn = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
               "十一", "十二"]
        _lc_month = _cn[_lc.lunar_month]
        _lc_day = _lc.lunar_day
        if _lc_day <= 10:
            _lc_day_str = f"初{_cn[_lc_day]}"
        elif _lc_day < 20:
            _lc_day_str = f"十{_cn[_lc_day - 10]}"
        elif _lc_day == 20:
            _lc_day_str = "二十"
        else:
            _lc_day_str = f"廿{_cn[_lc_day - 20]}"
        _time_header = (
            "【当前时间（BJT）】\n"
            f"{_now.year}.{_now.month}.{_now.day}（农历{_lc_month}月{_lc_day_str}，{_wd}） {_now.hour:02d}:{_now.minute:02d}"
        )

        # 1. 优先检查 AgentProfile 中的自定义 system_prompt
        with get_session() as db:
            profile = db.query(AgentProfile).filter(
                AgentProfile.hash == self.agent_hash
            ).first()
            if profile and profile.system_prompt:
                # V2 群聊模式：追加渠道上下文说明
                if self.is_group_mode:
                    return (
                        _time_header + "\n\n" + profile.system_prompt
                        + "\n\n【当前渠道】群组聊天模式（group_id=" + (self.group_id or "") + "）。"
                        "所有传入消息都来自群聊上下文（用户和其他 Agent），请以群聊身份回复。"
                    )
                return _time_header + "\n\n" + profile.system_prompt

        # 2. 回退到 VFS 读取 - 所有文件并行读取
        parts = [_time_header]

        all_paths = [
            "/workspace/agent/soul.md",
            "/workspace/agent/identity.md",
            "/workspace/agent/user.md",
            "/workspace/agent/memory.md",
            "/public/feclaw/index.md",
            "/public/feclaw/principles.md",
            "/public/feclaw/session_management.md",
            "/public/feclaw/image_processing.md",
            "/public/feclaw/skills/INDEX.md",
            "/workspace/agent/skills/INDEX.md",
            "/workspace/agent/BOOTSTRAP.md",
        ]
        files = await self._read_vfs_files_async(*all_paths)

        soul_content = files.get("/workspace/agent/soul.md")
        identity_content = files.get("/workspace/agent/identity.md")
        user_content = files.get("/workspace/agent/user.md")
        memory_content = files.get("/workspace/agent/memory.md")
        platform_info = files.get("/public/feclaw/index.md")
        principles_content = files.get("/public/feclaw/principles.md")
        session_mgmt = files.get("/public/feclaw/session_management.md")
        image_processing = files.get("/public/feclaw/image_processing.md")
        skills_index = files.get("/public/feclaw/skills/INDEX.md")
        local_skills = files.get("/workspace/agent/skills/INDEX.md")
        bootstrap_content = files.get("/workspace/agent/BOOTSTRAP.md")

        # 读取人格设定
        if soul_content:
            parts.append(f"【人格设定】\n{soul_content}")

        # 读取身份配置
        if identity_content:
            parts.append(f"【身份配置】\n{identity_content}")

        # 读取用户信息
        if user_content:
            parts.append(f"【用户信息】\n{user_content}")

            # 检查并注入画像摘要
            try:
                from services.user_profile_service import UserProfileService
                if UserProfileService.has_learning_profile(user_content):
                    injection = UserProfileService.build_injection(user_content)
                    if injection:
                        parts.append(injection)
            except Exception as e:
                logger.debug(f"[ChatService] User profile injection skipped: {e}")

        # 读取长期记忆（跳过纯标题的空模板）
        if memory_content and _has_meaningful_content(memory_content):
            parts.append(f"【长期记忆】\n{memory_content}")
            # 追加写入指南
            parts.append("""【长期记忆 - 写入指南】
使用 file_write 工具写入 /workspace/agent/memory.md 来维护以下类型的信息：

### 应该保存
- **用户偏好**：称呼、语气偏好、使用习惯
- **重要决策**：选择方案和理由、拒绝的方案
- **修正反馈**：用户纠正你做法的情况（"不是这样"、"别用X"）
- **完成确认**：用户明确认可的策略或方案
- **任务状态**：正在进行中的任务和进展

### 格式建议
使用 markdown 标题分类，按时间顺序追加，不要删除旧内容。

### 不要保存
- 代码或文件路径（从文件系统可读）
- Git 历史（git 命令可查）
- 临时对话状态（会过时）
- 当前消息的逐字记录（对话历史里已有）
""")
        else:
            parts.append("""【长期记忆】
你有长期记忆能力。当你发现需要记住以下类型的信息时，请使用 file_write 工具写入 /workspace/agent/memory.md：
- 用户偏好、重要决策、修正反馈、任务状态
保持简洁，按标题分类，不要保存代码路径或临时对话状态。
""")

        # 会话笔记（自动记录，按需读取）
        session_note_path = "/workspace/agent/session_memory.md"
        # IM Agent 的 session memory 会自我强化（"辩论已结束"写入后又读到），暂时注释
        is_im_agent = (
            self.channel == "group" and not self.group_id
        )
        if not is_im_agent and (self._session_memory_initialized or settings.SESSION_MEMORY_ENABLED):
            parts.append(f"【会话笔记】\n当前会话有自动记录的笔记文件，包含近期重要信息。如需了解会话上下文，请使用 file_read 读取 `{session_note_path}`。")

        # 平台信息
        if platform_info:
            parts.append(f"【平台信息】\n{platform_info}")

        # 工具调用原则（从公共空间读取）
        if principles_content:
            parts.append(f"【重要：工具调用原则】\n{principles_content}")
        else:
            parts.append("""【重要：工具调用原则】
🚨 **必须真实进行 tool call，而不是宣称调用了工具但实际没有。**
1️⃣ **工具结果 > 你的训练记忆**。搜索结果是实时信息，必须基于回答。
2️⃣ 历史中的 [⚠️] 标记可能已过时，重新调用确认。
3️⃣ 如果回复里出现了"我让子Agent干某某事"、"读取文件最新内容"、"保存到错题本"之类的话，但没有对应工具调用，那就是在编造，绝不允许！""")

        # 会话管理提示
        if session_mgmt:
            parts.append(f"【会话管理】\n{session_mgmt}")
        else:
            parts.append("""【会话管理】
你可以使用以下工具管理对话会话：
- end_conversation: 结束当前对话并保存会话记录
- list_conversations: 列出用户的所有已保存会话
- load_conversation: 加载指定的历史会话继续对话
- search_sessions: 根据关键词搜索相关会话
- auto_suggest_session: 根据当前对话内容自动建议相关历史会话
当用户想要回顾之前的讨论或切换到其他话题时，主动使用这些工具帮助管理会话。""")

        # 图片处理提示
        if image_processing:
            parts.append(f"【图片处理】\n{image_processing}")
        else:
            parts.append("""【图片处理】
规则1️⃣：**用户没文字、只发图 → 不要分析，只回复"收到图片"等待指示。**
规则2️⃣：**默认 spawn_subagent 用轻量模型（qwen3.6-35b-a3b）。doubao 系列太慢，除非确认极难任务否则禁用。**
规则3️⃣：**预识别提供 {场景/文字/风格/意图} 供参考，但你仍须遵循规则1。**

注意：微信默认图片先发文字后到，所以看到无文字图片时极大概率是用户还在编辑文字。""")

        # 微信渠道 - 长内容输出规则
        if self.channel == "wechat":
            parts.append("""【微信消息 - 长内容输出规则】
微信对复杂的格式（如LaTeX公式${...}$、表格、大段代码、结构化数据）支持不佳。
当涉及以下场景时，默认先将内容写入 VFS MD 文件，用 create_share_link 生成分享链接发送给用户，同时附简短摘要：
- 收集或展示题目列表、练习题答案详解
- 大段公式推导、多行计算过程
- 表格数据、代码片段
- 任何超过 200 字的格式化内容

除非用户明确指定了输出形式（如"直接发"、"计算器"等），否则默认走文件分享路径。""")

        # Skills 系统（按需加载）
        if skills_index:
            parts.append(f"【技能系统】\n{skills_index}")

        # Agent 本地技能（自动从对话中积累）
        if local_skills and len(local_skills) > 50:
            parts.append(f"【个人技能】\n{local_skills}")

        # 知识库 RAG：搜索相关 VFS 索引内容（静默失败，不阻塞主流程）
        try:
            from services.vector_search_service import VectorSearchService
            vs = VectorSearchService(agent_hash=self.agent_hash)
            # 使用空查询搜索最近索引的内容（最多 3 条）
            rag_results = await vs.search("", top_k=3)
            if rag_results:
                rag_lines = ["【知识库参考】", "以下内容来自您之前创建的文件："]
                for r in rag_results:
                    meta = r.get("metadata", {})
                    fp = meta.get("file_path", r.get("key", ""))
                    text = meta.get("text", "")[:200]
                    score = r.get("score", 0)
                    if score > 0.3:  # 相关性阈值
                        rag_lines.append(f"- {fp}: {text}")
                if len(rag_lines) > 2:
                    parts.append("\n".join(rag_lines))
        except Exception as e:
            logger.debug(f"[ChatService] RAG search skipped: {e}")

        # BOOTSTRAP.md 初始化引导（存在时注入，不存在则忽略）
        if bootstrap_content:
            parts.append(f"【初始化引导 — BOOTSTRAP.md】\n{bootstrap_content}")

        # 平台能力提示
        parts.append(
            "【平台能力】\n"
            "Markdown 分享链接支持渲染 Mermaid 图表。"
            "当你需要展示思维导图、流程图、时序图、类图等时，"
            "可用 ```mermaid 代码块语法编写，分享后自动渲染为矢量图。"
        )

        return "\n\n".join(parts)
    
    def _read_vfs_file(self, path: str) -> Optional[str]:
        """从 VFS 读取文件内容"""
        try:
            content = self.vfs.cat(path)
            if content and not content.startswith('Error'):
                return content.strip()
        except Exception as e:
            logger.debug(f"[ChatService] VFS read error for {path}: {e}")
        return None

    async def _read_vfs_files_async(self, *paths: str) -> Dict[str, Optional[str]]:
        """并行读取多个 VFS 文件（带 60s TTL 缓存，key 含 agent_hash 防跨 Agent 泄露）"""
        now = time.time()

        # 先检查缓存（带 agent_hash 前缀，防止不同 Agent 读到对方的文件）
        result = {}
        uncached = []
        for p in paths:
            cache_key = f"{self.agent_hash}:{p}"
            cached = _VFS_FILE_CACHE.get(cache_key)
            if cached and cached[1] > now:
                result[p] = cached[0]
            else:
                uncached.append(p)

        if not uncached:
            return result

        # 只拉取缓存失效的文件
        # 使用统一的 FileStorage 抽象（COS 或 LocalStorage），避免 COS SDK 握手开销
        # 与 httpx 网络下载。key 始终是 _resolve_path 返回的 COS key（如
        # "feclaw/agents/{hash}/..."），FileStorage 内部按存储后端解析到正确路径。
        from services.file_storage import create_file_storage
        storage = create_file_storage()

        async def _read_one(path):
            try:
                cos_key, err = self.vfs._resolve_path(path)
                if err or not cos_key:
                    return (path, None)

                # get_file_content 是同步 IO，放到 executor 避免阻塞事件循环
                loop = asyncio.get_event_loop()
                raw = await loop.run_in_executor(None, storage.get_file_content, cos_key)
                if raw:
                    content = raw.decode("utf-8", errors="ignore").strip()
                    if content:
                        cache_key = f"{self.agent_hash}:{path}"
                        _VFS_FILE_CACHE[cache_key] = (content, now + _VFS_CACHE_TTL)
                        return (path, content)
            except Exception as e:
                logger.debug(f"[ChatService] VFS read error for {path}: {e}")
            return (path, None)

        fresh = await asyncio.gather(*[_read_one(p) for p in uncached])
        for path, content in fresh:
            result[path] = content
        return result
    
    async def _load_history(self):
        """从 ChatHistory 表加载对话历史，注入到 ChatContext

        WeChat 渠道过渡期：ChatHistory 为空时 fallback 到 WeChatMessage。
        V2 群聊模式：从 GroupMessage 拉群聊上下文（所有消息标 role=user）。
        """
        try:
            # V2 群聊模式：从 GroupMessage 加载群聊上下文
            if self.is_group_mode:
                await self._load_history_from_group_messages()
                return

            with get_session() as db:
                query = db.query(ChatHistory).filter(
                    ChatHistory.user_id == int(self.user_id),
                    ChatHistory.agent_hash == self.agent_hash,
                    ChatHistory.channel == self.channel,
                )
                if self.session_id:
                    query = query.filter(ChatHistory.session_id == self.session_id)
                if self.session_reset_at:
                    query = query.filter(ChatHistory.created_at > self.session_reset_at)

                records = query.order_by(ChatHistory.created_at.asc()).limit(200).all()
                if records:
                    history: List[Dict[str, Any]] = []
                    for r in records:
                        # 加载 tool 字段（仅在新版（带 tool_call_id 等）记录时使用）
                        if getattr(r, "tool_call_id", None) or getattr(r, "tool_name", None):
                            history.append({
                                "role": r.role,
                                "content": r.content or "",
                                "tool_call_id": r.tool_call_id,
                                "tool_name": r.tool_name,
                                "tool_args": r.tool_args,
                            })
                        else:
                            history.append({
                                "role": r.role,
                                "content": r.content or "",
                            })
                    self.context.history = history
                elif self.channel == "wechat":
                    # 过渡期 fallback：ChatHistory 尚无数据，从 WeChatMessage 读取
                    await self._load_history_from_wechat_messages(db)
        except Exception as e:
            logger.debug(f"[ChatService] Failed to load history: {e}")

    async def _load_history_from_group_messages(self):
        """V2 群聊模式：从 GroupMessage 加载群聊上下文

        规则：所有群聊消息（用户/Agent）都标 role=user，让模型把它们视为「环境输入」。
        自己在本群之前的回复通过 session_memory 注入 system prompt。
        """
        try:
            from models.group import GroupMessage
            with get_session() as db:
                records = (
                    db.query(GroupMessage)
                    .filter(GroupMessage.group_id == self.group_id)
                    .order_by(GroupMessage.created_at.asc())
                    .limit(200)
                    .all()
                )
                if not records:
                    self.context.history = []
                    return

                # 加载 sender name 用于消息前缀
                from models.database import AgentProfile as _AP
                sender_hashes = {m.sender_hash for m in records if m.sender_hash}
                agent_rows = db.query(_AP.hash, _AP.name).filter(_AP.hash.in_(sender_hashes)).all() if sender_hashes else []
                hash_to_name = {h: n for h, n in agent_rows if n}

                history = []
                for m in records:
                    content = m.content or ""
                    # 加 sender 前缀便于 Agent 区分
                    if m.sender_type == "user":
                        prefix = "[用户]"
                    elif m.sender_type == "agent":
                        sname = hash_to_name.get(m.sender_hash, m.sender_hash or "?")
                        prefix = f"[Agent {sname}]"
                    else:
                        prefix = f"[{m.sender_type}]"
                    history.append({
                        "role": "user",
                        "content": f"{prefix} {content}".strip(),
                        "_sender_type": m.sender_type,
                        "_sender_hash": m.sender_hash,
                    })
                self.context.history = history
                logger.info(
                    f"[ChatService] Loaded {len(history)} group messages for group={self.group_id}"
                )
        except Exception as e:
            logger.warning(f"[ChatService] Failed to load group history: {e}")
            self.context.history = []

    async def _load_history_from_wechat_messages(self, db):
        """WeChat 渠道过渡期 fallback：从 WeChatMessage 读取历史并转换为 ChatHistory 格式"""
        from models.database import WeChatBinding, WeChatMessage
        import re as _re

        binding = db.query(WeChatBinding).filter(
            WeChatBinding.user_id == int(self.user_id),
            WeChatBinding.agent_hash == self.agent_hash,
        ).first()

        if not binding:
            return

        query = db.query(WeChatMessage).filter(
            WeChatMessage.binding_id == binding.id,
            WeChatMessage.agent_hash == binding.agent_hash,
        )
        if self.session_reset_at:
            query = query.filter(WeChatMessage.created_at > self.session_reset_at)

        db_messages = query.order_by(WeChatMessage.created_at.asc()).limit(50).all()

        history = []
        for msg in db_messages:
            if not msg.content or msg.content.startswith("{"):
                continue
            if msg.direction == "received":
                history.append({"role": "user", "content": msg.content})
            elif msg.direction == "sent":
                if msg.content.startswith("[工具调用]") or msg.content.startswith("{"):
                    continue
                content = msg.content
                content = _re.sub(r'</?invoke[^>]*>|</?parameter[^>]*>|🔧\s*执行工具:\s*[\w_]+', '', content).strip()
                history.append({"role": "assistant", "content": content})

        if history:
            self.context.history = history
            logger.info(f"[ChatService] Loaded {len(history)} messages from WeChatMessage fallback")
    
    def _inject_corrections(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """注入未消费的 pending_correction 到最新 user prompt 末尾"""
        if not self._pending_correction or self._pending_correction.get("consumed"):
            return messages

        corr = self._pending_correction
        msgs = list(messages)
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                ct = corr.get("topic", "").lower()
                ui = msgs[i].get("content", "")
                if isinstance(ui, str):
                    ui_lower = ui.lower()
                else:
                    continue
                # 短 topic 守卫：topic 太短不匹配，避免 "力" 匹配 "努力了"
                if len(ct) <= 2:
                    break
                if ct in ui_lower or ui_lower in ct:
                    msgs[i]["content"] = ui + f"\n\n[内部提醒] {corr['short_desc']}"
                    corr["consumed"] = True
                    logger.info(
                        "[ChatService] Injected pending_correction: topic=%s",
                        corr.get("topic")
                    )
                break
        return msgs

    async def _resolve_references(self, text: str) -> str:
        """自动解析消息中的 [reference:xxx] 引用令牌，替换为实际内容"""
        import re as _re
        refs = _re.findall(r'\[reference:([a-zA-Z0-9]+)\]', text)
        if not refs:
            return text
        try:
            from models.database import get_session, ShareReference
            with get_session() as db:
                for ref_hash in set(refs):
                    ref = db.query(ShareReference).filter(
                        ShareReference.ref_hash == ref_hash
                    ).first()
                    if ref and ref.selected_text:
                        insert = f"\n\n> 📖 引用内容：{ref.selected_text}"
                        if ref.context_before or ref.context_after:
                            ctx = ""
                            if ref.context_before:
                                ctx += f"...{ref.context_before[-150:]}"
                            ctx += ref.selected_text
                            if ref.context_after:
                                ctx += f"{ref.context_after[:150]}..."
                            insert += f"\n> 📖 上下文（供参考）：{ctx}"
                        text = text + insert
        except Exception as e:
            logger.warning(f"[ChatService] Reference resolution failed: {e}")
        return text

    def _build_messages(
        self,
        system_prompt: str,
        user_input: str,
        image_url: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """构建消息列表

        历史消息转换规则：
        - role="user" → {"role": "user", "content": ...}
        - role="assistant" + tool_name + tool_call_id → assistant 消息携带 tool_calls（OpenAI 格式）
        - role="assistant"（无工具字段）→ {"role": "assistant", "content": ...}
        - role="tool" → {"role": "tool", "tool_call_id": ..., "name": ..., "content": ...}
        - 旧数据兼容：role="assistant" 且 content 含 "\\n[调用工具: ...]" / "\\n[结果: ...]" 标记时，
          按行解析并拆分为若干条 LLM 消息（一条 assistant + tool_call、若干条 tool）。
        """
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # 添加历史消息（按规则转换）
        for h in self.context.history:
            converted = self._convert_history_record(h)
            if converted is None:
                continue
            if isinstance(converted, list):
                messages.extend(converted)
            else:
                messages.append(converted)

        # 添加当前用户消息
        if image_url:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": user_input},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            })
        else:
            messages.append({"role": "user", "content": user_input})

        messages = self._inject_corrections(messages)
        # 调试：记录完整 LLM 请求体
        _debug = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str):
                _debug.append(f"[{role}] {content[:500]}")
            elif isinstance(content, list):
                texts = [c.get("text","")[:200] for c in content if isinstance(c, dict)]
                _debug.append(f"[{role}] multimodal: {' | '.join(texts)[:500]}")
        logger.info(f"[LLM_FULL_REQUEST] agent={self.agent_hash} messages={json.dumps(_debug, ensure_ascii=False)[:3000]}")
        return messages

    def _convert_history_record(self, h: Dict[str, Any]):
        """把 ChatHistory 一行转换为 LLM 输入消息（可能返回 1 条或多条 LLM 消息，或 None 跳过）。

        支持：
        1. 新格式：role + (tool_call_id/tool_name/tool_args) 直接转换
        2. 旧格式：role="assistant" 且 content 含 "[调用工具: ...]" / "[结果: ...]" 标记
        """
        role = h.get("role", "")
        content = h.get("content", "") or ""

        if role == "user":
            return {"role": "user", "content": content}

        if role == "tool":
            return {
                "role": "tool",
                "tool_call_id": h.get("tool_call_id") or "",
                "name": h.get("tool_name") or "",
                "content": content,
            }

        if role == "assistant":
            # 新格式：携带 tool_call 元数据 → 转为 tool_call 消息
            tool_call_id = h.get("tool_call_id")
            tool_name = h.get("tool_name")
            tool_args = h.get("tool_args")
            if tool_call_id or (tool_name and tool_args is not None):
                if isinstance(tool_args, dict):
                    args_obj = tool_args
                else:
                    try:
                        args_obj = json.loads(tool_args) if tool_args else {}
                    except (json.JSONDecodeError, TypeError):
                        args_obj = {}
                msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [{
                        "id": tool_call_id or f"call_{int(time.time()*1000)}",
                        "type": "function",
                        "function": {
                            "name": tool_name or "",
                            "arguments": json.dumps(args_obj, ensure_ascii=False),
                        },
                    }],
                }
                return msg

            # 旧格式：content 中含 [调用工具: ...] / [结果: ...] 标记，解析拆分
            if "[调用工具:" in content or "[结果:" in content:
                return self._parse_legacy_assistant_content(content)

            # 普通 assistant 文本
            return {"role": "assistant", "content": content}

        # 未知 role：跳过
        return None

    def _parse_legacy_assistant_content(self, content: str):
        """解析旧格式的 assistant content（含 [调用工具: ...] / [结果: ...] 标记）

        拆分为若干条 LLM 消息（可能为空数组）。
        """
        import re as _re
        out: List[Dict[str, Any]] = []
        # 标记模式：
        #   [调用工具: name({"k": "v"})]
        #   [结果: <text>]
        _call_pat = _re.compile(r'\[调用工具:\s*([\w_]+)\((.*?)\)\]', _re.DOTALL)
        _result_pat = _re.compile(r'\[结果:\s*(.*?)\]', _re.DOTALL)
        _marker_pat = _re.compile(r'\[调用工具:.*?\]|\[结果:.*?\]', _re.DOTALL)

        # 逐段切分：纯文本 / 工具调用 / 工具结果
        cursor = 0
        current_text_parts: List[str] = []
        for m in _marker_pat.finditer(content):
            # 段间纯文本先 flush
            between = content[cursor:m.start()]
            current_text_parts.append(between)
            text_so_far = "".join(current_text_parts).strip()
            chunk = m.group(0)
            if chunk.startswith("[调用工具:"):
                cm = _call_pat.match(chunk)
                if cm:
                    fn = cm.group(1)
                    args_str = cm.group(2).strip()
                    try:
                        args_obj = json.loads(args_str) if args_str else {}
                    except json.JSONDecodeError:
                        args_obj = {"_raw": args_str}
                    # 累积的纯文本先 flush 为一条 assistant 消息
                    if text_so_far:
                        out.append({"role": "assistant", "content": text_so_far})
                        current_text_parts = []
                    out.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": f"legacy_{m.start()}",
                            "type": "function",
                            "function": {
                                "name": fn,
                                "arguments": json.dumps(args_obj, ensure_ascii=False),
                            },
                        }],
                    })
                cursor = m.end()
            elif chunk.startswith("[结果:"):
                rm = _result_pat.match(chunk)
                if rm:
                    result_text = rm.group(1).strip()
                    # 工具调用前 flush 过的 assistant 文本不再重复；这里直接续上一条 assistant + tool_call
                    # 因为旧格式是 assistant-text + tool_call + tool_result 的顺序；
                    # 如果最后一条是 assistant tool_call 消息，就用它的 tool_call_id 关联
                    tcid = f"legacy_{m.start()}"
                    if (out and out[-1].get("role") == "assistant"
                            and out[-1].get("tool_calls")):
                        tcid = out[-1]["tool_calls"][0]["id"]
                    out.append({
                        "role": "tool",
                        "tool_call_id": tcid,
                        "name": (out[-1]["tool_calls"][0]["function"]["name"]
                                 if (out and out[-1].get("tool_calls")) else ""),
                        "content": result_text,
                    })
                cursor = m.end()
        # 收尾
        tail = content[cursor:]
        if tail.strip():
            current_text_parts.append(tail)
        if current_text_parts:
            tail_text = "".join(current_text_parts).strip()
            if tail_text:
                out.append({"role": "assistant", "content": tail_text})

        if not out:
            # 没有匹配到任何标记，整段当作 assistant 文本
            return {"role": "assistant", "content": content}
        return out
    
    async def _stream_ai_response(
        self,
        messages: List[Dict[str, Any]],
        structured_events: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[ChatEvent, None]:
        """流式调用 AI 并处理工具调用

        Args:
            messages: LLM 输入消息列表
            structured_events: 输出参数，按出现顺序记录本轮 AI 输出的结构化事件
                - {role: "assistant", content: str} — 流式文本片段
                - {role: "assistant", content: "", tool_name, tool_args, tool_call_id} — 工具调用
                - {role: "tool", content: str, tool_call_id, tool_name} — 工具结果
                ChatHistory 持久化会按这些事件逐行入库，避免把工具调用/结果混入 assistant 文本。
        """
        try:
            # 使用 AgentExecutor 进行对话（支持工具调用）
            response_text = ""
            # 本地缓冲：当遇到工具调用/结果时，把已累积的纯文本 flush 成一行 assistant 记录
            _pending_text_buf: List[str] = []
            if structured_events is None:
                structured_events = []

            def _flush_text_buf():
                """把累积的纯文本片段 flush 成一条 assistant 记录"""
                if _pending_text_buf:
                    txt = "".join(_pending_text_buf)
                    _pending_text_buf.clear()
                    if txt:
                        structured_events.append({
                            "role": "assistant",
                            "content": txt,
                        })

            async for step in self.executor.chat_with_tools(messages=messages):
                if step.step_type == "token":
                    # 流式输出文本片段（真正的流式）
                    response_text += step.content
                    _pending_text_buf.append(step.content)
                    yield ChatEvent(
                        type=ChatEventType.TEXT,
                        content=step.content
                    )
                elif step.step_type == "pre_tool":
                    # 工具调用前的思考（仅前端展示，不入库）
                    yield ChatEvent(
                        type=ChatEventType.PRE_TOOL,
                        content=step.content or ""
                    )
                elif step.step_type == "tool_call":
                    # 工具调用：先把已累积的纯文本 flush，再追加 tool_call 记录
                    self._session_memory_tool_calls_since += 1
                    _flush_text_buf()
                    _tc_args = step.tool_args if isinstance(step.tool_args, dict) else {}
                    structured_events.append({
                        "role": "assistant",
                        "content": "",
                        "tool_name": step.tool_name,
                        "tool_args": _tc_args,
                        "tool_call_id": step.tool_call_id or "",
                    })
                    yield ChatEvent(
                        type=ChatEventType.TOOL_CALL,
                        tool_name=step.tool_name,
                        tool_args=step.tool_args,
                        content=step.content or ""
                    )
                elif step.step_type == "tool_result":
                    # 工具结果：追加 role=tool 记录（不混入 response_text 展示给用户）
                    structured_events.append({
                        "role": "tool",
                        "content": step.tool_result or "",
                        "tool_name": step.tool_name,
                        "tool_call_id": step.tool_call_id or "",
                    })
                    yield ChatEvent(
                        type=ChatEventType.TOOL_RESULT,
                        tool_name=step.tool_name,
                        tool_result=step.tool_result,
                        content=step.content or ""
                    )
                elif step.step_type == "final":
                    # 最终响应（可能是工具超限时的提示）
                    if step.content and step.content.strip():
                        response_text += step.content
                        _pending_text_buf.append(step.content)
                        yield ChatEvent(
                            type=ChatEventType.TEXT,
                            content=step.content
                        )
                elif step.step_type == "keepalive":
                    # 工具执行心跳，透传给前端
                    yield ChatEvent(
                        type=ChatEventType.KEEPALIVE,
                        content=step.content or ""
                    )
                elif step.step_type == "pipeline":
                    # 流水线状态更新（SmartRouter/预取等）
                    yield ChatEvent(
                        type=ChatEventType.PIPELINE,
                        content=step.content,
                        metadata=step.metadata
                    )
                elif step.step_type == "search_progress":
                    # 搜索结果的流式内容
                    yield ChatEvent(
                        type=ChatEventType.SEARCH_PROGRESS,
                        content=step.content,
                        metadata=step.metadata
                    )
                elif step.step_type == "reasoning":
                    # 深度思考推理过程
                    yield ChatEvent(
                        type=ChatEventType.REASONING,
                        content=step.content
                    )

            # 收尾：把最后未 flush 的纯文本追加为一行 assistant 记录
            _flush_text_buf()

            # 注意：DONE 事件由 chat() 方法统一发送，避免重复

        except Exception as e:
            logger.error(f"[ChatService] AI response error: {e}", exc_info=True)
            yield ChatEvent(
                type=ChatEventType.ERROR,
                error_message=str(e)
            )
    
    def _save_conversation(self, user_input: str, response: str,
                           structured_events: Optional[List[Dict[str, Any]]] = None,
                           meta: Optional[Dict] = None,
                           attachments: Optional[List] = None):
        """保存对话记录到内存和数据库（V2：群聊模式双写到 GroupMessage + ChatHistory）

        Args:
            user_input: 用户输入文本
            response: 拼装后的 assistant 纯文本（用于群聊展示 & 兼容老代码）
            structured_events: AI 本轮的结构化事件列表，每条对应一行 ChatHistory：
                - {"role": "assistant", "content": "..."} — 纯文本片段
                - {"role": "assistant", "content": "", "tool_name", "tool_args", "tool_call_id"} — 工具调用
                - {"role": "tool", "content": "...", "tool_call_id", "tool_name"} — 工具结果
                若为空，回退到旧逻辑（单条 assistant + 混入工具标记的文本）。
        """

        # 保存到内存：把 user 消息和 structured_events 全部按顺序追加
        self.context.history.append({"role": "user", "content": user_input})
        if structured_events:
            for ev in structured_events:
                self.context.history.append(dict(ev))
        else:
            # 兼容旧调用：回退到单条 assistant + response 文本
            self.context.history.append({"role": "assistant", "content": response})

        # 限制历史长度
        if len(self.context.history) > 200 * 2:
            self.context.history = self.context.history[-200 * 2:]

        # 自动触发消息压缩（token估算超过阈值时）
        total_est = sum(
            estimate_tokens(msg.get("content", ""))
            for msg in self.context.history
        )

        if total_est > settings.COMPACTION_MAX_TOKENS:
            from services.message_compactor import MessageCompactor
            compactor = MessageCompactor(max_tokens=settings.COMPACTION_MAX_TOKENS)
            self.context.history = compactor.l2_shear(self.context.history)
            self.context.history = compactor.l3_micro_compact(self.context.history)
            total_est = sum(
                estimate_tokens(msg.get("content", ""))
                for msg in self.context.history
            )
            if total_est > settings.COMPACTION_MAX_TOKENS:
                self.context.history = compactor.l4_context_crash(self.context.history)

        # wechat_msg_id 从 meta JSON 派生
        _wx_msg_id = None
        if meta and "wechat_metadata" in meta:
            _wx_msg_id = meta["wechat_metadata"].get("msg_id")

        # 序列化 attachments
        _attachments_json = None
        if attachments:
            _attachments_json = [a.dict() if hasattr(a, 'dict') else a for a in attachments]

        # V2 群聊模式：双写 GroupMessage + ChatHistory
        if self.is_group_mode:
            self._save_conversation_group_mode(
                user_input, response, structured_events,
                meta, attachments, _attachments_json
            )
            return

        # 单聊模式：保存到数据库（ChatHistory 表）
        try:
            with get_session() as db:
                user_msg = ChatHistory(
                    user_id=int(self.user_id),
                    agent_hash=self.agent_hash,
                    role="user",
                    content=user_input,
                    channel=self.channel,
                    session_id=self.session_id,
                    meta=meta,
                    attachments=_attachments_json,
                    wechat_msg_id=_wx_msg_id,
                )
                db.add(user_msg)

                if structured_events:
                    # 多行写入：每条结构化事件对应一行 ChatHistory
                    for ev in structured_events:
                        _role = ev.get("role", "assistant")
                        db.add(ChatHistory(
                            user_id=int(self.user_id),
                            agent_hash=self.agent_hash,
                            role=_role,
                            content=ev.get("content", "") or "",
                            tool_call_id=ev.get("tool_call_id") or None,
                            tool_name=ev.get("tool_name") or None,
                            tool_args=ev.get("tool_args") or None,
                            channel=self.channel,
                            session_id=self.session_id,
                            meta=meta,
                        ))
                else:
                    # 兼容旧调用：单条 assistant 行（含工具标记的文本）
                    db.add(ChatHistory(
                        user_id=int(self.user_id),
                        agent_hash=self.agent_hash,
                        role="assistant",
                        content=response,
                        channel=self.channel,
                        session_id=self.session_id,
                        meta=meta,
                    ))

                db.commit()
                logger.debug(
                    f"[ChatService] Saved conversation: user_id={self.user_id}, "
                    f"agent_hash={self.agent_hash}, "
                    f"structured_events={len(structured_events) if structured_events else 0}"
                )
        except Exception as e:
            logger.warning(f"[ChatService] Failed to save conversation to database: {e}")

    def _save_conversation_group_mode(
        self,
        user_input: str,
        response: str,
        structured_events: Optional[List[Dict[str, Any]]] = None,
        meta: Optional[Dict] = None,
        attachments: Optional[List] = None,
        _attachments_json: Optional[List] = None,
    ):
        """V2 群聊模式双写：GroupMessage（群可见）+ ChatHistory（个人历史）

        注意：
        - 工具调用结果只写 ChatHistory（个人历史），不写 GroupMessage
        - Agent 的回复写 GroupMessage 让所有群成员可见
        """
        import uuid as _uuid
        try:
            from models.group import GroupMessage
            with get_session() as db:
                # 1. Agent 回复写入 GroupMessage（群可见）
                # 注意：user_input 是触发本轮的最新消息——GroupMessage 已由 GroupDispatch 写入，
                # 这里只追加 Agent 的回复。
                if response:
                    reply_mentions = []
                    try:
                        import re as _re
                        at_names = _re.findall(r'@(\S+)', response)
                        if at_names:
                            # 查群成员名→hash 映射
                            from models.group import GroupMember as _GM
                            from models.database import AgentProfile as _AP
                            members = db.query(_GM).filter(
                                _GM.group_id == self.group_id,
                                _GM.agent_hash != '',
                            ).all()
                            agent_hashes = [m.agent_hash for m in members]
                            agent_rows = db.query(_AP.hash, _AP.name).filter(
                                _AP.hash.in_(agent_hashes)
                            ).all() if agent_hashes else []
                            name_to_hash = {n: h for h, n in agent_rows if n}
                            for n in at_names:
                                h = name_to_hash.get(n)
                                if h and h not in reply_mentions and h != self.agent_hash:
                                    reply_mentions.append(h)
                    except Exception:
                        pass

                    agent_msg = GroupMessage(
                        id=str(_uuid.uuid4()),
                        group_id=self.group_id,
                        sender_type="agent",
                        sender_hash=self.agent_hash,
                        content=response,
                        message_type="text",
                        attachments=None,
                        mentions=reply_mentions,
                        round=0,
                        created_at=datetime.utcnow(),
                    )
                    db.add(agent_msg)

                # 2. 工具调用结果/上下文写入 ChatHistory（个人历史）
                # user 消息（触发本轮的输入）
                user_msg = ChatHistory(
                    user_id=int(self.user_id),
                    agent_hash=self.agent_hash,
                    role="user",
                    content=user_input,
                    channel=self.channel,
                    session_id=self.session_id,
                    meta={**(meta or {}), "group_id": self.group_id},
                    attachments=_attachments_json,
                )
                db.add(user_msg)

                # 3. assistant / tool 消息：按结构化事件逐行写入
                if structured_events:
                    for ev in structured_events:
                        _role = ev.get("role", "assistant")
                        db.add(ChatHistory(
                            user_id=int(self.user_id),
                            agent_hash=self.agent_hash,
                            role=_role,
                            content=ev.get("content", "") or "",
                            tool_call_id=ev.get("tool_call_id") or None,
                            tool_name=ev.get("tool_name") or None,
                            tool_args=ev.get("tool_args") or None,
                            channel=self.channel,
                            session_id=self.session_id,
                            meta={**(meta or {}), "group_id": self.group_id},
                        ))
                else:
                    # 兼容旧调用：单条 assistant 行
                    db.add(ChatHistory(
                        user_id=int(self.user_id),
                        agent_hash=self.agent_hash,
                        role="assistant",
                        content=response,
                        channel=self.channel,
                        session_id=self.session_id,
                        meta={**(meta or {}), "group_id": self.group_id},
                    ))

                db.commit()
                logger.debug(
                    f"[ChatService] Group-mode dual-write: group={self.group_id} "
                    f"agent={self.agent_hash} response_len={len(response)} "
                    f"structured_events={len(structured_events) if structured_events else 0}"
                )

                # 4. WS push 通知（fire-and-forget）
                if response:
                    try:
                        import asyncio as _aio
                        from routers.desktop_ws import manager as _ws_manager
                        push_payload = {
                            "type": "group_message",
                            "group_id": self.group_id,
                            "sender_type": "agent",
                            "sender_hash": self.agent_hash,
                            "content": response,
                            "timestamp": int(__import__('time').time()),
                        }
                        # 在事件循环中调度
                        try:
                            loop = _aio.get_event_loop()
                            loop.create_task(_ws_manager.send(push_payload))
                        except Exception:
                            pass
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[ChatService] Group-mode save failed: {e}", exc_info=True)

    async def _maybe_extract_session_memory(self):
        """后置钩子：会话记忆提取（非阻塞，后台执行）"""
        async with self._session_memory_lock:
            try:
                from services.session_memory_service import SessionMemoryService

                svc = SessionMemoryService(agent_hash=self.agent_hash)

                # 检查是否已初始化
                if not self._session_memory_initialized:
                    self._session_memory_initialized = svc.is_memory_initialized()

                # 阈值判断
                decision = svc.should_extract(
                    messages=self.context.history,
                    is_initialized=self._session_memory_initialized,
                    last_extract_msg_index=self._session_memory_last_extract_idx,
                    tool_calls_since=self._session_memory_tool_calls_since,
                )

                if not decision["should_extract"]:
                    logger.debug(
                        "[ChatService] Session memory extraction skipped: %s",
                        decision["reason"]
                    )
                    return

                logger.info(
                    "[ChatService] Triggering session memory extraction: %s",
                    decision["reason"]
                )

                # 执行提取
                success = await svc.extract(self.context.history)

                if success:
                    self._session_memory_initialized = True
                    self._session_memory_last_extract_idx = len(self.context.history)
                    self._session_memory_tool_calls_since = 0
                    # 触发蒸馏检查
                    await svc.maybe_distill_to_longterm(self.context.history)
                    # 触发 Reflection 事实核查
                    asyncio.create_task(self._run_reflection(self.context.history))
                    logger.info("[ChatService] Session memory extraction completed successfully")
                else:
                    self._session_memory_tool_calls_since = 0
                    logger.warning("[ChatService] Session memory extraction failed")

            except Exception as e:
                self._session_memory_tool_calls_since = 0
                logger.error("[ChatService] Session memory extraction error: %s", e, exc_info=True)

    async def _run_reflection(self, messages):
        """在 Session Memory 提取成功后，异步触发 Reflection 事实核查"""
        try:
            from services.reflection_service import ReflectionService

            previous_correction = (
                self._pending_correction["short_desc"]
                if self._pending_correction else None
            )
            result = await ReflectionService.check_session_memory(messages, previous_correction)

            current_count = len(messages)

            # 覆盖策略：只有新结果消息更多才覆盖旧的 pending
            if self._pending_correction and current_count <= self._pending_correction.get("msg_count", 0):
                logger.debug(
                    "[ChatService] Reflection result discarded: "
                    "current msg_count=%d <= pending msg_count=%d",
                    current_count, self._pending_correction["msg_count"]
                )
                return

            logger.info(
                "[ChatService] Reflection result: %s",
                json.dumps({k: v for k, v in result.items() if k != "detail"}, ensure_ascii=False)
            )

            if result.get("has_errors"):
                self._pending_correction = {
                    "short_desc": result["short_desc"],
                    "topic": result["topic"],
                    "msg_count": current_count,
                    "consumed": False,
                }
                logger.info(
                    "[ChatService] Reflection found error: topic=%s, short_desc=%s",
                    result.get("topic"), result.get("short_desc")
                )
            else:
                if self._pending_correction:
                    logger.info(
                        "[ChatService] Reflection cleared previous pending_correction (topic=%s)",
                        self._pending_correction.get("topic")
                    )
                self._pending_correction = None

        except Exception as e:
            logger.error("[ChatService] Reflection error: %s", e, exc_info=True)

    async def _maybe_extract_user_profile(self):
        """后置钩子：用户画像提取（非阻塞，后台执行）"""
        async with self._user_profile_lock:
            try:
                from services.user_profile_service import UserProfileService

                svc = UserProfileService(agent_hash=self.agent_hash)

                # 检查是否已初始化
                if not self._user_profile_initialized:
                    self._user_profile_initialized = svc.is_profile_initialized()

                # 阈值判断
                decision = UserProfileService.should_extract(
                    messages=self.context.history,
                    is_initialized=self._user_profile_initialized,
                    last_extract_msg_index=self._user_profile_last_extract_idx,
                    last_extract_time=self._user_profile_last_extract_time,
                    tool_calls_since=self._user_profile_tool_calls_since,
                )

                if not decision["should_extract"]:
                    reason = decision.get("reason", "")
                    if "消息不足" in reason:
                        logger.debug(
                            "[ChatService] User profile extraction skipped: %s",
                            reason
                        )
                    else:
                        logger.info(
                            "[ChatService] User profile extraction skipped: %s",
                            reason
                        )
                    return

                logger.info(
                    "[ChatService] Triggering user profile extraction: %s",
                    decision["reason"]
                )

                # 执行提取
                success = await svc.extract(self.context.history)

                if success:
                    self._user_profile_initialized = True
                    self._user_profile_last_extract_idx = len(self.context.history)
                    self._user_profile_last_extract_time = time.time()
                    self._user_profile_tool_calls_since = 0
                    logger.info("[ChatService] User profile extraction completed successfully")
                else:
                    self._user_profile_tool_calls_since = 0
                    logger.info("[ChatService] User profile extraction finished (no changes or LLM decided no update needed)")

            except Exception as e:
                self._user_profile_tool_calls_since = 0
                logger.error("[ChatService] User profile extraction error: %s", e, exc_info=True)
