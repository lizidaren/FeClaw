"""
重排序服务 - 通过 Qwen3-Rerank 对向量搜索结果进行精排
"""

import logging
import threading
from typing import Dict, List, Optional

import httpx

from config import settings

# 模块级共享 HTTP 客户端（避免每次实例化创建新连接）
_shared_rerank_client: httpx.AsyncClient = None
_rerank_client_lock = threading.Lock()


def _get_rerank_client() -> httpx.AsyncClient:
    """获取共享的 httpx.AsyncClient 单例"""
    global _shared_rerank_client
    if _shared_rerank_client is None:
        with _rerank_client_lock:
            if _shared_rerank_client is None:
                _shared_rerank_client = httpx.AsyncClient(timeout=30.0)
    return _shared_rerank_client

async def close_rerank_client():
    """关闭共享的 httpx.AsyncClient（应用 shutdown 时调用）"""
    global _shared_rerank_client
    if _shared_rerank_client is not None:
        await _shared_rerank_client.aclose()
        _shared_rerank_client = None
        logger.info("Rerank HTTP client closed")

logger = logging.getLogger(__name__)

RERANK_URL = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
RERANK_MODEL = "qwen3-rerank"

# 截断参数 (from tests/rerank/test_rerank_v3_optimized.py)
# Qwen tokenizer: ~1.8 chars/token for Chinese-dominant text
# Rerank max: 4000 tokens per doc, leave ~500 for query
# Safe limit: 3500 tokens ≈ 6300 chars
MAX_TOKENS = 3500
CHARS_PER_TOKEN = 1.8
MAX_CHARS = int(MAX_TOKENS * CHARS_PER_TOKEN)  # ~6300


def smart_truncate(text: str) -> str:
    """保留头 + 按比例保留中间 + 尾"""
    if len(text) <= MAX_CHARS:
        return text

    head_len = 1000
    tail_len = 800
    middle_max = MAX_CHARS - head_len - tail_len
    middle_start = head_len
    middle_end = len(text) - tail_len

    if middle_end <= middle_start:
        return text[:head_len] + text[-tail_len:]

    middle_text = text[middle_start:middle_end]
    if len(middle_text) > middle_max:
        mid_head = middle_text[:middle_max // 2]
        mid_tail = middle_text[-(middle_max - middle_max // 2):]
        middle_text = mid_head + "\n...(snip)...\n" + mid_tail

    return text[:head_len] + "\n...\n" + middle_text + "\n...\n" + text[-tail_len:]


class RerankService:
    """Qwen3-Rerank 重排序服务"""

    def __init__(self):
        self._client = _get_rerank_client()

    @staticmethod
    def _get_api_key() -> Optional[str]:
        import os

        return settings.QWEN_API_KEY or os.getenv("QWEN_API_KEY")

    def _fallback_sort(self, documents: List[dict], top_n: int) -> List[dict]:
        """Fallback: sort by vector score without mutating input documents."""
        return sorted(
            ({**d, "rerank_score": d.get("score", 0)} for d in documents),
            key=lambda d: d.get("score", 0),
            reverse=True,
        )[:top_n]

    async def rerank(
        self,
        query: str,
        documents: List[dict],
        top_n: int = 5,
    ) -> List[dict]:
        """对文档列表进行重排序

        Args:
            query: 用户查询
            documents: [{'text': str, 'metadata': dict, ...}, ...]
            top_n: 返回前 N 条结果

        Returns:
            排序后的文档列表，每条增加 rerank_score 字段
        """
        if not documents:
            return []

        api_key = self._get_api_key()
        if not api_key:
            logger.warning("QWEN_API_KEY not configured, skipping rerank")
            return self._fallback_sort(documents, top_n)

        # 提取文本，跳过空文档
        texts = []
        valid_indices = []
        for i, d in enumerate(documents):
            text = d.get("text", d.get("metadata", {}).get("text", ""))
            if text and text.strip():
                texts.append(smart_truncate(text))
                valid_indices.append(i)

        if not texts:
            logger.warning("Rerank: all documents have empty text, falling back to vector scores")
            return self._fallback_sort(documents, top_n)

        # Token 估算日志
        total_chars = sum(len(t) for t in texts)
        estimated_tokens = int(total_chars / CHARS_PER_TOKEN)
        logger.info(
            "Rerank: %d docs (skipped %d), ~%d chars (~%d tokens), top_n=%d",
            len(texts), len(documents) - len(texts), total_chars, estimated_tokens, min(top_n, len(texts)),
        )

        payload = {
            "model": RERANK_MODEL,
            "query": query,
            "documents": texts,
            "parameters": {
                "top_n": min(top_n, len(texts)),
                "return_documents": False,
            },
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._client.post(RERANK_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Rerank API call failed: %s, falling back to vector scores", e)
            return self._fallback_sort(documents, top_n)

        output = data.get("output", {})
        results = output.get("results") if isinstance(output, dict) else None
        if not results:
            results = data.get("results", [])

        if not results:
            logger.warning("Rerank returned empty results, falling back to vector scores")
            return self._fallback_sort(documents, top_n)

        # 按 reranker 索引顺序重建结果
        reranked = []
        for r in results:
            api_idx = r["index"]
            relevance = r.get("relevance_score", 0)
            if api_idx < len(valid_indices):
                doc_idx = valid_indices[api_idx]
                doc = dict(documents[doc_idx])
                doc["rerank_score"] = relevance
                reranked.append(doc)

        logger.info(
            "Rerank: %d docs -> %d results, top score=%.4f",
            len(texts), len(reranked),
            reranked[0]["rerank_score"] if reranked else 0,
        )
        return reranked
