"""
Reflection Service — 会话记忆事实核查

在 Session Memory 提取后异步对 Agent 回复进行事实核查，
通过 web_search 验证事实性陈述，发现错误后记录到 pending_correction，
由 chat_service 在下一轮对话中注入修正提示。

主要入口：
- check_session_memory(): 全量事实核查（供 SessionMemoryService 调用）
- 也可由 end_conversation 工具触发
"""

import json
import logging

from services._format_conversation import format_conversation

logger = logging.getLogger(__name__)


def _reflection_tool_filter(name: str, args: dict) -> bool:
    """Reflection 工具权限：只允许搜索"""
    return name == "web_search"


class ReflectionService:
    """对话事实核查服务"""

    def __init__(self, agent_hash: str):
        self.agent_hash = agent_hash

    # ── 会话记忆事实核查（供 ChatService / end_conversation 调用）────

    @staticmethod
    async def check_session_memory(
        messages: list,
        previous_correction: str = None,
    ) -> dict:
        """
        全量会话记忆事实核查（带 web search 验证）。

        通过 chat_with_tools 让 LLM 调用 web_search 工具进行搜索验证，
        循环最多 3 轮，最终输出核查报告。

        Args:
            messages: 全量对话消息（1-N）
            previous_correction: 上一轮修正提示文本（如果有）

        Returns:
            {has_errors, short_desc, topic, detail, checked_clean}
        """
        from services.llm_service import llm_service
        from services.tool_registry import get_tool_schemas
        from services.search_service import SearchService

        conversation_text = format_conversation(messages, user_label="学生", assistant_label="AI")
        system_prompt = ReflectionService._build_check_prompt(conversation_text, previous_correction)

        try:
            llm_messages = [{"role": "system", "content": system_prompt}]

            max_rounds = 4
            for round_num in range(max_rounds):
                response = await llm_service.chat_with_tools(
                    messages=llm_messages,
                    tools=get_tool_schemas(),
                    request_type="reflection_check",
                    tool_filter=_reflection_tool_filter,
                )

                tool_calls = response.get("tool_calls")
                content = response.get("content", "")

                if not tool_calls:
                    # 无工具调用 → 最终输出，解析 JSON
                    parsed = {}
                    if isinstance(content, dict):
                        parsed = content
                    elif isinstance(content, str):
                        try:
                            parsed = json.loads(content.strip())
                        except json.JSONDecodeError:
                            parsed = {}
                    return ReflectionService._normalize_check_result(parsed)

                # 执行搜索工具调用
                tool_results = []
                for tc in tool_calls:
                    func_name = tc.get("function", {}).get("name", "")
                    args_str = tc.get("function", {}).get("arguments", "{}")

                    try:
                        args = json.loads(args_str)
                    except json.JSONDecodeError:
                        args = {}

                    if func_name == "web_search":
                        query = args.get("query", "")
                        try:
                            result_text = await SearchService().search_qwen(str(query))
                            tool_results.append((tc, func_name, f"## 搜索: {query}\n{result_text[:3000]}"))
                        except Exception as e:
                            tool_results.append((tc, func_name, f"## 搜索: {query}\n搜索失败: {e}"))
                    else:
                        tool_results.append((tc, func_name, f"Error: 未知工具 {func_name}"))

                # 添加 assistant 和 tool 消息
                llm_messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls,
                })
                for tc, func_name, result in tool_results:
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "name": func_name,
                        "content": result,
                    })

            return ReflectionService._normalize_check_result({"has_errors": False})

        except Exception as e:
            logger.error("[Reflection] check_session_memory failed: %s", e, exc_info=True)
            return {"has_errors": False, "short_desc": "", "topic": "", "detail": "", "checked_clean": []}

    @staticmethod
    def _build_check_prompt(conversation_text: str, previous_correction: str = None) -> str:
        """构建事实核查提示词"""
        prompt_parts = [
            "你是一名严格的事实核查员。你的任务是对以下对话中 Agent 的回复进行事实核查。",
            "",
            "## 重要规则",
            "- **必须搜索核实**：对于任何事实性陈述（日期、时间、地点、数字、公式、事件等），先调用 web_search() 工具进行搜索验证，再下结论",
            "- **不要凭记忆判断**：即使你\u201c觉得\u201d某个答案不对，也必须搜索确认",
            "- **仅当搜索确认有误时才报告**：如果搜索结果与 Agent 回复一致，视为正确",
            "- 如果无法通过搜索获得明确结果，标记为\u201c无法确认\u201d，不报告为错误",
            "",
            "## 本轮对话内容",
            conversation_text,
        ]

        if previous_correction:
            prompt_parts.extend([
                "",
                "## 前一轮修正提示",
                f'系统之前给出过以下修正提示："{previous_correction}"',
                "请确认：",
                "a) 本轮对话中用户是否再次讨论了这个 topic？",
                "b) 如果是，Agent 这次的回复是否已经修正了该问题？",
                "c) 如果 Agent 仍然犯了同样的错误，请重新报告",
                "d) 如果 Agent 已正确回答或本轮未涉及，则忽略",
            ])

        prompt_parts.extend([
            "",
            "## 核查流程",
            "1. 逐条阅读对话消息，提取 Agent 回复中的事实性陈述",
            "2. 对每一个存疑的陈述，调用 web_search() 工具进行搜索验证",
            "3. 系统会执行搜索并把结果返回给你",
            "4. 综合分析搜索结果后，输出最终核查报告 JSON",
            "",
            "## 最终输出格式（完成所有搜索后，必须输出 JSON）",
            "如果发现错误：",
            "{",
            '    "has_errors": true,',
            '    "short_desc": "一针见血的简短描述，不超过50字",',
            '    "topic": "话题标签，如圆周运动",',
            '    "detail": "详细修正建议，包含搜索验证说明",',
            '    "checked_clean": []',
            "}",
            "",
            "如果没有发现任何事实错误：",
            "{",
            '    "has_errors": false,',
            '    "short_desc": "",',
            '    "topic": "",',
            '    "detail": "",',
            '    "checked_clean": []',
            "}",
        ])

        return "\n".join(prompt_parts)

    @staticmethod
    def _normalize_check_result(result: dict) -> dict:
        """规范化核查结果，确保字段完整"""
        return {
            "has_errors": bool(result.get("has_errors", False)),
            "short_desc": str(result.get("short_desc", ""))[:100],
            "topic": str(result.get("topic", "")),
            "detail": str(result.get("detail", "")),
            "checked_clean": result.get("checked_clean", [])
            if isinstance(result.get("checked_clean"), list)
            else [],
        }
