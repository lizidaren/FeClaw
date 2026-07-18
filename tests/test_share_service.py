"""
Share Service 专项测试

测试覆盖:
- 分享链接创建（path 模式）
- 分享链接验证
- 过期处理
"""

import os
import sys
import pytest
import time
import hmac
import hashlib
import base64
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

# 确保项目根在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from services.share_service import (
    _compute_share_hash,
    _encode_share_token,
    decode_share_token,
    create_share_link,
    cleanup_expired_references,
)


# ============================================================================
# 1. HMAC 哈希计算测试
# ============================================================================

class TestShareHash:
    """测试 _compute_share_hash"""

    def test_compute_share_hash_format(self):
        """哈希是 16 位十六进制字符串"""
        with patch("services.share_service.settings") as mock_settings:
            mock_settings.JWT_SECRET = "test-secret"

            hash_result = _compute_share_hash("/path/to/file.txt")

            assert isinstance(hash_result, str)
            assert len(hash_result) == 16
            assert all(c in "0123456789abcdef" for c in hash_result)

    def test_compute_share_hash_deterministic(self):
        """相同路径产生相同哈希"""
        with patch("services.share_service.settings") as mock_settings:
            mock_settings.JWT_SECRET = "test-secret"

            path = "/path/to/file.txt"
            hash1 = _compute_share_hash(path)
            hash2 = _compute_share_hash(path)

            assert hash1 == hash2

    def test_compute_share_hash_different_paths(self):
        """不同路径产生不同哈希"""
        with patch("services.share_service.settings") as mock_settings:
            mock_settings.JWT_SECRET = "test-secret"

            hash1 = _compute_share_hash("/path/a.txt")
            hash2 = _compute_share_hash("/path/b.txt")

            assert hash1 != hash2


# ============================================================================
# 2. Token 编码解码测试
# ============================================================================

class TestShareTokenCodec:
    """测试 _encode_share_token 和 decode_share_token"""

    def test_encode_token_format(self):
        """编码后的 token 是 base64 字符串"""
        with patch("services.share_service.settings") as mock_settings:
            mock_settings.JWT_SECRET = "test-secret"

            token = _encode_share_token("abc123", int(time.time()) + 3600)

            assert isinstance(token, str)
            assert len(token) > 0

    def test_encode_token_is_base64(self):
        """编码后的 token 可以被 base64 解码"""
        with patch("services.share_service.settings") as mock_settings:
            mock_settings.JWT_SECRET = "test-secret"

            token = _encode_share_token("abc123", int(time.time()) + 3600)

            # 应该能正常解码（补齐 padding）
            padded = token + "=" * (4 - len(token) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode())

            assert isinstance(decoded, bytes)

    def test_decode_token_expired(self):
        """过期的 token 返回 None"""
        with patch("services.share_service.settings") as mock_settings:
            mock_settings.JWT_SECRET = "test-secret"

            share_hash = "abc123def4567890"
            expires_at = int(time.time()) - 1  # 已过期
            token = _encode_share_token(share_hash, expires_at)

            result = decode_share_token(token)

            assert result is None

    def test_decode_token_invalid_signature(self):
        """无效签名的 token 返回 None"""
        with patch("services.share_service.settings") as mock_settings:
            mock_settings.JWT_SECRET = "test-secret"

            # 创建一个 token 然后篡改
            share_hash = "abc123def4567890"
            expires_at = int(time.time()) + 3600
            token = _encode_share_token(share_hash, expires_at)

            # 篡改 token 最后几个字符
            tampered_token = token[:-5] + "xxxxx"

            result = decode_share_token(tampered_token)

            assert result is None

    def test_decode_token_invalid_base64(self):
        """无效 base64 的 token 返回 None"""
        result = decode_share_token("not-valid-base64!!!")

        assert result is None

    def test_decode_token_missing_parts(self):
        """缺少部分的 token 返回 None"""
        result = decode_share_token("abc123")

        assert result is None


# ============================================================================
# 3. 分享链接创建测试（path 模式）
# ============================================================================

class TestShareLinkCreation:
    """测试 create_share_link"""

    def test_create_share_link_mode_path(self):
        """mode=path 直接返回 URL"""
        with patch("services.share_service.settings") as mock_settings:
            mock_settings.FECLAW_STATIC_DOMAIN = "static.feclaw.chat"

            result = create_share_link(
                vfs_path="/files/doc.pdf",
                mode="path",
            )

            assert result is not None
            assert "url" in result
            assert "/files/doc.pdf" in result["url"]

    def test_create_share_link_mode_path_preserves_slash(self):
        """mode=path 保留前导斜杠"""
        with patch("services.share_service.settings") as mock_settings:
            mock_settings.FECLAW_STATIC_DOMAIN = "static.feclaw.chat"

            result = create_share_link(
                vfs_path="/workspace/file.txt",
                mode="path",
            )

            assert result["url"].startswith("https://")


# ============================================================================
# 4. 边缘情况测试
# ============================================================================

class TestShareServiceEdgeCases:
    """边缘情况测试"""

    def test_decode_share_token_exception_handling(self):
        """解码异常时返回 None"""
        result = decode_share_token(None)

        assert result is None
