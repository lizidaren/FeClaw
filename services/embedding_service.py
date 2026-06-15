"""
Embedding Service - 文本嵌入服务封装

复用 VectorSearchService 中的 embedding API 调用逻辑。
封装 Qwen3 text-embedding-v4 的批量/单条嵌入能力。
"""

import logging
from typing import List, Optional

from services.vector_search_service import VectorSearchService

logger = logging.getLogger(__name__)


class EmbeddingService:
    """文本嵌入服务

    复用 VectorSearchService 的 _call_embedding_api 能力，
    提供更简洁的 embed/embed_batch 接口。
    """

    MODEL = "text-embedding-v4"
    DIMENSION = 1024
    MAX_BATCH = 10

    def __init__(self):
        self._vector_service = VectorSearchService()

    async def embed(self, text: str) -> List[float]:
        """单条文本 → 向量"""
        try:
            return await self._vector_service.embed(text)
        except Exception as e:
            logger.error(f"[Embedding] embed failed: {e}")
            return []

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量文本 → 向量列表"""
        try:
            return await self._vector_service.embed_batch(texts)
        except Exception as e:
            logger.error(f"[Embedding] embed_batch failed: {e}")
            return [[0.0] * 1024 for _ in texts]
