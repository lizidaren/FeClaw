"""
Agent 执行层
提供 LLM 交互、工具调用循环、上下文管理等核心执行逻辑
"""

import json
import time
import logging
import asyncio
from enum import Enum
from typing import List, Dict, Optional, AsyncGenerator

from config import settings
from models.database import SessionLocal, AgentProfile
from services.llm_service import llm_service
from services.smart_router import SmartRouter, RouteDecision
from services.agent_tools_service import AgentToolsService, Step
from services.message_compactor import MessageCompactor

logger = logging.getLogger(__name__)

# 110K tokens 触发压缩（简单实现）
CONTEXT_LIMIT = 110000

# === Subagent 权限体系 ===

class SubagentPermission(str, Enum):
    """Subagent 权限等级"""
    READONLY = "readonly"   # 默认：搜索/读取/总结
    STANDARD = "standard"   # 可读写文件
    FULL     = "full"       # 可执行命令、递归

SUBAGENT_PERMISSION_SETS = {
    SubagentPermission.READONLY: {
        "blocked": [
            "spawn_subagent", "end_conversation", "create_cron_job",
            "schedule_reminder", "file_write", "file_delete",
            "file_append", "edit", "bash", "python_background",
        ]
    },
    SubagentPermission.STANDARD: {
        "blocked": [
            "spawn_subagent", "end_conversation", "create_cron_job",
            "schedule_reminder", "bash", "python_background",
        ]
    },
    SubagentPermission.FULL: {
        "blocked": ["spawn_subagent", "end_conversation"],
    },
}

# 向后兼容：保留旧名作为 READONLY 的别名
SUBAGENT_BLOCKED_TOOLS = SUBAGENT_PERMISSION_SETS[SubagentPermission.READONLY]["blocked"]


class AgentExecutor:
    """Agent 执行器：处理 LLM 对话、工具调用循环、上下文管理"""

    def __init__(self, agent_hash: str, tools: AgentToolsService, blocked_tools: Optional[List[str]] = None):
        """
        Args:
            agent_hash: Agent 的 4 位 hash
            tools: AgentToolsService 实例
            blocked_tools: 禁用的工具名称列表（用于 subagent 安全限制）
        """
        self.agent_hash = agent_hash
        self.tools = tools
        self.blocked_tools = set(blocked_tools or [])
        self._user_id = None
        self._agent_profile = None
        self._skip_compact = False  # 防止 pre_compact 递归调用 compact
        self._skip_smart_router = False  # 递归调用时跳过 SR 阶段
        self._typing_callback = None  # Set by caller for typing refresh
        self._cached_persona = None  # soul.md 缓存，减少 VFS I/O

    @property
    def user_id(self) -> str:
        """获取所属用户 ID（懒加载）"""
        if self._user_id is None:
            db = SessionLocal()
            try:
                agent = db.query(AgentProfile).filter(AgentProfile.hash == self.agent_hash).first()
                if agent:
                    self._user_id = str(agent.user_id)
                    self._agent_profile = agent
                else:
                    raise ValueError(f"Agent {self.agent_hash} not found")
            finally:
                db.close()
        return self._user_id

    @property
    def agent_profile(self) -> AgentProfile:
        """获取 AgentProfile（懒加载）"""
        if self._agent_profile is None:
            db = SessionLocal()
            try:
                self._agent_profile = db.query(AgentProfile).filter(AgentProfile.hash == self.agent_hash).first()
                if not self._agent_profile:
                    raise ValueError(f"Agent {self.agent_hash} not found")
            finally:
                db.close()
        return self._agent_profile

    def _refresh_agent_profile(self):
        """从 DB 重新加载 AgentProfile（用于获取最新设置，如 sr_enabled）"""
        db = SessionLocal()
        try:
            profile = db.query(AgentProfile).filter(AgentProfile.hash == self.agent_hash).first()
            if profile:
                self._agent_profile = profile
        finally:
            db.close()
        self._cached_persona = None  # 重置缓存，下次读取 soul.md 时重新加载

    def _read_vfs_file(self, path: str) -> Optional[str]:
        """读取 VFS 文件（压缩为纯文本）"""
        try:
            content = self.tools.vfs.cat(path)
            if content and not content.startswith('Error'):
                return content.strip()
        except Exception as e:
            logger.warning(f"[AgentExecutor] VFS read error for {path}: {e}")
        return None

    def _extract_known_image_paths(self, messages: List[Dict]) -> List[str]:
        """从消息历史中提取所有已知的图片 VFS 路径

        识别模式：
        - 「图片路径: /workspace/images/xxx.png」
        - 图片路径: /workspace/images/xxx.png
        - 已保存到"/workspace/images/xxx.png"
        - 已保存到VFS路径"/workspace/images/xxx.png"
        """
        import re
        paths = []
        seen = set()
        patterns = [
            r'图片路径[：:]\s*(/workspace/images/\S+?\.(?:png|jpg|jpeg|gif|webp|bmp))',
            r'已保存到"?(/workspace/images/\S+?\.(?:png|jpg|jpeg|gif|webp|bmp))"?',
            r'已保存到VFS路径"?(/workspace/images/\S+?\.(?:png|jpg|jpeg|gif|webp|bmp))"?',
        ]
        for msg in reversed(messages):
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            for pat in patterns:
                for m in re.finditer(pat, content):
                    p = m.group(1)
                    if p not in seen:
                        seen.add(p)
                        paths.append(p)
        return paths

    def _validate_path_with_hint(
        self, tool_name: str, args: Dict, messages: List[Dict]
    ) -> Optional[str]:
        """校验工具调用中的路径参数，返回提示信息

        如果路径不在已知的图片路径列表中，但存在已知路径，
        返回提示告知模型应使用哪个路径。
        """
        # 只对涉及文件路径的工具进行校验
        path_keys = []
        if tool_name in ("file_read", "file_list", "file_write", "file_append", "file_delete", "edit"):
            path_keys = ["path", "dir"]
        elif tool_name in ("spawn_subagent",):
            path_keys = ["image_path"]

        if not path_keys:
            return None

        known_paths = self._extract_known_image_paths(messages)
        if not known_paths:
            return None

        # 检查工具调用中的路径参数是否在已知列表中
        for key in path_keys:
            arg_path = args.get(key)
            if not arg_path or not isinstance(arg_path, str):
                continue
            if not arg_path.startswith("/workspace"):
                continue
            if arg_path not in known_paths:
                known_list = "\n".join(f"  - {p}" for p in known_paths)
                return (
                    f"\n\n⚠️ 路径校验提示：你使用的路径 \"{arg_path}\" 不在当前会话的已知图片路径中。\n"
                    f"当前会话的图片路径：\n{known_list}\n"
                    f"请使用上述已知路径重新调用工具。不要自己构造或猜测路径。"
                )

        return None

    async def chat_with_tools(
        self,
        messages: List[Dict],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> AsyncGenerator[Step, None]:
        """带工具调用的对话（流式），使用 function calling，逐token yield

        Args:
            messages: 对话消息列表
            provider: LLM 提供商（默认 settings.AGENT_LLM_PROVIDER）
            model: 模型名称（默认 settings.AGENT_LLM_MODEL）
            reasoning_effort: 深度思考强度（默认 settings.AGENT_LLM_REASONING_EFFORT）
        """
        MAX_TOOL_ROUNDS = 25
        TOOL_LOOP_TIMEOUT = 120  # 整体工具循环超时（秒）
        actual_provider = provider or settings.AGENT_LLM_PROVIDER
        actual_model = model or settings.AGENT_LLM_MODEL
        actual_reasoning = reasoning_effort if reasoning_effort is not None else settings.AGENT_LLM_REASONING_EFFORT

        # 0. 检查 Agent 状态
        if self.agent_profile.status == "pending":
            yield Step(step_type="error", content=f"Agent #{self.agent_hash} 尚未初始化，请先通过控制台初始化")
            return
        if self.agent_profile.status == "suspended":
            yield Step(step_type="error", content=f"Agent #{self.agent_hash} 已被暂停")
            return

        # 1. 检查是否需要压缩（跳过内部调用，防止递归）
        total_tokens = self._estimate_tokens(messages)
        if total_tokens > CONTEXT_LIMIT and not self._skip_compact:
            messages = await self._compact_context(messages)

        # 2. SmartRouter 阶段（仅在首次调用且 sr_enabled 时执行）
        decision = RouteDecision()
        _vec_task = None
        last_text = messages[-1]["content"] if messages else ""

        # 并行启动向量搜索（与 SR 同时进行，SR 关闭时也运行）
        try:
            from services.vector_search_service import VectorSearchService
            _vs = VectorSearchService(agent_hash=self.agent_hash)
            if getattr(self, '_skip_smart_router', False) or not getattr(self, 'agent_profile', None) or not self.agent_profile.sr_enabled:
                # SR disabled: use quality search with high threshold
                _vec_task = asyncio.create_task(_vs.search_public_with_quality(
                    last_text, top_k=5, agent_hash=self.agent_hash, min_score=0.5))
            else:
                # SR enabled: use quality search (SR will evaluate relevance)
                _vec_task = asyncio.create_task(_vs.search_public_with_quality(
                    last_text, top_k=5, agent_hash=self.agent_hash, min_score=0.0))
        except Exception as e:
            logger.warning(f"[AgentExecutor] VectorSearch init failed: {e}")

        # 每次 SR 检查前从 DB 刷新 profile，确保使用最新的 sr_enabled 设置
        self._refresh_agent_profile()
        if not self._skip_smart_router and self.agent_profile.sr_enabled:
            # 提取 image_info（由上游注入的图片预识别描述）
            image_info = None
            for msg in reversed(messages):
                c = msg.get("content", "")
                if isinstance(c, str) and "_image_description" in c:
                    image_info = {"has_image": True, "description": c}
                    break

            # 收集人设信息供 SR direct_reply 参考
            _persona_parts = []
            try:
                p = self.agent_profile
                if p.name:
                    _persona_parts.append(f"名称：{p.name}")
                if p.description:
                    _persona_parts.append(f"简介：{p.description}")
                # 读取灵魂设定（带缓存，减少 VFS I/O）
                if self._cached_persona is None:
                    self._cached_persona = self._read_vfs_file("/workspace/agent/soul.md")
                _soul = self._cached_persona
                if _soul:
                    _persona_parts.append(f"人格：{_soul[:500]}")
            except Exception as e:
                logger.debug(f"[AgentExecutor] Failed to read soul.md for persona: {e}")
            _persona = "\n".join(_persona_parts) if _persona_parts else None

            try:
                router = SmartRouter()
                _sr_t0 = time.time()
                decision = await router.route(last_text, context=messages[-6:],
                                              image_info=image_info, persona=_persona)
                logger.info(f"[PERF] SmartRouter: {time.time()-_sr_t0:.1f}s → thinking={decision.thinking} prefetch={len(decision.prefetch or [])}")
            except Exception as e:
                logger.warning(f"[AgentExecutor] SmartRouter failed: {e}")
                decision = RouteDecision()

        # 2a. L0 直接回复（不走主模型）
        if decision.direct_reply:
            logger.warning(f"[AgentExecutor] SR direct_reply used: {decision.direct_reply[:100]}...")
            if _vec_task:
                _vec_task.cancel()
            yield Step(step_type="final", content=decision.direct_reply)
            return

        # 2a2. 缓冲消息（由 SR 实时生成）
        if decision.buffer_msg:
            yield Step(step_type="thinking", content=decision.buffer_msg)
        elif decision.thinking or decision.prefetch:
            # 容错：SR 没生成缓冲消息时的后备
            yield Step(step_type="thinking", content="让我想想...🤔")

        # Schedule typing indicator refresh after SR buffer (1s delay)
        if decision.buffer_msg and hasattr(self, '_typing_callback') and self._typing_callback:
            async def _delayed_typing():
                await asyncio.sleep(1.0)
                try:
                    await self._typing_callback()
                except Exception:
                    pass
            asyncio.create_task(_delayed_typing())

        # 复制消息列表，避免修改原始消息
        working_messages = messages.copy()
        tool_definitions = self._get_tool_definitions()

        # 熔断器：同一工具连续错误/循环检测
        consecutive_errors = 0
        same_tool_count: Dict[str, int] = {}

        # 2b. 预取工具执行
        prefetch_results = []
        if decision.prefetch:
            async def _run_one(cmd: dict) -> Optional[str]:
                tool_name = cmd.get("tool", "")
                query = cmd.get("query", "")
                args = cmd.get("args", {})
                if tool_name in ("web_search", "file_read", "file_list", "knowledge_search") and query:
                    try:
                        # 将 tool/query 之外的所有字段作为额外参数
                        extra_args = {k: v for k, v in cmd.items() if k not in ("tool", "query")}
                        result = await self.execute_tool(tool_name, {"query": query, **args, **extra_args})
                        logger.info(f"[AgentExecutor] Prefetch {tool_name}({query[:50]}) OK ({len(result)} chars)")
                        return result
                    except Exception as e:
                        logger.warning(f"[AgentExecutor] Prefetch {tool_name} failed: {e}")
                elif tool_name:
                    logger.warning(f"[AgentExecutor] Prefetch unknown tool: {tool_name}")
                return None

            _tasks = [_run_one(cmd) for cmd in decision.prefetch]
            _done = await asyncio.gather(*_tasks)
            prefetch_results = [r for r in _done if r is not None]

        # 2c. 注射规则（追加到 system 消息，确保主模型在 prompt 层面接收）
        if decision.inject_rules:
            rules_text = "\n".join(f"- {r}" for r in decision.inject_rules)
            _rule_block = f"\n\n【人设注入】\n{rules_text}"
            _found_system = False
            for _i, _msg in enumerate(working_messages):
                if _msg.get("role") == "system":
                    _msg["content"] = _msg["content"] + _rule_block
                    _found_system = True
                    break
            if not _found_system:
                working_messages.insert(0, {"role": "system", "content": _rule_block.strip()})

        # 2d. 注射预取数据（标记为低优先级参考信息，避免误导主模型）
        if prefetch_results:
            working_messages.append({
                "role": "user",
                "content": "[系统预取数据-仅供参考]\n⚠️ 以下为预取数据，仅供参考。如果与用户消息中的实际路径/内容不一致，以用户消息为准。\n" + "\n---\n".join(prefetch_results[:3]) + "\n[/系统预取数据]"
            })

        # 2d2. 注射向量搜索结果（等并行向量搜索完成，超时 3s）
        if _vec_task:
            _vec_results = []
            try:
                _vec_results = await asyncio.wait_for(_vec_task, timeout=4.0)
            except (asyncio.TimeoutError, Exception):
                pass
            if _vec_results:
                _kb_text = self._build_knowledge_injection(_vec_results)
                if _kb_text:
                    for _i in range(len(working_messages) - 1, -1, -1):
                        if working_messages[_i]["role"] == "user":
                            _user_msg = working_messages[_i].copy()
                            _user_msg["content"] = _user_msg["content"] + f"\n\n【相关知识库】\n{_kb_text}"
                            working_messages[_i] = _user_msg
                            break

        # 2e. 控制 thinking 强度
        if decision.thinking:
            actual_reasoning = "high"
            logger.info("[AgentExecutor] SmartRouter: thinking=high (SR override)")

        _loop_start = time.time()
        for round_num in range(MAX_TOOL_ROUNDS):
            # 整体超时检查
            if time.time() - _loop_start > TOOL_LOOP_TIMEOUT:
                yield Step(step_type="final", content="抱歉，工具执行超时，请简化请求后重试。")
                return

            # 如果是第二轮及以后（即工具执行后），先输出分隔空行
            if round_num > 0:
                yield Step(step_type="token", content="\n\n")

            # 使用流式调用 LLM
            tool_calls = None
            full_content = ""
            _llm_start = time.time()

            try:
                async for event in llm_service.chat_with_tools_stream(
                    messages=working_messages,
                    provider=actual_provider,
                    model=actual_model,
                    tools=tool_definitions,
                    reasoning_effort=actual_reasoning,
                ):
                    if event["type"] == "token":
                        # 流式输出文本片段
                        full_content += event["content"]
                        yield Step(step_type="token", content=event["content"])

                    elif event["type"] == "done":
                        # 流结束，获取完整信息
                        tool_calls = event.get("tool_calls")
                        full_content = event.get("content", full_content)
            except Exception as e:
                logger.error(f"[AgentExecutor] LLM API 调用失败: {e}", exc_info=True)
                yield Step(step_type="error", content=f"LLM API 调用失败: {e}")
                return

            logger.info(f"[PERF] LLM call round {round_num}: {time.time()-_llm_start:.1f}s, tool_calls={len(tool_calls) if tool_calls else 0}")

            if not tool_calls:
                # 没有工具调用，流式输出已完成，返回
                return

            # 有工具调用，执行工具
            # 先添加 assistant 消息（带 tool_calls 和 reasoning_content）
            assistant_msg = {
                "role": "assistant",
                "content": full_content if full_content else None,
                "tool_calls": tool_calls
            }
            # DeepSeek 要求：如果 response 包含 reasoning_content，后续请求必须传回
            reasoning_content = event.get("reasoning_content", "")
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            working_messages.append(assistant_msg)

            # 发送工具调用步骤（简洁提示）
            if tool_calls:
                func_names = [tc["function"]["name"] for tc in tool_calls]
                yield Step(
                    step_type="tool_call",
                    content=f"🔧 执行工具: {', '.join(func_names)}",
                    tool_name=func_names[0] if len(func_names) == 1 else None,
                    tool_args={}
                )

            # 执行每个工具调用，添加工具结果
            for tc in tool_calls:
                func_name = tc["function"]["name"]
                args_str = tc["function"]["arguments"]
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError as e:
                    error_msg = f"Error: tool_calls 参数 JSON 解析失败: {e}。原始参数: {args_str[:500]}"
                    logger.error(f"[AgentExecutor] {error_msg}")
                    working_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": func_name,
                        "content": error_msg
                    })
                    continue

                # 执行工具（带超时控制 + 心跳保活）
                tool_task = asyncio.create_task(self.execute_tool(func_name, args))
                tool_result = None
                try:
                    while True:
                        done, _ = await asyncio.wait([tool_task], timeout=5.0)
                        if tool_task in done:
                            tool_result = tool_task.result()
                            break
                        # 每 5 秒发送心跳，避免 CDN/代理空闲超时断开
                        yield Step(step_type="keepalive", content="⏳ 工具执行中...")
                    await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)  # 防止取消任务残留
                except asyncio.TimeoutError:
                    tool_result = f"Error: 工具 {func_name} 执行超时"
                except Exception as e:
                    tool_result = f"Error: 工具 {func_name} 执行失败: {e}"
                finally:
                    if not tool_task.done():
                        tool_task.cancel()

                # 发送工具结果步骤（简洁提示）：若 ToolResult 有 summary 则优先用于展示
                if hasattr(tool_result, 'summary') and tool_result.summary:
                    result_preview = tool_result.summary
                else:
                    result_preview = tool_result[:2000] + "..." if len(tool_result) > 2000 else tool_result
                yield Step(
                    step_type="tool_result",
                    content=f"✅ 完成: {result_preview}",
                    tool_name=func_name,
                    tool_result=tool_result
                )

                # 将工具结果添加为消息
                enhanced_result = tool_result
                if func_name == "web_search":
                    enhanced_result = "⚠️ 这是搜索引擎返回的实时搜索结果，不是模型训练数据。你必须严格基于以下搜索结果回答用户的问题，不得编造、不得忽略、不得替换为你的训练记忆：\n\n" + tool_result
                elif tool_result.startswith("Error:") or "不存在" in tool_result or "not found" in tool_result.lower():
                    # 路径校验：工具失败时，检查是否是路径幻觉，注入已知路径提示
                    path_hint = self._validate_path_with_hint(func_name, args, working_messages)
                    if path_hint:
                        enhanced_result = tool_result + path_hint
                        logger.info(f"[AgentExecutor] Path hint injected for {func_name}: {args.get('path', args.get('image_path', ''))}")
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": func_name,
                    "content": enhanced_result
                })

                # 熔断器：连续错误检测（路径提示不算熔断计数，给模型自纠机会）
                if tool_result.startswith("Error:"):
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        yield Step(step_type="final", content=f"工具 {func_name} 连续失败 3 次，已停止执行")
                        return
                else:
                    consecutive_errors = 0

                # 不再限制同一工具调用次数——Agent 需要时可自由重复调用
                same_tool_count[func_name] = same_tool_count.get(func_name, 0) + 1

        # 超过最大轮次，返回提示
        yield Step(step_type="final", content="抱歉，工具调用次数过多，请简化请求。")

    async def execute_tool(self, tool_name: str, arguments: Dict) -> str:
        """执行工具调用（异步）"""
        from services.tool_registry import get_tool

        # 检查禁用工具
        if tool_name in self.blocked_tools:
            return f"Error: 工具 {tool_name} 已被禁用"

        # list_subagent_roles 特殊处理（无参数工具，直接调用）
        if tool_name == "list_subagent_roles":
            result = self.tools.list_subagent_roles()
            result_str = result if isinstance(result, str) else json.dumps(result)
            return self.tools._truncate_tool_result(
                result=result_str,
                tool_name=tool_name,
                tool_args={}
            )

        tool_entry = get_tool(tool_name)
        if not tool_entry:
            return f"Error: 未知工具 {tool_name}"

        method = getattr(self.tools, tool_name, None)
        if not method:
            return f"Error: 工具 {tool_name} 在 AgentToolsService 上不可用"

        try:
            # 过滤参数：只传 tool_entry 中声明的参数
            valid_args = {}
            import inspect
            for key in tool_entry["param_names"]:
                if key in arguments:
                    valid_args[key] = arguments[key]

            # 异步工具直接 await，同步工具在线程池执行（避免阻塞事件循环）
            loop = asyncio.get_event_loop()
            if inspect.iscoroutinefunction(method):
                result = await method(**valid_args)
            else:
                result = await loop.run_in_executor(None, lambda: method(**valid_args))

            # 处理返回的 coroutine（如 spawn_subagent 的 ThreadPoolExecutor 封装）
            if inspect.iscoroutine(result):
                result = await result

            result_str = result if isinstance(result, str) else json.dumps(result)

            # P0-Tool-Result-Budget: 截断超大工具结果
            result_str = self.tools._truncate_tool_result(
                result=result_str,
                tool_name=tool_name,
                tool_args=valid_args
            )

            return result_str
        except Exception as e:
            return f"Error: {e}"

    def _estimate_tokens(self, messages: List[Dict]) -> int:
        """估算消息的 token 数"""
        return llm_service.estimate_tokens(messages)

    async def _compact_context(self, messages: List[Dict]) -> List[Dict]:
        """压缩上下文：使用 MessageCompactor 保留最近 N 轮 + 历史摘要"""
        if not messages:
            return messages

        compactor = MessageCompactor(max_recent=20, compression_ratio=0.3)
        compressed = await compactor.compact(messages, agent=self)

        return compressed

    def _get_tool_definitions(self) -> List[Dict]:
        """返回工具定义列表（从 TOOL_REGISTRY 自动生成），排除禁用的工具"""
        from services.tool_registry import get_tool_schemas
        schemas = get_tool_schemas()
        if self.blocked_tools:
            schemas = [
                s for s in schemas
                if s["function"]["name"] not in self.blocked_tools
            ]
        return schemas

    @staticmethod
    def _smart_crop(text: str, max_head: int = 400, max_tail: int = 300) -> str:
        """智能头尾裁剪：短文本全文保留，长文本取头+尾"""
        if len(text) <= 600:
            return text
        # 尽量在换行处断开
        head_end = text.rfind("\n", 0, max_head)
        if head_end < max_head // 2:
            head_end = max_head
        tail_start = text.find("\n", len(text) - max_tail)
        if tail_start == -1 or tail_start > len(text) - max_tail // 2:
            tail_start = len(text) - max_tail
        return (
            f"{text[:head_end]}\n"
            f"……（中间省略，可使用 knowledge_get 工具查看全文）……\n"
            f"{text[tail_start:]}"
        )

    @staticmethod
    def _build_knowledge_injection(vec_results: list) -> str:
        """构建向量搜索结果的注入文本

        按来源分组（公共知识库 / 私有知识库 / 对话记忆），
        对每条结果应用智能裁剪，并附上 key 供工具调用。
        """
        # 按来源分组
        # search_public_with_quality 返回的 source 值:
        # "textbook", "gaokao", "math_trends", "knowledge_base", "conversation_memory"
        # 教材/高考/考向数据归入公共知识库
        PUBLIC_SOURCES = {"textbook", "gaokao", "math_trends", "public_knowledge"}

        sections = {
            "public_knowledge": [],
            "knowledge_base": [],
            "conversation_memory": [],
        }
        for r in vec_results[:5]:
            score = r.get("score", 0)
            if score <= 0.3:
                continue
            source = r.get("source", "knowledge_base")
            key = r.get("key", "")
            text = r.get("metadata", {}).get("text", "")
            if not text:
                continue
            section_key = "public_knowledge" if source in PUBLIC_SOURCES else source
            if section_key in sections:
                sections[section_key].append((key, score, text))

        # 来源→库映射（对应 knowledge_get 的 source 参数值）
        source_to_lib = {
            "public_knowledge": "public",
            "knowledge_base": "agent",
            "conversation_memory": "agent",
        }

        # 构建注入文本
        section_labels = [
            ("public_knowledge", "📖 公共知识库"),
            ("knowledge_base", "📚 私有知识库"),
            ("conversation_memory", "【相关历史会话】"),
        ]
        inject_parts = []
        for key_name, label in section_labels:
            items = sections[key_name]
            if not items:
                continue
            lib = source_to_lib.get(key_name, "public")
            lines = [f"{label}"]
            for key, score, text in items:
                cropped = AgentExecutor._smart_crop(text)
                lines.append(f"  key: {key} | 相似度: {score:.2f} | source: {lib}")
                lines.append(f"  {cropped}")
            inject_parts.append("\n".join(lines))

        return "\n\n".join(inject_parts)
