"""
Agent 工具服务 - 网页搜索工具
包含 web_search, multi_web_search 及各类搜索引擎后端
"""

import re
import os
import json
import time
import base64
import hashlib
import asyncio
import tempfile
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, quote

import httpx
import aiohttp

from config import settings
from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase, _ensure_nest_asyncio
from services.search_service import SearchService
from services.tools.universal_parser import _vlm_chat, _qwen_chat, VLM_MODEL, QWEN_CHAT_URL

logger = logging.getLogger(__name__)

# 搜索缓存配置
SEARCH_CACHE_TTL_SECONDS = 300   # 5分钟内相同 query 不重复请求
SEARCH_CACHE_MAX_SIZE = 200      # 最多缓存 200 个 query

# 图片搜索配置（Alibaba Responses API + web_search_image 工具）
QWEN_RESPONSES_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/responses"
IMAGE_SEARCH_MODEL = "qwen3.6-flash"
IMAGE_SEARCH_MAX_COUNT = 5       # 单次搜索最多返回的图片数
IMAGE_SEARCH_TIMEOUT = 60.0      # Responses API 整体超时（腾讯云→阿里云跨网慢）
IMAGE_DOWNLOAD_TIMEOUT = 10.0    # 单张图片下载超时
SUPPORTED_IMAGE_EXTS = ("jpg", "jpeg", "png", "gif", "webp")


class WebToolsMixin(AgentToolsServiceBase):
    """网页搜索工具 Mixin"""

    def __init__(self, agent_hash: str, **kwargs):
        super().__init__(agent_hash, **kwargs)
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
                    result = await self.search.search_qwen(query, on_progress=on_progress)
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

    @tool(
        description=(
            "网页搜索，支持四级搜索: raw(原始数据~1s) / quick(快速验证~2s) / balanced(均衡推荐~4s) / deep(深度研究~15s) / auto(自动选择)。\n"
            "balanced 级别（含 auto 解析为 balanced）下，设置 allow_images=True 会同时调用图片搜索（最多 5 张），自动下载到 VFS 的 images/fetched/ 目录并返回路径。其他级别忽略 allow_images。"
        ),
        category="web"
    )
    async def web_search(
        self,
        query: str,
        level: str = "balanced",
        use_cache: bool = True,
        allow_images: bool = False,
        on_progress=None
    ) -> str:
        """
        网页搜索，支持四级搜索服务。

        Args:
            query: 搜索关键词
            level: 搜索级别（raw / quick / balanced / deep / auto）
            use_cache: 是否使用缓存（默认 True；allow_images=True 时自动禁用）
            allow_images: 是否同时搜索并下载图片（仅 balanced 级别生效）

        Returns:
            搜索结果文本（含可选的图片段落）
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

        # 图片搜索仅在 balanced 级别生效（auto 解析为 balanced 时也会生效）
        if allow_images and level != "balanced":
            logger.info(f"[web_search] allow_images=True 但 level={level} 非 balanced，忽略图片搜索")
            allow_images = False

        # 图片搜索会改变返回内容（增加图片段落），绕过缓存避免命中老结果
        if use_cache and not allow_images:
            cached_result = self._get_cached_search(query, level)
            if cached_result:
                return f"{cached_result}\n\n💾 (缓存命中)"

        start_time = time.time()
        result = None
        service_name = ""
        image_section = ""

        if level == "balanced":
            # 并行：文本搜索 + 可选图片搜索
            tasks = [self.search.search_qwen(query, on_progress=on_progress)]
            if allow_images:
                tasks.append(self._search_qwen_images(query))
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            text_result = gathered[0]
            if isinstance(text_result, Exception):
                result = f"Error: 文本搜索异常: {text_result}"
            else:
                result = text_result
            service_name = "Qwen3.5-Flash"

            # 处理图片搜索结果
            if allow_images and len(gathered) > 1:
                img_res = gathered[1]
                if isinstance(img_res, list) and img_res:
                    try:
                        image_section = await self._download_and_format_images(img_res, query)
                    except Exception as e:
                        logger.warning(f"[web_search] 图片下载汇总失败: {e}")
                        image_section = "\n\n[图片搜索结果] (下载失败)"
                elif isinstance(img_res, Exception):
                    logger.warning(f"[web_search] 图片搜索异常: {img_res}")
        elif level == "raw":
            result = await self.search.search_tencent(query)
            service_name = "腾讯搜狗(原始)"
        elif level == "quick":
            result = await self.search.search_tencent_deepseek(query)
            service_name = "腾讯+DeepSeek"
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

        if isinstance(result, str) and result.startswith("Error:"):
            fallback_result = await self._try_fallback_search_async(query, level, start_time)
            if fallback_result:
                return fallback_result
            return f"{result} | 耗时: {elapsed_sec:.1f}s [{service_name}]"

        if use_cache and not allow_images:
            self._set_cached_search(query, level, result)

        final = f"{result}\n\n⏱️ 耗时: {elapsed_sec:.1f}s | 级别: {level} ({service_name})"
        if image_section:
            final += image_section
        return final

    # ========== 图片搜索（Responses API + VFS 下载） ==========

    @staticmethod
    def _slugify_query(query: str, max_len: int = 40) -> str:
        """
        将 query 转为文件名安全的 slug
        - 保留 ASCII 字母数字、下划线、连字符、Unicode 字母（含中文）
        - 其他字符替换为 _
        - 限制最大长度，避免超长文件名
        """
        slug = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", query.strip())
        slug = re.sub(r"_+", "_", slug).strip("_-")
        if not slug:
            slug = "search"
        return slug[:max_len]

    @staticmethod
    def _infer_image_ext(url: str) -> Optional[str]:
        """
        从 URL 路径推断图片扩展名（必须是白名单内的格式）
        """
        try:
            path = urlparse(url).path.lower()
        except Exception:
            return None
        for ext in SUPPORTED_IMAGE_EXTS:
            if path.endswith(f".{ext}"):
                return "jpg" if ext == "jpeg" else ext  # 统一用 jpg 扩展名
        return None

    async def _search_qwen_images(self, query: str) -> List[Dict[str, str]]:
        """
        调用 Alibaba Responses API 的 web_search_image 工具，返回图片列表
        失败 / 无结果时返回空列表（不抛异常，让文本搜索结果照常返回）

        Returns:
            [{"url": "...", "title": "..."}, ...] （最多 IMAGE_SEARCH_MAX_COUNT 项）
        """
        qwen_key = settings.QWEN_API_KEY or settings.QWEN_VL_KEY or ""
        if not qwen_key:
            logger.warning("[IMAGE-SEARCH] QWEN_API_KEY 未配置，跳过图片搜索")
            return []

        headers = {
            "Authorization": f"Bearer {qwen_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": IMAGE_SEARCH_MODEL,
            "input": query,
            "tools": [{"type": "web_search_image"}],
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(timeout=IMAGE_SEARCH_TIMEOUT) as client:
                resp = await client.post(QWEN_RESPONSES_URL, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning(f"[IMAGE-SEARCH] Responses API 调用失败: {e}")
            return []

        images: List[Dict[str, str]] = []
        try:
            for item in data.get("output", []) or []:
                if item.get("type") != "web_search_image_call":
                    continue
                output_raw = item.get("output", "[]")
                if isinstance(output_raw, list):
                    items = output_raw
                else:
                    try:
                        items = json.loads(output_raw)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("[IMAGE-SEARCH] 无法解析 web_search_image_call.output")
                        continue
                if not isinstance(items, list):
                    continue
                for img in items[:IMAGE_SEARCH_MAX_COUNT]:
                    url = (
                        img.get("url")
                        or img.get("image_url")
                        or img.get("imageUrl")
                        or ""
                    )
                    title = (
                        img.get("title")
                        or img.get("alt")
                        or img.get("description")
                        or img.get("name")
                        or ""
                    )
                    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                        continue
                    if not self._infer_image_ext(url):
                        # 跳过非图片 URL（防 SVG / ico / 视频缩略图等）
                        continue
                    images.append({"url": url, "title": title or "image"})
                    if len(images) >= IMAGE_SEARCH_MAX_COUNT:
                        break
                if len(images) >= IMAGE_SEARCH_MAX_COUNT:
                    break
        except Exception as e:
            logger.warning(f"[IMAGE-SEARCH] 解析 output 异常: {e}")

        logger.info(f"[IMAGE-SEARCH] query={query[:30]!r} 返回 {len(images)} 张图片")
        return images

    async def _download_image_to_vfs(self, url: str, base_vfs_path: str) -> Optional[str]:
        """
        下载单张图片并上传到 VFS（通过 COS put_object 直传二进制）
        失败返回 None；成功返回最终 VFS 路径（含扩展名）

        base_vfs_path: 不含扩展名的 VFS 路径，如 'images/fetched/xxx_1'
        """
        ext = self._infer_image_ext(url)
        if not ext:
            return None

        # base_vfs_path 已 strip 过 '/'
        vfs_path = base_vfs_path
        if not vfs_path.endswith(f".{ext}"):
            vfs_path = f"{base_vfs_path}.{ext}"

        cos_key = self._resolve(vfs_path.lstrip("/"))
        if not cos_key:
            logger.warning(f"[IMAGE-SEARCH] _resolve 失败: {vfs_path}")
            return None

        try:
            async with httpx.AsyncClient(
                timeout=IMAGE_DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": self._WEB_FETCH_USER_AGENT},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content = resp.content

            # 基本校验：非空 + 至少 100 字节（避免 HTML 错误页 / 1x1 占位图）
            if not content or len(content) < 100:
                logger.warning(f"[IMAGE-SEARCH] 内容过小或为空: {url} ({len(content) if content else 0} bytes)")
                return None

            # 写入 COS（FileStorage.put_object 是同步方法）
            await asyncio.to_thread(self.storage.put_object, cos_key, content)
            logger.info(f"[IMAGE-SEARCH] saved {url} → {cos_key} ({len(content)} bytes)")
            return vfs_path
        except Exception as e:
            logger.warning(f"[IMAGE-SEARCH] 下载/上传失败 {url}: {e}")
            return None

    async def _download_and_format_images(
        self,
        images: List[Dict[str, str]],
        query: str,
    ) -> str:
        """
        并发下载图片列表，格式化为结果段落。
        任一图片失败不影响其他图片；全部失败时返回简短提示。
        """
        if not images:
            return ""

        slug = self._slugify_query(query)
        images = images[:IMAGE_SEARCH_MAX_COUNT]

        async def _one(idx: int, img: Dict[str, str]) -> Optional[str]:
            url = img.get("url", "")
            if not url:
                return None
            base_path = f"images/fetched/{slug}_{idx}"
            return await self._download_image_to_vfs(url, base_path)

        tasks = [_one(i + 1, img) for i, img in enumerate(images)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        saved: List[Optional[str]] = []
        for r in results:
            if isinstance(r, BaseException):
                saved.append(None)
            else:
                saved.append(r)

        success_count = sum(1 for s in saved if s)
        if success_count == 0:
            return "\n\n[图片搜索结果] (全部下载失败)"

        lines = [f"\n\n[图片搜索结果] 共 {len(images)} 张（成功 {success_count}）"]
        for i, (img, path) in enumerate(zip(images, saved), 1):
            title = img.get("title") or "image"
            # 截断过长标题（控制台/上下文显示友好）
            if len(title) > 60:
                title = title[:57] + "..."
            if path:
                # VFS 路径返回绝对路径形式（与现有 file_* 工具一致）
                vfs_abs = f"/{path.lstrip('/')}"
                lines.append(f"  [{i}] {title} → vfs: {vfs_abs}")
            else:
                lines.append(f"  [{i}] {title} → (下载失败)")

        return "\n".join(lines)

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

        if isinstance(result, str) and result.startswith("Error:"):
            fallback_result = self._try_fallback_search(query, level, start_time)
            if fallback_result:
                return fallback_result
            return f"{result} | 耗时: {elapsed_sec:.1f}s [{service_name}]"

        if use_cache:
            self._set_cached_search(query, level, result)

        return f"{result}\n\n⏱️ 耗时: {elapsed_sec:.1f}s | 级别: {level} ({service_name})"

    # ========== web_fetch: 网页抓取（Playwright + 反检测 + curl 降级） ==========

    # 真实的 Chrome User-Agent，避免被反爬识别
    _WEB_FETCH_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    # Playwright 启动参数：禁用自动化特征
    _WEB_FETCH_LAUNCH_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--disable-automation",
        "--disable-infobars",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]

    @tool(
        description=(
            "抓取任意 URL 的内容。支持 4 种 mode：\n"
            "- 'text'(默认): 渲染后提取纯文本（Playwright + load + 1s 等待），最长 100K 字符\n"
            "- 'image': 截取首屏 1280x720 截图，保存到临时文件并返回路径\n"
            "- 'llm-text': 提取文本后调用 qwen3.6-flash 回答 prompt\n"
            "- 'llm-image': 截图后调用 qwen3.6-flash VLM 分析图片并回答 prompt\n\n"
            "Playwright 失败时自动降级到 httpx（截图类降级为文本提取）。\n"
            "适用场景：抓取被反爬保护的网页、JS 渲染后才能看到的内容、需要视觉分析的页面。"
        ),
        category="web",
    )
    async def web_fetch(
        self,
        url: str,
        mode: str = "text",
        prompt: str = "",
    ) -> str:
        """
        抓取 URL 内容（反爬版）。

        :param url: 目标网址（必须以 http:// 或 https:// 开头）
        :param mode: 抓取模式 — text / image / llm-text / llm-image
        :param prompt: llm-text / llm-image 模式下的问题（必填，否则降级为 text/image）
        """
        mode = (mode or "text").lower().strip()

        if mode not in ("text", "image", "llm-text", "llm-image"):
            return f"Error: 不支持的 mode '{mode}'，可选: text, image, llm-text, llm-image"

        if not url or not url.startswith(("http://", "https://")):
            return f"Error: URL 必须以 http:// 或 https:// 开头，收到: {url!r}"

        # llm-* 模式必须提供 prompt，否则降级
        if mode.startswith("llm-") and not prompt.strip():
            logger.warning(f"web_fetch: mode={mode} 但 prompt 为空，自动降级到 {mode.replace('llm-', '')}")
            mode = mode.replace("llm-", "")

        try:
            if mode == "text":
                return await self._web_fetch_text(url)
            elif mode == "image":
                return await self._web_fetch_image(url)
            elif mode == "llm-text":
                text = await self._web_fetch_text(url)
                return await self._web_fetch_llm_answer(text, prompt, url, image_mode=False)
            else:  # llm-image
                img_path = await self._web_fetch_image_path(url)
                return await self._web_fetch_llm_answer(img_path, prompt, url, image_mode=True)
        except Exception as e:
            logger.exception(f"web_fetch({mode}) failed for {url}: {e}")
            return f"Error: web_fetch 失败: {e}"

    # ── 主路径：Playwright + 反检测 ───────────────────────────

    async def _web_fetch_text(self, url: str) -> str:
        """Playwright 抓取 → 渲染 → 提取 innerText。失败则降级 httpx。"""
        try:
            text = await self._playwright_fetch_text(url)
            if text and text.strip():
                truncated = text[:100_000]
                note = f"📄 来源: {url}\n"
                if len(text) > 100_000:
                    note += f"⚠️ 内容已截断至 100,000 字符（原文 {len(text):,} 字符）\n"
                return note + "\n" + truncated
        except Exception as e:
            logger.warning(f"Playwright text fetch failed for {url}, falling back to httpx: {e}")

        # curl/httpx 降级
        return await self._httpx_fetch_text(url)

    async def _web_fetch_image(self, url: str) -> str:
        """Playwright 截图 → 保存 → 返回路径。失败则降级到文本提取。"""
        try:
            img_path = await self._playwright_fetch_screenshot(url)
            size_kb = os.path.getsize(img_path) / 1024
            return (
                f"🖼️ 截图已保存: {img_path}\n"
                f"   大小: {size_kb:.1f} KB | 尺寸: 1280x720\n"
                f"   来源: {url}\n"
                f"   （提示：临时文件会在工具调用结束后自动清理）"
            )
        except Exception as e:
            logger.warning(f"Playwright screenshot failed for {url}, falling back to text: {e}")
            text = await self._httpx_fetch_text(url)
            return (
                f"⚠️ 截图失败（{e}），已降级为文本提取：\n\n"
                + text
            )

    async def _web_fetch_image_path(self, url: str) -> str:
        """截图并返回文件路径（供 llm-image 使用）。失败则返回空串。"""
        try:
            return await self._playwright_fetch_screenshot(url)
        except Exception as e:
            logger.warning(f"Playwright screenshot failed for {url}: {e}")
            return ""

    # ── Playwright 内部函数 ─────────────────────────────────

    async def _playwright_fetch_text(self, url: str) -> str:
        """用 Playwright 反检测抓取渲染后的文本。"""
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=self._WEB_FETCH_LAUNCH_ARGS,
            )
            try:
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent=self._WEB_FETCH_USER_AGENT,
                    locale="en-US",
                    timezone_id="America/New_York",
                )

                # 反检测 init script：在每个新页面加载前注入
                await context.add_init_script(
                    """
                    // 隐藏 webdriver 痕迹
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    // 伪造 plugins 数组
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [
                            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                            { name: 'Native Client', filename: 'internal-nacl-plugin' },
                        ],
                    });
                    // 伪造 languages
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en'],
                    });
                    // 隐藏 chrome runtime automation
                    window.chrome = window.chrome || { runtime: {} };
                    // 隐藏 permissions query 自动化特征
                    const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
                    if (originalQuery) {
                        window.navigator.permissions.query = (parameters) =>
                            parameters.name === 'notifications'
                                ? Promise.resolve({ state: Notification.permission })
                                : originalQuery(parameters);
                    }
                    """
                )

                page = await context.new_page()
                # 再次保险：加载前覆盖 webdriver
                await page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                )

                await page.goto(url, wait_until="load", timeout=30_000)
                await asyncio.sleep(1)  # 给 JS 渲染时间

                # 等待一下确保 JS 渲染完成
                await asyncio.sleep(0.5)

                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                return text or ""
            finally:
                await browser.close()

    async def _playwright_fetch_screenshot(self, url: str) -> str:
        """用 Playwright 反检测截图，保存到临时文件，返回路径。"""
        from playwright.async_api import async_playwright

        # 生成临时文件路径
        tmp_dir = tempfile.gettempdir()
        filename = f"webfetch_{int(time.time() * 1000)}_{os.urandom(3).hex()}.png"
        img_path = os.path.join(tmp_dir, filename)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=self._WEB_FETCH_LAUNCH_ARGS,
            )
            try:
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent=self._WEB_FETCH_USER_AGENT,
                    locale="en-US",
                    timezone_id="America/New_York",
                    device_scale_factor=1,
                )

                await context.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [
                            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                            { name: 'Native Client', filename: 'internal-nacl-plugin' },
                        ],
                    });
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en'],
                    });
                    window.chrome = window.chrome || { runtime: {} };
                    const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
                    if (originalQuery) {
                        window.navigator.permissions.query = (parameters) =>
                            parameters.name === 'notifications'
                                ? Promise.resolve({ state: Notification.permission })
                                : originalQuery(parameters);
                    }
                    """
                )

                page = await context.new_page()
                await page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                )

                await page.goto(url, wait_until="load", timeout=30_000)
                await asyncio.sleep(1)  # 给 JS 渲染时间
                await asyncio.sleep(0.5)

                await page.screenshot(path=img_path, type="png", full_page=False)
                return img_path
            finally:
                # 确保浏览器关闭（文件保留给调用者清理）
                await browser.close()

    # ── httpx 降级 ─────────────────────────────────────────

    async def _httpx_fetch_text(self, url: str) -> str:
        """curl/httpx 降级：抓 HTML → 去标签 → 纯文本。"""
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={
                    "User-Agent": self._WEB_FETCH_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                },
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
        except httpx.HTTPError as e:
            return f"Error: HTTP 抓取失败: {e}"
        except Exception as e:
            return f"Error: 抓取 {url} 失败: {e}"

        # 去标签（与 universal_parser._handle_url 同样的策略）
        html = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
        html = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
        html = re.sub(r"<!--[\s\S]*?-->", " ", html)  # 注释
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            return f"Error: 页面内容为空或无法提取（URL: {url}）"

        truncated = text[:100_000]
        note = f"📄 来源: {url}（httpx 降级抓取，未执行 JS）\n"
        if len(text) > 100_000:
            note += f"⚠️ 内容已截断至 100,000 字符（原文 {len(text):,} 字符）\n"
        return note + "\n" + truncated

    # ── LLM/VLM 调用 ────────────────────────────────────────

    async def _web_fetch_llm_answer(
        self,
        input_data: str,
        prompt: str,
        url: str,
        image_mode: bool,
    ) -> str:
        """调用 qwen3.6-flash 处理文本或图片，返回答案。

        input_data: text 模式下是文本；image 模式下是图片文件路径。
        """
        api_key = getattr(settings, "QWEN_API_KEY", None)
        if not api_key:
            return "Error: QWEN_API_KEY 未配置，无法调用 LLM/VLM"

        if image_mode:
            # VLM 图片分析
            if not input_data or not os.path.isfile(input_data):
                return f"Error: 截图文件不存在或已清理: {input_data!r}"
            try:
                answer = await _vlm_chat([input_data], prompt, api_key, max_tokens=4096)
            finally:
                # 清理临时截图
                try:
                    os.remove(input_data)
                except OSError:
                    pass

            if not answer:
                return f"Error: VLM 未返回内容（URL: {url}）"
            return f"🤖 VLM 分析（{VLM_MODEL}）— {url}\n\n{answer}"

        # 文本 LLM
        if not input_data or input_data.startswith("Error:"):
            return input_data or "Error: 文本内容为空"

        # 文本可能很长，截断到 16K 字符保留 prompt 余量
        truncated = input_data[:16_000]
        if len(input_data) > 16_000:
            truncated += f"\n\n[... 内容已截断，原文 {len(input_data):,} 字符 ...]"

        messages = [
            {
                "role": "system",
                "content": "你是一个网页内容分析助手。基于提供的网页文本回答用户问题。",
            },
            {
                "role": "user",
                "content": f"网页内容（来自 {url}）：\n\n{truncated}\n\n问题：{prompt}",
            },
        ]

        try:
            answer = await _qwen_chat(VLM_MODEL, messages, api_key, temperature=0.3, max_tokens=2000)
        except Exception as e:
            logger.warning(f"_qwen_chat for web_fetch failed: {e}")
            answer = None

        if not answer:
            # 降级：返回原始文本，让用户自己看
            return (
                f"⚠️ LLM 调用失败，已返回原始文本（截断至 8K）：\n\n"
                + input_data[:8_000]
            )

        return f"🤖 LLM 分析（{VLM_MODEL}）— {url}\n\n{answer}"
