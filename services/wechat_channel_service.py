"""
WeChat 渠道服务 - ChatService 的 WeChat 适配层

职责：
- 将 AgentExecutor 流式步骤转换为微信消息（buffer 限流 + typing 刷新）
- 10 条微信消息上限控制
- 不重复实现聊天逻辑，调用统一的 ChatService
"""
import asyncio
import logging
import time
import traceback
from typing import Optional, List, Dict

from services.chat_service import ChatService
from services.llm_service import llm_service
from services.point_service import PointService
from config import settings

logger = logging.getLogger(__name__)

# ========== 微信去重前置钩子 ==========

async def wechat_dedup_hook(channel, meta, user_input):
    """WeChat 去重钩子：msg_id + 文本内容双重校验（AND 逻辑）

    pre_hook 规范：
        None=继续正常流程, ""=静默终止(重复消息),
        "文本"=回复文本并跳过LLM

    依赖 ChatHistory.wechat_msg_id 索引列（B-tree），避免全表扫描 JSON。
    """
    if channel != "wechat":
        return None

    from models.database import ChatHistory, SessionLocal

    wechat_meta = (meta or {}).get("wechat_metadata", {})
    msg_id = wechat_meta.get("msg_id")

    with SessionLocal() as db:
        # msg_id 检查（走索引列）
        msg_id_match = False
        if msg_id:
            existing = db.query(ChatHistory).filter(
                ChatHistory.channel == "wechat",
                ChatHistory.wechat_msg_id == msg_id,
            ).first()
            msg_id_match = existing is not None

        # 文本检查（仅上一条）
        recent = db.query(ChatHistory).filter(
            ChatHistory.channel == "wechat",
            ChatHistory.role == "user"
        ).order_by(ChatHistory.created_at.desc()).first()
        text_match = recent and recent.content == (user_input or "")

        # AND 逻辑
        if msg_id_match and text_match:
            return ""  # 静默终止：真重复

        if msg_id_match and not text_match:
            logger.warning(f"[WeChat] msg_id collision: {msg_id} but text differs")

        return None

# 历史消息压缩配置
MAX_HISTORY_MESSAGES = 30
COMPACT_ENABLED = True


def compact_messages(messages: List[Dict], max_pairs: int = MAX_HISTORY_MESSAGES) -> List[Dict]:
    """
    地中海式历史消息压缩。

    当对话历史超过 max_pairs 对消息时，将早期消息压缩为摘要，
    只保留最近的 max_pairs 对 + system prompt + compact 摘要。
    """
    if not COMPACT_ENABLED or len(messages) <= max_pairs * 2:
        return messages

    system_msg = messages[0] if messages and messages[0]["role"] == "system" else None
    conv_messages = messages[1:] if system_msg else messages

    user_count = sum(1 for m in conv_messages if m["role"] == "user")
    if user_count <= max_pairs:
        return messages

    early_messages = []
    recent_messages = []
    remaining_users = max_pairs

    for m in conv_messages:
        if m["role"] == "user":
            if remaining_users > 0:
                recent_messages.append(m)
                remaining_users -= 1
            else:
                early_messages.append(m)
        else:
            if recent_messages and recent_messages[-1]["role"] == "assistant":
                recent_messages.append(m)
            else:
                recent_messages.append(m)

    if not early_messages:
        return messages

    early_summary = _summarize_messages(early_messages)

    compact_msg = {
        "role": "system",
        "content": f"【早期对话摘要】以下是对当前对话早期部分的摘要：\n\n{early_summary}\n\n如需了解早期对话的详细内容，请另行询问。"
    }

    result = []
    if system_msg:
        result.append(system_msg)
    result.append(compact_msg)
    result.extend(recent_messages)

    logger.info(f"[COMPACT] {len(conv_messages)} messages → {len(recent_messages)} recent + 1 summary")
    return result


def _summarize_messages(messages: List[Dict]) -> str:
    """调用 LLM 生成早期对话摘要"""
    if not messages:
        return "（无历史对话）"

    conv_text = "\n".join(
        f"[{'用户' if m['role'] == 'user' else '助手'}] {m['content'][:200]}"
        + ("..." if len(m["content"]) > 200 else "")
        for m in messages[:40]
    )

    summary_prompt = f"""请为以下对话生成一段简洁的摘要（100字以内），描述：
1. 对话的主要话题或任务
2. 用户问了什么关键问题
3. 助手给出了什么重要回答或执行了什么操作

对话内容：
{conv_text}

摘要："""

    try:
        import threading
        result_holder = [None]
        error_holder = [None]

        def call_llm():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result_holder[0] = loop.run_until_complete(
                    llm_service.chat([{"role": "user", "content": summary_prompt}], stream=False)
                )
            except Exception as e:
                error_holder[0] = e

        t = threading.Thread(target=call_llm, daemon=True)
        t.start()
        t.join(timeout=10)

        if error_holder[0]:
            raise error_holder[0]
        summary = result_holder[0]
        if summary:
            summary = summary.strip()[:200]
            return summary if summary else "（对话内容摘要生成失败）"
        return "（对话内容摘要生成失败）"
    except Exception as e:
        logger.info(f"[COMPACT] Summary generation failed: {e}")
        return f"（摘要生成失败，仅保留最近 {MAX_HISTORY_MESSAGES} 条消息）"


class WeChatChannelService:
    """WeChat 渠道服务 - 将 ChatService 事件流转换为微信 iLink 消息"""

    def __init__(self, wechat_svc, user_id: int, agent_hash: str,
                 to_user_id: str, context_token: Optional[str] = None):
        self.wechat_service = wechat_svc
        self.user_id = user_id
        self.agent_hash = agent_hash
        self.to_user_id = to_user_id
        self.context_token = context_token

        # 消息缓冲和限流控制
        self.msg_count = 0
        self.buffer: List[str] = []
        self.first_pre_tool_sent = False
        self.system_warning_sent = False

    async def _flush_buffer(self):
        """发送当前组的内容"""
        if not self.buffer:
            return

        if self.msg_count == 6 and not self.system_warning_sent:
            self.system_warning_sent = True
            self.buffer[-1] += "\n\n当前对话已接近微信消息发送上限。剩余内容将合并为一条消息发送。"

        merged_text = "\n---\n".join(self.buffer)
        self.buffer = []
        self.msg_count += 1
        await self.wechat_service.send_message(
            to_user_id=self.to_user_id,
            text=merged_text,
            context_token=self.context_token
        )
        logger.info(f"[WeChat] Flushed buffer as message #{self.msg_count}")

    async def _send_single(self, text: str):
        """发送单条消息（第一条 pre_tool）"""
        logger.debug(f"[WX_DEBUG] _send_single CALLED: text={text[:80]!r}")
        self.msg_count += 1
        await self.wechat_service.send_message(
            to_user_id=self.to_user_id,
            text=text,
            context_token=self.context_token
        )
        logger.info(f"[WeChat] Sent single message #{self.msg_count}")

    async def stream_response(
        self,
        user_input: str,
        image_url: Optional[str] = None,
        msg_id: Optional[str] = None,
        client_id: Optional[str] = None
    ):
        """
        调用 AI 服务获取回复（流式，逐步骤发送微信消息）

        Args:
            user_input: 用户输入（已处理过的，含图片路径信息等）
            image_url: 图片 URL（可选）
            msg_id: 微信消息 ID（用于去重）
            client_id: 微信客户端 ID

        消息限流机制：
        - 第1条：第一个 pre_tool 单独发送
        - 第2-7条：pre_tool + tool_call + tool_result 合并发送
        - 第8条：追加系统提示到第7条消息末尾
        - 第9条：合并所有剩余内容发送
        - 第10条：留作24小时限制时的备用推送
        """
        db = None
        try:
            logger.info(f"[WeChat] stream_response: starting for user {self.user_id}")

            # 积分检查和扣减
            try:
                points_ok = PointService.try_deduct(str(self.user_id))
                if not points_ok:
                    await self.wechat_service.send_message(
                        to_user_id=self.to_user_id,
                        text=f"抱歉，您的今日积分已用完（每日{settings.DAILY_FREE_POINTS}点），请明日再来～",
                        context_token=self.context_token
                    )
                    return
            except Exception as e:
                logger.warning(f"[WeChat] Points check failed: {e}, allowing request")

            # 创建 ChatService（渠道无关的聊天服务）
            from services.wechat_service import wechat_service as _global_wechat
            binding = _global_wechat.get_binding_by_user(self.user_id)
            session_reset_at = binding.session_reset_at if binding else None

            chat_service = ChatService(
                agent_hash=self.agent_hash,
                channel="wechat",
                session_id="wechat_main",
                session_reset_at=session_reset_at,
                pre_process_hook=wechat_dedup_hook,
            )
            executor = chat_service.executor
            logger.info(f"[WeChat] stream_response: ChatService created for user {self.user_id}")

            # 构建系统提示词
            system_prompt = await chat_service.build_system_prompt()
            system_prompt = "你正在通过微信与用户交流。\n\n请友好地与用户交流，帮助他们解决问题。\n\n" + system_prompt

            # 加载历史消息
            history_messages = []
            try:
                await chat_service._load_history()
                if chat_service.context.history:
                    history_messages = list(chat_service.context.history)
                    logger.info(f"[WeChat] Loaded {len(history_messages)} history messages for user {self.user_id}")
            except Exception as e:
                logger.warning(f"[WeChat] Failed to load chat history: {e}")

            # 构建完整的消息上下文
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(history_messages)

            # 地中海式历史消息压缩
            original_count = len(messages)
            messages = compact_messages(messages, max_pairs=MAX_HISTORY_MESSAGES)
            if original_count > len(messages):
                logger.info(f"[COMPACT] {original_count} → {len(messages)} messages")

            # 构建用户消息
            if image_url:
                if image_url.startswith('/'):
                    vfs_path = image_url
                    if user_input and "已保存到" in user_input:
                        user_content = user_input
                    else:
                        prefix = f'用户给你发送了一张图片，已保存到"{vfs_path}"。\n⚠️ 后续任何工具调用中如需引用此图片，必须使用上述路径的值，不得使用其他路径或自己构造路径（如 current_image.png 等）。\n用户可能有后续文字说明，无需额外等待。'
                        if user_input:
                            user_content = prefix + "\n\n用户消息：" + user_input
                        else:
                            user_content = prefix
                else:
                    vfs_path = await download_and_save_image_to_vfs(image_url, self.user_id, agent_hash=binding.agent_hash)

                    if vfs_path:
                        prefix = f'用户给你发送了一张图片，已保存到"{vfs_path}"。\n⚠️ 后续任何工具调用中如需引用此图片，必须使用上述路径的值，不得使用其他路径或自己构造路径（如 current_image.png 等）。\n用户可能有后续文字说明，无需额外等待。'
                        if user_input:
                            user_content = prefix + "\n\n用户消息：" + user_input
                        else:
                            user_content = prefix
                        logger.info(f"[WeChat] Image saved to VFS: {vfs_path}")
                    else:
                        image_b64 = await _download_image_base64(image_url)
                        if image_b64:
                            import base64
                            user_content = [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{image_b64}"}
                                },
                                {"type": "text", "text": user_input}
                            ]
                        else:
                            logger.warning(f"[WeChat] Image download failed for user {self.user_id}")
                            await self.wechat_service.send_message(
                                to_user_id=self.to_user_id,
                                text="图片处理失败，请稍后重试。",
                                context_token=self.context_token
                            )
                            return
            else:
                user_content = user_input

            messages.append({"role": "user", "content": user_content})

            # 路由层拦截：开启新会话
            if user_input in ("开启新会话", "新对话", "新会话", "重新开始", "结束对话", "结束会话"):
                logger.warning(f"[WeChat] Intercepted '开启新会话' at router level, skipping LLM")
                from services.agent_tools_service import AgentToolsService
                _tools = AgentToolsService(self.agent_hash)
                _result = _tools.end_conversation()
                if _result and ("会话已存档" in _result or "无内容，未保存" in _result):
                    _greeting = await _generate_greeting_message(str(self.user_id), self.agent_hash)
                    if _greeting:
                        await self.wechat_service.send_message(
                            to_user_id=self.to_user_id,
                            text=_greeting,
                            context_token=self.context_token
                        )
                        logger.info(f"[WeChat] Sent greeting via router-level end_conversation")
                else:
                    await self.wechat_service.send_message(
                        to_user_id=self.to_user_id,
                        text=_result or "操作失败",
                        context_token=self.context_token
                    )
                return

            logger.info(f"[WeChat] stream_response: invoking executor.chat_with_tools for user {self.user_id}")
            _llm_start = time.time()

            try:
                accumulated_tokens = ""
                full_response = ""

                async for step in executor.chat_with_tools(messages):
                    logger.debug(f"[WeChat] stream_response: got step {step.step_type} for user {self.user_id}")

                    if step.step_type == "thinking":
                        await self.wechat_service.send_message(
                            to_user_id=self.to_user_id,
                            text=step.content,
                            context_token=self.context_token
                        )
                        asyncio.create_task(
                            self.wechat_service.send_typing(self.to_user_id, self.context_token)
                        )
                        logger.warning(f"[WeChat] Sent thinking buffer + typing to {self.to_user_id[:20]}")

                    elif step.step_type == "token":
                        accumulated_tokens += step.content
                        full_response += step.content

                    elif step.step_type == "final":
                        if step.content:
                            self.buffer.append(step.content)
                            full_response += step.content  # 非流式场景：final 是唯一响应来源
                        if self.buffer:
                            await self._flush_buffer()
                        elif accumulated_tokens:
                            await self.wechat_service.send_message(
                                to_user_id=self.to_user_id,
                                text=accumulated_tokens,
                                context_token=self.context_token
                            )
                        accumulated_tokens = ""

                    elif step.step_type == "error":
                        error_msg = step.content or "AI 服务暂时不可用，请稍后再试。"
                        if accumulated_tokens:
                            error_msg = accumulated_tokens + "\n\n" + error_msg
                            accumulated_tokens = ""
                        await self.wechat_service.send_message(
                            to_user_id=self.to_user_id,
                            text=error_msg,
                            context_token=self.context_token
                        )
                        self.buffer = []
                        accumulated_tokens = ""

                    elif step.step_type == "pre_tool":
                        if accumulated_tokens:
                            full_response += accumulated_tokens
                            if self.system_warning_sent:
                                self.buffer.append(accumulated_tokens)
                            else:
                                if self.buffer:
                                    await self._flush_buffer()
                                await self.wechat_service.send_message(
                                    to_user_id=self.to_user_id,
                                    text=accumulated_tokens,
                                    context_token=self.context_token
                                )
                                logger.info(f"[WeChat] Sent accumulated tokens to {self.to_user_id}")
                            accumulated_tokens = ""

                        if not self.first_pre_tool_sent:
                            self.first_pre_tool_sent = True
                            if not accumulated_tokens:
                                content = step.content
                                sep_idx = -1
                                for sep in ["\n\n🔧", "\n---\n🔧"]:
                                    idx = content.find(sep)
                                    if idx >= 0:
                                        sep_idx = idx
                                        break
                                if sep_idx > 0:
                                    friendly = content[:sep_idx].strip()
                                    tool_part = content[sep_idx:].strip()
                                    if friendly:
                                        await self._send_single(friendly)
                                    if tool_part:
                                        self.buffer.append(tool_part)
                                else:
                                    await self._send_single(content)
                        elif self.system_warning_sent:
                            self.buffer.append(step.content)
                        else:
                            if self.buffer:
                                await self._flush_buffer()
                            self.buffer.append(step.content)

                    elif step.step_type in ["tool_call", "tool_result"]:
                        if step.content:
                            _truncated = step.content[:200] + "..." if len(step.content) > 200 else step.content
                            self.buffer.append(_truncated)

                # 循环结束，发送剩余内容
                if accumulated_tokens:
                    full_response += accumulated_tokens
                    if self.buffer:
                        self.buffer.append(accumulated_tokens)
                        await self._flush_buffer()
                    else:
                        await self.wechat_service.send_message(
                            to_user_id=self.to_user_id,
                            text=accumulated_tokens,
                            context_token=self.context_token
                        )
                        logger.info(f"[WeChat] Sent remaining accumulated tokens to {self.to_user_id}")
                elif self.buffer:
                    await self._flush_buffer()

                logger.debug(f"[WeChat] stream_response: streaming loop completed for user {self.user_id}")
                logger.info(f"[WeChat] stream_response: completed for user {self.user_id}, total messages: {self.msg_count}")
                logger.info(f"[PERF] stream_response: llm_generation ({time.time()-_llm_start:.1f}s)")

                # 后置钩子：保存实际发给 LLM 的用户内容 + 完整响应
                try:
                    _meta = {"wechat_metadata": {"msg_id": msg_id, "client_id": client_id}} if msg_id or client_id else None
                    _user_text = user_content if isinstance(user_content, str) else user_input
                    chat_service._save_conversation(_user_text, full_response, meta=_meta)
                except Exception as e:
                    logger.warning(f"[WeChat] save_conversation failed: {e}")

                if settings.SESSION_MEMORY_ENABLED:
                    asyncio.create_task(chat_service._maybe_extract_session_memory())

                if getattr(settings, 'USER_PROFILE_ENABLED', True):
                    asyncio.create_task(chat_service._maybe_extract_user_profile())
            except Exception as e:
                logger.debug(f"[WeChat] stream_response: error during streaming loop for user {self.user_id}: {e}")
                logger.debug(f"[WeChat] stream_response: traceback:\n{traceback.format_exc()}")
                logger.error(f"[WeChat] stream_response: error during streaming loop: {e}")
                logger.error(f"[WeChat] stream_response: traceback:\n{traceback.format_exc()}")
                await self.wechat_service.send_message(
                    to_user_id=self.to_user_id,
                    text="执行出错，请重试",
                    context_token=self.context_token
                )
        except Exception as e:
            logger.error(f"[WeChat] stream_response error: {e}")
            logger.error(f"[WeChat] traceback: {traceback.format_exc()}")
            await self.wechat_service.send_message(
                to_user_id=self.to_user_id,
                text="执行出错，请重试",
                context_token=self.context_token
            )
        finally:
            try:
                if db:
                    db.close()
            except Exception as e:
                logger.debug(f"db.close() failed in stream_response cleanup: {e}")


async def _generate_greeting_message(user_id: str, agent_hash: str = None) -> str:
    """
    用 Agent 主模型生成打招呼消息（含历史会话上下文）
    """
    if not agent_hash:
        try:
            from models.database import WeChatBinding
            from models.database import SessionLocal
            _db = SessionLocal()
            try:
                _b = _db.query(WeChatBinding).filter(
                    WeChatBinding.user_id == int(user_id),
                    WeChatBinding.status == "active"
                ).order_by(WeChatBinding.id.desc()).first()
                if _b and _b.agent_hash:
                    agent_hash = _b.agent_hash
            finally:
                _db.close()
        except Exception:
            pass

    if not agent_hash:
        return ""

    try:
        from services.agent_executor import AgentExecutor

        # 读取最近会话摘要
        recent_context = ""
        try:
            from services.vector_search_service import VectorSearchService
            from models.database import SessionLocal, ConversationSession
            _vs = VectorSearchService(agent_hash=agent_hash)
            _vec_results = await _vs.search("", index=f"idx-{agent_hash}-conv", top_k=5)
            if _vec_results:
                summaries = []
                for r in _vec_results:
                    meta = r.get("metadata", {})
                    sid = meta.get("session_id")
                    text = meta.get("text", meta.get("summary", ""))[:200]
                    if sid and text:
                        summaries.append(f"- 会话 {sid[:8]}: {text}")
                if summaries:
                    recent_context = "\n".join(summaries[:3])
        except Exception:
            try:
                _db2 = SessionLocal()
                try:
                    _recent = _db2.query(ConversationSession).filter(
                        ConversationSession.agent_hash == agent_hash
                    ).order_by(ConversationSession.updated_at.desc()).limit(3).all()
                    if _recent:
                        summaries = []
                        for s in _recent:
                            if s.summary and s.summary not in ("生成中...", ""):
                                summaries.append(f"- {s.topic or '话题'}: {s.summary[:200]}")
                        if summaries:
                            recent_context = "\n".join(summaries)
                finally:
                    _db2.close()
            except Exception:
                pass

        # 读取人设文件
        persona_parts = []
        try:
            from services.storage_service import StorageService
            _store = StorageService()
            _base = f"feclaw/agents/{agent_hash}/workspace/agent/"
            def _read_persona(key):
                d = _store.get_file_content(_base + key)
                return d.decode("utf-8", errors="replace").strip() if d else ""

            _soul = _read_persona("soul.md")
            _identity = _read_persona("identity.md")
            _user_info = _read_persona("user.md")
            _memory = _read_persona("memory.md")

            if _soul: persona_parts.append(f"【性格设定】\n{_soul[:500]}")
            if _identity: persona_parts.append(f"【身份配置】\n{_identity[:500]}")
            if _user_info: persona_parts.append(f"【用户信息】\n{_user_info[:500]}")
            if _memory: persona_parts.append(f"【长期记忆】\n{_memory[:500]}")
        except Exception:
            pass

        persona_section = "\n\n".join(persona_parts) if persona_parts else ""

        # 构建打招呼专用系统提示词
        greeting_system = ""
        if persona_section:
            greeting_system += persona_section + "\n\n"
        greeting_system += (
            "用户开启了新会话，请以上述人设向用户打个招呼。\n\n"
            "将人设中的性格、语气、说话风格完全代入，输出你的第一次问候。\n"
            "不需要列方案或选项，简短一句即可。\n\n"
            "【补充指引】\n"
            "- 核心是问候，不是续聊\n"
            "- 如果记得之前的事，可以轻松提及一句（顺带一提）\n"
            "- 不要调用任何工具\n"
            "- 不需要开场白或解释，直接说话\n"
        )

        if recent_context:
            greeting_system += f"\n【用户最近几次会话摘要】\n{recent_context}\n\n你可以参考这些来打招呼，也可忽略。"

        from services.agent_tools_service import AgentToolsService
        _tools = AgentToolsService(agent_hash)
        _executor = AgentExecutor(
            agent_hash,
            _tools,
            blocked_tools=["web_search", "spawn_subagent", "image_generate",
                           "generate_image", "text_summarize", "text_translate",
                           "file_write", "file_delete", "edit", "bash",
                           "create_share_link", "generate_totp",
                           "end_conversation", "list_conversations", "load_conversation",
                           "search_sessions", "auto_suggest_session", "generate_summary"]
        )
        _executor._skip_smart_router = True

        _messages = [
            {"role": "system", "content": greeting_system},
            {"role": "user", "content": "你好！开启新会话。"}
        ]

        full_response = ""
        async for step in _executor.chat_with_tools(_messages):
            if step.step_type == "final":
                full_response = step.content
                break
            elif step.step_type == "token":
                full_response += step.content
            elif step.step_type == "error":
                logger.warning(f"[WeChat] Greeting generation error: {step.content[:100]}")
                full_response = ""
                break
            elif step.step_type in ("tool_call", "pre_tool", "tool_result"):
                logger.warning(f"[WeChat] Greeting triggered unexpected tool call: {step.content[:100]}")
                continue

        if full_response and len(full_response.strip()) > 5:
            return full_response.strip()

        logger.warning("[WeChat] Greeting returned empty or too short, using fallback")
        return ""

    except Exception as e:
        logger.error(f"[WeChat] Greeting generation error: {e}")
        return ""


async def _download_image_base64(image_url: str) -> Optional[str]:
    """下载图片并转换为 base64 编码"""
    import base64
    import httpx

    if not image_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(image_url)
            response.raise_for_status()
            image_bytes = response.content
            return base64.b64encode(image_bytes).decode("utf-8")
    except Exception as e:
        logger.warning(f"[WeChat] Failed to download image: {e}")
        return None


async def download_and_save_image_to_vfs(image_url: str, user_id: int, agent_hash: str = None) -> Optional[str]:
    """
    保存图片到 Agent VFS 工作区

    Args:
        image_url: 图片 URL 或 data:image/...;base64,... URI
        user_id: 用户 ID（仅用于去重）
        agent_hash: Agent hash（用于确定存储路径）

    Returns:
        VFS 文件路径，失败返回 None
    """
    import base64

    if not image_url:
        return None

    try:
        if image_url.startswith('data:'):
            header, data = image_url.split(',', 1)
            image_bytes = base64.b64decode(data)
        else:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(image_url)
                response.raise_for_status()
                image_bytes = response.content

        from services.vfs_image_dedup import VFSImageDeduplicationService
        dedup = VFSImageDeduplicationService(user_id=str(user_id), agent_hash=agent_hash)
        existing_path = dedup.find_duplicate(image_bytes)
        if existing_path:
            logger.info(f"[WeChat] Image deduplicated: reusing {existing_path}")
            return existing_path

        timestamp = int(time.time() * 1000)
        filename = f"temp_{timestamp}.png"
        vfs_path = f"/workspace/images/{filename}"

        from services.storage_service import StorageService
        storage = StorageService()
        if agent_hash:
            abs_key = f"feclaw/agents/{agent_hash}/workspace/images/{filename}"
        else:
            abs_key = f"feclaw/user_workspaces/{user_id}/workspace/images/{filename}"
        vfs_path = f"/workspace/images/{filename}"
        storage.upload_file(
            file_bytes=image_bytes,
            key=abs_key
        )

        dedup.register_image(vfs_path, image_bytes)

        logger.info(f"[WeChat] Saved image to VFS: {vfs_path}")
        return vfs_path

    except Exception as e:
        logger.warning(f"[WeChat] Failed to save image to VFS: {e}")
        return None
