"""
Group Dispatch Service - Phase 4 Engine
Group chat multi-agent dispatch and LLM reply orchestration
"""

import asyncio
import inspect
import json
import logging
import time
import uuid as _uuid
from datetime import datetime
from typing import Optional, List, Dict, Any, Set, Tuple

from sqlalchemy.orm import Session

from models.database import SessionLocal, AgentProfile
from models.group import Group, GroupMember, GroupMessage
from config import settings
from services.message_compactor import estimate_tokens

logger = logging.getLogger(__name__)

NO_REPLY_MAGIC = "NO_REPLY"     # 本轮不回，下轮可继续唤醒
SILENT_MAGIC = "SILENT"         # 保持沉默，除非被 @ 点名才唤醒

# 群聊工具调用相关常量
GROUP_TOOL_ROUNDS_MAX = 3          # 单轮 reply 中最多 3 轮工具调用
GROUP_TOOL_TOTAL_TIMEOUT = 30.0    # 单轮 reply 工具调用整体超时（秒）
GROUP_TOOL_PER_TIMEOUT = 25.0      # 单个工具执行超时（秒）
GROUP_SESSION_MEMORY_MAX = 20      # 每个 (group, agent) 最多保留的工具调用轮数
GROUP_SESSION_TTL = 3600           # 1 小时无活动清理 session

def _calc_wake_threshold(member_count: int) -> int:
    """silent agent 自动唤醒阈值：min(成员数 × 2, 15)"""
    return min(max(member_count, 1) * 2, 15)

# 群聊默认允许的工具（读类、信息获取类），排除写入/破坏性工具
GROUP_ALLOWED_TOOLS: Set[str] = {
    # 信息获取
    "web_search", "web_fetch",
    # 文件读取
    "file_read", "file_list",
    # 知识库
    "knowledge_search", "knowledge_get",
    # 文本处理（无副作用）
    "text_summarize", "text_translate", "generate_summary",
    # 会话检索（只读）
    "search_sessions", "list_conversations", "load_conversation",
    "auto_suggest_session",
    # 分享引用解析
    "resolve_share_reference",
    # SubAgent 日志读取
    "read_subagent_log",
    # VCS 只读
    "fe_vcs_diff", "fe_vcs_log",
    # 权限查询
    "file_permission_list", "file_permission_ask",
    # 路由查询
    "route_list",
    # 文件解析
    "parse_file",
    # TOTP 生成（只读计算）
    "generate_totp",
}


class GroupDispatchService:
    MAX_ROUNDS = 100
    CONTEXT_COMPACTION_THRESHOLD = 0.15  # keep 15% when compacting

    def __init__(self):
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._round_active: Dict[str, int] = {}  # group_id → 当前活跃轮次
        # session memory: (group_id, agent_hash) → {created_at, items: [...]}
        # items 为 list of {"role", "content", "tool_calls"|"tool_call_id"|"name"}
        self._session_memory: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # ========== Entry Point ==========

    async def on_message(
        self,
        group_id: str,
        sender_type: str,
        sender_hash: str,
        content: str,
        mentions: Optional[List[str]] = None,
        attachments: Optional[List[Dict]] = None,
        message_type: str = "text",
    ) -> str:
        """
        Handle an incoming group message from any channel.

        Returns the message ID of the saved message.
        """
        # 1. Save GroupMessage to DB
        db = SessionLocal()
        try:
            msg_id = str(_uuid.uuid4())
            msg = GroupMessage(
                id=msg_id,
                group_id=group_id,
                sender_type=sender_type,
                sender_hash=sender_hash,
                content=content,
                message_type=message_type,
                attachments=attachments,
                mentions=mentions or [],
                round=0,
                created_at=datetime.utcnow(),
            )
            db.add(msg)
            db.commit()

            logger.info(
                f"[GroupDispatch] group={group_id} sender={sender_type}:{sender_hash} "
                f"content_len={len(content)}, mentions={mentions}"
            )

            # 2. Dispatch to members (fire-and-forget)
            asyncio.create_task(
                self.dispatch_to_members(group_id, round=0, exclude=sender_hash)
            )

            return msg_id
        finally:
            db.close()

    # ========== Dispatch ==========

    async def dispatch_to_members(
        self,
        group_id: str,
        round: int = 0,
        exclude: Optional[str] = None,
    ):
        """Dispatch agent replies to all members except sender."""
        # Guard: round limit
        if round >= self.MAX_ROUNDS:
            logger.info(f"[GroupDispatch] group={group_id} reached MAX_ROUNDS={self.MAX_ROUNDS}, stopping")
            return

        # Guard: 防止指数级级联——同一 group 同一轮只调度一次
        prev_round = self._round_active.get(group_id, -1)
        if round <= prev_round:
            logger.info(f"[GroupDispatch] group={group_id} round={round} already active (prev={prev_round}), skipping")
            return
        self._round_active[group_id] = round

        db = SessionLocal()
        try:
            members = (
                db.query(GroupMember)
                .filter(GroupMember.group_id == group_id)
                .all()
            )

            if not members:
                return

            # 加载最新消息的 mentions（用于 silent agent @唤醒）
            # mentions 中存的是 agent name（如"Engineer-张昊"），需要转成 hash
            latest_mentions: List[str] = []
            try:
                latest_msg = (
                    db.query(GroupMessage)
                    .filter(GroupMessage.group_id == group_id)
                    .order_by(GroupMessage.created_at.desc())
                    .first()
                )
                if latest_msg and latest_msg.mentions:
                    # 把 name → hash 转换，同时保留原始 name
                    name_to_hash = {}
                    for m in members:
                        if m.agent_hash:
                            name_to_hash[m.agent_hash] = m.agent_hash  # hash→hash
                    # 从 DB 查 agent name→hash 映射
                    agent_rows = db.query(AgentProfile.hash, AgentProfile.name).filter(
                        AgentProfile.hash.in_([m.agent_hash for m in members if m.agent_hash])
                    ).all()
                    for h, n in agent_rows:
                        if n:
                            name_to_hash[n] = h
                    for mention in latest_msg.mentions:
                        h = name_to_hash.get(mention, mention)
                        if h:
                            latest_mentions.append(h)
            except Exception:
                pass

            for member in members:
                if not member.agent_hash:       # 跳过群主（agent_hash 为空）
                    continue
                if member.agent_hash == exclude:
                    continue

                if self.should_wake(member, group_id, round, mentions=latest_mentions):
                    task_key = f"{group_id}:{member.agent_hash}:{round}"
                    # Cancel any existing task for this slot
                    if task_key in self._running_tasks:
                        self._running_tasks[task_key].cancel()
                    self._running_tasks[task_key] = asyncio.create_task(
                        self.agent_reply(member.agent_hash, group_id, round)
                    )
                elif member.is_silent:
                    # silent 自动唤醒：如果 silent 后已有足够新消息则清除标志
                    try:
                        last_msg = db.query(GroupMessage).filter(
                            GroupMessage.group_id == group_id,
                            GroupMessage.sender_type == "agent",
                            GroupMessage.sender_hash == member.agent_hash,
                        ).order_by(GroupMessage.created_at.desc()).first()
                        if last_msg:
                            new_since = db.query(GroupMessage).filter(
                                GroupMessage.group_id == group_id,
                                GroupMessage.created_at > last_msg.created_at
                            ).count()
                            threshold = _calc_wake_threshold(len(members))
                            if new_since >= threshold:
                                member.is_silent = False
                                db.commit()
                                logger.info(
                                    f"[GroupDispatch] Auto-wake agent={member.agent_hash} "
                                    f"after {new_since} new msgs (threshold={threshold})"
                                )
                    except Exception:
                        pass
        finally:
            db.close()

    def should_wake(
        self,
        member: GroupMember,
        group_id: str,
        round: int,
        mentions: Optional[List[str]] = None,
    ) -> bool:
        """
        Decide whether to wake an agent for this round.

        round==0       → always wake (fresh user message triggers all)
        member.is_silent → wake only if agent_hash is in @mentions
        otherwise       → wake
        """
        if round == 0:
            return True

        if member.is_silent:
            # Silent agents can be woken by @mention
            if mentions and member.agent_hash in mentions:
                return True
            return False

        return True

    # ========== Agent Reply ==========

    async def agent_reply(self, agent_hash: str, group_id: str, round: int):
        """Generate and save an agent reply for one member."""
        task_key = f"{group_id}:{agent_hash}:{round}"
        try:
            logger.info(f"[GroupDispatch] agent_reply agent={agent_hash} group={group_id} round={round}")

            db = SessionLocal()
            try:
                # Build context
                context_messages, persona = self.build_context(agent_hash, group_id)

                if not context_messages:
                    logger.warning(f"[GroupDispatch] No context for agent={agent_hash}, skipping")
                    return

                # 先尝试走 chat_with_tools（带 session memory）；失败/超时则降级到纯文本
                response: Optional[str] = None
                used_tools = False
                try:
                    response, used_tools = await self._call_llm_with_tools(
                        agent_hash, group_id, context_messages, persona
                    )
                except Exception as e:
                    logger.warning(
                        f"[GroupDispatch] tool path failed for agent={agent_hash}, "
                        f"falling back to plain text: {e}"
                    )
                    response = None

                if response is None:
                    # Fallback: 原纯文本路径
                    prompt = self._build_group_prompt(agent_hash, group_id, context_messages, persona)
                    response = await self._call_llm(prompt, agent_hash)

                if not response:
                    logger.info(f"[GroupDispatch] agent={agent_hash} got empty response, skipping")
                    asyncio.create_task(
                        self.dispatch_to_members(group_id, round=round + 1, exclude=agent_hash)
                    )
                    return

                # 记录使用了工具（用于后续扩展如统计）
                if used_tools:
                    logger.info(f"[GroupDispatch] agent={agent_hash} replied with tool assistance")

                # Check NO_REPLY / SILENT signal
                if response.strip() == SILENT_MAGIC:
                    # SILENT: 保持沉默，本轮不回复，标记 silent，但继续调度下一轮
                    member = (
                        db.query(GroupMember)
                        .filter(GroupMember.group_id == group_id, GroupMember.agent_hash == agent_hash)
                        .first()
                    )
                    if member:
                        member.is_silent = True
                        db.commit()
                    logger.info(f"[GroupDispatch] agent={agent_hash} returned SILENT, marked silent")
                    asyncio.create_task(
                        self.dispatch_to_members(group_id, round=round + 1, exclude=agent_hash)
                    )
                    return

                if response.strip() == NO_REPLY_MAGIC:
                    # NO_REPLY: 本轮不回复，但继续调度下一轮（防止链条断裂）
                    logger.info(f"[GroupDispatch] agent={agent_hash} returned NO_REPLY, skipping round")
                    asyncio.create_task(
                        self.dispatch_to_members(group_id, round=round + 1, exclude=agent_hash)
                    )
                    return

                # 从回复中提取 @{name} 自动转为 mentions
                reply_mentions: List[str] = []
                try:
                    import re as _re
                    at_names = _re.findall(r'@(\S+)', response)
                    if at_names:
                        # 查群成员名→hash 映射
                        name_to_hash = {}
                        member_list = db.query(GroupMember).filter(
                            GroupMember.group_id == group_id,
                            GroupMember.agent_hash != '',
                        ).all()
                        agent_hashes = [m.agent_hash for m in member_list]
                        agent_rows = db.query(AgentProfile.hash, AgentProfile.name).filter(
                            AgentProfile.hash.in_(agent_hashes)
                        ).all()
                        for h, n in agent_rows:
                            if n:
                                name_to_hash[n] = h
                        for n in at_names:
                            h = name_to_hash.get(n)
                            if h and h not in reply_mentions and h != agent_hash:
                                reply_mentions.append(h)
                except Exception:
                    pass

                # Save reply as GroupMessage
                msg_id = str(_uuid.uuid4())
                reply_msg = GroupMessage(
                    id=msg_id,
                    group_id=group_id,
                    sender_type="agent",
                    sender_hash=agent_hash,
                    content=response,
                    message_type="text",
                    attachments=None,
                    mentions=reply_mentions,
                    round=round,
                    created_at=datetime.utcnow(),
                )
                db.add(reply_msg)
                db.commit()

                logger.info(f"[GroupDispatch] agent={agent_hash} replied in group={group_id}, msg_id={msg_id}")

                # WS push to clients (fire-and-forget)
                asyncio.create_task(self._push_to_clients(group_id, msg_id, agent_hash, response))

                # Continue dispatch chain
                asyncio.create_task(
                    self.dispatch_to_members(group_id, round=round + 1, exclude=agent_hash)
                )
            finally:
                db.close()

        except asyncio.CancelledError:
            logger.info(f"[GroupDispatch] Task {task_key} cancelled")
            raise
        except Exception as e:
            logger.error(f"[GroupDispatch] agent_reply error: agent={agent_hash} group={group_id} {e}", exc_info=True)
        finally:
            self._running_tasks.pop(task_key, None)

    # ========== Context Building ==========

    def build_context(self, agent_hash: str, group_id: str) -> tuple:
        """
        Build chronological group history + agent personality for LLM prompt.

        Returns (messages_list, persona_str).
        """
        db = SessionLocal()
        try:
            # Load recent messages for this group (chronological)
            messages = (
                db.query(GroupMessage)
                .filter(GroupMessage.group_id == group_id)
                .order_by(GroupMessage.created_at.asc())
                .limit(200)
                .all()
            )

            if not messages:
                return [], ""

            # Build message list
            context_messages = []
            for msg in messages:
                role = "user" if msg.sender_type in ("user", "human") else "assistant"
                context_messages.append({
                    "role": role,
                    "content": msg.content or "",
                    "sender_hash": msg.sender_hash,
                })

            # Compact if > model window
            context_messages = self._compact_context(context_messages)

            # Load agent personality
            persona = ""
            try:
                from services.agent_init_service import agent_init_service
                persona = agent_init_service.load_agent_persona(agent_hash) or ""
            except Exception as e:
                logger.warning(f"[GroupDispatch] Failed to load persona for {agent_hash}: {e}")

            return context_messages, persona
        finally:
            db.close()

    def _compact_context(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Compact context to ~15% of recent messages when too large."""
        total_tokens = sum(estimate_tokens(m.get("content", "")) for m in messages)
        max_tokens = 110000 * self.CONTEXT_COMPACTION_THRESHOLD

        if total_tokens <= max_tokens:
            return messages

        # Keep last 15%
        keep_count = max(5, int(len(messages) * 0.15))
        compacted = messages[-keep_count:]
        logger.info(
            f"[GroupDispatch] Compacting context: {len(messages)} msgs → {len(compacted)} msgs"
        )
        return compacted

    def _build_group_prompt(
        self,
        agent_hash: str,
        group_id: str,
        context_messages: List[Dict[str, Any]],
        persona: str,
    ) -> str:
        """Build the system prompt for group chat."""
        import zoneinfo as _zi
        from datetime import datetime as _dt
        _tz = _zi.ZoneInfo("Asia/Shanghai")
        _now = _dt.now(_tz)

        lines = [
            f"【当前时间（BJT）】 {_now.year}.{_now.month}.{_now.day} {_now.hour:02d}:{_now.minute:02d}",
            "",
            "【群组聊天模式】",
            "你正在一个群组中与多个 AI Agent 和用户对话。",
            "请以你的角色身份，根据对话历史给出回复。",
            "如果某条消息明显是在单独对你说话，请重点回应。",
            "如果消息是群组闲聊，可以选择性回应或保持沉默。",
            "",
        ]

        if persona:
            lines.append("【你的人格设定】")
            lines.append(persona)
            lines.append("")

        lines.append("【群组消息历史】")
        for msg in context_messages:
            sender = msg.get("sender_hash", "?")
            role_label = "用户" if msg["role"] == "user" else "Agent"
            lines.append(f"[{role_label} {sender}]: {msg['content']}")

        lines.append("")
        lines.append("【@提及机制】")
        lines.append("在回复中写 @{对方名字}（如 @Designer-林一）可唤醒被@的Agent（即使他处于静默状态）。")
        lines.append("需要在下一轮继续推进某个话题，或者需要某位成员回应你的观点时，请使用 @提及。")
        lines.append("")

        lines.append("【回复规则】")
        lines.append("1. 如果你不需要回复（消息不针对你或无实质内容），请回复：NO_REPLY")
        lines.append("2. 对于@类消息，如果你未被@且消息也与你的专业领域关系不大")
        lines.append("   （包括需要别人先完成前置工作才轮到你），建议回复NO_REPLY")
        lines.append("3. 可以适当发散，但不要过度偏离用户给出的任务和讨论主线")
        lines.append("   如果讨论进入死胡同（技术细节钻牛角尖、反复纠结同一问题等），")
        lines.append("   请主动将注意力拉回核心目标")
        lines.append("4. 回复NO_REPLY仅跳过本轮，下轮仍可被唤醒")
        lines.append("5. 回复SILENT则标记为静默，后续不再唤醒——除非被 @ 点名")
        lines.append("   静默后错过若干条新消息将自动解除沉默，你也可以重新回复SILENT续期")
        lines.append("6. 否则，请以你的角色身份自然回复，不要声明你的思考过程")

        return "\n".join(lines)

    # ========== LLM Call ==========

    async def _call_llm(self, prompt: str, agent_hash: str) -> str:
        """Call the LLM for a group chat response (plain text fallback)."""
        try:
            from services.llm_service import LLMService
            from services.model_registry import resolve as _resolve

            cfg = _resolve(settings.MAIN_TEXT_MODEL)
            llm = LLMService()

            full_response = ""
            async for chunk in llm.chat(
                messages=[{"role": "user", "content": prompt}],
                provider=cfg["provider"],
                model=settings.MAIN_TEXT_MODEL,
            ):
                full_response += chunk

            return full_response.strip()
        except Exception as e:
            logger.error(f"[GroupDispatch] LLM call failed for agent={agent_hash}: {e}", exc_info=True)
            return ""

    # ========== Tool-Capable LLM Call ==========

    def _get_group_tool_schemas(self) -> List[Dict[str, Any]]:
        """获取群聊可用的工具 schema 列表（仅 GROUP_ALLOWED_TOOLS）"""
        from services.tool_registry import get_tool_schemas
        all_schemas = get_tool_schemas()
        return [
            s for s in all_schemas
            if s.get("function", {}).get("name") in GROUP_ALLOWED_TOOLS
        ]

    async def _call_llm_with_tools(
        self,
        agent_hash: str,
        group_id: str,
        context_messages: List[Dict[str, Any]],
        persona: str,
    ) -> Tuple[Optional[str], bool]:
        """
        带工具能力的群聊 LLM 调用。

        流程：
        1. 构造 messages: [system(prompt + persona + session summary), ...context, ...prior tool calls/results]
        2. 循环调用 chat_with_tools，最多 GROUP_TOOL_ROUNDS_MAX 轮，整体超时 GROUP_TOOL_TOTAL_TIMEOUT
        3. 执行工具并把结果回填到 messages
        4. 更新 session memory
        5. 返回最终纯文本 (response, used_tools)；失败返回 (None, False) 让上层 fallback

        Returns:
            (response_text or None, used_tools_bool)
        """
        from services.llm_service import LLMService
        from services.model_registry import resolve as _resolve

        tool_schemas = self._get_group_tool_schemas()
        if not tool_schemas:
            # 没有任何可用工具，直接返回 None 让上层 fallback
            return None, False

        cfg = _resolve(settings.MAIN_TEXT_MODEL)
        llm = LLMService()

        # 1. 构造 system prompt（含 persona + session 摘要）
        system_prompt = self._build_system_prompt_with_memory(
            agent_hash, group_id, context_messages, persona
        )

        # 2. 构造 messages 列表
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for m in context_messages:
            messages.append({
                "role": m["role"],
                "content": m.get("content", "") or "",
            })

        # 3. 注入 session memory 中先前的工具调用/结果（保持对话连贯）
        prior_items = self._get_session_items(group_id, agent_hash)
        for item in prior_items:
            messages.append(item)

        # 4. 循环调用 chat_with_tools
        used_tools = False
        loop_start = time.time()
        try:
            for round_idx in range(GROUP_TOOL_ROUNDS_MAX):
                # 整体超时检查
                if time.time() - loop_start > GROUP_TOOL_TOTAL_TIMEOUT:
                    logger.warning(
                        f"[GroupDispatch] tool loop total timeout ({GROUP_TOOL_TOTAL_TIMEOUT}s) "
                        f"agent={agent_hash} group={group_id}"
                    )
                    break

                result = await llm.chat_with_tools(
                    messages=messages,
                    provider=cfg["provider"],
                    model=settings.MAIN_TEXT_MODEL,
                    tools=tool_schemas,
                    request_type="group_chat_tool",
                )

                content = (result.get("content") or "").strip()
                tool_calls = result.get("tool_calls")

                if not tool_calls:
                    # 没有工具调用 → 这就是最终回复
                    return (content if content else None), used_tools

                # 有工具调用 → 记录 assistant 消息 + 执行工具
                used_tools = True
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_msg)

                # 把本轮的 assistant 调用追加到 session memory
                self._append_session_item(group_id, agent_hash, assistant_msg)

                # 执行每个工具
                any_failed = False
                for tc in tool_calls:
                    func_name = (tc.get("function") or {}).get("name", "")
                    args_str = (tc.get("function") or {}).get("arguments", "{}")
                    tool_call_id = tc.get("id", "")

                    try:
                        args = json.loads(args_str) if args_str else {}
                    except json.JSONDecodeError:
                        args = {}

                    # 工具级超时（绝对值上限 = 剩余时间）
                    remaining = GROUP_TOOL_TOTAL_TIMEOUT - (time.time() - loop_start)
                    per_timeout = max(1.0, min(GROUP_TOOL_PER_TIMEOUT, remaining))

                    tool_result = await self._execute_group_tool(
                        agent_hash, group_id, func_name, args, timeout=per_timeout
                    )

                    if tool_result.startswith("Error:"):
                        any_failed = True

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": func_name,
                        "content": tool_result,
                    }
                    messages.append(tool_msg)
                    self._append_session_item(group_id, agent_hash, tool_msg)

                # 如果所有工具都失败，下次循环模型可能继续生成纯文本
                if any_failed:
                    logger.debug(
                        f"[GroupDispatch] some tools failed for agent={agent_hash}, "
                        f"continuing loop to let LLM respond"
                    )

            # 超过最大轮次 → 用最后一轮的 content 作为回复
            # 再发一次纯工具调用（不传 tools）让 LLM 基于历史汇总
            try:
                if time.time() - loop_start < GROUP_TOOL_TOTAL_TIMEOUT:
                    fallback = await llm.chat(
                        messages=messages,
                        provider=cfg["provider"],
                        model=settings.MAIN_TEXT_MODEL,
                    )
                    full = ""
                    async for chunk in fallback:
                        full += chunk
                    text = full.strip()
                    return (text if text else None), used_tools
            except Exception:
                pass
            return None, used_tools

        except Exception as e:
            logger.error(
                f"[GroupDispatch] tool-capable LLM call failed: agent={agent_hash} "
                f"group={group_id}: {e}", exc_info=True
            )
            return None, used_tools

    def _build_system_prompt_with_memory(
        self,
        agent_hash: str,
        group_id: str,
        context_messages: List[Dict[str, Any]],
        persona: str,
    ) -> str:
        """构造含 persona + session memory 摘要的 system prompt。"""
        import zoneinfo as _zi
        from datetime import datetime as _dt
        _tz = _zi.ZoneInfo("Asia/Shanghai")
        _now = _dt.now(_tz)

        lines: List[str] = [
            f"【当前时间（BJT）】 {_now.year}.{_now.month}.{_now.day} {_now.hour:02d}:{_now.minute:02d}",
            "",
            "【群组聊天模式】",
            "你正在一个群组中与多个 AI Agent 和用户对话。",
            "请以你的角色身份，根据对话历史给出回复。",
            "如果某条消息明显是在单独对你说话，请重点回应。",
            "如果消息是群组闲聊，可以选择性回应或保持沉默。",
            "",
        ]

        if persona:
            lines.append("【你的人格设定】")
            lines.append(persona)
            lines.append("")

        # session memory 摘要：让 Agent 知道之前自己做过什么工具调用
        summary = self._build_session_summary(agent_hash, group_id)
        if summary:
            lines.append("【近期你自己的工具调用（session memory）】")
            lines.append(summary)
            lines.append("")
            lines.append("引用时请直接基于上述结果，不要重新调用同一查询。")
            lines.append("")

        lines.append("【可用工具说明】")
        lines.append(
            "你可以调用工具来获取实时信息（web_search/web_fetch、知识库、文件读取等）。"
            "如果根据历史已经能回答，则无需重复调用工具。"
        )
        lines.append("")
        lines.append("")
        lines.append("【@提及机制】")
        lines.append("在回复中写 @{对方名字}（如 @Designer-林一）可唤醒被@的Agent（即使他处于静默状态）。")
        lines.append("")
        lines.append("【回复规则】")
        lines.append("1. 如果你不需要回复（消息不针对你或无实质内容），请回复：NO_REPLY")
        lines.append("2. 对于@类消息，如果你未被@且消息也与你的专业领域关系不大")
        lines.append("   （包括需要别人先完成前置工作才轮到你），建议回复NO_REPLY")
        lines.append("3. 可以适当发散，但不要过度偏离用户给出的任务和讨论主线")
        lines.append("4. 回复NO_REPLY仅跳过本轮，下轮仍可被唤醒")
        lines.append("5. 回复SILENT则标记为静默，后续不再唤醒——除非被 @ 点名")
        lines.append("   静默后错过若干条新消息将自动解除沉默，你也可以重新回复SILENT续期")
        lines.append("6. 否则，请以你的角色身份自然回复，不要声明你的思考过程")
        lines.append("7. 仅在确实需要实时数据或文件内容时才调用工具；闲聊/问候/已知信息直接回复")
        lines.append("")
        return "\n".join(lines)
    # ========== Tool Execution (group-scoped) ==========

    async def _execute_group_tool(
        self,
        agent_hash: str,
        group_id: str,
        tool_name: str,
        args: Dict[str, Any],
        timeout: float,
    ) -> str:
        """执行单个群聊工具调用，带超时和权限过滤。"""
        # 权限过滤：不允许的工具直接拒绝
        if tool_name not in GROUP_ALLOWED_TOOLS:
            return f"Error: 工具 {tool_name} 不允许在群聊中使用"

        try:
            from services.tool_registry import get_tool
            tool_entry = get_tool(tool_name)
            if not tool_entry:
                return f"Error: 未知工具 {tool_name}"

            # 懒加载 AgentToolsService
            tools_service = await self._get_or_create_tools(agent_hash, group_id)
            method = getattr(tools_service, tool_name, None)
            if not method:
                return f"Error: 工具 {tool_name} 在 AgentToolsService 上不可用"

            # 过滤参数
            valid_args: Dict[str, Any] = {}
            for key in tool_entry["param_names"]:
                if key in args:
                    valid_args[key] = args[key]

            # 异步/同步分派
            coro_or_result = method(**valid_args)
            if inspect.iscoroutine(coro_or_result):
                result_str = await asyncio.wait_for(coro_or_result, timeout=timeout)
            else:
                # 同步工具放入默认执行器
                loop = asyncio.get_event_loop()
                result_str = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: method(**valid_args)),
                    timeout=timeout,
                )

            # 统一为字符串
            if not isinstance(result_str, str):
                result_str = json.dumps(result_str, ensure_ascii=False)

            # 应用 P0 截断
            result_str = await tools_service._truncate_tool_result(
                result=result_str,
                tool_name=tool_name,
                tool_args=valid_args,
            )
            return result_str

        except asyncio.TimeoutError:
            return f"Error: 工具 {tool_name} 执行超时（>{timeout:.1f}s）"
        except Exception as e:
            logger.warning(
                f"[GroupDispatch] tool {tool_name} failed for agent={agent_hash}: {e}",
                exc_info=True,
            )
            return f"Error: 工具 {tool_name} 执行失败: {e}"

    async def _get_or_create_tools(self, agent_hash: str, group_id: str):
        """懒加载 AgentToolsService（群聊作用域）。"""
        # 简单缓存：避免每次 reply 都重建
        cache_key = (agent_hash, group_id)
        svc = getattr(self, "_tools_cache", {}).get(cache_key)
        if svc is None:
            from services.agent_tools_service import AgentToolsService
            svc = AgentToolsService(agent_hash=agent_hash, group_id=group_id)
            if not hasattr(self, "_tools_cache"):
                self._tools_cache = {}
            self._tools_cache[cache_key] = svc
        return svc

    # ========== Session Memory ==========

    def _get_session_items(
        self,
        group_id: str,
        agent_hash: str,
    ) -> List[Dict[str, Any]]:
        """获取 session memory 中的工具调用/结果项。"""
        self._cleanup_session_memory()
        entry = self._session_memory.get((group_id, agent_hash))
        if not entry:
            return []
        # 仅回填 assistant(tool_calls) + tool(result) 给 LLM
        items: List[Dict[str, Any]] = []
        for it in entry.get("items", []):
            role = it.get("role")
            if role in ("assistant", "tool"):
                items.append(it)
        return items

    def _append_session_item(
        self,
        group_id: str,
        agent_hash: str,
        item: Dict[str, Any],
    ) -> None:
        """追加一项到 session memory。"""
        key = (group_id, agent_hash)
        entry = self._session_memory.get(key)
        if entry is None:
            entry = {"created_at": time.time(), "last_used": time.time(), "items": []}
            self._session_memory[key] = entry
        entry["last_used"] = time.time()
        entry["items"].append(item)
        # 截断到 GROUP_SESSION_MEMORY_MAX 轮（粗略：每 2 项 ≈ 1 轮）
        max_items = GROUP_SESSION_MEMORY_MAX * 2
        if len(entry["items"]) > max_items:
            entry["items"] = entry["items"][-max_items:]

    def _build_session_summary(
        self,
        agent_hash: str,
        group_id: str,
    ) -> str:
        """构造 session memory 的摘要文本（注入到 system prompt）。"""
        entry = self._session_memory.get((group_id, agent_hash))
        if not entry or not entry.get("items"):
            return ""

        lines: List[str] = []
        # 仅取最近 GROUP_SESSION_MEMORY_MAX 项中能产出文本的 tool 结果
        recent = entry["items"][-GROUP_SESSION_MEMORY_MAX:]
        for it in recent:
            if it.get("role") != "tool":
                continue
            name = it.get("name", "?")
            content = it.get("content", "")
            if not content:
                continue
            # 截断每个结果到 300 字符
            preview = content if len(content) <= 300 else content[:300] + "..."
            # 去掉 Error: 开头的失败结果（避免污染 prompt）
            if content.startswith("Error:"):
                lines.append(f"- {name}: (失败) {content[:120]}")
            else:
                lines.append(f"- {name}: {preview}")

        if not lines:
            return ""
        return "\n".join(lines)

    def _cleanup_session_memory(self) -> None:
        """清理超过 TTL 的 session memory。"""
        now = time.time()
        stale = [
            k for k, v in self._session_memory.items()
            if now - v.get("last_used", 0) > GROUP_SESSION_TTL
        ]
        for k in stale:
            self._session_memory.pop(k, None)

    # ========== WS Push ==========

    async def _push_to_clients(
        self,
        group_id: str,
        msg_id: str,
        agent_hash: str,
        content: str,
    ):
        """Push a new group message to connected WS clients."""
        try:
            from routers.desktop_ws import manager
            payload = {
                "type": "group_message",
                "group_id": group_id,
                "msg_id": msg_id,
                "sender_type": "agent",
                "sender_hash": agent_hash,
                "content": content,
                "timestamp": int(time.time()),
            }
            await manager.send(payload)
        except Exception as e:
            logger.debug(f"[GroupDispatch] WS push skipped: {e}")

    # ========== CRUD Helpers ==========

    def create_group(
        self,
        db: Session,
        name: str,
        owner_user_id: int,
        member_hashes: Optional[List[str]] = None,
        settings: Optional[Dict] = None,
    ) -> Group:
        """Create a new group and add owner as first member."""
        group = Group(
            name=name,
            owner_user_id=owner_user_id,
            settings=settings or {},
            created_at=datetime.utcnow(),
        )
        db.add(group)
        db.flush()

        # Add owner as first member
        owner_member = GroupMember(
            group_id=group.id,
            agent_hash="",  # owner is user, not agent
            role="owner",
            is_silent=False,
            joined_at=datetime.utcnow(),
        )
        db.add(owner_member)

        # Add initial agent members
        if member_hashes:
            for h in member_hashes:
                db.add(GroupMember(
                    group_id=group.id,
                    agent_hash=h,
                    role="member",
                    is_silent=False,
                    joined_at=datetime.utcnow(),
                ))

        db.commit()
        db.refresh(group)
        logger.info(f"[GroupDispatch] Created group id={group.id} name={name} owner={owner_user_id}")
        return group

    def add_member(
        self,
        db: Session,
        group_id: str,
        agent_hash: str,
        role: str = "member",
    ) -> GroupMember:
        """Add an agent to a group."""
        existing = (
            db.query(GroupMember)
            .filter(GroupMember.group_id == group_id, GroupMember.agent_hash == agent_hash)
            .first()
        )
        if existing:
            return existing

        member = GroupMember(
            group_id=group_id,
            agent_hash=agent_hash,
            role=role,
            is_silent=False,
            joined_at=datetime.utcnow(),
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        logger.info(f"[GroupDispatch] Added member agent={agent_hash} to group={group_id}")
        return member

    def remove_member(self, db: Session, group_id: str, agent_hash: str) -> bool:
        """Remove an agent from a group."""
        member = (
            db.query(GroupMember)
            .filter(GroupMember.group_id == group_id, GroupMember.agent_hash == agent_hash)
            .first()
        )
        if not member:
            return False
        db.delete(member)
        db.commit()
        logger.info(f"[GroupDispatch] Removed member agent={agent_hash} from group={group_id}")
        return True

    def get_messages(
        self,
        db: Session,
        group_id: str,
        before: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[GroupMessage]:
        """Get group messages, newest first."""
        query = db.query(GroupMessage).filter(GroupMessage.group_id == group_id)
        if before:
            query = query.filter(GroupMessage.created_at < before)
        return (
            query.order_by(GroupMessage.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_group(self, db: Session, group_id: str) -> Optional[Group]:
        return db.query(Group).filter(Group.id == group_id).first()

    def get_member(
        self,
        db: Session,
        group_id: str,
        agent_hash: str,
    ) -> Optional[GroupMember]:
        return (
            db.query(GroupMember)
            .filter(GroupMember.group_id == group_id, GroupMember.agent_hash == agent_hash)
            .first()
        )

    def list_user_groups(self, db: Session, user_id: int) -> List[Group]:
        """List all groups owned by or joined by a user (via agents)."""
        # Groups where user is owner
        owned = db.query(Group).filter(Group.owner_user_id == user_id, Group.deleted_at.is_(None)).all()
        return owned


# Global singleton
group_dispatch_service = GroupDispatchService()