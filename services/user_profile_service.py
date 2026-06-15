"""
User Profile Service — 用户画像后台提取器

基于 ECNUClaw 的 5 维学习者画像理论框架，结合布鲁姆分类、ZPD、自我效能、
归因理论、ICAP、Kort 学习螺旋等教育心理学理论。

参考 Session Memory 架构：
- 阈值判断：消息数 / 关键词达到阈值时触发提取
- 提取执行：用受限工具集调用 LLM，读完现有 USER.md 后写入更新
- 非阻塞：通过 asyncio.create_task 在后台运行，不影响主对话流
"""

import asyncio
import json
import logging
import re
import time
from typing import List, Dict, Optional, Any

from config import settings
from services._format_conversation import format_conversation
from services.tool_registry import get_tool_schemas
from services.virtual_filesystem import VirtualFileSystem

USER_PROFILE_FILE_PATH = "/workspace/agent/USER.md"
MAX_USER_PROFILE_SIZE = 50 * 1024  # 50KB


def _user_profile_tool_filter(name: str, args: dict) -> bool:
    """User Profile 工具权限：只允许读写 /workspace/agent/USER.md"""
    if name in ("file_read", "cat"):
        path = args.get("path", "")
        return path == "/workspace/agent/USER.md"
    if name in ("file_write", "write"):
        path = args.get("path", "")
        return path == "/workspace/agent/USER.md"
    return False

logger = logging.getLogger(__name__)


# ── 阈值配置 ──────────────────────────────────────────────

class UserProfileConfig:
    MIN_MESSAGES_TO_INIT = 3               # 至少 N 轮对话后开始首次提取
    MIN_MESSAGES_BETWEEN_UPDATES = 8       # 每 N 轮新消息触发一次更新（比 SessionMemory 的 3 更稀疏）
    MIN_SECONDS_BETWEEN_UPDATES = 120      # 2 分钟冷却期，防止频繁更新

    # 特殊激活关键词（检测到立即触发）
    STRONG_SIGNAL_KEYWORDS = {
        "learning": ["听不懂", "明白了", "终于懂了", "还是不会", "做对了"],
        "interest": ["我最喜欢", "我对...感兴趣", "我想学"],
        "info": ["我是", "我选了", "我今年"],
    }


# ── 提取提示词 ────────────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = """你是当前 Agent 的「用户观察助手」。你的任务是从对话中捕捉关于这位学生的信号，更新 Agent 对学生的认知档案。

# 你的工作方式
你是一个后台分析器，分析结束后通过 file_write() 工具写入 /workspace/agent/USER.md。
你正在分析学生和 Agent 之间的对话。如果 USER.md 中已有信息，先用 file_read() 读取再合并。

# 对话类型判断（第一步）
开始分析前，先判断对话类型：
- **学习型**：涉及知识学习、解题、概念理解等 → 全量分析（认知 + 情感 + 学习行为 + 自适应建议）
- **闲聊型**：问候、日常聊天、非学习内容 → 只记兴趣/性格/沟通风格。如果没有有价值的观察信号，就不要写任何东西，直接返回
- **简单查询**：用户问"帮我查一下XX资料"等 → 简单记录兴趣方向，不做深度学习画像分析
- **混合型**：消息级拆分处理

# 信号提取框架（基于教育学理论）

## 适用于所有类型的信号
- 个人信息（用户主动补充的年级、选科、兴趣等）
- 沟通风格（追问型/接受型/反抗型）
- 兴趣领域
- 性格观察

## 仅适用于学习型对话的信号

### 认知诊断
- 当前布鲁姆层次（记忆/理解/应用/分析/评价/创造）及置信度：基于对话语义推断，注明依据
- 布鲁姆层次变化趋势（进步↑/稳定→/退步↓）
- ZPD 感知：学生当前能独立做什么，在帮助下能做什么
- SOLO 层次（前结构/单点/多点/关联/扩展抽象）：学生回答问题时展现的理解深度
- 知识追踪：列出提及的知识点，评估掌握度（0-1）+ 置信度（高/中/低）
  - 较高置信度（学生连续答对/表现出清晰的推理）vs 较低置信度（仅一次提及）
  - 不要因为学生一次答错就标记为"薄弱"——可能是粗心

### 情感与动机诊断
- 自我效能感水平（高/中/低）+ 变化趋势（↑/→/↓）。列出观察依据（具体的学生说辞）
- 当前情感状态 + 处于 Kort 学习螺旋的哪个位置（好奇心→尝试→困惑→挫折→洞察→成就）
- 归因风格倾向：健康（可控归因）/ 不健康（不可控归因）/混合
- 成就目标导向：掌握-趋近/掌握-回避/表现-趋近/表现-回避（如可判断）

### 学习行为与元认知
- ICAP 参与模式（被动P/主动A/建构C/互动I）及变化趋势
- 思维模式观察：固定/成长/混合（附依据）
- 元认知水平：低/中/高（附典型表现）
- 学习策略偏好：引导式/探索式/协作式/独立式

### 自适应策略建议
综合以上分析，为当前 Agent 提供 2-3 条具体建议：
- 当前需要更多鼓励还是更多挑战？
- 是否需要降低/提升讲解的抽象程度？
- 下一阶段的关注点是什么？

# 重要规则
1. 每个结论都要**注明依据**—引用学生说过的话或观察到的行为
2. 标注置信度（高/中/低）—特别是当你只凭一次观测就下结论时
3. 趋势比绝对值重要：学生受挫后重新尝试，标记为"积极方向"
4. 旧信息如果不再适用，标注「(可能已过时)」而非直接删除
5. 关于知识点掌握度：如果只有一次接触，暂标记为 confidence 低，标注"有待观察"
6. 允许不确定："无法确定"是合法的结论
7. 核心原则：不要为了分析而分析。如果是闲聊或简单查询，没有有价值的观察，就不更新 USER.md，直接返回

# 输出格式
先分析对话，再调用 file_write 写入。格式为 Markdown，需包含以下章节（按实际内容取舍）：
- # 用户名
- ## 个人信息
- ## Agent 观察
- ## 学习画像（仅学习型对话）
  - ### 认知
  - ### 情感与动机
  - ### 学习行为
  - ### 自适应建议"""


# ── Service ───────────────────────────────────────────────

class UserProfileService:
    """用户画像提取服务 — 轻量级 SubAgent"""

    def __init__(self, agent_hash: str):
        self.agent_hash = agent_hash

    # ── 读写 ──────────────────────────────────────────────

    def get(self, user_id: str = None) -> Optional[str]:
        """读取当前 USER.md 内容"""
        vfs = VirtualFileSystem(agent_hash=self.agent_hash)
        content = vfs.cat(USER_PROFILE_FILE_PATH)
        if content and not content.startswith("Error: 文件不存在") and not content.startswith("Error:"):
            return content
        return None

    def save(self, content: str) -> str:
        """写入更新后的 USER.md"""
        if not content or not content.strip():
            return "Error: 内容不能为空"
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_USER_PROFILE_SIZE:
            return f"Error: 内容过大 ({len(content_bytes)} bytes)，上限为 {MAX_USER_PROFILE_SIZE} bytes"
        vfs = VirtualFileSystem(agent_hash=self.agent_hash)
        return vfs.echo(content, USER_PROFILE_FILE_PATH, append=False)

    # ── 阈值判断 ──────────────────────────────────────────

    @staticmethod
    def should_extract(
        messages: List[Dict],
        is_initialized: bool,
        last_extract_msg_index: int = 0,
        last_extract_time: float = 0,
        tool_calls_since: int = 0,
    ) -> Dict[str, Any]:
        """
        判断是否应触发用户画像提取。

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

        # 1. 检测特殊信号关键词
        recent_text = ""
        if messages:
            for msg in messages[-5:]:
                content = msg.get("content", "")
                if isinstance(content, str):
                    recent_text += content + " "

        for category, keywords in UserProfileConfig.STRONG_SIGNAL_KEYWORDS.items():
            for kw in keywords:
                if kw in recent_text:
                    return {
                        "should_extract": True,
                        "reason": f"特殊信号触发: 关键词 '{kw}' (类别: {category})",
                        "new_msg_count": new_msg_count,
                        "initialized": is_initialized,
                    }

        # 2. 未初始化：检查消息数是否达到首次提取阈值
        if not is_initialized:
            if current_msg_count >= UserProfileConfig.MIN_MESSAGES_TO_INIT:
                return {
                    "should_extract": True,
                    "reason": f"首次提取：消息数 {current_msg_count} >= {UserProfileConfig.MIN_MESSAGES_TO_INIT}",
                    "new_msg_count": current_msg_count,
                    "initialized": False,
                }
            return {
                "should_extract": False,
                "reason": f"消息不足：{current_msg_count} < {UserProfileConfig.MIN_MESSAGES_TO_INIT}",
                "new_msg_count": current_msg_count,
                "initialized": False,
            }

        # 3. 已初始化：检查冷却期
        if last_extract_time > 0:
            elapsed = time.time() - last_extract_time
            if elapsed < UserProfileConfig.MIN_SECONDS_BETWEEN_UPDATES:
                return {
                    "should_extract": False,
                    "reason": f"冷却期：距上次提取仅 {elapsed:.0f}s < {UserProfileConfig.MIN_SECONDS_BETWEEN_UPDATES}s",
                    "new_msg_count": new_msg_count,
                    "initialized": True,
                }

        # 4. 已初始化：检查新消息数
        if new_msg_count >= UserProfileConfig.MIN_MESSAGES_BETWEEN_UPDATES:
            return {
                "should_extract": True,
                "reason": f"增量更新：新消息 {new_msg_count} >= {UserProfileConfig.MIN_MESSAGES_BETWEEN_UPDATES}",
                "new_msg_count": new_msg_count,
                "initialized": True,
            }

        return {
            "should_extract": False,
            "reason": f"增量不足：新消息 {new_msg_count}，冷却检查已通过",
            "new_msg_count": new_msg_count,
            "initialized": True,
        }

    # ── 画像文件是否已存在 ────────────────────────────────

    def is_profile_initialized(self) -> bool:
        """检查 USER.md 是否已有画像内容（非空且有实际内容）"""
        vfs = VirtualFileSystem(agent_hash=self.agent_hash)
        content = vfs.cat(USER_PROFILE_FILE_PATH)
        if content and not content.startswith("Error: 文件不存在") and not content.startswith("Error:"):
            # 进一步检查是否有实质内容（不只是标题行）
            lines = [l.strip() for l in content.split('\n') if l.strip() and not l.startswith('#')]
            if len(lines) > 1:  # 至少有一些非标题内容
                return True
        return False

    # ── 提取执行 ──────────────────────────────────────────

    async def extract(self, messages: List[Dict]) -> bool:
        """
        执行用户画像提取。

        Args:
            messages: 对话消息列表 [{"role": ..., "content": ...}, ...]

        Returns:
            True if extraction succeeded, False otherwise
        """
        t0 = time.time()

        try:
            async with asyncio.timeout(60):
                return await self._extract_impl(messages, t0)
        except asyncio.TimeoutError:
            elapsed = time.time() - t0
            logger.error("[UserProfile] Extraction timeout after %.1fs", elapsed)
            return False
        except Exception as e:
            elapsed = time.time() - t0
            logger.error("[UserProfile] Extraction failed in %.1fs: %s", elapsed, e, exc_info=True)
            return False

    async def _extract_impl(self, messages: List[Dict], t0: float) -> bool:
        """extract 的实际实现（在 timeout 上下文中运行）"""
        from services.llm_service import llm_service

        vfs = VirtualFileSystem(agent_hash=self.agent_hash)

        # 1. 读取现有画像
        current_profile = vfs.cat(USER_PROFILE_FILE_PATH)

        # 2. 构建对话文本（取最后 15 条消息，比 session_memory 多因为需要更多上下文来分析学习信号）
        recent_messages = messages[-15:] if len(messages) > 15 else messages
        conversation_text = format_conversation(recent_messages, user_label="用户", assistant_label="助手")

        # 3. 构建完整消息
        system_prompt = EXTRACTION_SYSTEM_PROMPT
        if current_profile and not current_profile.startswith("Error:"):
            system_prompt += f"\n\n## 现有用户画像\n{current_profile}"
        else:
            system_prompt += "\n\n## 现有用户画像\n(空 — 这是首次提取)"

        extraction_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"## 近期对话\n\n{conversation_text}\n\n请按照操作步骤读取现有 USER.md 后更新用户画像。"},
        ]

        # 4. 调用 LLM（带受限工具集），最多 3 轮
        max_rounds = 3
        for round_num in range(max_rounds):
            response = await llm_service.chat_with_tools(
                messages=extraction_messages,
                tools=get_tool_schemas(),
                request_type="user_profile",
                tool_filter=_user_profile_tool_filter,
            )

            tool_calls = response.get("tool_calls")
            content = response.get("content", "")

            if not tool_calls:
                # 无工具调用 → LLM 可能认为无需更新（闲聊/简单查询场景）
                logger.info(
                    "[UserProfile] LLM returned no tool_calls in round %d (likely no update needed), content preview: %s",
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

                if func_name in ("file_read", "cat"):
                    result = vfs.cat(USER_PROFILE_FILE_PATH)
                elif func_name in ("file_write", "write"):
                    profile_content = args.get("content", "")
                    if not profile_content or not profile_content.strip():
                        result = "Error: 内容不能为空"
                    else:
                        content_bytes = profile_content.encode("utf-8")
                        if len(content_bytes) > MAX_USER_PROFILE_SIZE:
                            result = f"Error: 内容过大 ({len(content_bytes)} bytes)，上限为 {MAX_USER_PROFILE_SIZE} bytes"
                        else:
                            result = vfs.echo(profile_content, USER_PROFILE_FILE_PATH, append=False)
                    logger.info(
                        "[UserProfile] Write result: %s",
                        result[:100] if len(result) > 100 else result
                    )
                else:
                    result = f"Error: 未知工具 {func_name}"

                tool_results.append((tc, func_name, result))

            # 统一添加 assistant 消息
            extraction_messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls,
            })

            # 再添加 tool 结果消息
            for tc, func_name, result in tool_results:
                extraction_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": func_name,
                    "content": result,
                })

                if func_name in ("file_write", "write"):
                    elapsed = time.time() - t0
                    logger.info(
                        "[UserProfile] Extraction complete in %.1fs (rounds=%d)",
                        elapsed, round_num + 1
                    )
                    return True

        elapsed = time.time() - t0
        logger.info("[UserProfile] Extraction finished in %.1fs (no write performed)", elapsed)
        return False

    # ── SR 注入 ───────────────────────────────────────────

    @staticmethod
    def has_learning_profile(profile_text: str) -> bool:
        """检查 USER.md 中是否有学习画像内容"""
        if not profile_text:
            return False
        # 检查是否有 # 学习画像 或 ## 学习画像 等标题
        return bool(re.search(r'^#{1,3}\s*学习画像', profile_text, re.MULTILINE))

    @staticmethod
    def build_injection(profile_text: str) -> str:
        """从 USER.md 内容中提取学习画像摘要，生成注入文本（供 SR 使用）"""
        if not profile_text:
            return ""

        lines = profile_text.split('\n')
        parts = []

        # 提取个人信息摘要
        in_personal = False
        personal_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('## 个人信息'):
                in_personal = True
                continue
            if in_personal and stripped.startswith('## '):
                in_personal = False
            if in_personal and stripped.startswith('- '):
                personal_lines.append(stripped)

        if personal_lines:
            parts.append("【用户档案摘要】")
            # 最多取 5 条个人信息
            for pl in personal_lines[:5]:
                parts.append(pl)

        # 提取学习画像摘要
        in_learning = False
        in_subsection = False
        summary_lines = []
        for line in lines:
            stripped = line.strip()
            if re.search(r'^#{1,3}\s*学习画像', stripped):
                in_learning = True
                continue
            if in_learning and stripped.startswith('#') and not stripped.startswith('####'):
                in_learning = False
            if in_learning:
                # 收集子标题和要点
                if stripped.startswith('### '):
                    in_subsection = True
                    summary_lines.append(f"\n{stripped}")
                elif stripped.startswith('#### '):
                    summary_lines.append(f"  {stripped}")
                elif stripped.startswith('- ') and len(summary_lines) < 20:
                    summary_lines.append(stripped)
                elif stripped.startswith('> '):
                    summary_lines.append(stripped)

        if summary_lines:
            if not parts:
                parts.append("【用户档案摘要】")
            parts.extend(summary_lines[:20])

        # 提取自适应建议
        in_adaptive = False
        adaptive_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('### 自适应建议'):
                in_adaptive = True
                continue
            if in_adaptive and stripped.startswith('#'):
                in_adaptive = False
            if in_adaptive and stripped.startswith('- '):
                adaptive_lines.append(stripped)

        if adaptive_lines:
            parts.append("\n【自适应教学建议】")
            for al in adaptive_lines[:5]:
                parts.append(al)

        if not parts:
            return ""

        return "\n".join(parts)
