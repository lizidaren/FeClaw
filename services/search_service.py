"""
搜索服务 - 封装各类搜索引擎后端

从 services/tools/web_tools.py 提取出的独立搜索服务。
"""

import json
import logging
import time
from typing import Optional, Callable, Awaitable

import httpx

from config import settings

logger = logging.getLogger(__name__)


class SearchService:
    """搜索服务，封装多引擎搜索逻辑"""

    def __init__(self, agent_hash: str = ""):
        self.agent_hash = agent_hash

    async def search_tencent(self, query: str) -> str:
        """
        L1 极简搜索：腾讯搜索
        - 速度：~1s
        - 返回：原始搜索结果（多条）
        """
        if not settings.TENCENT_SEARCH_API_KEY:
            return "Error: 腾讯搜索 API Key 未配置（TENCENT_SEARCH_API_KEY）"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    settings.TENCENT_SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {settings.TENCENT_SEARCH_API_KEY}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={"Query": query}
                )
                resp.raise_for_status()
                data = resp.json()

                if "Response" not in data:
                    return "Error: 响应格式异常"

                response_data = data["Response"]

                if "Error" in response_data:
                    return f"Error: {response_data['Error'].get('Code', 'Unknown')}: {response_data['Error'].get('Message', '')}"

                pages = response_data.get("Pages", [])
                if not pages:
                    return f"未找到与「{query}」相关的结果"

                results = []
                for i, page in enumerate(pages[:8], 1):
                    try:
                        page_data = json.loads(page)
                        title = page_data.get("title", "")
                        passage = page_data.get("passage", "")
                        url = page_data.get("url", "")
                        site = page_data.get("site", "")
                        score = page_data.get("score", 0)

                        results.append(
                            f"【{i}】{title}\n"
                            f"   评分: {score:.2f} | 来源: {site}\n"
                            f"   {passage}\n"
                            f"   链接: {url}"
                        )
                    except Exception:
                        continue

                if not results:
                    return f"未找到与「{query}」相关的结果"

                return f"🔍 搜索「{query}」(腾讯搜狗)\n\n" + "\n\n".join(results)

        except Exception as e:
            return f"Error: 腾讯搜索失败: {e}"

    async def search_tencent_deepseek(self, query: str) -> str:
        """
        L1 快速验证：腾讯搜索 + DeepSeek V4 Flash 清洗
        - 速度：~2s
        - 返回：LLM 整理后的核心事实
        """
        raw_result = await self.search_tencent(query)
        if raw_result.startswith("Error:"):
            return raw_result

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                ds_prompt = (
                    f"根据以下关于「{query}」的搜索结果，"
                    f"提取核心事实（时间、地点、数据等），保持简洁客观。如果搜索结果与查询无关请如实说明。\n\n{raw_result}"
                )
                resp = await client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-v4-flash",
                        "messages": [{"role": "user", "content": ds_prompt}],
                        "thinking": {"type": "disabled"},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                cleaned = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if cleaned:
                    return f"🔍 搜索「{query}」(腾讯+DeepSeek)\n\n{cleaned}"
                return raw_result
        except Exception as e:
            return raw_result + f"\n\n(DeepSeek 清洗失败: {e})"

    async def search_qwen(self, query: str, on_progress: Optional[Callable[[str], Awaitable[None]]] = None) -> str:
        """
        L2 均衡搜索：Qwen3.5-Flash 联网搜索（流式）
        - 速度：~4s
        - 返回：LLM 总结的搜索结果
        - on_progress: 可选进度回调，接收逐字的搜索结果增量
        """
        import os

        qwen_key = settings.QWEN_API_KEY or settings.QWEN_VL_KEY or ""
        if not qwen_key:
            return "Error: QWEN_API_KEY 环境变量未配置"
        _t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream(
                    "POST",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {qwen_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "qwen3.5-flash",
                        "messages": [
                            {"role": "system", "content": '你是搜索助手。直接输出搜索结果中的核心事实，不要添加任何开场白。多源交叉验证：对关键信息标注来源数量（如「3个来源一致认为」、「仅1个来源提到」），有矛盾时指出分歧。不确定或信息存疑时明确标注。'},
                            {"role": "user", "content": query},
                        ],
                        "enable_search": True,
                        "thinking": {"type": "disabled"},
                        "stream": True,
                    },
                ) as resp:
                    resp.raise_for_status()
                    full_content = ""
                    async for line in resp.aiter_lines():
                        if line and line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                choices = chunk.get("choices", [])
                                if not choices:
                                    continue
                                delta = choices[0].get("delta", {}).get("content", "")
                                if delta:
                                    full_content += delta
                                    if on_progress:
                                        await on_progress(delta)
                            except json.JSONDecodeError:
                                continue
                _elapsed = time.time() - _t0
                logger.info(f"[SEARCH] search_qwen: {_elapsed:.1f}s for query={query[:50]}")
                if full_content:
                    return f"🔍 搜索「{query}」(Qwen3.5-Flash)\n\n{full_content}"
                return "(搜索结果为空)"
        except Exception as e:
            _elapsed = time.time() - _t0
            logger.warning(f"[SEARCH] search_qwen FAILED: {_elapsed:.1f}s, {e}")
            return f"Error: Qwen联网搜索失败: {e}"

    async def search_kimi(self, query: str) -> str:
        """
        L3 深度研究：Kimi
        - 速度：~15s
        - 返回：LLM 总结的搜索结果
        """
        if not settings.KIMI_API_KEY:
            return "Error: Kimi API Key 未配置（KIMI_API_KEY）"

        KIMI_WEB_SEARCH_TOOL = {
            "type": "builtin_function",
            "function": {"name": "$web_search"}
        }

        endpoint = f"{settings.KIMI_BASE_URL.rstrip('/')}/chat/completions"
        messages = [{"role": "user", "content": query}]

        async with httpx.AsyncClient(timeout=60.0) as client:
            for round_num in range(3):
                try:
                    resp = await client.post(
                        endpoint,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {settings.KIMI_API_KEY}"
                        },
                        json={
                            "model": settings.KIMI_MODEL,
                            "messages": messages,
                            "tools": [KIMI_WEB_SEARCH_TOOL],
                            "thinking": {"type": "disabled"}
                        }
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    choice = data.get("choices", [{}])[0]
                    message = choice.get("message", {})
                    text = message.get("content", "")
                    tool_calls = message.get("tool_calls", [])

                    if choice.get("finish_reason") != "tool_calls" or not tool_calls:
                        return f"🔍 搜索「{query}」(Kimi)\n\n{text or '（无搜索结果）'}"

                    messages.append({
                        "role": "assistant",
                        "content": message.get("content", ""),
                        "tool_calls": tool_calls
                    })
                    for tc in tool_calls:
                        tid = tc.get("id", "").strip()
                        if tid:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tid,
                                "name": tc.get("function", {}).get("name", "$web_search"),
                                "content": tc.get("function", {}).get("arguments", "{}")
                            })
                except Exception as e:
                    return f"Error: Kimi 搜索失败: {e}"

        return "（搜索超时）"
