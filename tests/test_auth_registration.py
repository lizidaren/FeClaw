"""
T18a —— 注册门禁、登录会话、改密与限流后端（认证集成测试）

覆盖（对应 spec §5 T18 / §9 A21）：

1. **注册四种模式**：`REGISTRATION_MODE=open|invite|closed`，以及
   「配了 invite 但没配邀请码」的 fail-closed 分支。
2. **注册限流 + 最小审计日志**（不落密码/邀请码/token）。
3. **登录会话**：HttpOnly cookie、`redirect_to` 白名单、待停用分支。
4. **改密**：CSRF 保护、限流、成功后其它设备令牌失效、当前会话续签。
5. **会话接管** `/api/user/session`：只接受会话类令牌。
6. **限流后端切换**：memory / redis（fake 客户端）/ Redis 不可用时的
   `RATE_LIMIT_FAIL_MODE=open|closed`。

运行说明：需要完整依赖（fastapi/pydantic-settings/sqlalchemy/jose/bcrypt...）。
本工作副本的 `.venv` 在写这些用例时仍在安装，因此用例与实现一起交付，
执行记录见 T18a 报告的「测试命令/退出码」栏；未跑过的项在报告里明确标未验证。
"""

import logging
import os

import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app


# ======================================================================
# 基础设施
# ======================================================================

@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_limiters():
    """每个用例前清空全部限流桶（进程内单例，否则用例之间互相污染）。"""
    from services import rate_limiter as rl

    for limiter in (
        rl.login_limiter, rl.login_ip_limiter, rl.register_limiter,
        rl.password_change_limiter, rl.totp_verify_limiter, rl.share_unlock_limiter,
    ):
        limiter.clear()
    yield


@pytest.fixture(autouse=True)
def _default_registration_settings(monkeypatch):
    """把注册相关配置恢复成「默认值」，避免开发机 .env 影响用例。"""
    monkeypatch.setattr(settings, "OAUTH_ENABLED", False)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite")
    monkeypatch.setattr(settings, "REGISTRATION_INVITE_CODE", "")
    monkeypatch.setattr(settings, "REGISTRATION_DEFAULT_TIER", "free")
    yield


def _register(client, **body):
    return client.post("/api/user/register", json=body)


def _seed_user(real_db, name="alice", password="pw123456", **extra):
    from models.database import User
    from utils.auth import hash_password

    db = real_db()
    user = User(username=name, password_hash=hash_password(password), salt=None, is_admin=False, **extra)
    db.add(user)
    db.commit()
    db.refresh(user)
    uid = user.id
    db.close()
    return uid


def _count_users(real_db):
    from models.database import User

    db = real_db()
    try:
        return db.query(User).count()
    finally:
        db.close()


def _login(client, name="alice", password="pw123456", **kw):
    return client.post("/api/user/login", json={"username": name, "password": password}, **kw)


# ======================================================================
# 1. 注册模式
# ======================================================================

class TestRegistrationModes:
    def test_closed_returns_403_and_creates_nothing(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "closed")
        before = _count_users(real_db)

        resp = _register(client, username="newbie", password="pw123456")

        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["error"] == "registration_closed"
        assert _count_users(real_db) == before

    def test_invite_without_code_rejected(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite")
        monkeypatch.setattr(settings, "REGISTRATION_INVITE_CODE", "S3CRET-INVITE")
        before = _count_users(real_db)

        resp = _register(client, username="newbie", password="pw123456")

        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "invalid_invite_code"
        assert _count_users(real_db) == before

    def test_invite_with_wrong_code_rejected(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite")
        monkeypatch.setattr(settings, "REGISTRATION_INVITE_CODE", "S3CRET-INVITE")

        resp = _register(client, username="newbie", password="pw123456", invite_code="nope")

        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "invalid_invite_code"

    def test_invite_with_correct_code_succeeds(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite")
        monkeypatch.setattr(settings, "REGISTRATION_INVITE_CODE", "S3CRET-INVITE")
        before = _count_users(real_db)

        resp = _register(client, username="newbie", password="pw123456", invite_code="S3CRET-INVITE")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success"
        assert body["user_id"]
        assert _count_users(real_db) == before + 1

    def test_invite_mode_without_configured_code_fails_closed(self, real_db, client, monkeypatch):
        """配了 invite 但没配邀请码 ⇒ 等价 closed（不能变成谁都能注册）。"""
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite")
        monkeypatch.setattr(settings, "REGISTRATION_INVITE_CODE", "")
        before = _count_users(real_db)

        resp = _register(client, username="newbie", password="pw123456", invite_code="")

        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "registration_unavailable"
        assert _count_users(real_db) == before

    def test_open_mode_allows_registration(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "open")

        resp = _register(client, username="openuser", password="pw123456")

        assert resp.status_code == 200, resp.text
        assert resp.json()["user_id"]

    def test_oauth_enabled_blocks_local_registration(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "OAUTH_ENABLED", True)
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "open")

        resp = _register(client, username="oauthblocked", password="pw123456")

        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "oauth_required"

    def test_unknown_mode_fails_closed(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "wiDe-open-oops")

        resp = _register(client, username="typo", password="pw123456")

        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "registration_closed"


# ======================================================================
# 2. 新用户档位（不再默认无限 pro）
# ======================================================================

class TestNewUserTier:
    def test_open_mode_sets_free_tier_by_default(self, real_db, client, monkeypatch):
        from models.database import User

        monkeypatch.setattr(settings, "REGISTRATION_MODE", "open")
        monkeypatch.setattr(settings, "REGISTRATION_DEFAULT_TIER", "free")

        resp = _register(client, username="tierfree", password="pw123456")
        assert resp.status_code == 200, resp.text
        uid = resp.json()["user_id"]

        db = real_db()
        try:
            user = db.query(User).filter(User.id == uid).first()
            assert user is not None
            assert user.tier == "free"
        finally:
            db.close()

    def test_permissions_endpoint_reports_free_fallback(self, real_db, client, monkeypatch):
        """缺 tier 的账号不再被报成 pro。"""
        from models.database import User

        uid = _seed_user(real_db, name="notier")
        db = real_db()
        try:
            db.query(User).filter(User.id == uid).update({"tier": None})
            db.commit()
        finally:
            db.close()

        login = _login(client, name="notier")
        assert login.status_code == 200, login.text
        token = login.json()["token"]

        resp = client.get("/api/user/permissions", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tier"] == "free"


# ======================================================================
# 3. 注册限流 + 审计日志
# ======================================================================

class TestRegistrationRateLimitAndAudit:
    def test_register_rate_limited_per_ip(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "open")
        monkeypatch.setattr(settings, "RATE_LIMIT_REGISTER_ATTEMPTS", 2)
        monkeypatch.setattr(settings, "RATE_LIMIT_REGISTER_WINDOW", 3600)

        first = _register(client, username="rl_one", password="pw123456")
        second = _register(client, username="rl_two", password="pw123456")
        third = _register(client, username="rl_three", password="pw123456")

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert third.status_code == 429, third.text

    def test_register_writes_audit_log_without_secrets(self, real_db, client, monkeypatch, caplog):
        monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite")
        monkeypatch.setattr(settings, "REGISTRATION_INVITE_CODE", "AUDIT-INVITE-CODE")

        with caplog.at_level(logging.INFO, logger="routers.user"):
            ok = _register(client, username="audituser", password="SuperSecretPW1",
                           invite_code="AUDIT-INVITE-CODE")
            bad = _register(client, username="audituser2", password="SuperSecretPW1",
                            invite_code="WRONG-INVITE-CODE")

        assert ok.status_code == 200, ok.text
        assert bad.status_code == 403, bad.text

        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "[Audit][register]" in text
        assert "result=created" in text
        assert "result=bad_invite" in text
        # 绝不落密码 / 邀请码
        assert "SuperSecretPW1" not in text
        assert "AUDIT-INVITE-CODE" not in text
        assert "WRONG-INVITE-CODE" not in text


# ======================================================================
# 4. 登录会话 cookie 与 redirect 白名单
# ======================================================================

class TestLoginSession:
    def test_login_sets_httponly_session_cookie(self, real_db, client):
        _seed_user(real_db)
        resp = _login(client)

        assert resp.status_code == 200, resp.text
        set_cookie = resp.headers.get("set-cookie", "")
        assert "feclaw_jwt=" in set_cookie
        assert "HttpOnly" in set_cookie
        # 双提交 CSRF cookie 必须可读（否则前端无法回传）
        assert "feclaw_csrf=" in set_cookie
        assert resp.json().get("csrf_token")

    def test_login_still_returns_token_for_legacy_clients(self, real_db, client):
        """移动端/桌面端过渡期仍从响应体取 token。"""
        _seed_user(real_db)
        resp = _login(client)
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]
        assert token.count(".") == 2
        # 同一 token 用 Bearer 也能访问受保护接口
        me = client.get("/api/console/user", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text

    @pytest.mark.parametrize("redirect_to,expected", [
        ("/settings", "/settings"),
        ("/agent/abcd?tab=2", "/agent/abcd?tab=2"),
        ("//evil.example/pwn", "/dashboard"),
        ("/\\evil.example/pwn", "/dashboard"),
        ("https://evil.example/", "/dashboard"),
        ("javascript:alert(1)", "/dashboard"),
        ("/%2f%2fevil.example", "/dashboard"),
        ("/%09//evil.example", "/dashboard"),
        ("/ok\r\nSet-Cookie: x=1", "/dashboard"),
    ])
    def test_redirect_to_whitelist(self, real_db, client, redirect_to, expected):
        _seed_user(real_db)
        resp = client.post(
            "/api/user/login",
            json={"username": "alice", "password": "pw123456", "redirect_to": redirect_to},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["redirect"] == expected


# ======================================================================
# 5. 改密：CSRF、限流、吊销
# ======================================================================

class TestChangePassword:
    def _cookie_login(self, client, name="alice"):
        resp = _login(client, name=name)
        assert resp.status_code == 200, resp.text
        return resp

    def test_anonymous_is_401_not_403(self, client):
        """依赖顺序：认证在 CSRF 之前 ⇒ 匿名拿 401（保持既有路由审计口径）。"""
        resp = client.post("/api/user/change-password",
                           json={"current_password": "x", "new_password": "yyyyyy"})
        assert resp.status_code == 401, resp.text

    def test_cookie_request_without_csrf_header_is_403(self, real_db, client):
        _seed_user(real_db)
        self._cookie_login(client)

        resp = client.post("/api/user/change-password",
                           json={"current_password": "pw123456", "new_password": "newpw123"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["status"] == "csrf_failed"

    def test_cookie_request_with_csrf_header_succeeds(self, real_db, client):
        uid = _seed_user(real_db)
        self._cookie_login(client)
        csrf = client.cookies.get("feclaw_csrf")
        assert csrf

        resp = client.post(
            "/api/user/change-password",
            json={"current_password": "pw123456", "new_password": "newpw123"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked_other_sessions"] is True

        # 旧令牌（改密前签发）必须失效：从 DB 取当前版本核对
        from models.database import User
        db = real_db()
        try:
            user = db.query(User).filter(User.id == uid).first()
            assert int(user.jwt_version) >= 1
        finally:
            db.close()

    def test_mismatched_csrf_header_is_403(self, real_db, client):
        _seed_user(real_db)
        self._cookie_login(client)

        resp = client.post(
            "/api/user/change-password",
            json={"current_password": "pw123456", "new_password": "newpw123"},
            headers={"X-CSRF-Token": "not-the-cookie-value"},
        )
        assert resp.status_code == 403, resp.text

    def test_bearer_client_skips_csrf_token(self, real_db, client):
        """非浏览器显式 Bearer 不带 CSRF token 也应可用（移动端不坏）。"""
        _seed_user(real_db)
        token = _login(client).json()["token"]
        client.cookies.clear()

        resp = client.post(
            "/api/user/change-password",
            json={"current_password": "pw123456", "new_password": "newpw123"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    def test_rate_limited(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_PASSWORD_ATTEMPTS", 1)
        _seed_user(real_db)
        token = _login(client).json()["token"]
        client.cookies.clear()
        headers = {"Authorization": f"Bearer {token}"}

        first = client.post("/api/user/change-password",
                            json={"current_password": "wrong", "new_password": "newpw123"},
                            headers=headers)
        second = client.post("/api/user/change-password",
                             json={"current_password": "wrong", "new_password": "newpw123"},
                             headers=headers)
        assert first.status_code == 400, first.text
        assert second.status_code == 429, second.text


# ======================================================================
# 6. 会话接管 /api/user/session（SSO 中继 token → HttpOnly cookie）
# ======================================================================

class TestSessionAdoption:
    def test_adopts_session_token_into_httponly_cookie(self, real_db, client):
        uid = _seed_user(real_db)
        from utils.auth import create_jwt_token

        token = create_jwt_token({"user_id": uid, "username": "alice"})
        client.cookies.clear()

        resp = client.post("/api/user/session", json={"token": token})
        assert resp.status_code == 200, resp.text
        assert "HttpOnly" in resp.headers.get("set-cookie", "")
        assert resp.json()["user_id"] == uid

        me = client.get("/api/console/user")
        assert me.status_code == 200, me.text

    def test_rejects_garbage_token(self, client):
        resp = client.post("/api/user/session", json={"token": "not-a-jwt"})
        assert resp.status_code == 401, resp.text

    def test_rejects_totp_type_token(self, real_db, client):
        """只接受会话类令牌：TOTP/agent/refresh 一律拒绝。"""
        import datetime as _dt
        from jose import jwt

        payload = {
            "user_id": 1,
            "typ": "totp",
            "agent_hash": "abcd",
            "exp": _dt.datetime.utcnow() + _dt.timedelta(hours=1),
        }
        token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")

        resp = client.post("/api/user/session", json={"token": token})
        assert resp.status_code == 401, resp.text

    def test_anonymous_missing_body_is_not_2xx(self, client):
        """路由审计矩阵口径：匿名请求不得 2xx。"""
        resp = client.post("/api/user/session", json={})
        assert resp.status_code >= 400, resp.text


# ======================================================================
# 7. 限流后端切换（memory / redis / 故障策略）
# ======================================================================

class _FakeRedis:
    """最小假 Redis：只实现限流用到的 INCR / EXPIRE / TTL / PING。"""

    def __init__(self):
        self.counts = {}
        self.ttls = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def ttl(self, key):
        return self.ttls.get(key, -1)

    def ping(self):
        return True

    def close(self):
        return None


class TestRateLimitBackends:
    def test_memory_backend_isolated_per_process(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "memory")
        from services.rate_limiter import RateLimiterGuard

        worker_a = RateLimiterGuard("test_mem", 2, 300)
        worker_b = RateLimiterGuard("test_mem", 2, 300)

        assert worker_a.is_limited("k") is False
        assert worker_a.is_limited("k") is False
        # B 是完全独立的进程内桶 —— 这就是「多 worker 阈值被放大」的成因
        assert worker_b.is_limited("k") is False
        assert worker_a.is_limited("k") is True

    def test_redis_backend_shares_counter_across_workers(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "redis")
        from services import rate_limiter as rl

        fake = _FakeRedis()
        monkeypatch.setattr(rl, "_get_sync_redis", lambda: fake)

        worker_a = rl.RateLimiterGuard("test_redis", 2, 300)
        worker_b = rl.RateLimiterGuard("test_redis", 2, 300)

        assert worker_a.is_limited("k") is False   # 1
        assert worker_b.is_limited("k") is False   # 2（共享计数）
        assert worker_a.is_limited("k") is True    # 3 > 2
        assert worker_b.is_limited("k") is True    # 4

    def test_redis_unavailable_closed_mode_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "redis")
        monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_MODE", "closed")
        from services import rate_limiter as rl

        monkeypatch.setattr(rl, "_get_sync_redis", lambda: None)
        guard = rl.RateLimiterGuard("test_closed", 5, 300)

        with pytest.raises(rl.RateLimitUnavailable):
            guard.is_limited("k")

    def test_redis_unavailable_open_mode_falls_back_to_memory(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "redis")
        monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_MODE", "open")
        from services import rate_limiter as rl

        monkeypatch.setattr(rl, "_get_sync_redis", lambda: None)
        guard = rl.RateLimiterGuard("test_open", 2, 300)

        assert guard.is_limited("k") is False
        assert guard.is_limited("k") is False
        # 兜底仍是**有限**的：不是静默放无限
        assert guard.is_limited("k") is True

    def test_login_returns_503_when_backend_down_and_fail_closed(self, real_db, client, monkeypatch):
        _seed_user(real_db)
        monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "redis")
        monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_MODE", "closed")
        from services import rate_limiter as rl

        monkeypatch.setattr(rl, "_get_sync_redis", lambda: None)

        resp = _login(client)
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"]["status"] == "temporarily_unavailable"

    def test_login_succeeds_when_backend_down_and_fail_open(self, real_db, client, monkeypatch):
        _seed_user(real_db)
        monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "redis")
        monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_MODE", "open")
        from services import rate_limiter as rl

        monkeypatch.setattr(rl, "_get_sync_redis", lambda: None)

        resp = _login(client)
        assert resp.status_code == 200, resp.text

    def test_compat_aliases_still_point_at_same_objects(self):
        """既有断言/调用点（routers/user.py、routers/feclaw_domain.py）不变。"""
        import routers.user as user_mod
        from services import rate_limiter as rl

        assert user_mod._login_limiter is rl.login_limiter
        assert user_mod._login_ip_limiter is rl.login_ip_limiter
        # 旧接口仍在（max_attempts / window_seconds / _buckets / clear）
        assert rl.totp_verify_limiter.max_attempts == 10
        assert rl.totp_verify_limiter.window_seconds == 300
        assert isinstance(rl.totp_verify_limiter._buckets, dict)
        rl.totp_verify_limiter.clear()


# ======================================================================
# 8. 真 Redis（可选）：跨 worker 计数一致 + 故障策略
# ======================================================================
#
# 默认 skip：需要显式 `FECLAW_TEST_REDIS=1` 与本地测试 Redis 连接参数
# （`FECLAW_TEST_REDIS_HOST/PORT/PASSWORD`）。**绝不**指向生产 Redis。

_REAL_REDIS = os.environ.get("FECLAW_TEST_REDIS", "") == "1"


@pytest.mark.skipif(not _REAL_REDIS, reason="需要 FECLAW_TEST_REDIS=1 + 本地测试 Redis")
class TestRealRedisBackend:
    @pytest.fixture
    def redis_guard_env(self, monkeypatch):
        from services import rate_limiter as rl

        monkeypatch.setattr(settings, "REDIS_ENABLED", True)
        monkeypatch.setattr(settings, "REDIS_HOST", os.environ.get("FECLAW_TEST_REDIS_HOST", "127.0.0.1"))
        monkeypatch.setattr(settings, "REDIS_PORT", int(os.environ.get("FECLAW_TEST_REDIS_PORT", "6379")))
        monkeypatch.setattr(settings, "REDIS_PASSWORD", os.environ.get("FECLAW_TEST_REDIS_PASSWORD", ""))
        monkeypatch.setattr(settings, "RATE_LIMIT_BACKEND", "redis")
        rl._reset_redis_client_cache()
        client = rl._get_sync_redis()
        if client is None:
            pytest.skip("本地测试 Redis 不可达")
        yield client
        rl._reset_redis_client_cache()

    def test_two_workers_share_counter(self, redis_guard_env):
        import uuid
        from services import rate_limiter as rl

        name = "t18a-test-" + uuid.uuid4().hex[:8]
        worker_a = rl.RateLimiterGuard(name, 3, 60)
        worker_b = rl.RateLimiterGuard(name, 3, 60)
        try:
            assert worker_a.is_limited("k") is False
            assert worker_b.is_limited("k") is False
            assert worker_a.is_limited("k") is False
            # 第 4 次超过 3 次配额 —— 两个「worker」共享同一计数
            assert worker_b.is_limited("k") is True
        finally:
            from services.redis_client import _make_key
            keys = list(redis_guard_env.scan_iter(match=_make_key(f"*{name}*")))
            if keys:
                redis_guard_env.delete(*keys)

    def test_dead_redis_closed_raises(self, redis_guard_env, monkeypatch):
        from services import rate_limiter as rl

        monkeypatch.setattr(settings, "REDIS_PORT", 6390)  # 假设无人监听
        monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_MODE", "closed")
        rl._reset_redis_client_cache()
        with pytest.raises(rl.RateLimitUnavailable):
            rl.RateLimiterGuard("t18a-test-dead", 2, 60).is_limited("k")

