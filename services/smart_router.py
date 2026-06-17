"""
智能路由层（SmartRouter）

在调主模型之前，用小模型做决策：是否需要深度思考（thinking）、
是否需要预取外部数据（prefetch）、是否可以直接回复（L0）、
以及需要注入给主模型的规则提示（inject_rules）。
"""

import asyncio
import json
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

from config import settings as _sr_settings

SR_TEXT_MODEL = _sr_settings.MAIN_TEXT_MODEL
SR_VL_MODEL = _sr_settings.MAIN_VISION_MODEL

SR_PROMPT = """你是AI系统的智能路由层，任务是在主模型处理用户请求前，提前做两件事：
1. 预取外部数据（避免主模型额外花一轮工具调用）
2. 判断是否需要深度思考

返回JSON：

{
  "thinking": true/false,
  "prefetch": [],
  "buffer_msg": null,
  "direct_reply": null,
  "inject_rules": []
}

字段说明：
- thinking: 需要深度思考/逻辑推理/数学计算/复杂决策时true
- prefetch: 提前获取数据的工具列表 [{"tool": "工具名", "query": "关键词"}]。
  knowledge_search 可以带 "index" 参数：{"tool": "knowledge_search", "query": "关键词", "index": "questions"}
  index 可选值：textbook | gaokao | questions | all。
  web_search关键词：使用自然语言完整问句，含时间/地点/具体维度。8-25字。
  用2-3条不同角度的搜索词覆盖全面。如 ["罗湖图书馆2026年入馆需不需要预约", "深圳图书馆现在入馆规定", "深圳图书馆预约规则最新"]
  ⚠️ file_read/file_list 的路径必须从用户消息中提取（如「图片路径:」标注的路径），禁止自己构造或猜测路径（如 current_image.png 等）。如果用户消息中没有明确路径标注，不要预取文件读取。
- buffer_msg: 等待主模型时的缓冲消息（自然、不重复、语气多样）
- direct_reply: 极简单的问题直接回复（不走主模型）
- inject_rules: 注入给主模型的规则提示，每条≤30字，最多3条。精确简洁，让主模型一看就懂
  用途示例：当并行知识搜索结果不相关时，提示主模型"忽略搜索结果，以实际工具调用为准"。

可用预取工具：
- web_search: 需要新闻/天气/股票/百科等实时信息时
- file_read: 需要读取已知路径的文件内容时
- file_list: 需要浏览目录结构时
- knowledge_search: 需要教材知识库/学科知识信息时。query用语义明确的自然语言问题，index指定学科(如chemistry/physics/biology)或留空
  可选 index 参数：textbook | gaokao | questions | all | 具体学科索引名。用于区分教材知识、高考真题、练习题库。
  index="questions" 触发：当用户要求"出题"、"练习题"、"考题"、"例题"、"刷题"、"找题"、"典型题"、"易错题"、"常考题"、"试卷"时。
  此时 query 应该加上"练习题"等后缀以确保搜索质量。

决策规则：
- 预取为了给主模型省工具调用轮次，能预取就预取
- thinking=true：问题涉及推理/分析/数学/代码/复杂决策
- 当thinking=true或prefetch有值时，应生成buffer_msg让用户知道在干什么
- buffer_msg要自然多样，每次不同，10-20字
- direct_reply：仅打招呼/感谢/用户只发图无文字时使用。此时直接回复，不走主模型。
  ❌ 不用于：涉及记忆查询/历史对话/过去讨论的问题（应走主模型，依赖向量搜索注入）
  ❌ 绝对不用于：涉及知识/教材/学科的问题。即使是简单知识问题也交给主模型处理。
  ❌ 绝对不用于：看到knowledge_search结果后自行回答问题。知识应交给主模型处理。
- 用户只发图无文字时用 direct_reply 友好回应（10-20字，询问需要做什么）
- 不确定时所有字段保持默认值"""


@dataclass
class RouteDecision:
    """智能路由决策结果"""
    thinking: bool = False
    prefetch: List[Dict[str, str]] = field(default_factory=list)
    buffer_msg: Optional[str] = None
    direct_reply: Optional[str] = None
    inject_rules: List[str] = field(default_factory=list)


class SmartRouter:
    """智能路由层：在调主模型之前，用小模型做决策"""

    def __init__(self):
        pass

    async def route(
        self,
        text: str,
        context: Optional[List[Dict]] = None,
        image_info: Optional[Dict] = None,
        persona: Optional[str] = None,
    ) -> RouteDecision:
        """
        执行路由决策。

        Args:
            text: 用户消息文本
            context: 对话上下文消息列表（可选）
            image_info: 图片信息（可选），含 3D 预识别后的描述
                {"has_image": True, "description": "3D 描述文本", ...}
            persona: 主模型的人设描述（可选），用于 direct_reply 时保持风格一致

        Returns:
            RouteDecision 对象
        """
        if not text and not image_info:
            # 空消息 + 无图片，直接返回默认决策
            return RouteDecision()

        # 构建路由消息

        # 构建路由消息
        import zoneinfo as _zi
        from utils.lunar_date import LunarDate as _LunarDate2
        _sr_now = datetime.now(_zi.ZoneInfo("Asia/Shanghai"))
        _sr_naive = _sr_now.replace(tzinfo=None)
        _sr_lc = _LunarDate2.from_datetime(_sr_naive)
        _cn = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
               "十一", "十二"]
        _lm = _cn[_sr_lc.lunar_month]
        _ld = _sr_lc.lunar_day
        _lds = f"初{_cn[_ld]}" if _ld <= 10 else (
            f"十{_cn[_ld-10]}" if _ld < 20 else ("二十" if _ld == 20 else f"廿{_cn[_ld-20]}"))
        _weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        _wd = _weekday_cn[_sr_now.weekday()]
        _sr_time_str = (f"当前时间：{_sr_now.year}.{_sr_now.month}.{_sr_now.day}"
                        f"（农历{_lm}月{_lds}，{_wd}）{_sr_now.hour:02d}:{_sr_now.minute:02d} (BJT)")
        _sr_prompt_content = SR_PROMPT
        if persona:
            _sr_prompt_content = f"{_sr_prompt_content}\n\n【主模型人设参考】\n{persona}"
        _sr_prompt_content += f"\n\n{_sr_time_str}"
        messages = [{"role": "system", "content": _sr_prompt_content}]

        # 注入上下文（最后 2 轮）
        context_text = ""
        if context:
            recent = context[-10:]  # 最近 10 条（约 5 轮对话）
            lines = []
            for msg in recent:
                role = "用户" if msg.get("role") == "user" else "助手"
                content = msg.get("content", "")[:200]
                lines.append(f"{role}: {content}")
            if lines:
                context_text = "\n".join(lines)
                context_text = "[对话上下文]\n" + context_text + "\n"

        # 注入图片 3D 预识别信息
        image_text = ""
        if image_info:
            if image_info.get("description"):
                image_text = f"[图片预识别]\n{image_info['description']}\n"
            elif image_info.get("has_image"):
                image_text = "[图片预识别]\n用户发送了一张图片，但尚无详细描述。\n"

        # 构建用户消息
        parts = []
        if context_text:
            parts.append(context_text)
        if image_text:
            parts.append(image_text)
        if text:
            parts.append(f"[用户消息]\n{text}")
        else:
            parts.append("[用户消息]\n（用户发送了一张图片，无文字消息）")

        user_content = "\n".join(parts)
        messages.append({"role": "user", "content": user_content})

        try:
            from services.llm_service import llm_service

            # 有图片时使用 VL 模型，否则使用纯文本 Flash 模型
            model = SR_VL_MODEL if (image_info and image_info.get("has_image")) else SR_TEXT_MODEL

            logger.info(
                f"[SMART_ROUTER] text='{text[:50]}' context={bool(context_text)} image={bool(image_info)} "
                f"model={model}"
            )

            from services.model_registry import resolve
            provider = resolve(model)["provider"]

            result = await asyncio.wait_for(
                llm_service.chat_json(
                    messages=messages,
                    provider=provider,
                    model=model,
                    disable_thinking=True,
                    request_type="smart_router",
                ),
                timeout=15.0,
            )
            decision = self._parse_decision(result)
            logger.info(
                f"[SMART_ROUTER] decision: thinking={decision.thinking} "
                f"prefetch={[p.get('tool','?') for p in (decision.prefetch or [])]} "
                f"direct_reply={'yes' if decision.direct_reply else 'no'} "
                f"inject_rules_count={len(decision.inject_rules or [])}"
            )
            return decision
        except Exception as e:
            logger.warning(f"[SmartRouter] route failed: {e}, fallback to default")
            return RouteDecision()

    def _parse_decision(self, result: dict) -> RouteDecision:
        """解析 LLM 返回的 JSON 为 RouteDecision"""
        decision = RouteDecision()

        if isinstance(result, dict):
            # thinking
            if result.get("thinking") is True:
                decision.thinking = True
            elif "thinking" in result and result.get("thinking") is not True:
                logger.warning(
                    f"[SmartRouter] _parse_decision: thinking field discarded "
                    f"(type={type(result.get('thinking')).__name__}, "
                    f"value={repr(result.get('thinking'))[:80]})"
                )

            # buffer_msg
            buf = result.get("buffer_msg")
            if buf and isinstance(buf, str) and len(buf.strip()) > 3:
                decision.buffer_msg = buf.strip()[:200]
            elif "buffer_msg" in result and result.get("buffer_msg") is not None:
                logger.warning(
                    f"[SmartRouter] _parse_decision: buffer_msg field discarded "
                    f"(type={type(buf).__name__}, len={len(str(buf)) if buf else 0})"
                )

            # prefetch
            raw_prefetch = result.get("prefetch", [])
            if isinstance(raw_prefetch, list):
                valid_tools = []
                for i, item in enumerate(raw_prefetch):
                    if isinstance(item, dict) and item.get("tool"):
                        entry = {
                            "tool": item["tool"],
                            "query": item.get("query", ""),
                        }
                        if item.get("index"):
                            entry["index"] = item["index"]
                        valid_tools.append(entry)
                    else:
                        logger.warning(
                            f"[SmartRouter] _parse_decision: prefetch[{i}] discarded "
                            f"(is_dict={isinstance(item, dict)}, "
                            f"has_tool={bool(isinstance(item, dict) and item.get('tool'))})"
                        )
                decision.prefetch = valid_tools

            # direct_reply
            raw_reply = result.get("direct_reply")
            if isinstance(raw_reply, str) and raw_reply.strip():
                decision.direct_reply = raw_reply.strip()
            elif "direct_reply" in result and result.get("direct_reply") is not None:
                logger.warning(
                    f"[SmartRouter] _parse_decision: direct_reply field discarded "
                    f"(type={type(raw_reply).__name__}, "
                    f"value={repr(raw_reply)[:80] if raw_reply else 'N/A'})"
                )

            # inject_rules
            raw_rules = result.get("inject_rules", [])
            if isinstance(raw_rules, list):
                decision.inject_rules = [
                    str(r)[:80] for r in raw_rules if isinstance(r, str) and r.strip()
                ]
                # 记录被丢弃的条目
                for i, r in enumerate(raw_rules):
                    if not isinstance(r, str) or not r.strip():
                        logger.warning(
                            f"[SmartRouter] _parse_decision: inject_rules[{i}] discarded "
                            f"(type={type(r).__name__}, "
                            f"value={repr(r)[:80] if r else 'N/A'})"
                        )

        return decision
