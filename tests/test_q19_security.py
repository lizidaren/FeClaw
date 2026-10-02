"""
Q19 安全审计 Critical 组修复回归测试

每个修过的点至少一条「未授权 ⇒ 401/403」断言（审计报告 §7.15 要求）：
- C1  沙箱 fail-closed（bwrap 不可用 ⇒ 拒绝；非 0 退出 ⇒ 不回退）
- C2  TOTP 越权（非 owner generate ⇒ 403）+ verify 限流
- C5/C9 setup 鉴权（匿名 ⇒ 401/404）
- C6  public-config 白名单（不泄 DATABASE_URL / SETUP_TOKEN / *_KEY）
- C7  sync host 白名单（attacker.com/.feclaw.chat ⇒ 不同源外跳）
- C8  SSRF URL 校验（元数据 / 回环 / 私网 / 十进制 IP / 非 http）
- H1  apps_gateway 鉴权（未登录 401 / 非 owner 403）
- H3  sandbox 归属（非 owner 403）
- H13 vfs_view 归属（非 owner 403）
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


# ======================================================================
# C8 —— 出站 URL 校验（纯函数，不碰网络）
# ======================================================================

class TestUrlValidation:
    def test_reject_metadata_ip(self):
        from utils.url_validation import validate_public_http_url
        assert validate_public_http_url("http://169.254.169.254/latest/meta-data/iam/security-credentials/") is False

    def test_reject_loopback(self):
        from utils.url_validation import validate_public_http_url
        assert validate_public_http_url("http://127.0.0.1:8000/x") is False
        assert validate_public_http_url("http://[::1]/x") is False

    def test_reject_private_ranges(self):
        from utils.url_validation import validate_public_http_url
        assert validate_public_http_url("http://10.0.0.1/x") is False
        assert validate_public_http_url("http://192.168.1.1/x") is False
        assert validate_public_http_url("http://172.16.0.1/x") is False

    def test_reject_decimal_ip_notation(self):
        from utils.url_validation import validate_public_http_url
        assert validate_public_http_url("http://2130706433/x") is False  # 127.0.0.1

    def test_reject_non_http_scheme(self):
        from utils.url_validation import validate_public_http_url
        assert validate_public_http_url("file:///etc/passwd") is False
        assert validate_public_http_url("gopher://localhost:80/_") is False

    def test_accept_public_url(self):
        from utils import url_validation
        with patch.object(
            url_validation.socket, "getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
        ):
            assert url_validation.validate_public_http_url("https://example.com/a.png") is True


# ======================================================================
# C7 —— sync host 白名单
# ======================================================================

class TestSyncHostname:
    def test_valid_subdomain_allowed(self):
        from config import settings
        from routers.feclaw_domain import _safe_sync_hostname
        with patch.object(settings, "FECLAW_PUBLIC_URL", "feclaw.chat"):
            assert _safe_sync_hostname("5178.feclaw.chat") == "5178.feclaw.chat"

    def test_attacker_suffix_bypass_rejected(self):
        from config import settings
        from routers.feclaw_domain import _safe_sync_hostname
        with patch.object(settings, "FECLAW_PUBLIC_URL", "feclaw.chat"):
            assert _safe_sync_hostname("attacker.com/.feclaw.chat") is None

    def test_no_domain_configured_rejects_all(self):
        from config import settings
        from routers.feclaw_domain import _safe_sync_hostname
        with patch.object(settings, "FECLAW_PUBLIC_URL", ""):
            assert _safe_sync_hostname("attacker.com") is None
            assert _safe_sync_hostname("5178.feclaw.chat") is None


# ======================================================================
# C2 —— /api/totp/verify 限流
# ======================================================================

class TestTotpVerifyRateLimit:
    def test_blocks_after_max_attempts(self):
        from routers.feclaw_domain import (
            _totp_verify_rate_limited, _totp_verify_attempts, _TOTP_VERIFY_MAX,
        )
        _totp_verify_attempts.clear()
        key = "203.0.113.7:5178"
        for _ in range(_TOTP_VERIFY_MAX):
            assert _totp_verify_rate_limited(key) is False
        assert _totp_verify_rate_limited(key) is True


# ======================================================================
# C1 —— 沙箱 fail-closed
# ======================================================================

class TestSandboxFailClosed:
    def test_bwrap_unavailable_refuses(self):
        from services.sandbox_manager import SandboxManager
        mgr = object.__new__(SandboxManager)
        mgr._bwrap_available = False
        result = mgr._execute_with_sandbox("print(1)", 10, "sid")
        assert result.exit_code == 1
        assert "refused" in result.stderr.lower()

    def test_nonzero_exit_does_not_fallback(self):
        from services.sandbox_manager import SandboxManager
        mgr = object.__new__(SandboxManager)
        mgr._bwrap_available = True
        fake_result = type("R", (), {"exit_code": 1, "stderr": "boom", "stdout": "", "sandbox_id": "sid"})()
        mgr._execute_with_bwrap = MagicMock(return_value=fake_result)
        mgr._execute_with_subprocess_safe = MagicMock()
        result = mgr._execute_with_sandbox("x", 10, "sid")
        assert result.exit_code == 1
        # 关键：绝不回退到无隔离 subprocess 重跑
        mgr._execute_with_subprocess_safe.assert_not_called()


# ======================================================================
# 路由级 —— 未授权 ⇒ 401 / 403
# ======================================================================

class TestPublicConfigWhitelist:
    def test_no_sensitive_fields(self, client):
        resp = client.get("/api/console/public-config")
        assert resp.status_code == 200
        cfg = resp.json().get("config", {})
        assert "DATABASE_URL" not in cfg
        assert "SETUP_TOKEN" not in cfg
        assert "JWT_SECRET" not in cfg
        assert "MYSQL_USER" not in cfg
        assert "MYSQL_HOST" not in cfg
        for k in cfg:
            ku = k.upper()
            assert "TOKEN" not in ku
            assert "SECRET" not in ku
            assert "PASSWORD" not in ku


class TestSetupAuth:
    def test_admin_endpoint_requires_admin(self, client):
        resp = client.post("/setup/admin", json={})
        assert resp.status_code == 401

    def test_database_endpoint_refused_in_normal_mode(self, client):
        resp = client.post("/setup/database", json={})
        assert resp.status_code == 404

    def test_api_keys_requires_admin(self, client):
        resp = client.post("/setup/api-keys", json={"keys": {"JWT_SECRET": "x"}})
        assert resp.status_code == 401

    def test_storage_requires_admin(self, client):
        resp = client.post("/setup/storage", json={"database_url": "mysql+pymysql://x"})
        assert resp.status_code == 401


class TestTotpOwnership:
    def test_non_owner_generate_returns_403(self, real_db, client):
        import pyotp
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        owner = User(username="owner", password_hash=hash_password("pw"), salt=None, is_admin=False)
        attacker = User(username="attacker", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add_all([owner, attacker])
        db.commit()
        owner_id = owner.id
        attacker_id = attacker.id
        db.add(AgentProfile(user_id=owner_id, hash="abcd", totp_secret=pyotp.random_base32(), name="a", status="pending"))
        db.commit()
        db.close()

        token = create_jwt_token({"sub": str(attacker_id)})
        with patch("routers.feclaw_domain.SessionLocal", real_db):
            resp = client.post(
                "/api/totp/generate",
                json={"agent_hash": "abcd", "code": ""},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 403


class TestSandboxOwnership:
    def test_non_owner_execute_returns_403(self, real_db, client):
        import pyotp
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        owner = User(username="owner2", password_hash=hash_password("pw"), salt=None, is_admin=False)
        attacker = User(username="attacker2", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add_all([owner, attacker])
        db.commit()
        owner_id = owner.id
        attacker_id = attacker.id
        db.add(AgentProfile(user_id=owner_id, hash="abcd", totp_secret=pyotp.random_base32(), name="a", status="pending"))
        db.commit()
        db.close()

        token = create_jwt_token({"sub": str(attacker_id)})
        with patch("routers.sandbox.SessionLocal", real_db):
            resp = client.post(
                "/api/sandbox/execute",
                json={"code": "print(1)", "agent_hash": "abcd"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 403


class TestVfsViewOwnership:
    def test_non_owner_view_returns_403(self, real_db, client):
        import pyotp
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        owner = User(username="owner3", password_hash=hash_password("pw"), salt=None, is_admin=False)
        attacker = User(username="attacker3", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add_all([owner, attacker])
        db.commit()
        owner_id = owner.id
        attacker_id = attacker.id
        db.add(AgentProfile(user_id=owner_id, hash="abcd", totp_secret=pyotp.random_base32(), name="a", status="pending"))
        db.commit()
        db.close()

        token = create_jwt_token({"sub": str(attacker_id)})
        resp = client.get(
            "/api/vfs/view?path=agents/abcd/workspace/soul.md",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestAppsGatewayAuth:
    def test_register_unauthenticated_returns_401(self, client):
        resp = client.post(
            "/api/apps/register",
            json={"app_id": "myapp"},
            headers={"Host": "abcd.example.com"},
        )
        assert resp.status_code == 401

    def test_register_non_owner_returns_403(self, real_db, client):
        import pyotp
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        owner = User(username="owner4", password_hash=hash_password("pw"), salt=None, is_admin=False)
        attacker = User(username="attacker4", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add_all([owner, attacker])
        db.commit()
        owner_id = owner.id
        attacker_id = attacker.id
        db.add(AgentProfile(user_id=owner_id, hash="abcd", totp_secret=pyotp.random_base32(), name="a", status="pending"))
        db.commit()
        db.close()

        token = create_jwt_token({"sub": str(attacker_id)})
        resp = client.post(
            "/api/apps/register",
            json={"app_id": "myapp"},
            headers={"Authorization": f"Bearer {token}", "Host": "abcd.example.com"},
        )
        assert resp.status_code == 403
