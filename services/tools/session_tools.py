"""
Agent 工具服务 - 对话会话工具
包含会话存档、加载、搜索、摘要、意图分析等
"""

import os
import re
import json
import asyncio
import time
import logging
from datetime import datetime
from typing import Any, Optional
from concurrent.futures import ThreadPoolExecutor

from config import settings
from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase
from sqlalchemy import or_

logger = logging.getLogger(__name__)


class SessionToolsMixin(AgentToolsServiceBase):
    """对话会话工具 Mixin"""

    # ========== 会话摘要 ==========

    @tool(description="为一段对话生成简洁的摘要", category="agent")
    def generate_summary(self, messages_json: str) -> str:
        """
        生成会话摘要

        Args:
            messages_json: JSON 序列化的消息列表

        Returns:
            生成的摘要字符串
        """
        from services.llm_service import LLMService
        from services.model_registry import resolve as _sum_resolve

        try:
            messages = json.loads(messages_json)
        except Exception:
            return "Error: Invalid messages JSON"

        if not messages:
            return "Error: Empty messages"

        conversation_text = "\n".join([
            f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')[:200]}"
            for msg in messages[-10:]
        ])

        prompt = f"""请为以下对话生成一段简洁的摘要（100字以内）：
- 涵盖主要话题
- 包含关键结论或答案
- 用中文回复

对话内容：
{conversation_text}

摘要："""

        def _run_async_in_thread():
            async def collect_response():
                llm = LLMService()
                full_response = ""
                async for chunk in llm.chat([{"role": "user", "content": prompt}], provider=_sum_resolve("deepseek-v4-flash")["provider"]):
                    full_response += chunk
                return full_response.strip()

            return asyncio.run(collect_response())

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run_async_in_thread)
                return future.result(timeout=60)
        except Exception as e:
            return f"Error generating summary: {e}"

    @tool(description="结束当前对话并保存会话记录", category="agent")
    def end_conversation(self, messages_json: str = None, summary: str = None) -> str:
        """
        结束当前会话，自动存档（无参数版本）

        Args:
            messages_json: 消息列表的JSON字符串（可选，后端自动获取）
            summary: 可选的对话摘要（可选，后端自动生成）

        Returns:
            "会话已存档，新会话已开始" 或错误信息
        """
        from models.database import ConversationSession, SessionLocal, WeChatBinding, WeChatMessage
        from services.wechat_service import WeChatService

        try:
            if messages_json is None:
                try:
                    binding = WeChatService().get_binding_by_user(int(self.user_id))
                    if not binding:
                        return "Error: 未找到微信绑定，且未提供 messages_json 参数。Web/API 用户请使用 end_conversation(messages_json=...) 传入消息。"
                except Exception:
                    return "Error: 无法获取消息内容。Web/API 用户请使用 end_conversation(messages_json=...) 传入消息。"

                db = SessionLocal()
                try:
                    db_messages = db.query(WeChatMessage).filter(
                        WeChatMessage.binding_id == binding.id,
                        WeChatMessage.agent_hash == self.agent_hash
                    ).order_by(WeChatMessage.created_at.asc()).all()

                    messages = []
                    for msg in db_messages:
                        if msg.direction == "received" and msg.content and not msg.content.startswith("{"):
                            messages.append({"role": "user", "content": msg.content})
                        elif msg.direction == "sent" and msg.content and not msg.content.startswith("[工具调用]") and not msg.content.startswith("{"):
                            messages.append({"role": "assistant", "content": msg.content})

                    if not messages:
                        return "Error: 没有消息可存档"

                    messages_json = json.dumps(messages)
                finally:
                    db.close()

            try:
                messages = json.loads(messages_json)
                if not isinstance(messages, list):
                    return "Error: messages_json 必须是消息列表"
            except json.JSONDecodeError:
                return "Error: messages_json 不是有效的JSON格式"

            # 跳过纯"开启新会话"的无意义会话（用户只发了新会话指令，Assistant 回复随意）
            _skip_trivial = False
            _user_msgs = [m for m in messages if m.get("role") == "user"]
            if len(_user_msgs) == 1:
                _trivial_commands = {"开启新会话", "新对话", "新会话", "重新开始", "结束对话", "结束会话", "你好！开启新会话。"}
                _user_text = _user_msgs[0].get("content", "").strip()
                if _user_text in _trivial_commands:
                    _skip_trivial = True
                    logger.info(f"[AgentTools] Skipping trivial session (user only sent new-conversation command)")

            import uuid
            session_id = str(uuid.uuid4())[:16]

            if _skip_trivial:
                # 仍清理微信消息记录，不创建 ConversationSession
                db = SessionLocal()
                try:
                    binding = WeChatService().get_binding_by_user(int(self.user_id))
                    if binding:
                        db_binding = db.query(WeChatBinding).filter(WeChatBinding.id == binding.id).first()
                        if db_binding:
                            db_binding.session_reset_at = datetime.utcnow()
                        deleted_count = db.query(WeChatMessage).filter(
                            WeChatMessage.binding_id == binding.id,
                            WeChatMessage.agent_hash == self.agent_hash
                        ).delete()
                        logger.info(f"[AgentTools] Cleared {deleted_count} WeChatMessage records (trivial session)")
                    db.commit()
                finally:
                    db.close()
                return "会话已重置（无内容，未保存）"

            if not summary:
                summary = "生成中..."
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(self._generate_summary_async(session_id, messages_json))
                else:
                    if running_loop.is_running():
                        asyncio.ensure_future(self._generate_summary_async(session_id, messages_json))
                    else:
                        running_loop.run_until_complete(self._generate_summary_async(session_id, messages_json))

            topic = self._extract_topic(messages)
            message_count = len(messages)
            token_count = self._estimate_tokens(messages_json)

            db = SessionLocal()
            try:
                session = ConversationSession(
                    session_id=session_id,
                    user_id=int(self.user_id),
                    agent_hash=self.agent_hash,
                    messages=messages_json,
                    summary=summary if summary else None,
                    topic=topic,
                    importance=3,
                    message_count=message_count,
                    token_count=token_count,
                    is_archived=False
                )
                db.add(session)

                binding = WeChatService().get_binding_by_user(int(self.user_id))
                if binding:
                    db_binding = db.query(WeChatBinding).filter(WeChatBinding.id == binding.id).first()
                    if db_binding:
                        db_binding.session_reset_at = datetime.utcnow()

                    deleted_count = db.query(WeChatMessage).filter(
                        WeChatMessage.binding_id == binding.id,
                        WeChatMessage.agent_hash == self.agent_hash
                    ).delete()
                    logger.info(f"[AgentTools] Archived {deleted_count} WeChatMessage records for agent {self.agent_hash}, session_reset_at updated")

                db.commit()

                # 触发 Reflection 事实核查（异步，不阻塞 end_conversation 返回）
                self._schedule_reflection_on_end(messages)

                return "会话已存档，新会话已开始"
            finally:
                db.close()
        except Exception as e:
            return f"Error: 保存会话失败: {e}"

    def _schedule_reflection_on_end(self, messages: list):
        """在 end_conversation 时异步触发 Reflection 事实核查"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        from services.reflection_service import ReflectionService

        async def _run():
            try:
                svc = ReflectionService(self.agent_hash)
                result = await svc.check_session_memory(messages)
                logger.info(
                    "[end_conversation] Reflection result: has_errors=%s, topic=%s, short_desc=%s",
                    result.get("has_errors"), result.get("topic"),
                    result.get("short_desc", "")[:80]
                )
            except Exception as e:
                logger.debug("[end_conversation] Reflection failed: %s", e)

        asyncio.ensure_future(_run())

    async def _generate_summary_async(self, session_id: str, messages_json: str):
        """后台异步生成摘要"""
        from models.database import SessionLocal, ConversationSession

        try:
            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(
                None,
                lambda: self._generate_summary_sync(messages_json)
            )

            db = SessionLocal()
            try:
                session = db.query(ConversationSession).filter(
                    ConversationSession.session_id == session_id
                ).first()
                if session:
                    session.summary = summary
                    db.commit()
                    logger.info(f"[AgentTools] Summary generated for session {session_id}")

                    # 摘要生成后立即向量化，存入 Agent 记忆索引
                    try:
                        from services.vector_search_service import VectorSearchService
                        _vs = VectorSearchService(agent_hash=self.agent_hash)
                        _key = f"conv-{session_id}-{int(time.time())}"
                        await _vs.index_text(
                            key=_key,
                            text=summary,
                            index=f"idx-{self.agent_hash}-conv",
                            metadata={
                                "session_id": session_id,
                                "summary": summary[:100],
                                "timestamp": str(datetime.now()),
                            }
                        )
                        logger.info(f"[AgentTools] Vectorized summary for session {session_id} → {_key}")
                    except Exception:
                        pass  # 向量化失败不影响摘要生成
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[AgentTools] Failed to generate summary: {e}")

    def _generate_summary_sync(self, messages_json: str) -> str:
        """同步生成摘要（两级策略：Qwen Flash 判断价值 → 高价值用 DeepSeek 详细摘要）"""
        import httpx

        try:
            messages = json.loads(messages_json)
        except Exception:
            return "Error: Invalid JSON"

        if not messages:
            return "空会话"

        conversation_text = "\n".join([
            f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')[:200]}"
            for msg in messages[-10:]
        ])

        # Step 1: Qwen Flash 快速判断价值 + 生成摘要
        qwen_prompt = f"""分析以下对话，输出JSON格式摘要和判断。

{{
  "summary": "30-50字中文摘要",
  "value": "high或low"
}}

判断标准：
- high: 有实质信息/问题解答/技术讨论/数据分析/具体请求
- low: 仅打招呼/简单确认/仅开启新会话/纯图片无文字

注意：
- 不要过度自信，不确定时选low
- 就算问题简单（如"今天股票多少"），只要有实质信息需求就算high
- 摘要必须包含具体的关键信息

对话：
{conversation_text}

JSON："""

        qwen_api_key = settings.QWEN_API_KEY or os.getenv("QWEN_API_KEY", "")

        quick_summary = ""
        value = "high"
        try:
            resp = httpx.post(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                headers={"Authorization": f"Bearer {qwen_api_key}", "Content-Type": "application/json"},
                json={
                    "model": "qwen3.6-flash",
                    "messages": [{"role": "user", "content": qwen_prompt}],
                    "thinking": {"type": "disabled"},
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            qwen_content = resp.json()["choices"][0]["message"]["content"]

            # 解析 JSON
            try:
                result = json.loads(qwen_content.strip())
            except json.JSONDecodeError:
                match = re.search(r'\{[^}]*"summary"[^}]*"value"[^}]*\}', qwen_content, re.DOTALL)
                if match:
                    result = json.loads(match.group())
                else:
                    result = {"summary": qwen_content.strip()[:100], "value": "low"}

            quick_summary = result.get("summary", qwen_content.strip()[:100]).strip()
            value = result.get("value", "low")

        except Exception as e:
            # Qwen 失败时降级到 DeepSeek
            logger.warning(f"[AgentTools] Qwen Flash summary failed, fallback to DeepSeek: {e}")
            quick_summary = ""
            value = "high"

        # Step 2: 高价值 → DeepSeek 详细摘要（含用户原话）
        if value == "high":
            deepseek_prompt = f"""为以下对话生成一段精炼的摘要。按这个格式：

## 话题
[对话核心话题]

## 用户
[用引号保留用户关键原话，如用户问"此楼有几层"就写"此楼有几层"]

## 关键信息
[助手回答的结论、关键数据、决策]

注意：用户的原话尽量保留原文，这对后续搜索至关重要。

对话：
{conversation_text}"""
            try:
                resp = httpx.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-v4-flash",
                        "messages": [{"role": "user", "content": deepseek_prompt}],
                        "thinking": {"type": "disabled"},
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
                detail = data["choices"][0]["message"]["content"].strip()

                # Step 3: 反思式校验（使用 AgentExecutor 工具调用能力）
                try:
                    reflect_result = self._verify_summary_with_tools(conversation_text, detail)
                    if reflect_result:
                        return reflect_result
                except Exception as e2:
                    logger.debug(f"[Session] Reflect verification failed: {e2}")
            except Exception as e:
                # DeepSeek 失败时回退到 Qwen 的摘要
                logger.warning(f"[AgentTools] DeepSeek summary failed, fallback to Qwen: {e}")
                return quick_summary[:100] if quick_summary else "Error: 摘要生成失败"
        else:
            return quick_summary[:100]

    # ========== 反思式校验（AgentExecutor 工具调用） ==========

    def _verify_summary_with_tools(self, conversation_text: str, summary: str) -> Optional[str]:
        """用 AgentExecutor（可调工具）反思式校验摘要"""
        try:
            result = asyncio.run(self._async_verify(conversation_text, summary))
            return result
        except Exception as e:
            logger.warning(f"[AgentTools] Async verify failed: {e}")
            return None

    async def _async_verify(self, conversation_text: str, summary: str) -> str:
        """异步执行反思验证（可调 web_search / file_read 工具）"""
        import json
        import httpx
        from config import settings
        from services.search_service import SearchService

        messages = [
            {"role": "system", "content": "你是摘要质量审核员。检查摘要是否准确反映对话内容。必要时使用工具核实事实，给出准确结论。"},
            {"role": "user", "content": f"""检查以下对话摘要是否准确：

对话内容：
{conversation_text}

生成的摘要：
{summary}

检查要点：
1. 摘要中的事实性信息（数字、名称、结论）是否有误？
2. 是否有重要信息被遗漏？
3. 是否有主观推断被当作事实陈述？

如果需要核实事实，可以使用以下工具：
- web_search: 搜索验证关键事实
- file_read: 读取已知文件核实

请先检查，必要时搜索验证，然后给出最终结论。
如果无误，回复"✅ 通过"后附上摘要。
如果有误，用工具核实后给出修正后的准确摘要。"""}
        ]

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "搜索验证事实（核实数字、日期、事件等）",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "file_read",
                    "description": "读取已知文件核实内容",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "文件路径"}},
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "file_list",
                    "description": "浏览目录查找相关文件",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "目录路径"}},
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "spawn_subagent",
                    "description": "启动子Agent验证视觉信息（图片楼层数、场景等）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "model": {"type": "string", "description": "推荐qwen3.6-35b-a3b"},
                            "reasoning_effort": {"type": "string", "description": "off"},
                            "task": {"type": "string", "description": "验证任务描述"},
                            "image_path": {"type": "string", "description": "图片路径"},
                            "timeout_seconds": {"type": "integer", "description": "超时秒数"},
                        },
                        "required": ["task"],
                    },
                },
            },
        ]

        max_rounds = 2
        final_text = ""

        for round_num in range(max_rounds):
            req_body = {
                "model": "deepseek-v4-flash",
                "messages": messages,
                "thinking": {"type": "disabled"},
                "temperature": 0.3,
            }
            if round_num == 0:
                req_body["tools"] = tools

            try:
                resp = httpx.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=req_body,
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                msg = choice["message"]
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls", [])
            except Exception as e:
                logger.warning(f"[AgentTools] Verify round {round_num} failed: {e}")
                break

            if not tool_calls:
                final_text = content or ""
                break

            # 执行工具调用
            assistant_msg = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except Exception:
                    args = {}

                tool_result = ""
                if func_name == "web_search":
                    query = args.get("query", "")
                    if query:
                        try:
                            ss = SearchService()
                            search_text = await ss.search_qwen(query)
                            tool_result = search_text[:2000]
                        except Exception as e:
                            tool_result = f"搜索失败: {e}"
                elif func_name == "file_read":
                    path = args.get("path", "")
                    if path:
                        try:
                            tool_result = (await self.vfs.async_cat(path))[:2000]
                            if tool_result.startswith("Error"):
                                tool_result = f"读取失败: {tool_result}"
                        except Exception as e:
                            tool_result = f"读取失败: {e}"
                elif func_name == "file_list":
                    path = args.get("path", "")
                    try:
                        items = await self.vfs.async_ls(path)
                        tool_result = "\n".join(items[:20]) if items else "(空)"
                    except Exception as e:
                        tool_result = f"列出目录失败: {e}"
                elif func_name == "spawn_subagent":
                    task = args.get("task", "")
                    image_path = args.get("image_path", "")
                    try:
                        from services.agent_tools_service import AgentToolsService
                        sa = AgentToolsService(agent_hash=self.agent_hash)
                        sub_result = sa.spawn_subagent(
                            model="qwen3.6-35b-a3b",
                            reasoning_effort="off",
                            task=task,
                            image_path=image_path if image_path else None,
                            timeout_seconds=30,
                        )
                        tool_result = sub_result[:2000] if sub_result else "无返回"
                    except Exception as e:
                        tool_result = f"子Agent失败: {e}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })

        # 解析最终结果
        if "✅ 通过" in final_text or "通过" in final_text:
            return summary  # 通过，用原摘要
        if final_text.strip():
            return final_text[:500]  # 修正版本
        return summary  # 无结果时 fallback

    def _extract_topic(self, messages: list) -> str:
        """
        从消息中提取话题标签（简单关键词检测）
        """
        text = " ".join([
            msg.get("content", "")[:500]
            for msg in messages[-5:]
        ]).lower()

        if any(k in text for k in ["数学", "math", "计算", "方程", "函数", "几何"]):
            return "math"
        elif any(k in text for k in ["编程", "code", "python", "代码", "程序", "bug", "debug"]):
            return "programming"
        elif any(k in text for k in ["英语", "english", "单词", "vocabulary"]):
            return "english"
        elif any(k in text for k in ["物理", "physics", "力学", "电磁"]):
            return "physics"
        elif any(k in text for k in ["日记", "day", "今天", "日常", "生活"]):
            return "daily"
        else:
            return "general"

    def _estimate_tokens(self, messages_json: str) -> int:
        """
        估算消息的token数量（1 token ≈ 2 chars）
        """
        return len(messages_json) // 2

    @tool(description="列出用户的历史会话，包含话题和摘要信息", category="agent")
    def list_conversations(self) -> str:
        """
        列出当前用户的所有保存的对话会话

        Returns:
            会话列表
        """
        from models.database import ConversationSession, SessionLocal

        try:
            db = SessionLocal()
            try:
                sessions = db.query(ConversationSession).filter(
                    ConversationSession.user_id == int(self.user_id)
                ).order_by(ConversationSession.updated_at.desc()).limit(20).all()

                if not sessions:
                    return "（无保存的对话）"

                lines = []
                for s in sessions:
                    summary = s.summary[:40] + "..." if s.summary and len(s.summary) > 40 else (s.summary or "")
                    from datetime import timedelta
                    if s.updated_at:
                        local_time = s.updated_at + timedelta(hours=8)
                        time_str = local_time.strftime('%Y-%m-%d %H:%M')
                    else:
                        time_str = "未知时间"
                    lines.append(f"[{s.session_id}] {time_str} (UTC+8): {summary}")
                return "\n".join(lines)
            finally:
                db.close()
        except Exception as e:
            return f"Error: 查询会话失败: {e}"

    @tool(description="加载指定会话ID的历史对话上下文", category="agent")
    def load_conversation(self, session_id: str) -> str:
        """
        根据会话ID加载对话

        Args:
            session_id: 会话ID

        Returns:
            友好的对话摘要形式，便于 Agent 理解历史上下文
        """
        from models.database import ConversationSession, SessionLocal

        try:
            db = SessionLocal()
            try:
                session = db.query(ConversationSession).filter(
                    ConversationSession.session_id == session_id,
                    ConversationSession.user_id == int(self.user_id)
                ).first()

                if not session:
                    return "Error: 会话不存在或无权访问"

                session.updated_at = datetime.utcnow()
                db.commit()

                try:
                    messages = json.loads(session.messages)
                except (json.JSONDecodeError, TypeError):
                    return f"【历史对话】\n\n{session.messages}"

                result = "【历史对话】\n\n"

                max_messages = 20
                if len(messages) > max_messages:
                    result += f"（共 {len(messages)} 条消息，显示最近 {max_messages} 条）\n\n"
                    messages = messages[-max_messages:]

                for msg in messages:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")

                    if role == "user":
                        role_label = "👤 用户"
                    elif role == "assistant":
                        role_label = "🤖 助手"
                    else:
                        role_label = f"📋 {role}"

                    if len(content) > 500:
                        content = content[:500] + "...（内容过长已截断）"

                    result += f"{role_label}：{content}\n\n"
                    result += "---\n\n"

                total_messages = len(messages)
                date_str = session.updated_at.strftime('%Y-%m-%d %H:%M') if session.updated_at else '未知'
                result += f"📊 共 {total_messages} 条消息 | 最后更新：{date_str}"

                if session.topic:
                    result += f" | 话题：{session.topic}"

                result += "\n\n💡 你可以基于这些历史上下文继续对话。"

                return result
            finally:
                db.close()
        except Exception as e:
            return f"Error: 恢复会话失败: {e}"

    @tool(description="搜索历史会话，根据语义/向量相似度查找相关的会话记录（使用向量搜索）", category="agent")
    def search_sessions(self, query: str) -> str:
        """
        搜索历史会话（语义向量搜索）

        Args:
            query: 搜索关键词，可以是话题、摘要内容等

        Returns:
            匹配到的会话列表
        """
        from models.database import ConversationSession, SessionLocal
        from services.vector_search_service import VectorSearchService
        import asyncio

        if not query or len(query.strip()) < 2:
            return "Error: 搜索关键词至少需要2个字符"

        query = query.strip()

        try:
            vs = VectorSearchService(agent_hash=self.agent_hash)
            try:
                results = asyncio.run(vs.search(query, top_k=10))
            except RuntimeError:
                loop = asyncio.get_event_loop()
                results = loop.run_until_complete(vs.search(query, top_k=10))
        except Exception as e:
            logger.warning(f"[search_sessions] Vector search failed, falling back to SQL LIKE: {e}")
            results = []

        if results:
            # Filter to only conv results that have a session_id in metadata
            session_ids = []
            metadata_map = {}
            for r in results:
                meta = r.get("metadata", {})
                if meta and meta.get("session_id"):
                    sid = meta["session_id"]
                    if sid not in session_ids:
                        session_ids.append(sid)
                        metadata_map[sid] = {"score": r.get("score", 0), "summary": meta.get("summary", "")}

            if session_ids:
                db = SessionLocal()
                try:
                    sessions = db.query(ConversationSession).filter(
                        ConversationSession.session_id.in_(session_ids),
                        ConversationSession.user_id == int(self.user_id),
                    ).all()

                    # Sort by vector search score (maintain the order from search results)
                    session_map = {s.session_id: s for s in sessions}
                    ordered = [(sid, metadata_map[sid]) for sid in session_ids if sid in session_map]

                    if ordered:
                        result = f"找到 {len(ordered)} 个相关会话（语义搜索）：\n\n"
                        for sid, meta in ordered:
                            s = session_map[sid]
                            date = s.updated_at.strftime('%Y-%m-%d %H:%M') if s.updated_at else '未知'
                            topic = s.topic or '未分类'
                            summary = s.summary[:60] + "..." if s.summary and len(s.summary) > 60 else (s.summary or '无摘要')
                            result += f"━━━━━━━━━━━━━━━━━━━━\n"
                            result += f"ID: {s.session_id}\n"
                            result += f"话题: {topic} | 日期: {date}\n"
                            result += f"摘要: {summary}\n"
                            result += f"消息数: {s.message_count or 0}\n"

                        result += "\n输入 load_conversation(session_id='xxx') 可以加载指定会话"
                        return result
                finally:
                    db.close()

        # Fallback: SQL LIKE search
        db = SessionLocal()
        try:
            sessions = db.query(ConversationSession).filter(
                ConversationSession.user_id == self.user_id,
                ConversationSession.is_archived == False,
                or_(
                    ConversationSession.summary.ilike(f"%{query}%"),
                    ConversationSession.topic.ilike(f"%{query}%"),
                    ConversationSession.messages.ilike(f"%{query}%")
                )
            ).order_by(ConversationSession.updated_at.desc()).limit(10).all()

            if not sessions:
                return f"没有找到包含 '{query}' 的会话"

            result = f"找到 {len(sessions)} 个相关会话（关键词匹配）：\n\n"
            for s in sessions:
                date = s.updated_at.strftime('%Y-%m-%d %H:%M') if s.updated_at else '未知'
                topic = s.topic or '未分类'
                summary = s.summary[:60] + "..." if s.summary and len(s.summary) > 60 else (s.summary or '无摘要')
                result += f"━━━━━━━━━━━━━━━━━━━━\n"
                result += f"ID: {s.session_id}\n"
                result += f"话题: {topic} | 日期: {date}\n"
                result += f"摘要: {summary}\n"
                result += f"消息数: {s.message_count or 0}\n"

            result += "\n输入 load_conversation(session_id='xxx') 可以加载指定会话"
            return result
        finally:
            db.close()

    # ========== 意图分析 ==========

    @tool(description="自动分析用户输入并在需要时建议加载相关会话（内部使用）", category="agent")
    def auto_suggest_session(self, user_input: str) -> str:
        """
        自动分析用户意图并建议加载相关会话 - 已废弃

        现在由 SessionMemory + 向量化检索提供更精确的会话推荐，
        此功能已禁用，保留接口供向后兼容。
        """
        return ""

    def analyze_intent(self, user_input: str) -> str:
        """
        分析用户意图：是否想继续某个之前的会话

        Args:
            user_input: 用户当前输入

        Returns:
            JSON格式的意图分析结果
        """
        from models.database import ConversationSession, SessionLocal
        from services.llm_service import LLMService
        from services.model_registry import resolve as _sum_resolve2

        if not user_input or len(user_input.strip()) < 3:
            return json.dumps({"intent": "unclear", "confidence": 0, "related_session_id": None, "reason": "输入太短"})

        user_input = user_input.strip()
        db = SessionLocal()
        try:
            recent_sessions = db.query(ConversationSession).filter(
                ConversationSession.user_id == self.user_id,
                ConversationSession.is_archived == False
            ).order_by(ConversationSession.updated_at.desc()).limit(5).all()

            if not recent_sessions:
                return json.dumps({"intent": "new_topic", "confidence": 1.0, "related_session_id": None, "reason": "没有历史会话"})

            session_contexts = []
            for s in recent_sessions:
                topic = s.topic or '未分类'
                summary = s.summary or '无摘要'
                session_contexts.append(f"- session_id: {s.session_id}, topic: {topic}, summary: {summary}")

            context_text = "\n".join(session_contexts)

            prompt = f"""你是一个意图分析助手。用户当前输入："{user_input}"

参考的历史会话：
{context_text}

请分析用户是否想继续某个之前的会话，还是想开启一个新话题。

分析标准：
- 如果用户明确提到之前讨论的内容、引用之前的话题关键词、使用"继续"、"之前"、"那个"等词汇 → continue_topic
- 如果用户提到具体的session_id → continue_topic
- 如果用户明显在问新问题、与之前话题无关 → new_topic
- 意图不明确 → unclear

请返回JSON格式（不带markdown代码块）：
{{"intent": "continue_topic|new_topic|unclear", "confidence": 0.0-1.0, "related_session_id": "如果intent是continue_topic，填入最相关的session_id，否则填null", "reason": "分析理由（简短中文）"}}
"""

            llm = LLMService()

            def _run_async_in_thread():
                async def collect():
                    result = ""
                    async for chunk in llm.chat([{"role": "user", "content": prompt}], provider=_sum_resolve2("deepseek-v4-flash")["provider"]):
                        result += chunk
                    return result.strip()

                return asyncio.run(collect())

            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_run_async_in_thread)
                    return future.result(timeout=30)
            except Exception as e:
                return json.dumps({"intent": "error", "confidence": 0, "related_session_id": None, "reason": str(e)})
        finally:
            db.close()
