"""
Session Memory Service — 会话记忆后台提取器

参考 Claude Code v2.1.88 的 Session Memory 架构：
- 阈值判断：消息数 / 工具调用数达到阈值时触发提取
- 提取执行：用受限工具集调用 LLM，读完现有笔记后写入更新
- 非阻塞：通过 asyncio.create_task 在后台运行，不影响主对话流

V2 渠道隔离（设计文档 §6 Session Memory 渠道隔离）：
- Classic Agent: 同文件 (session_memory.md) 不同 Markdown 章节
  全文件加载不过滤，靠提示词告知当前渠道上下文
- 当前实现：统一返回 session_memory.md 路径（不做物理章节解析）
- 完整函数：build_session_memory_path(agent_hash, channel, group_id)
"""

import asyncio
import json
import logging
import time
from typing import List, Dict, Any, Optional

from config import settings
from services._format_conversation import format_conversation
from services.virtual_filesystem import VirtualFileSystem

MEMORY_FILE_PATH = "/workspace/agent/session_memory.md"

logger = logging.getLogger(__name__)


# ── V2 渠道路径构建 ─────────────────────────────────────────

def build_session_memory_path(
    agent_hash: str,
    channel: Optional[str] = None,
    group_id: Optional[str] = None,
) -> str:
    """
    V2 渠道隔离：构造 session_memory.md 路径。

    当前实现（简化版）：
    - 所有渠道统一返回 MEMORY_FILE_PATH（"session_memory.md"）
    - 文件内容是同一份，章节隔离靠提示词说明当前渠道
    - IM Agent 模式下不隔离（设计文档 §6）

    Args:
        agent_hash: Agent 标识
        channel: "wechat" / "api" / "web" / "group" / None
        group_id: 群组 ID（仅 channel="group" 时使用）

    Returns:
        VFS 路径（统一为 session_memory.md）
    """
    # V2 P0/P1: 不做物理章节拆分——同文件全读，靠 system prompt 提示
    return MEMORY_FILE_PATH


# ── 阈值配置 ──────────────────────────────────────────────

class SessionMemoryConfig:
    MIN_MESSAGES_TO_INIT = 3          # 至少 N 条消息后开始首次提取
    MIN_MESSAGES_BETWEEN_UPDATES = 3  # 每 N 轮新消息触发一次更新
    MIN_TOOL_CALLS_BETWEEN_UPDATES = 3  # 或每 N 次工具调用触发（预留）

    # 自然断点：最后一条助手回复未触发工具调用且
    # 新消息累积超过本阈值时也触发
    NATURAL_BREAK_MIN_MESSAGES = 2


# ── 提取提示词 ────────────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = """你是一个会话记忆助手。请分析以下对话，提取需要长期记住的关键信息，
更新到 session_memory.md 文件中。

## 提取原则
1. 用户偏好和说话风格（喜好、厌恶、语气偏好）
2. 重要决策及其理由
3. 正在进行中的任务和进展
4. 用户的修正和反馈（什么做对了、什么做错了）
5. 需要跨会话保留的上下文
6. **不记录分享链接、文件路径、临时 hash 等临时信息**

## 操作步骤
1. 先用 read 读取现有笔记（read /workspace/agent/session_memory.md）
2. 分析近期对话，提取关键信息
3. 用 write 写入合并后的完整笔记（write /workspace/agent/session_memory.md）
4. 用 file_list 检查 /workspace/agent/skills/ 目录中已有的技能文件
5. 如果对话中解决了可复用的复杂问题（如配置、模板、代码套路）：
   a. 用 file_list 列出 skills/ 目录，查看已有哪些技能
   b. 如果发现文件名**语义相似**（如"nginx-reverse-proxy" vs "nginx配置"），
      先用 read 读取已有技能的内容，确认是否重复
   c. 如果确实重复 → 跳过不写
   d. 如果部分重叠但不同 → 更新已有技能（追加或合并）
   e. 如果完全不重复 → 用 write 在 /workspace/agent/skills/{short_name}.md 创建新技能

## 输出要求
- 使用 session_memory_write 写入 /workspace/agent/session_memory.md
- 保持笔记结构化（按类别分节）
- 与现有笔记合并，不要保存重复信息
- 删除已明确过时的信息
- 用 Markdown 格式，简洁清晰

## 技能提取（新增）
如果对话中 Agent 解决了一个**可复用的复杂问题**（配置 Nginx、生成图表、调试技巧等），
请在 /workspace/agent/skills/{short_name}.md 写入一个技能文档。
创建前先用 session_memory_file_list 确认是否已存在同名技能。
技能文档格式：
```markdown
# 技能名称

## 用途
解决什么问题

## 步骤
清晰可复现的操作步骤

## 注意事项
边界条件和陷阱

## 示例
一个具体案例
```"""


# ── 蒸馏提示词 ────────────────────────────────────────────

DISTILL_SYSTEM_PROMPT = """你是一个记忆整理助手。请将下方"会话笔记"中的信息合并到"长期记忆"中。

合并原则：
1. 将会话笔记中有价值的信息合并到长期记忆对应的分类下
2. 避免重复：如果长期记忆中已有相同内容，跳过
3. 更新过时内容：如果会话笔记中的新信息覆盖了旧信息，替换之
4. 保持长期记忆的格式和风格
5. 会话笔记中不重要或已过时的内容不要合并

输出要求：
- 先用 file_read 读取 /workspace/agent/memory.md
- 再用 file_write 写入合并后的完整长期记忆内容"""


# ── 蒸馏触发间隔 ──────────────────────────────────────────

DISTILL_INTERVAL = 5  # 每 N 次提取后触发一次蒸馏

# 模块级提取计数器（按 agent_hash 分组）
_extraction_counts: Dict[str, int] = {}


class SessionMemoryService:
    """会话记忆提取服务 — 轻量级 SubAgent"""

    def __init__(self, agent_hash: str):
        self.agent_hash = agent_hash

    # ── 阈值判断 ──────────────────────────────────────────

    @staticmethod
    def should_extract(
        messages: List[Dict],
        is_initialized: bool,
        last_extract_msg_index: int = 0,
        tool_calls_since: int = 0,
    ) -> Dict[str, Any]:
        """
        判断是否应触发会话记忆提取。

        Returns:
            {
                "should_extract": bool,
                "reason": str,          # 触发原因（用于日志）
                "new_msg_count": int,   # 新增消息数
                "initialized": bool,    # 是否已初始化
            }
        """
        current_msg_count = len(messages)
        new_msg_count = current_msg_count - last_extract_msg_index

        # 未初始化：检查消息数是否达到首次提取阈值
        if not is_initialized:
            if current_msg_count >= SessionMemoryConfig.MIN_MESSAGES_TO_INIT:
                return {
                    "should_extract": True,
                    "reason": f"首次提取：消息数 {current_msg_count} >= {SessionMemoryConfig.MIN_MESSAGES_TO_INIT}",
                    "new_msg_count": current_msg_count,
                    "initialized": False,
                }
            return {
                "should_extract": False,
                "reason": f"消息不足：{current_msg_count} < {SessionMemoryConfig.MIN_MESSAGES_TO_INIT}",
                "new_msg_count": current_msg_count,
                "initialized": False,
            }

        # 已初始化：检查新消息数 + 工具调用数
        if new_msg_count >= SessionMemoryConfig.MIN_MESSAGES_BETWEEN_UPDATES:
            if tool_calls_since >= SessionMemoryConfig.MIN_TOOL_CALLS_BETWEEN_UPDATES:
                return {
                    "should_extract": True,
                    "reason": f"增量更新：新消息 {new_msg_count} >= {SessionMemoryConfig.MIN_MESSAGES_BETWEEN_UPDATES}，工具调用 {tool_calls_since} >= {SessionMemoryConfig.MIN_TOOL_CALLS_BETWEEN_UPDATES}",
                    "new_msg_count": new_msg_count,
                    "initialized": True,
                }

            # 自然断点：新消息数量达标时触发
            if new_msg_count >= SessionMemoryConfig.NATURAL_BREAK_MIN_MESSAGES:
                return {
                    "should_extract": True,
                    "reason": f"自然断点：最后助手回复无工具调用，新消息 {new_msg_count} >= {SessionMemoryConfig.NATURAL_BREAK_MIN_MESSAGES}",
                    "new_msg_count": new_msg_count,
                    "initialized": True,
                }

        return {
            "should_extract": False,
            "reason": f"增量不足：新消息 {new_msg_count}，工具调用 {tool_calls_since}",
            "new_msg_count": new_msg_count,
            "initialized": True,
        }

    # ── 记忆文件是否已存在 ────────────────────────────────

    def is_memory_initialized(self) -> bool:
        """检查 session_memory.md 是否已有内容"""
        vfs = VirtualFileSystem(agent_hash=self.agent_hash)
        content = vfs.cat(MEMORY_FILE_PATH)
        if content and not content.startswith("(空") and not content.startswith("Error"):
            return True
        return False

    # ── 提取执行 ──────────────────────────────────────────

    async def extract(self, messages: List[Dict]) -> bool:
        """
        执行会话记忆提取。

        Args:
            messages: 对话消息列表 [{"role": ..., "content": ...}, ...]

        Returns:
            True if extraction succeeded, False otherwise
        """
        t0 = time.time()
        success = False

        try:
            async with asyncio.timeout(60):
                success = await self._extract_impl(messages, t0)
        except asyncio.TimeoutError:
            elapsed = time.time() - t0
            logger.error("[SessionMemory] Extraction timeout after %.1fs", elapsed)
            return False
        except Exception as e:
            elapsed = time.time() - t0
            logger.error("[SessionMemory] Extraction failed in %.1fs: %s", elapsed, e, exc_info=True)
            return False

        return success

    async def _extract_impl(self, messages: List[Dict], t0: float) -> bool:
        """extract 的实际实现（在 timeout 上下文中运行）"""
        from services.llm_service import llm_service
        from services.tool_registry import get_tool_schemas

        vfs = VirtualFileSystem(agent_hash=self.agent_hash)

        def _session_memory_tool_filter(name: str, args: dict) -> bool:
            """Session Memory 工具权限过滤器"""
            if name in ("file_read", "cat", "read"):
                path = args.get("path", "")
                return path.startswith("/workspace/agent/")
            if name in ("file_write", "write"):
                path = args.get("path", "")
                allowed = [
                    "/workspace/agent/session_memory.md",
                    "/workspace/agent/memory.md",
                ]
                return path in allowed or path.startswith("/workspace/agent/skills/")
            if name in ("file_list", "ls"):
                return True  # 列表操作不限制路径
            return False  # 其他工具不可用

        # 1. 读取现有记忆
        current_memory = vfs.cat(MEMORY_FILE_PATH)
        if current_memory.startswith("Error:") and "文件不存在" in current_memory:
            current_memory = "(空 — 文件不存在)"
        elif current_memory.startswith("Error:"):
            current_memory = ""

        # 2. 构建对话文本（取最后 10 条消息）
        recent_messages = messages[-10:] if len(messages) > 10 else messages
        conversation_text = format_conversation(recent_messages, user_label="用户", assistant_label="助手")

        # 3. 构建完整消息
        system_prompt = EXTRACTION_SYSTEM_PROMPT
        if current_memory and not current_memory.startswith("(空"):
            system_prompt += f"\n\n## 现有会话笔记\n{current_memory}"
        else:
            system_prompt += "\n\n## 现有会话笔记\n(空 — 这是首次提取)"

        extraction_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"## 近期对话\n\n{conversation_text}\n\n请按照操作步骤读取现有笔记后更新 session_memory.md。"},
        ]

        # 4. 调用 LLM（用全局工具 schema + filter 限制权限），最多 3 轮
        max_rounds = 3
        for round_num in range(max_rounds):
            response = await llm_service.chat_with_tools(
                messages=extraction_messages,
                tools=get_tool_schemas(),
                request_type="session_memory",
                tool_filter=_session_memory_tool_filter,
            )

            tool_calls = response.get("tool_calls")
            content = response.get("content", "")

            if not tool_calls:
                logger.warning(
                    "[SessionMemory] LLM returned no tool_calls in round %d, content preview: %s",
                    round_num, content[:200]
                )
                break

            # 执行工具调用并收集结果
            tool_results = []
            for tc in tool_calls:
                func_name = tc.get("function", {}).get("name", "")
                args_str = tc.get("function", {}).get("arguments", "{}")

                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = {}

                if func_name in ("file_read", "cat", "read"):
                    path = args.get("path", MEMORY_FILE_PATH)
                    result = vfs.cat(path)
                elif func_name in ("file_write", "write"):
                    path = args.get("path", MEMORY_FILE_PATH)
                    mem_content = args.get("content", "")
                    result = vfs.echo(mem_content, path, append=False)
                    logger.info("[SessionMemory] Write result: %s", result[:100])
                elif func_name in ("file_list", "ls"):
                    directory = args.get("path", "/workspace/agent/")
                    try:
                        items = vfs.ls(directory)
                        result = "\n".join(items) if items else "(空目录)"
                    except Exception as e:
                        result = f"Error: 列出目录失败: {e}"
                else:
                    result = f"Error: 未知工具 {func_name}"

                tool_results.append((tc, func_name, result))

            # 统一添加 assistant 消息
            extraction_messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls,
            })

            # 添加 tool 结果消息
            for tc, func_name, result in tool_results:
                extraction_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": func_name,
                    "content": result,
                })

                if func_name in ("file_write", "write") and not result.startswith("Error"):
                    elapsed = time.time() - t0
                    logger.info(
                        "[SessionMemory] Extraction complete in %.1fs (rounds=%d)",
                        elapsed, round_num + 1
                    )
                    return True

        elapsed = time.time() - t0
        logger.warning("[SessionMemory] Extraction did not complete successfully in %.1fs", elapsed)
        return False

    # ── 蒸馏到长期记忆 ────────────────────────────────────

    async def maybe_distill_to_longterm(self, messages: List[Dict]) -> bool:
        """检查是否需要蒸馏，如需要则蒸馏"""
        # 递增提取计数器
        _extraction_counts[self.agent_hash] = _extraction_counts.get(self.agent_hash, 0) + 1
        count = _extraction_counts[self.agent_hash]

        if count % DISTILL_INTERVAL == 0:
            logger.info(
                "[SessionMemory] Triggering distill (extraction count=%d, interval=%d)",
                count, DISTILL_INTERVAL
            )
            return await self._do_distill(messages)

        logger.debug(
            "[SessionMemory] Skipping distill (extraction count=%d, interval=%d)",
            count, DISTILL_INTERVAL
        )
        return False

    async def _do_distill(self, messages: List[Dict]) -> bool:
        """执行蒸馏：读取会话笔记和长期记忆，合并后写入长期记忆"""
        try:
            from services.llm_service import llm_service
            from services.tool_registry import get_tool_schemas

            vfs = VirtualFileSystem(agent_hash=self.agent_hash)

            # 读取会话笔记
            session_memory_content = vfs.cat(MEMORY_FILE_PATH)
            if not session_memory_content or session_memory_content.startswith("(空"):
                logger.info("[SessionMemory] Distill skipped: session memory is empty")
                return False

            # 读取长期记忆
            memory_path = "/workspace/agent/memory.md"
            current_memory = vfs.cat(memory_path)
            if current_memory.startswith("Error:"):
                current_memory = ""

            def _distill_tool_filter(name: str, args: dict) -> bool:
                """蒸馏工具权限过滤器：只允许读写 memory.md 和 session_memory.md"""
                if name in ("file_read", "cat", "read"):
                    path = args.get("path", "")
                    return path in (MEMORY_FILE_PATH, memory_path)
                if name in ("file_write", "write"):
                    path = args.get("path", "")
                    return path in (MEMORY_FILE_PATH, memory_path)
                return False

            # 构建提示词
            distill_messages = [
                {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    "## 会话笔记（来源：/workspace/agent/session_memory.md）\n\n"
                    f"{session_memory_content}\n\n"
                    "## 现有长期记忆（/workspace/agent/memory.md）\n\n"
                    f"{current_memory if current_memory else '(空)'}\n\n"
                    "请先读取现有长期记忆文件，再将会话笔记中有价值的信息合并写入。"
                )},
            ]

            # 调用 LLM（最多 2 轮）
            try:
                async with asyncio.timeout(60):
                    for round_num in range(2):
                        response = await llm_service.chat_with_tools(
                            messages=distill_messages,
                            tools=get_tool_schemas(),
                            request_type="session_memory_distill",
                            tool_filter=_distill_tool_filter,
                        )

                        tool_calls = response.get("tool_calls")
                        content = response.get("content", "")

                        if not tool_calls:
                            logger.warning(
                                "[SessionMemory] Distill LLM returned no tool_calls in round %d",
                                round_num
                            )
                            break

                        # 收集工具结果
                        tool_results = []
                        for tc in tool_calls:
                            func_name = tc.get("function", {}).get("name", "")
                            args_str = tc.get("function", {}).get("arguments", "{}")
                            try:
                                args = json.loads(args_str)
                            except json.JSONDecodeError:
                                args = {}

                            if func_name in ("file_read", "cat", "read"):
                                path = args.get("path", memory_path)
                                result = vfs.cat(path)
                            elif func_name in ("file_write", "write"):
                                path = args.get("path", memory_path)
                                write_content = args.get("content", "")
                                result = vfs.echo(write_content, path, append=False)
                                logger.info("[SessionMemory] Distill write result: %s", result[:100])
                            else:
                                result = f"Error: 未知工具 {func_name}"

                            tool_results.append((tc, func_name, result))

                        # 统一添加 assistant 消息
                        distill_messages.append({
                            "role": "assistant",
                            "content": content or None,
                            "tool_calls": tool_calls,
                        })

                        # 添加 tool 结果
                        wrote = False
                        for tc, func_name, result in tool_results:
                            distill_messages.append({
                                "role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "name": func_name,
                                "content": result,
                            })
                            if func_name in ("file_write", "write") and not result.startswith("Error"):
                                wrote = True

                        if wrote:
                            logger.info("[SessionMemory] Distill completed successfully")
                            return True

                logger.warning("[SessionMemory] Distill did not complete successfully")
                return False

            except asyncio.TimeoutError:
                logger.error("[SessionMemory] Distill timeout")
                return False

        except Exception as e:
            logger.error("[SessionMemory] Distill error: %s", e, exc_info=True)
            return False
