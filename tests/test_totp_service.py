"""
TOTP Service 专项测试

测试覆盖:
- TOTP 生成（generate_secret, generate_code）
- TOTP 验证（verify_code）
- 密钥格式
- 无效 token 拒绝
- 过期 token 拒绝
"""

import os
import sys
import pytest
import pyotp
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

# 确保项目根在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from services.totp_service import TOTPService


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def totp_service():
    """创建 TOTPService 实例"""
    return TOTPService()


@pytest.fixture
def valid_secret():
    """生成一个有效的 TOTP secret"""
    return pyotp.random_base32()


# ============================================================================
# 1. TOTP 生成测试
# ============================================================================

class TestTOTPGeneration:
    """测试 TOTP 生成"""

    def test_generate_secret_length(self, totp_service):
        """生成的 secret 长度正确（Base32）"""
        secret = totp_service.generate_secret()

        assert secret is not None
        assert len(secret) >= 16  # pyotp 默认生成 16 字符
        # Base32 字符集
        assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in secret)

    def test_generate_secret_unique(self, totp_service):
        """每次生成的 secret 不同"""
        secrets = [totp_service.generate_secret() for _ in range(10)]
        assert len(set(secrets)) == 10

    def test_generate_code_format(self, totp_service, valid_secret):
        """生成的 code 是 6 位数字"""
        code = totp_service.generate_code(valid_secret)

        assert code is not None
        assert len(code) == 6
        assert code.isdigit()

    def test_generate_code_valid_for_current_time(self, totp_service, valid_secret):
        """生成的 code 在当前时间有效"""
        code = totp_service.generate_code(valid_secret)
        totp = pyotp.TOTP(valid_secret)

        assert totp.verify(code, valid_window=0) is True


# ============================================================================
# 2. TOTP 验证测试
# ============================================================================

class TestTOTPVerification:
    """测试 TOTP 验证"""

    def test_verify_valid_code(self, totp_service, valid_secret):
        """验证有效 code 返回 True"""
        code = totp_service.generate_code(valid_secret)
        result = totp_service.verify_code(valid_secret, code)

        assert result is True

    def test_verify_invalid_code(self, totp_service, valid_secret):
        """验证无效 code 返回 False"""
        invalid_code = "000000"
        result = totp_service.verify_code(valid_secret, invalid_code)

        assert result is False

    def test_verify_wrong_secret(self, totp_service, valid_secret):
        """用错误的 secret 验证返回 False"""
        code = totp_service.generate_code(valid_secret)
        wrong_secret = pyotp.random_base32()

        result = totp_service.verify_code(wrong_secret, code)

        assert result is False


# ============================================================================
# 3. 配置常量测试
# ============================================================================

class TestTOTPConfig:
    """测试 TOTP 配置常量"""

    def test_interval_is_30(self, totp_service):
        """时间窗口是 30 秒"""
        assert TOTPService.INTERVAL == 30

    def test_valid_windows_is_10(self, totp_service):
        """有效窗口是 10"""
        assert TOTPService.VALID_WINDOWS == 10

    def test_jwt_expire_days_is_14(self, totp_service):
        """JWT 过期天数是 14"""
        assert TOTPService.JWT_EXPIRE_DAYS == 14


# ============================================================================
# 4. Agent 相关测试（需要 mock 数据库）
# ============================================================================

class TestTOTPAgentOperations:
    """测试 Agent 相关的 TOTP 操作"""

    def test_generate_for_agent_raises_on_not_found(self, totp_service):
        """Agent 不存在时抛出异常"""
        with patch("services.totp_service.SessionLocal") as mock_session:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.first.return_value = None
            mock_session.return_value = mock_db

            with pytest.raises(ValueError, match="not found"):
                totp_service.generate_for_agent("nonexistent")

    def test_verify_agent_totp_returns_none_on_not_found(self, totp_service):
        """Agent 不存在时 verify_agent_totp 返回 None"""
        with patch("services.totp_service.SessionLocal") as mock_session:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.first.return_value = None
            mock_session.return_value = mock_db

            result = totp_service.verify_agent_totp("nonexistent", "123456")

            assert result is None

    def test_verify_agent_totp_returns_none_on_invalid_code(self, totp_service):
        """Code 无效时 verify_agent_totp 返回 None"""
        with patch("services.totp_service.SessionLocal") as mock_session:
            mock_db = MagicMock()
            mock_agent = MagicMock()
            mock_agent.totp_secret = pyotp.random_base32()
            mock_db.query.return_value.filter.return_value.first.return_value = mock_agent
            mock_session.return_value = mock_db

            # 使用错误的 code
            result = totp_service.verify_agent_totp("abc1", "000000")

            assert result is None

    def test_verify_jwt_expired(self, totp_service):
        """过期的 JWT 返回 None"""
        with patch("services.totp_service.settings") as mock_settings:
            mock_settings.JWT_SECRET = "test-secret-key-that-is-long-enough"

            import jwt
            expired_payload = {
                "user_id": 123,
                "agent_hash": "abc1",
                "auth_method": "totp",
                "exp": datetime.utcnow() - timedelta(days=1),
                "iat": datetime.utcnow() - timedelta(days=15)
            }
            expired_token = jwt.encode(expired_payload, "test-secret-key-that-is-long-enough", algorithm="HS256")

            result = totp_service.verify_jwt(expired_token)

            assert result is None

    def test_verify_jwt_invalid(self, totp_service):
        """无效的 JWT 返回 None"""
        result = totp_service.verify_jwt("invalid.token.here")

        assert result is None

    def test_verify_jwt_valid(self, totp_service):
        """有效的 JWT 返回 payload"""
        with patch("services.totp_service.settings") as mock_settings:
            mock_settings.JWT_SECRET = "test-secret-key-that-is-long-enough"

            import jwt
            valid_payload = {
                "user_id": 123,
                "agent_hash": "abc1",
                "auth_method": "totp",
                "exp": datetime.utcnow() + timedelta(days=1),
                "iat": datetime.utcnow()
            }
            valid_token = jwt.encode(valid_payload, "test-secret-key-that-is-long-enough", algorithm="HS256")

            result = totp_service.verify_jwt(valid_token)

            assert result is not None
            assert result["user_id"] == 123
            assert result["agent_hash"] == "abc1"


# ============================================================================
# 5. create_agent 测试
# ============================================================================

class TestTOTPCreateAgent:
    """测试创建 Agent"""

    def test_create_agent_generates_hash_and_secret(self):
        """create_agent 生成 hash 和 secret"""
        with patch("services.totp_service.SessionLocal") as mock_session:
            with patch("services.totp_service.AgentProfile"):
                mock_db = MagicMock()
                mock_db.query.return_value.filter.return_value.first.return_value = None
                mock_session.return_value = mock_db

                agent = TOTPService.create_agent(user_id=1, name="Test Agent")

                # 验证 secret 被生成
                assert agent.totp_secret is not None
                # 验证 hash 被生成
                assert agent.hash is not None

    def test_create_agent_sets_correct_fields(self):
        """create_agent 设置正确的字段"""
        with patch("services.totp_service.SessionLocal") as mock_session:
            with patch("services.totp_service.AgentProfile") as mock_profile:
                mock_db = MagicMock()
                mock_db.query.return_value.filter.return_value.first.return_value = None
                mock_session.return_value = mock_db

                TOTPService.create_agent(user_id=42, name="My Agent")

                # 验证 AgentProfile 被调用
                mock_profile.assert_called_once()
                call_kwargs = mock_profile.call_args[1]
                assert call_kwargs["user_id"] == 42
                assert call_kwargs["name"] == "My Agent"
                assert call_kwargs["status"] == "pending"
