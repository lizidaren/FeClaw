#!/usr/bin/env python3
"""
COS SDK StreamBody.read() 补丁

SDK 的 StreamBody.read(chunk_size=1024) 默认只读取一个 1024 字节的 chunk，
而不是读取全部内容，与 Python 标准 read() 行为不一致。

此补丁将 read() 改为循环读取到 EOF，使行为符合预期。
"""

import logging

logger = logging.getLogger(__name__)


def apply_cos_patch():
    """应用 CosS3Client StreamBody.read() 补丁"""
    try:
        from qcloud_cos.streambody import StreamBody

        _original_read = StreamBody.read

        def _patched_read(self, chunk_size=4096, auto_decompress=False):
            """读取 StreamBody 全部内容（而非仅一个 chunk）"""
            from requests.exceptions import StreamConsumedError
            chunks = []
            while True:
                try:
                    chunk = _original_read(self, chunk_size, auto_decompress)
                except StreamConsumedError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b''.join(chunks)

        StreamBody.read = _patched_read
        logger.info("[COS_Patch] StreamBody.read() patched - now reads full content")
        return True
    except Exception as e:
        logger.warning(f"[COS_Patch] Failed to patch StreamBody.read(): {e}")
        return False
