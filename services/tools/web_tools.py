"""
Agent 工具服务 - 网页搜索工具
包含 web_search, multi_web_search 及各类搜索引擎后端
"""

import re
import json
import time
import hashlib
import asyncio
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, quote

import httpx

from config import settings
from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase, _ensure_nest_asyncio
from services.search_service import SearchService

logger = logging.getLogger(__name__)

# 搜索缓存配置
SEARCH_CACHE_TTL_SECONDS = 300   # 5分钟内相同 query 不重复请求
SEARCH_CACHE_MAX_SIZE = 200      # 最多缓存 200 个 query


class WebToolsMixin(AgentToolsServiceBase):
    """网页搜索工具 Mixin"""

    def __init__(self, agent_hash: str):
        super().__init__(agent_hash)
        self.search = SearchService(agent_hash)

    # 类级搜索缓存：所有实例共享
    _search_cache: Dict[str, tuple] = {}

    # ========== 搜索缓存方法 ==========

    @classmethod
    def _get_search_cache_key(cls, query: str, level: str) -> str:
        """生成缓存 key（归一化 query + level）"""
        normalized = query.strip().lower()
        return hashlib.md5(f"{level}:{normalized}".encode()).hexdigest()

    @classmethod
    def _get_cached_search(cls, query: str, level: str) -> Optional[str]:
        """检查缓存是否有效"""
        key = cls._get_search_cache_key(query, level)
        if key in cls._search_cache:
            ts, result = cls._search_cache[key]
            if time.time() - ts < SEARCH_CACHE_TTL_SECONDS:
                return result
            else:
                del cls._search_cache[key]
        return None

    @classmethod
    def _set_cached_search(cls, query: str, level: str, result: str):
        """写入缓存（自动清理超量缓存）"""
        key = cls._get_search_cache_key(query, level)
        cls._search_cache[key] = (time.time(), result)
        if len(cls._search_cache) > SEARCH_CACHE_MAX_SIZE:
            oldest_key = min(cls._search_cache, key=lambda k: cls._search_cache[k][0])
            del cls._search_cache[oldest_key]

    @classmethod
    def get_search_cache_stats(cls) -> Dict[str, Any]:
        """获取缓存统计"""
        now = time.time()
        valid = sum(1 for ts, _ in cls._search_cache.values() if now - ts < SEARCH_CACHE_TTL_SECONDS)
        return {
            "total_entries": len(cls._search_cache),
            "valid_entries": valid,
            "ttl_seconds": SEARCH_CACHE_TTL_SECONDS,
            "max_size": SEARCH_CACHE_MAX_SIZE
        }

    @classmethod
    def clear_search_cache(cls) -> None:
        """清空搜索缓存"""
        cls._search_cache.clear()

    # ========== 搜索级别自动选择 ==========

    def _auto_select_search_level(self, query: str) -> str:
        """
        自动选择搜索级别

        选择逻辑：
        - 短查询（<10字符）→ quick（简单问题，快速验证）
        - 研究关键词（分析、比较、研究、深度、详细等）→ deep
        - 默认 → balanced（平衡速度和质量）
        """
        query_len = len(query)
        query_lower = query.lower()

        deep_keywords = [
            "分析", "比较", "研究", "深度", "详细", "全面",
            "调研", "报告", "综述", "evaluate", "compare",
            "analyze", "research", "deep", "comprehensive"
        ]

        quick_keywords = [
            "是什么", "定义", "多少", "何时", "who", "when",
            "what is", "how much", "简单", "快速"
        ]

        for kw in deep_keywords:
            if kw in query_lower:
                return "deep"

        for kw in quick_keywords:
            if kw in query_lower:
                return "quick"

        if query_len < 10:
            return "quick"
        elif query_len > 100 or "?" in query or "？" in query:
            return "deep"

        return "balanced"

    # ========== 搜索降级 ==========

    def _try_fallback_search(self, query: str, failed_level: str, original_start_time: float) -> str:
        """
        搜索失败时尝试降级到其他级别（同步版本）
        """
        level_map = {"research": "deep", "advanced": "balanced", "minimal": "quick"}
        failed_level = level_map.get(failed_level, failed_level)

        fallback_order = {
            "deep": ["balanced", "quick", "bing_fallback"],
            "balanced": ["quick", "bing_fallback"],
            "quick": ["bing_fallback"],
            "raw": ["bing_fallback"],
        }

        fallback_levels = fallback_order.get(failed_level, [])
        for fallback_level in fallback_levels:
            try:
                if fallback_level == "quick":
                    result = self._search_tencent_sync(query)
                    service_name = "腾讯搜狗(降级)"
                elif fallback_level == "balanced":
                    result = self._search_kimi_sync(query)
                    service_name = "Kimi(降级)"
                elif fallback_level == "bing_fallback":
                    result = self._search_bing_fallback(query)
                    service_name = "Bing CN(无需API)"
                else:
                    continue

                if not result.startswith("Error:"):
                    elapsed_sec = (time.time() - original_start_time)
                    return f"{result}\n\n⏱️ 耗时: {elapsed_sec:.1f}s | 级别: {fallback_level} ({service_name})"
            except Exception:
                continue

        return None

    async def _try_fallback_search_async(self, query: str, failed_level: str, original_start_time: float) -> str:
        """
        搜索失败时尝试降级到其他级别（异步版本）
        """
        level_map = {"research": "deep", "advanced": "balanced", "minimal": "quick"}
        failed_level = level_map.get(failed_level, failed_level)

        fallback_order = {
            "deep": ["balanced", "quick", "bing_fallback"],
            "balanced": ["quick", "bing_fallback"],
            "quick": ["bing_fallback"],
            "raw": ["bing_fallback"],
        }

        fallback_levels = fallback_order.get(failed_level, [])
        for fallback_level in fallback_levels:
            try:
                if fallback_level == "quick":
                    result = await self.search.search_tencent(query)
                    service_name = "腾讯搜狗(降级)"
                elif fallback_level == "balanced":
                    result = await self.search.search_qwen(query)
                    service_name = "Qwen(降级)"
                elif fallback_level == "bing_fallback":
                    result = await self._search_bing_fallback_async(query)
                    service_name = "Bing CN(无需API)"
                else:
                    continue

                if not result.startswith("Error:"):
                    elapsed_sec = (time.time() - original_start_time)
                    return f"{result}\n\n⏱️ 耗时: {elapsed_sec:.1f}s | 级别: {fallback_level} ({service_name})"
            except Exception:
                continue

        return None

    async def _search_baidu(self, query: str) -> str:
        """
        L3 研究级搜索：百度千帆
        - 速度：~40s
        - 返回：深度分析 + 引用
        """
        if not settings.BAIDU_SEARCH_API_KEY:
            return "Error: 百度搜索 API Key 未配置（BAIDU_SEARCH_API_KEY）"

        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    settings.BAIDU_SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {settings.BAIDU_SEARCH_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "messages": [{"role": "user", "content": query}],
                        "stream": False,
                        "model": "ernie-3.5-8k",
                        "web_search": {
                            "enable": True,
                            "enable_citation": True,
                            "search_num": 10,
                            "reference_num": 5
                        }
                    }
                )
                resp.raise_for_status()
                data = resp.json()

                if "choices" in data and len(data["choices"]) > 0:
                    content = data["choices"][0].get("message", {}).get("content", "")
                    return f"🔍 搜索「{query}」(百度千帆深度搜索)\n\n{content}"
                else:
                    return f"Error: 百度搜索响应格式异常"

        except Exception as e:
            return f"Error: 百度搜索失败: {e}"

    async def _search_bing_fallback_async(self, query: str) -> str:
        """
        Bing CN 备选搜索：异步版本
        """
        encoded_query = quote(query)
        search_url = f"https://cn.bing.com/search?q={encoded_query}&ensearch=0"

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(
                    search_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
                    }
                )
                resp.raise_for_status()
                html = resp.text

                results = self._parse_bing_results(html)

                if not results:
                    return f"未找到与「{query}」相关的结果"

                output_lines = [f"🔍 搜索「{query}」(Bing CN 备选)"]
                output_lines.append("")
                for i, r in enumerate(results[:8], 1):
                    output_lines.append(
                        f"【{i}】{r['title']}\n"
                        f"   来源: {r['source']}\n"
                        f"   {r['snippet']}\n"
                        f"   链接: {r['url']}"
                    )

                return "\n\n".join(output_lines)

        except Exception as e:
            return f"Error: Bing 搜索请求失败: {e}"

    def _parse_bing_results(self, html: str) -> list:
        """
        解析 Bing 搜索结果页面

        Returns:
            [{"title": ..., "url": ..., "snippet": ..., "source": ...}, ...]
        """
        results = []

        p_pattern = r'<p[^>]*>(.*?)</p>'
        p_matches = re.findall(p_pattern, html, re.DOTALL)

        snippets = []
        for p in p_matches:
            text = re.sub(r'<[^>]+>', '', p).strip()
            text = re.sub(r'\s+', ' ', text)
            if (len(text) > 30 and
                not text.startswith('//') and
                not text.startswith('function') and
                not text.startswith('var ') and
                not text.startswith('if(') and
                'navigator.' not in text and
                'window.' not in text and
                'document.' not in text and
                '&' not in text[:10]):
                snippets.append(text[:200])

        h2_pattern = r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>'
        h2_matches = re.findall(h2_pattern, html, re.DOTALL)

        for i, (url, title_html) in enumerate(h2_matches):
            try:
                title = re.sub(r'<[^>]+>', '', title_html).strip()
                title = re.sub(r'\s+', ' ', title)

                if not url.startswith('http'):
                    continue

                parsed = urlparse(url)
                source = parsed.netloc
                snippet = snippets[i] if i < len(snippets) else "（点击链接查看详情）"

                if title and url:
                    results.append({
                        "title": title,
                        "url": url,
                        "snippet": snippet,
                        "source": source
                    })
            except Exception:
                continue

        if not results:
            link_pattern = r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a>'
            link_matches = re.findall(link_pattern, html)

            seen_urls = set()
            for url, title in link_matches[:15]:
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                skip_domains = ['bing.com', 'microsoft.com', 'go.microsoft.com']
                if any(domain in url for domain in skip_domains):
                    continue

                if title and len(title) > 3:
                    parsed = urlparse(url)
                    source = parsed.netloc
                    results.append({
                        "title": title.strip(),
                        "url": url,
                        "snippet": "（点击链接查看详情）",
                        "source": source
                    })

        return results

    # ========== 搜索引擎后端（同步版本） ==========

    def _search_tencent_sync(self, query: str, count: int = 8) -> str:
        """
        L1 极简搜索：腾讯搜狗（同步版本）
        """
        if not settings.TENCENT_SEARCH_API_KEY:
            return "Error: 腾讯搜索 API Key 未配置（TENCENT_SEARCH_API_KEY）"

        async def _async_search():
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
                    return f"Error: 响应格式异常"

                response_data = data["Response"]

                if "Error" in response_data:
                    return f"Error: {response_data['Error'].get('Code', 'Unknown')}: {response_data['Error'].get('Message', '')}"

                pages = response_data.get("Pages", [])
                if not pages:
                    return f"未找到与「{query}」相关的结果"

                results = []
                for i, page in enumerate(pages[:count], 1):
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
                            f"   {passage[:200]}{'...' if len(passage) > 200 else ''}\n"
                            f"   链接: {url}"
                        )
                    except Exception:
                        continue

                if not results:
                    return f"未找到与「{query}」相关的结果"

                return f"🔍 搜索「{query}」(腾讯搜狗 L1)\n\n" + "\n\n".join(results)

        try:
            _ensure_nest_asyncio()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_async_search())
            finally:
                loop.close()
        except Exception as e:
            return f"Error: 腾讯搜索失败: {e}"

    def _search_kimi_sync(self, query: str) -> str:
        """
        L2 高级搜索：Kimi 128k（同步版本）
        """
        if not settings.KIMI_API_KEY:
            return "Error: Kimi API Key 未配置（KIMI_API_KEY）"

        KIMI_WEB_SEARCH_TOOL = {
            "type": "builtin_function",
            "function": {"name": "$web_search"}
        }

        endpoint = f"{settings.KIMI_BASE_URL.rstrip('/')}/chat/completions"

        async def _async_search():
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
                                "tools": [KIMI_WEB_SEARCH_TOOL]
                            }
                        )
                        resp.raise_for_status()
                        data = resp.json()

                        choice = data.get("choices", [{}])[0]
                        message = choice.get("message", {})
                        text = message.get("content", "")
                        tool_calls = message.get("tool_calls", [])

                        if choice.get("finish_reason") != "tool_calls" or not tool_calls:
                            return f"🔍 搜索「{query}」(Kimi L2)\n\n{text or '（无搜索结果）'}"

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

        try:
            _ensure_nest_asyncio()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_async_search())
            finally:
                loop.close()
        except Exception as e:
            return f"Error: Kimi 搜索失败: {e}"

    def _search_baidu_sync(self, query: str) -> str:
        """
        L3 研究级搜索：百度千帆（同步版本）
        """
        if not settings.BAIDU_SEARCH_API_KEY:
            return "Error: 百度搜索 API Key 未配置（BAIDU_SEARCH_API_KEY）"

        async def _async_search():
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    settings.BAIDU_SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {settings.BAIDU_SEARCH_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "messages": [{"role": "user", "content": query}],
                        "stream": False,
                        "model": "ernie-3.5-8k",
                        "web_search": {
                            "enable": True,
                            "enable_citation": True,
                            "search_num": 10,
                            "reference_num": 5
                        }
                    }
                )
                resp.raise_for_status()
                data = resp.json()

                if "choices" in data and len(data["choices"]) > 0:
                    content = data["choices"][0].get("message", {}).get("content", "")
                    return f"🔍 搜索「{query}」(百度千帆 L3 深度搜索)\n\n{content}"
                else:
                    return f"Error: 百度搜索响应格式异常"

        try:
            _ensure_nest_asyncio()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_async_search())
            finally:
                loop.close()
        except Exception as e:
            return f"Error: 百度搜索失败: {e}"

    def _search_bing_fallback(self, query: str) -> str:
        """
        Bing CN 备选搜索：无需 API Key
        """
        try:
            _ensure_nest_asyncio()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(self._search_bing_fallback_async(query))
                return result
            finally:
                loop.close()
        except Exception as e:
            return f"Error: Bing 备选搜索失败: {e}"

    # ========== 主搜索工具（异步 @tool） ==========

    @tool(description="网页搜索，支持四级搜索: raw(原始数据~1s) / quick(快速验证~2s) / balanced(均衡推荐~4s) / deep(深度研究~15s) / auto(自动选择)", category="web")
    async def web_search(
        self,
        query: str,
        level: str = "balanced",
        use_cache: bool = True
    ) -> str:
        """
        网页搜索，支持四级搜索服务。

        Args:
            query: 搜索关键词
            level: 搜索级别
            use_cache: 是否使用缓存（默认 True）

        Returns:
            搜索结果文本
        """
        if level == "auto":
            query_len = len(query)
            if query_len < 10:
                level = "quick"
            elif query_len < 50:
                level = "balanced"
            else:
                level = "deep"

        level_map = {"minimal": "raw", "advanced": "balanced", "research": "deep"}
        level = level_map.get(level, level)

        if use_cache:
            cached_result = self._get_cached_search(query, level)
            if cached_result:
                return f"{cached_result}\n\n💾 (缓存命中)"

        start_time = time.time()
        result = None
        service_name = ""

        if level == "raw":
            result = await self.search.search_tencent(query)
            service_name = "腾讯搜狗(原始)"
        elif level == "quick":
            result = await self.search.search_tencent_deepseek(query)
            service_name = "腾讯+DeepSeek"
        elif level == "balanced":
            result = await self.search.search_qwen(query)
            service_name = "Qwen3.5-Flash"
        elif level == "deep":
            # 并发发动 Kimi + 百度，结果合并
            _kimi_task = asyncio.create_task(self.search.search_kimi(query))
            _baidu_task = asyncio.create_task(self._search_baidu(query))
            _kimi_res, _baidu_res = await asyncio.gather(_kimi_task, _baidu_task, return_exceptions=True)
            _parts = []
            if isinstance(_kimi_res, str) and not _kimi_res.startswith("Error:"):
                _parts.append(f"── Kimi k2.6 ──\n{_kimi_res}")
            if isinstance(_baidu_res, str) and not _baidu_res.startswith("Error:"):
                _parts.append(f"── 百度千帆 ──\n{_baidu_res}")
            if _parts:
                result = "🔍 深度搜索「%s」\n\n%s" % (query, "\n\n".join(_parts))
                service_name = "Kimi + 百度"
            else:
                # 都失败，用第一个非 Error 的 fallback
                if isinstance(_kimi_res, str) and not _kimi_res.startswith("Error:"):
                    result = _kimi_res
                elif isinstance(_baidu_res, str) and not _baidu_res.startswith("Error:"):
                    result = _baidu_res
                else:
                    result = "Error: 深度搜索全部失败"
                service_name = "Kimi + 百度(失败)"
        else:
            return f"Error: 未知的搜索级别 '{level}'，可选: raw, quick, balanced, deep, auto"

        elapsed_sec = (time.time() - start_time)

        if result.startswith("Error:"):
            fallback_result = await self._try_fallback_search_async(query, level, start_time)
            if fallback_result:
                return fallback_result
            return f"{result} | 耗时: {elapsed_sec:.1f}s [{service_name}]"

        if use_cache:
            self._set_cached_search(query, level, result)

        return f"{result}\n\n⏱️ 耗时: {elapsed_sec:.1f}s | 级别: {level} ({service_name})"

    # ========== 多引擎搜索（保留旧接口，已禁用 @tool） ==========

    # multi_web_search 已禁用，请使用 web_search 工具
    def multi_web_search(
        self,
        query: str,
        level: str = "auto",
        count: int = 8,
        use_cache: bool = True
    ) -> str:
        """
        网页搜索工具，支持三级搜索服务（已弃用，请使用 web_search）
        """
        if level == "auto":
            level = self._auto_select_search_level(query)

        if use_cache:
            cached_result = self._get_cached_search(query, level)
            if cached_result:
                return f"{cached_result}\n\n💾 (缓存命中)"

        start_time = time.time()

        if level == "minimal":
            result = self._search_tencent_sync(query, count)
            service_name = "腾讯搜狗"
        elif level == "advanced":
            result = self._search_kimi_sync(query)
            service_name = "Kimi"
        elif level == "research":
            result = self._search_baidu_sync(query)
            service_name = "百度千帆"
        else:
            return f"Error: 未知的搜索级别 '{level}'，可选: minimal, advanced, research, auto"

        elapsed_ms = int((time.time() - start_time) * 1000)
        elapsed_sec = elapsed_ms / 1000

        if result.startswith("Error:"):
            fallback_result = self._try_fallback_search(query, level, start_time)
            if fallback_result:
                return fallback_result
            return f"{result} | 耗时: {elapsed_sec:.1f}s [{service_name}]"

        if use_cache:
            self._set_cached_search(query, level, result)

        return f"{result}\n\n⏱️ 耗时: {elapsed_sec:.1f}s | 级别: {level} ({service_name})"
