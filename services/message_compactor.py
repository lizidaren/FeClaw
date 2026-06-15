"""
消息压缩器（Message Compactor）
使用 LLM 对长对话历史进行摘要压缩，保留关键信息同时减少 token 消耗。

功能：
1. pre_compact_memory_save() - 压缩前要求 Agent 保存重要上下文到工作目录（基于文件 mtime 检测）
2. compact() - 执行对话压缩

压缩管道（借鉴 Claude Code L2-L4）：
- L2 历史剪切：删除重复包装器、旧跨度信息
- L3 微压缩：双路径压缩（冷路径高强度压缩，热路径保留细节）
- L4 上下文崩溃：投影折叠多个消息为摘要
- LLM 摘要：最终压缩步骤
"""

import json
import math
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any, List, Optional

from services.llm_service import llm_service

logger = logging.getLogger(__name__)


def estimate_tokens(text) -> int:
    """估算 token 数量（中英文混合场景）。

    中文每个字符约 1.5 tokens，英文约 4 chars/token。
    接受 str 或 List[Dict]，后者会先序列化为 JSON 再估算。
    """
    if isinstance(text, list):
        text = json.dumps(text, ensure_ascii=False)
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if unicodedata.east_asian_width(c) in ('W', 'F'))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars * 1.5 + other_chars / 4 + 0.5)

# 摘要模型 — 现在由 settings.AGENT_LLM_MODEL 动态决定，不再需要默认常量

# L2-L4 压缩参数默认值
DEFAULT_COLD_THRESHOLD = 50  # 冷路径阈值：超过此数量的消息进入冷路径
DEFAULT_HOT_WINDOW = 20     # 热路径窗口：最近 N 条消息保留更多细节
DEFAULT_SHEAR_AGGRESSIVE = False  # 剪切激进程度


class MessageCompactor:
    """
    对话历史压缩器。

    算法：
    1. 保留最近 max_recent 条消息（不压缩）
    2. 对更早的消息按时间顺序分成若干组
    3. 每组用 LLM 提取关键信息，生成摘要
    4. 组装：最近消息 + 摘要压缩块

    压缩质量优先，摘要包含：
    - 用户意图和目标
    - 已完成的工作
    - 关键工具调用结果
    - 当前状态
    """

    def __init__(
        self,
        max_recent: int = 20,
        max_tokens: int = 80000,
        compression_ratio: float = 0.3,
        summary_provider: str = None,
        summary_model: str = None,
        cold_threshold: int = DEFAULT_COLD_THRESHOLD,
        hot_window: int = DEFAULT_HOT_WINDOW,
        shear_aggressive: bool = DEFAULT_SHEAR_AGGRESSIVE,
    ):
        """
        Args:
            max_recent: 保留最近 N 条消息不压缩（决定保留多少，不触发压缩）
            max_tokens: 触发压缩的上下文 token 上限（默认 80k）
            compression_ratio: 历史消息压缩后保留的比例（0.3 = 压缩到 30%）
            summary_provider: 摘要使用的 LLM 提供商
            summary_model: 摘要使用的模型
            cold_threshold: L3 冷路径阈值，超过此数量的消息进入冷路径压缩
            hot_window: L3 热路径窗口，最近 N 条消息保留更多细节
            shear_aggressive: L2 剪切激进程度，激进模式下删除更多信息
        """
        self.max_recent = max_recent
        self.max_tokens = max_tokens
        self.compression_ratio = compression_ratio
        from config import settings
        from services.model_registry import resolve
        self.summary_model = summary_model or settings.MAIN_TEXT_MODEL
        self.summary_provider = summary_provider or resolve(self.summary_model)["provider"]
        self.cold_threshold = cold_threshold
        self.hot_window = hot_window
        self.shear_aggressive = shear_aggressive

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """估算消息列表的总 token 数（粗略估算）"""
        total = 0
        for msg in messages:
            content = msg.get('content', '')
            if isinstance(content, str):
                total += estimate_tokens(content)
            elif isinstance(content, list):
                # 多模态消息（图片+文本），图片估算为 1000 tokens
                for item in content:
                    if item.get('type') == 'image':
                        total += 1000
                    elif item.get('type') == 'text':
                        total += estimate_tokens(item.get('text', ''))
        return int(total)

    # ========== L2-L4 压缩管道 ==========

    def l2_shear(self, messages: list[dict]) -> list[dict]:
        """Public API: L2 shear compaction."""
        return self._l2_shear(messages)

    def _l2_shear(self, messages: list[dict]) -> list[dict]:
        """
        L2 历史剪切（History Shear）

        删除重复包装器和旧跨度信息：
        1. 删除重复的 system 消息（如多个"可用工具"提示、"上下文压缩"标记）
        2. 删除重复的工具调用格式包装（保留工具名和结果，删除格式包装）
        3. 删除过时的 timestamp 信息（激进模式下）
        4. 删除已处理的 tool_call_id 信息

        Args:
            messages: 消息列表

        Returns:
            剪切后的消息列表
        """
        if not messages:
            return messages

        logger.info(f"[L2 Shear] 开始剪切，原始消息数: {len(messages)}")

        result = []
        seen_system_contents = set()  # 用于检测重复的 system 消息
        tool_call_ids_seen = set()    # 用于跟踪已处理的 tool_call_id

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # 处理 system 消息：删除重复的包装器
            if role == "system":
                # 检测是否是重复的包装器消息
                if self._is_repeated_wrapper(content, seen_system_contents):
                    logger.debug(f"[L2 Shear] 删除重复 system 包装器: {content[:50]}...")
                    continue
                seen_system_contents.add(self._get_wrapper_key(content))

            # 处理 tool 消息：删除已处理的 tool_call_id
            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                if tool_call_id and tool_call_id in tool_call_ids_seen:
                    logger.debug(f"[L2 Shear] 删除已处理的 tool_call_id: {tool_call_id}")
                    continue
                if tool_call_id:
                    tool_call_ids_seen.add(tool_call_id)

            # 清理消息中的冗余字段
            cleaned_msg = self._clean_message_fields(msg)

            result.append(cleaned_msg)

        logger.info(f"[L2 Shear] 剪切完成，结果消息数: {len(result)}, 减少: {len(messages) - len(result)}")
        return result

    def _is_repeated_wrapper(self, content: str, seen: set) -> bool:
        """
        检测是否是重复的包装器消息。

        包装器消息特征：
        - 包含"可用工具"或"Available tools"
        - 包含"上下文压缩"标记
        - 包含重复的工具列表格式
        """
        if not isinstance(content, str):
            return False

        # 提取包装器关键特征
        wrapper_patterns = [
            r"可用工具[:：]",
            r"Available tools[:：]",
            r"【上下文压缩】",
            r"【历史摘要】",
            r"工具列表[:：]",
        ]

        for pattern in wrapper_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                # 使用内容前 100 字符作为 key 检测重复
                key = content[:100]
                return key in seen

        return False

    def _get_wrapper_key(self, content: str) -> str:
        """获取包装器消息的唯一标识 key"""
        if not isinstance(content, str):
            return ""
        # 对于包装器消息，使用前 100 字符作为 key
        # 这样可以检测相似但不完全相同的重复包装器
        return content[:100]

    def _clean_message_fields(self, msg: dict) -> dict:
        """
        清理消息中的冗余字段。

        激进模式下删除更多字段。
        """
        cleaned = dict(msg)

        # 基础清理：删除 tool_call_id（已处理的信息）
        if "tool_call_id" in cleaned and self.shear_aggressive:
            del cleaned["tool_call_id"]

        # 激进模式：删除 timestamp
        if self.shear_aggressive and "timestamp" in cleaned:
            del cleaned["timestamp"]

        # 清理过长的 content（截断重复的工具列表）
        if isinstance(cleaned.get("content"), str):
            cleaned["content"] = self._clean_repeated_tool_list(cleaned["content"])

        return cleaned

    def _clean_repeated_tool_list(self, content: str) -> str:
        """
        清理内容中重复的工具列表格式包装。

        保留工具名和结果，删除格式包装。
        """
        if not isinstance(content, str):
            return content

        # 删除重复的工具调用格式包装（如 "调用工具 xxx..."）
        patterns_to_remove = [
            r"\n*工具调用[:：].*?\n",
            r"\n*Tool call[:：].*?\n",
        ]

        for pattern in patterns_to_remove:
            content = re.sub(pattern, "", content, flags=re.IGNORECASE)

        return content.strip()

    def l3_micro_compact(self, messages: list[dict]) -> list[dict]:
        """Public API: L3 micro-compaction."""
        return self._l3_micro_compact(messages)

    def _l3_micro_compact(self, messages: list[dict]) -> list[dict]:
        """
        L3 微压缩（Micro-Compact）

        双路径压缩策略：
        - 时间冷路径：超过 cold_threshold 的消息，高强度压缩（只保留关键摘要）
        - 缓存热路径：在 hot_window 内的消息，保留更多细节

        Args:
            messages: 消息列表

        Returns:
            微压缩后的消息列表
        """
        if not messages:
            return messages

        logger.info(f"[L3 Micro-Compact] 开始微压缩，消息数: {len(messages)}, "
                    f"冷阈值: {self.cold_threshold}, 热窗口: {self.hot_window}")

        # 分离 system 消息（始终保留）
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # 判断是否需要冷路径压缩
        if len(non_system) <= self.cold_threshold:
            logger.info(f"[L3 Micro-Compact] 消息数未超过冷阈值，跳过冷路径压缩")
            return messages

        # 分离冷路径和热路径
        hot_start = len(non_system) - self.hot_window
        cold_msgs = non_system[:hot_start]
        hot_msgs = non_system[hot_start:]

        logger.info(f"[L3 Micro-Compact] 冷路径消息: {len(cold_msgs)}, 热路径消息: {len(hot_msgs)}")

        # 对冷路径消息进行高强度压缩
        cold_compressed = self._compress_cold_path(cold_msgs)

        # 热路径消息保留更多细节（只做基础清理）
        hot_cleaned = [self._light_clean_message(m) for m in hot_msgs]

        # 组装结果
        result = system_msgs + cold_compressed + hot_cleaned

        logger.info(f"[L3 Micro-Compact] 微压缩完成，结果数: {len(result)}, 减少: {len(messages) - len(result)}")
        return result

    def _compress_cold_path(self, messages: list[dict]) -> list[dict]:
        """
        对冷路径消息进行高强度压缩。

        只保留关键信息：
        - 用户意图（user 消息的关键内容）
        - 工具调用结果（tool 消息的关键输出）
        - 删除 assistant 消息中的详细推理过程
        """
        if not messages:
            return messages

        compressed = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                # 用户消息：保留关键意图，截断过长内容
                compressed_msg = {
                    "role": "user",
                    "content": self._extract_user_intent(content)
                }
                compressed.append(compressed_msg)

            elif role == "assistant":
                # assistant 消息：只保留结论，删除详细推理
                compressed_msg = {
                    "role": "assistant",
                    "content": self._extract_assistant_summary(content)
                }
                # 只有有意义的内容才保留
                if compressed_msg["content"]:
                    compressed.append(compressed_msg)

            elif role == "tool":
                # tool 消息：保留关键结果，截断过长输出
                compressed_msg = {
                    "role": "tool",
                    "content": self._extract_tool_result(content),
                    "name": msg.get("name", ""),
                    "tool_call_id": msg.get("tool_call_id", "")
                }
                compressed.append(compressed_msg)

            else:
                # 其他类型消息保留
                compressed.append(msg)

        return compressed

    def _extract_user_intent(self, content: str) -> str:
        """提取用户消息的关键意图"""
        if not isinstance(content, str):
            return str(content) if content else ""

        # 提取图片路径标注（关键元数据，不可丢失）
        image_path_matches = self._extract_image_paths(content)

        # 截断到 200 字符，保留核心意图
        if len(content) <= 200:
            result = content
        else:
            # 提取前 150 字符 + "...[意图摘要]"
            result = content[:150] + "...[意图摘要]"

        # 如果原始消息包含图片路径但截断后丢失了，追加回来
        if image_path_matches:
            for p in image_path_matches:
                if p not in result:
                    # 在截断消息末尾追加图片路径信息
                    truncated = result.rstrip()
                    if truncated.endswith("...[意图摘要]"):
                        result = truncated[:-7] + f"（图片路径: {p})...[意图摘要]"
                    else:
                        result = truncated + f"（图片路径: {p})"

        return result

    @staticmethod
    def _extract_image_paths(content: str) -> List[str]:
        """从内容中提取图片 VFS 路径"""
        import re
        paths = []
        seen = set()
        patterns = [
            r'图片路径[：:]\s*(/workspace/images/\S+?\.(?:png|jpg|jpeg|gif|webp|bmp))',
            r'已保存到"?(/workspace/images/\S+?\.(?:png|jpg|jpeg|gif|webp|bmp))"?',
            r'已保存到VFS路径"?(/workspace/images/\S+?\.(?:png|jpg|jpeg|gif|webp|bmp))"?',
        ]
        for pat in patterns:
            for m in re.finditer(pat, content):
                p = m.group(1)
                if p not in seen:
                    seen.add(p)
                    paths.append(p)
        return paths

    def _extract_assistant_summary(self, content: str) -> str:
        """提取 assistant 消息的关键结论"""
        if not isinstance(content, str):
            return str(content) if content else ""

        # 删除详细推理过程，只保留结论
        # 查找结论性语句（如"结论"、"结果"、"完成"等）
        conclusion_patterns = [
            r"结论[:：].*",
            r"结果[:：].*",
            r"完成[:：].*",
            r"总结[:：].*",
        ]

        conclusions = []
        for pattern in conclusion_patterns:
            matches = re.findall(pattern, content)
            conclusions.extend(matches)

        if conclusions:
            # 如果找到结论，只保留结论部分
            return "【结论】" + "; ".join(conclusions[:3])  # 最多 3 个结论

        # 没有明确结论时，截断到 100 字符
        if len(content) <= 100:
            return content

        return content[:80] + "...[摘要]"

    def _extract_tool_result(self, content: str) -> str:
        """提取工具结果的关键信息"""
        if not isinstance(content, str):
            return str(content) if content else ""

        # 工具结果截断到 300 字符
        if len(content) <= 300:
            return content

        return content[:250] + "...[结果摘要]"

    def _light_clean_message(self, msg: dict) -> dict:
        """轻度清理消息（热路径使用）"""
        cleaned = dict(msg)

        # 只删除明显的冗余字段
        if "timestamp" in cleaned:
            del cleaned["timestamp"]

        return cleaned

    def l4_context_crash(self, messages: list[dict]) -> list[dict]:
        """Public API: L4 context-crash compaction."""
        return self._l4_context_crash(messages)

    def _l4_context_crash(self, messages: list[dict]) -> list[dict]:
        """
        L4 上下文崩溃（Context Crash）

        破坏性投影折叠：
        - 将多个连续的消息折叠成一个摘要
        - 保留关键信息：用户意图、工具调用、结果
        - 使用投影而非删除：关键信息投影到摘要中，次要信息折叠

        Args:
            messages: 消息列表

        Returns:
            折叠后的消息列表
        """
        if not messages:
            return messages

        logger.info(f"[L4 Context Crash] 开始上下文崩溃，消息数: {len(messages)}")

        # 分离 system 消息
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # 如果消息太少，不需要崩溃
        if len(non_system) <= 10:
            logger.info(f"[L4 Context Crash] 消息数太少，跳过崩溃")
            return messages

        # 将消息分组并折叠
        # 每组包含一个完整的对话轮次（user + assistant + tool calls）
        groups = self._group_messages_for_crash(non_system)

        logger.info(f"[L4 Context Crash] 分为 {len(groups)} 个崩溃组")

        # 对每个组生成投影摘要
        crashed = []
        for group in groups:
            summary = self._crash_group_to_summary(group)
            crashed.append(summary)

        # 组装结果
        result = system_msgs + crashed

        logger.info(f"[L4 Context Crash] 崩溃完成，结果数: {len(result)}, 减少: {len(messages) - len(result)}")
        return result

    def _group_messages_for_crash(self, messages: list[dict]) -> list[list[dict]]:
        """
        将消息分组用于崩溃。

        分组策略：
        - 每组包含一个完整的对话轮次
        - user 消息作为新组的开始
        - 后续的 assistant 和 tool 消息归入同一组
        """
        groups = []
        current_group = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "user":
                # 新对话轮次开始
                if current_group:
                    groups.append(current_group)
                current_group = [msg]
            else:
                # assistant 或 tool 消息归入当前组
                current_group.append(msg)

        # 添加最后一组
        if current_group:
            groups.append(current_group)

        return groups

    def _crash_group_to_summary(self, group: list[dict]) -> dict:
        """
        将一个消息组崩溃为摘要。

        投影关键信息：
        - 用户意图
        - 工具调用名称
        - 工具调用结果（关键部分）
        - 图片路径（关键元数据）
        """
        if not group:
            return {"role": "system", "content": "【空消息组】"}

        # 提取关键信息
        user_intent = ""
        tool_calls = []
        tool_results = []
        assistant_summary = ""
        all_image_paths = []  # 收集该组中所有图片路径

        for msg in group:
            role = msg.get("role", "")
            content = msg.get("content", "")
            tool_name = msg.get("name", "")

            if role == "user":
                user_intent = self._extract_user_intent(content)
                # 收集图片路径
                img_paths = self._extract_image_paths(content)
                for p in img_paths:
                    if p not in all_image_paths:
                        all_image_paths.append(p)

            elif role == "tool":
                if tool_name:
                    tool_calls.append(tool_name)
                tool_results.append(self._extract_tool_result(content))

            elif role == "assistant":
                assistant_summary = self._extract_assistant_summary(content)

        # 构建摘要内容
        summary_parts = []

        if user_intent:
            summary_parts.append(f"用户: {user_intent}")

        if all_image_paths:
            summary_parts.append(f"图片: {', '.join(all_image_paths)}")

        if tool_calls:
            summary_parts.append(f"工具: {', '.join(tool_calls)}")

        if tool_results:
            # 只保留关键结果
            key_results = [r[:100] for r in tool_results if r][:2]
            if key_results:
                summary_parts.append(f"结果: {'; '.join(key_results)}")

        if assistant_summary:
            summary_parts.append(f"助手: {assistant_summary}")

        summary_content = " | ".join(summary_parts)

        return {
            "role": "system",
            "content": f"【对话摘要】{summary_content}"
        }

    async def pre_compact_memory_save(
        self,
        messages: list[dict],
        agent: Any = None,
        workspace_root: Optional[str] = None
    ) -> dict:
        """
        在压缩前，要求 Agent 保存重要上下文到工作目录。

        通过向 Agent 发送内部消息，要求其保存关键上下文。
        使用 Agent 响应文本检测来判断是否已保存。

        Args:
            messages: 当前的完整消息列表
            agent: Agent 实例（需实现 chat_with_tools 方法）
                   如果为 None，则跳过预保存步骤（简单截断模式）
            workspace_root: 工作目录路径（已弃用，保留参数兼容性）

        Returns:
            {"saved": True/False, "summary": "..."}
        """
        logger.info("[MessageCompactor] Running pre_compact_memory_save...")

        if agent is None:
            logger.info("[MessageCompactor] No agent provided, skipping pre-compact save")
            return {"saved": False, "summary": "no_agent"}

        try:
            # 构建预压缩指令
            instruction = f"""【上下文压缩预警】

对话历史即将被压缩以释放上下文空间。请在压缩前保存重要信息到工作目录。

**请立即使用 `file_write` 工具保存以下内容到 workspace/**：
1. 当前任务的进展和状态
2. 已完成的重要决策
3. 需要跨对话保留的工具调用结果（如文件内容、配置信息等）

**使用方式**：
```
file_write(path="workspace/context_backup.md", content="要保存的内容")
```

可以追加写入保存不同类别的内容。保存完成后回复"已保存"。"""

            # 向 Agent 发送内部消息要求保存上下文
            internal_messages = list(messages)
            internal_messages.append({
                "role": "user",
                "content": instruction
            })

            logger.info("[MessageCompactor] Sending memory save request to agent...")
            response_text = ""

            try:
                # 设置 skip_compact 标志，防止递归调用 compact
                original_skip_compact = getattr(agent, '_skip_compact', False)
                agent._skip_compact = True

                # 调用 Agent（使用 AgentExecutor.chat_with_tools，LLM 直接调用兜底）
                if hasattr(agent, 'chat_with_tools'):
                    async for chunk in agent.chat_with_tools(internal_messages):
                        if hasattr(chunk, 'content'):
                            response_text += chunk.content
                        elif isinstance(chunk, str):
                            response_text += chunk
                else:
                    async for chunk in llm_service.chat(
                        messages=internal_messages,
                        provider=self.summary_provider,
                        model=self.summary_model,
                        stream=False
                    ):
                        response_text += chunk
            except Exception as e:
                logger.warning(f"[MessageCompactor] Agent memory save failed: {e}")
                return {"saved": False, "summary": str(e)}
            finally:
                if hasattr(agent, '_skip_compact'):
                    agent._skip_compact = original_skip_compact

            response_text = response_text.strip()

            # 响应中包含"已保存"确认
            if "已保存" in response_text or "保存成功" in response_text:
                logger.info("[MessageCompactor] Agent confirmed memory saved via text")
                return {"saved": True, "summary": response_text[:200]}

            # 响应中包含 "OK"（兼容旧逻辑）
            if "OK" in response_text.upper() and len(response_text) < 200:
                logger.info("[MessageCompactor] Agent confirmed memory saved (legacy OK)")
                return {"saved": True, "summary": response_text}

            # 未检测到保存确认
            logger.info(f"[MessageCompactor] Agent response: {response_text[:100]}... (no save confirmation)")
            return {"saved": False, "summary": response_text[:200]}

        except Exception as e:
            logger.error(f"[MessageCompactor] pre_compact_memory_save error: {e}")
            return {"saved": False, "summary": str(e)}

    async def pre_compact_async(
        self,
        messages: list[dict],
        agent: Any = None,
        workspace_root: Optional[str] = None
    ) -> dict:
        """
        异步版本的预压缩记忆保存。
        在压缩前要求 Agent 保存重要上下文到工作目录。

        Args:
            messages: 当前消息列表
            agent: Agent 实例
            workspace_root: 工作目录路径（可选）

        Returns:
            {"saved": True/False, "summary": "..."}
        """
        return await self.pre_compact_memory_save(messages, agent, workspace_root)

    async def compact(self, messages: list[dict], agent: Any = None, workspace_root: Optional[str] = None) -> list[dict]:
        """
        压缩消息列表（异步版本）。

        压缩管道（L2->L3->L4->LLM 摘要）：
        1. 检查是否需要压缩
        2. 预压缩记忆保存：要求 Agent 保存上下文 + 文件 mtime 检测
        3. L2 历史剪切：删除重复包装器、旧跨度信息
        4. L3 微压缩：双路径压缩（冷路径高强度压缩，热路径保留细节）
        5. L4 上下文崩溃：投影折叠多个消息为摘要
        6. LLM 摘要：最终压缩步骤
        7. 组装结果

        Args:
            messages: [{"role": "user"|"assistant"|"system"|"tool", "content": "...", ...}, ...]
            agent: Agent 实例（可选，用于预压缩记忆保存）
                   如果为 None 或 pre_compact 失败，使用简单截断
            workspace_root: 工作目录路径（可选，用于文件 mtime 检测）

        Returns:
            压缩后的消息列表：[摘要块, ..., 最近消息...]
        """
        if not messages:
            return messages

        total_tokens = self._estimate_tokens(messages)
        original_count = len(messages)

        logger.info(f"[Compact] 开始压缩，原始消息数: {original_count}, 估算 tokens: {total_tokens}")

        # Token 太少，不需要压缩
        if total_tokens <= self.max_tokens:
            logger.info(f"[Compact] Token 数未超过阈值 ({self.max_tokens}), 跳过压缩")
            return messages

        # 0. 尝试预压缩记忆保存（要求 Agent 保存重要上下文）
        pre_save_result = {"saved": False, "summary": "no_agent"}
        if agent is not None:
            try:
                pre_save_result = await self.pre_compact_memory_save(messages, agent, workspace_root)
            except Exception as e:
                logger.warning(f"[MessageCompactor] pre_compact_memory_save failed: {e}")
                pre_save_result = {"saved": False, "summary": str(e)}

        logger.info(f"[MessageCompactor] pre_compact: saved={pre_save_result.get('saved')}, "
                    f"summary={str(pre_save_result.get('summary', ''))[:100]}")

        # ========== L2-L4 压缩管道 ==========

        # L2 历史剪切：删除重复包装器
        messages = self.l2_shear(messages)
        after_l2_count = len(messages)
        logger.info(f"[Compact] L2 剪切后: {after_l2_count} 条消息")

        # L3 微压缩：双路径压缩
        messages = self.l3_micro_compact(messages)
        after_l3_count = len(messages)
        logger.info(f"[Compact] L3 微压缩后: {after_l3_count} 条消息")

        # 重新估算 token 数，如果已经足够小，跳过 L4 和 LLM 摘要
        current_tokens = self._estimate_tokens(messages)
        if current_tokens <= self.max_tokens:
            logger.info(f"[Compact] L2-L3 压缩后 tokens 已足够 ({current_tokens}), 跳过 L4 和 LLM 摘要")
            return messages

        # L4 上下文崩溃：投影折叠
        messages = self.l4_context_crash(messages)
        after_l4_count = len(messages)
        logger.info(f"[Compact] L4 崩溃后: {after_l4_count} 条消息")

        # 再次检查 token 数
        current_tokens = self._estimate_tokens(messages)
        if current_tokens <= self.max_tokens:
            logger.info(f"[Compact] L2-L4 压缩后 tokens 已足够 ({current_tokens}), 跳过 LLM 摘要")
            return messages

        # ========== LLM 摘要（最终压缩步骤） ==========

        # 1. 分离 system 消息（始终保留在最前面）
        system_msgs = [m for m in messages if m.get("role") == "system"]
        history = [m for m in messages if m.get("role") != "system"]

        # 2. 保留最近消息
        recent = history[-self.max_recent:]
        older = history[:-self.max_recent]

        if not older:
            # 没有需要压缩的历史
            logger.info(f"[Compact] 无历史消息需要 LLM 摘要")
            return messages

        # 3. 对更早的消息分组
        target_compressed_count = max(1, int(len(older) * self.compression_ratio))
        num_groups = max(1, min(target_compressed_count, len(older) // 3))
        group_size = math.ceil(len(older) / num_groups)

        groups = []
        for i in range(0, len(older), group_size):
            groups.append(older[i:i + group_size])

        logger.info(f"[Compact] LLM 摘要: {len(older)} 条历史消息分为 {len(groups)} 组")

        # 4. 对每组进行摘要（串行，await）
        summaries = []
        for group in groups:
            try:
                summary = await self._summarize_group_async(group)
                summaries.append(summary)
            except Exception as e:
                logger.warning(f"[MessageCompactor] 摘要失败: {e}，保留原始消息")
                for msg in group:
                    summaries.append(msg)

        # 5. 组装结果：system + 摘要块 + 最近消息
        result: list[dict] = []
        result.extend(system_msgs)

        # 添加压缩说明
        if older:
            result.append({
                "role": "system",
                "content": (
                    f"【上下文压缩】原始 {original_count} 条消息 -> "
                    f"L2剪切 {after_l2_count} -> L3压缩 {after_l3_count} -> "
                    f"L4崩溃 {after_l4_count} -> LLM摘要 {len(summaries)} + 最近 {len(recent)}"
                )
            })

        result.extend(summaries)
        result.extend(recent)

        final_count = len(result)
        logger.info(f"[Compact] 压缩完成: {original_count} -> {final_count}, "
                    f"压缩率: {(original_count - final_count) / original_count * 100:.1f}%")

        return result

    def _summarize_group_sync(self, group: list[dict]) -> dict:
        """
        同步调用 LLM 对一组消息进行摘要。
        内部通过 asyncio.run 在同步上下文中运行异步代码。
        """
        import asyncio

        try:
            # Python 3.10+ 兼容：直接尝试获取运行中的事件循环
            try:
                loop = asyncio.get_running_loop()
                # 如果能获取到运行中的循环，说明我们在异步上下文中
                # 使用 ThreadPoolExecutor 在新线程中运行
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        self._summarize_group_async(group)
                    )
                    return future.result(timeout=60)
            except RuntimeError:
                # 没有运行中的事件循环，可以直接用 asyncio.run()
                return asyncio.run(self._summarize_group_async(group))
        except Exception as e:
            logger.error(f"[MessageCompactor] LLM 摘要调用失败: {e}")
            raise

    async def _summarize_group_async(self, group: list[dict]) -> dict:
        """
        异步用 LLM 对一组消息进行摘要。
        """
        # 构建摘要提示
        prompt = self._build_summary_prompt(group)

        messages = [{"role": "user", "content": prompt}]
        summary_text = ""

        async for chunk in llm_service.chat(
            messages=messages,
            provider=self.summary_provider,
            model=self.summary_model,
            stream=False,  # 摘要不需要流式
            request_type="compact_summary"
        ):
            summary_text += chunk

        return {
            "role": "system",
            "content": f"【历史摘要】{summary_text.strip()}"
        }

    def _build_summary_prompt(self, group: list[dict]) -> str:
        """
        构建摘要提示词，引导 LLM 生成高质量摘要。
        """
        # 将消息格式化为文本
        lines = []
        for i, msg in enumerate(group):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            # 截断过长的内容
            if len(content) > 800:
                content = content[:800] + "...[已截断]"
            timestamp = msg.get("timestamp", "")
            tool_name = msg.get("name", "")
            lines.append(f"[{i+1}] [{role}]" +
                        (f"[{timestamp}]" if timestamp else "") +
                        (f"[tool:{tool_name}]" if tool_name else "") +
                        f": {content}")

        conversation_text = "\n".join(lines)

        return f"""你是一个对话历史摘要助手。请对下面的会话片段进行压缩摘要。

## 要求
提取并保留以下关键信息：
1. **用户意图和目标**：用户想要完成什么
2. **已完成的工作**：哪些任务已成功完成
3. **关键工具调用结果**：重要工具的输出（如文件内容、搜索结果等）
4. **当前状态**：当前在做什么、有什么未解决的问题
5. **重要决策或结论**：对话中达成的关键结论

## 格式要求
- 使用中文
- 简洁有条理，每条信息不超过 2-3 句话
- 不要逐条重复原始消息，要提炼要点
- 如果某轮对话无重要信息可跳过
- 总字数控制在 300 字以内

## 会话片段
{conversation_text}

## 摘要
"""
