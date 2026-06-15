"""
VFS Indexer - VFS 文件自动索引调度器

流程:
1. 读取文件内容（通过 VFS cat / COS 直接读取）
2. Markdown 分块
3. 删除该文件的旧向量（文件更新/覆盖时）
4. 批量嵌入
5. 写入 COS 向量桶（index: idx-{agent_hash}-kb）
"""

import asyncio
import hashlib
import json
import logging
from typing import List, Optional

from services.vfs_markdown_chunker import MarkdownChunker
from services.embedding_service import EmbeddingService
from services.vector_search_service import (
    VectorSearchService,
    VECTOR_BUCKET,
)

logger = logging.getLogger(__name__)


class VfsIndexer:
    """VFS 文件自动索引调度器"""

    def __init__(self, agent_hash: str, file_path: str):
        self.agent_hash = agent_hash
        self.file_path = file_path
        self._chunker = MarkdownChunker()
        self._embedder = EmbeddingService()
        self._vector_service = VectorSearchService(agent_hash=agent_hash)

    async def run(self):
        """执行索引流程（异步，fire-and-forget）"""
        try:
            # 1. 读取文件内容
            content = await self._read_file()
            if not content:
                logger.debug(f"[VFS Index] 文件为空或无法读取: {self.file_path}")
                return

            # 2. Markdown / 纯文本分块
            if self.file_path.endswith(".md"):
                chunks = self._chunker.chunk(content, self.file_path, self.agent_hash)
            else:
                # .txt 文件按段落切分
                chunks = self._chunker._chunk_by_paragraph(
                    content, self.file_path, self.agent_hash
                )

            if not chunks:
                logger.debug(f"[VFS Index] 无有效分块: {self.file_path}")
                return

            # 3. 删除该文件的旧向量（先清理再写入）
            await self._delete_file_vectors()

            # 4. 批量嵌入
            texts = [c.content for c in chunks]
            vectors = await self._embedder.embed_batch(texts)

            # 5. 写入索引
            index_name = self._get_index_name()
            items = []
            for chunk, vec in zip(chunks, vectors):
                if not vec:
                    continue
                chunk_dict = chunk.to_dict()
                chunk_dict["vector"] = vec
                items.append({
                    "key": f"vfs::{self.file_path}::{chunk.chunk_id}",
                    "text": chunk.content,
                    "metadata": {
                        "source": "vfs_index",
                        "file_path": self.file_path,
                        "chunk_id": chunk.chunk_id,
                        "headings": json.dumps(chunk.headings, ensure_ascii=False),
                        "text": chunk.content[:500],  # 搜索展示用
                    },
                })

            if items:
                await self._vector_service.index_batch(items, index_name)
                logger.info(
                    f"[VFS Index] 已索引 {len(items)} 块: {self.file_path} -> {index_name}"
                )

        except Exception as e:
            logger.error(f"[VFS Index] 索引失败: {self.file_path} - {e}", exc_info=True)

    async def _read_file(self) -> Optional[str]:
        """读取文件内容"""
        try:
            from services.virtual_filesystem import VirtualFileSystem
            vfs = VirtualFileSystem(agent_hash=self.agent_hash)
            content = vfs.cat(self.file_path)
            if content and not content.startswith("Error"):
                return content
            return None
        except Exception as e:
            logger.debug(f"[VFS Index] read_file failed: {e}")
            return None

    async def _delete_file_vectors(self):
        """删除该文件的所有现有向量索引"""
        try:
            index_name = self._get_index_name()
            key_prefix = f"vfs::{self.file_path}::"
            client = self._vector_service._get_client()

            # 通过 list_objects 找出该文件的所有旧向量 key
            _, data = await asyncio.to_thread(
                client.list_objects,
                Bucket=VECTOR_BUCKET,
                Prefix=key_prefix,
            )

            keys = []
            if data and isinstance(data, dict):
                for obj in data.get("Contents", []):
                    key = obj.get("Key", "")
                    if key.startswith(key_prefix):
                        keys.append(key)

            if keys:
                await self._vector_service.delete(keys, index_name)
                logger.info(
                    f"[VFS Index] Deleted {len(keys)} old vectors for {self.file_path}"
                )
        except Exception as e:
            logger.warning(
                f"[VFS Index] Failed to delete old vectors for {self.file_path}: {e}"
            )

    def _get_index_name(self) -> str:
        """获取索引名称"""
        return self._vector_service._get_index_name("kb")
