"""
Q22 令牌分级（typ + aud 显式白名单）+ 登录回归测试

审计 §7.13 遗留结构性项：会话 / TOTP / Agent / 分享令牌曾共用同一 JWT_SECRET、
同为 HS256、无 typ/aud 区分 —— get_current_user* 接受任何带 user_id/sub 的 token，
TOTP/Agent 令牌可冒充会话令牌（C2 载体）。本批落地分级：

  入口                   放行                      拒绝
  ─────────────────────────────────────────────────────────────
  session   (auth_deps)  session                     agent / totp / refresh
  agent ctx (verify_jwt) session / totp              agent / refresh
  agent     (verify_ag)  agent                       session / totp / refresh
  refresh   (decode_ref) refresh                     session / totp / agent
  share     (HMAC)       非 JWT，独立密钥，天然隔离

TOTP 是**认证方式**而非令牌类型：verify_agent_totp 通过后签发标准会话令牌
（typ=session + auth_method=totp + agent_hash + jwt_version）。

本文件测试：
1. token_type() 分类（含旧令牌兼容回推）
2. 每类令牌 × 每个入口 的矩阵（正确放行 / 错误拒绝）
3. 真 HTTP 冒烟：登录 → 访问 → 登出 → 再访问 401
4. TOTP 流程：verify → 会话令牌 → 受保护接口 200
5. SSO 同步流：会话 cookie → /api/auth/sync 302 带 token
"""
import datetime
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from jose import jwt

from main import app
from config import settings
from utils.auth import (
    create_jwt_token,
    token_type,
    TOKEN_TYPE_SESSION,
    TOKEN_TYPE_TOTP,
    TOKEN_TYPE_AGENT,
    TOKEN_TYPE_REFRESH,
)


@pytest.fixture
def client():
    return TestClient(app)


# ======================================================================
# 工具：按 typ 构造令牌（jose HS256，与 utils.auth 同一密钥/算法）
# ======================================================================

def _mk(typ=None, user_id=1, **extra):
    """构造一个 FeClaw HS256 token。typ=None 时表示旧令牌（无 typ 声明）。"""
    payload = {
        "user_id": user_id,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1),
    }
    if typ is not None:
        payload["typ"] = typ
    payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


# ======================================================================
# 1. token_type() 分类
# ======================================================================

class TestTokenTypeClassification:
    def test_new_typ_claims(self):
        assert token_type({"typ": "session"}) == TOKEN_TYPE_SESSION
        assert token_type({"typ": "totp"}) == TOKEN_TYPE_TOTP
        assert token_type({"typ": "agent"}) == TOKEN_TYPE_AGENT
        assert token_type({"typ": "refresh"}) == TOKEN_TYPE_REFRESH

    def test_legacy_agent_jwt(self):
        assert token_type({"type": "agent_jwt", "user_id": 1}) == TOKEN_TYPE_AGENT

    def test_legacy_refresh(self):
        assert token_type({"type": "refresh", "user_id": 1}) == TOKEN_TYPE_REFRESH

    def test_legacy_access_is_session(self):
        assert token_type({"type": "access", "user_id": 1}) == TOKEN_TYPE_SESSION

    def test_legacy_totp_marker(self):
        assert token_type({"auth_method": "totp", "agent_hash": "abcd"}) == TOKEN_TYPE_TOTP
        assert token_type({"agent_hash": "abcd"}) == TOKEN_TYPE_TOTP

    def test_legacy_plain_session(self):
        # 老本地登录 / OAuth 会话令牌：无 typ / 无 type / 无 agent_hash / auth_method != totp
        assert token_type({"user_id": 1, "username": "u"}) == TOKEN_TYPE_SESSION
        assert token_type({"auth_method": "local", "user_id": 1}) == TOKEN_TYPE_SESSION

    def test_unknown_typ_falls_back_to_markers(self):
        # 未知 typ 值不算数，退回既有标记回推（不因脏 typ 放行/误杀）
        assert token_type({"typ": "nonsense", "type": "agent_jwt"}) == TOKEN_TYPE_AGENT


# ======================================================================
# 2. 每类令牌 × 每个入口 矩阵（纯函数，无 DB）
# ======================================================================

class TestAgentEntry:
    """Agent 入口：只放行 agent。"""

    def test_agent_allowed(self):
        from services.agent_jwt_service import agent_jwt_service
        assert agent_jwt_service.verify_agent_jwt(_mk("agent", type="agent_jwt")) is not None

    def test_legacy_agent_allowed(self):
        from services.agent_jwt_service import agent_jwt_service
        assert agent_jwt_service.verify_agent_jwt(_mk(None, type="agent_jwt")) is not None

    def test_session_rejected(self):
        from services.agent_jwt_service import agent_jwt_service
        assert agent_jwt_service.verify_agent_jwt(_mk("session")) is None

    def test_totp_rejected(self):
        from services.agent_jwt_service import agent_jwt_service
        assert agent_jwt_service.verify_agent_jwt(
            _mk("totp", auth_method="totp", agent_hash="abcd")) is None

    def test_refresh_rejected(self):
        from services.agent_jwt_service import agent_jwt_service
        assert agent_jwt_service.verify_agent_jwt(_mk("refresh", type="refresh")) is None


class TestAgentContextEntry:
    """页面/Agent 上下文入口（TOTPService.verify_jwt）：session + totp 放行，agent/refresh 拒绝。"""

    def test_session_allowed(self):
        from services.totp_service import TOTPService
        r = TOTPService.verify_jwt(_mk("session", username="u"))
        assert r is not None and r["user_id"] == 1

    def test_totp_allowed(self):
        from services.totp_service import TOTPService
        r = TOTPService.verify_jwt(_mk("totp", auth_method="totp", agent_hash="abcd"))
        assert r is not None and r["agent_hash"] == "abcd"

    def test_legacy_totp_allowed(self):
        from services.totp_service import TOTPService
        r = TOTPService.verify_jwt(_mk(None, auth_method="totp", agent_hash="abcd"))
        assert r is not None and r["agent_hash"] == "abcd"

    def test_agent_rejected(self):
        from services.totp_service import TOTPService
        assert TOTPService.verify_jwt(_mk("agent", type="agent_jwt")) is None

    def test_refresh_rejected(self):
        from services.totp_service import TOTPService
        assert TOTPService.verify_jwt(_mk("refresh", type="refresh")) is None


class TestRefreshEntry:
    """refresh 入口：只放行 refresh。"""

    def test_refresh_allowed(self, real_db):
        from utils.oauth_helpers import decode_refresh_token
        # user_id=1 不存在 → is_token_revoked 无法判定 → 放行，返回 user_id
        assert decode_refresh_token(_mk("refresh", type="refresh", user_id=424242)) == 424242

    def test_legacy_refresh_allowed(self, real_db):
        from utils.oauth_helpers import decode_refresh_token
        assert decode_refresh_token(_mk(None, type="refresh", user_id=424242)) == 424242

    def test_session_rejected(self, real_db):
        from utils.oauth_helpers import decode_refresh_token
        assert decode_refresh_token(_mk("session")) is None

    def test_totp_rejected(self, real_db):
        from utils.oauth_helpers import decode_refresh_token
        assert decode_refresh_token(_mk("totp", auth_method="totp", agent_hash="abcd")) is None

    def test_agent_rejected(self, real_db):
        from utils.oauth_helpers import decode_refresh_token
        assert decode_refresh_token(_mk("agent", type="agent_jwt")) is None


class TestSessionEntry:
    """会话入口（utils.auth_dependencies._decode_or_none）：只放行 session。"""

    def _session_user(self, db):
        from models.database import User
        from utils.auth import hash_password
        u = User(username="q22sess", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add(u)
        db.commit()
        return u.id

    def test_session_allowed(self, real_db):
        from utils.auth_dependencies import _decode_or_none
        db = real_db()
        uid = self._session_user(db)
        db.close()
        tok = create_jwt_token({"user_id": uid, "username": "q22sess"})
        payload = _decode_or_none(tok, db=real_db())
        assert payload is not None and payload["typ"] == TOKEN_TYPE_SESSION

    def test_agent_rejected(self, real_db):
        from utils.auth_dependencies import _decode_or_none
        db = real_db()
        uid = self._session_user(db)
        db.close()
        assert _decode_or_none(_mk("agent", type="agent_jwt", user_id=uid), db=real_db()) is None

    def test_totp_rejected(self, real_db):
        from utils.auth_dependencies import _decode_or_none
        db = real_db()
        uid = self._session_user(db)
        db.close()
        tok = _mk("totp", auth_method="totp", agent_hash="abcd", user_id=uid)
        assert _decode_or_none(tok, db=real_db()) is None

    def test_refresh_rejected(self, real_db):
        from utils.auth_dependencies import _decode_or_none
        db = real_db()
        uid = self._session_user(db)
        db.close()
        tok = _mk("refresh", type="refresh", user_id=uid)
        assert _decode_or_none(tok, db=real_db()) is None


# ======================================================================
# 3. 真 HTTP：错误类型令牌 ⇒ 401（不是 500，也不是放行）
# ======================================================================

class TestWrongTypeTokenHttp:
    def _user_id(self, db):
        from models.database import User
        from utils.auth import hash_password
        u = User(username="q22http", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add(u)
        db.commit()
        return u.id

    def test_agent_token_rejected_at_session_route(self, real_db, client):
        db = real_db()
        uid = self._user_id(db)
        db.close()
        tok = _mk("agent", type="agent_jwt", user_id=uid)
        resp = client.get("/api/console/user", headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 401

    def test_totp_token_rejected_at_session_route(self, real_db, client):
        db = real_db()
        uid = self._user_id(db)
        db.close()
        tok = _mk("totp", auth_method="totp", agent_hash="abcd", user_id=uid)
        resp = client.get("/api/console/user", headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 401

    def test_refresh_token_rejected_at_session_route(self, real_db, client):
        db = real_db()
        uid = self._user_id(db)
        db.close()
        tok = _mk("refresh", type="refresh", user_id=uid)
        resp = client.get("/api/console/user", headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 401

    def test_session_token_allowed_at_session_route(self, real_db, client):
        db = real_db()
        uid = self._user_id(db)
        db.close()
        tok = create_jwt_token({"user_id": uid, "username": "q22http"})
        resp = client.get("/api/console/user", headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 200


# ======================================================================
# 4. 真 HTTP 冒烟：登录 → 访问 → 登出 → 再访问 401
# ======================================================================

class TestLoginLogoutSmoke:
    def test_login_access_logout_regression(self, real_db, client):
        from models.database import User
        from utils.auth import hash_password

        db = real_db()
        db.add(User(username="q22login", password_hash=hash_password("pw123456"), salt=None, is_admin=False))
        db.commit()
        db.close()

        # 登录 → 会话令牌
        resp = client.post("/api/user/login", json={"username": "q22login", "password": "pw123456"})
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]
        assert token

        # 访问受保护接口 → 200
        r1 = client.get("/api/console/user", headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 200, r1.text

        # 登出（jwt_version 吊销）
        r2 = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200, r2.text

        # 再访问同一令牌 → 401
        r3 = client.get("/api/console/user", headers={"Authorization": f"Bearer {token}"})
        assert r3.status_code == 401


# ======================================================================
# 5. TOTP 流程：verify → 会话令牌（typ=session）→ 受保护接口 200
# ======================================================================

class TestTotpFlow:
    def test_totp_verify_issues_session_token(self, real_db, client):
        import pyotp
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password
        from routers.feclaw_domain import _totp_verify_attempts

        db = real_db()
        u = User(username="q22totp", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add(u)
        db.commit()
        secret = pyotp.random_base32()
        db.add(AgentProfile(user_id=u.id, hash="abcd", totp_secret=secret, name="a", status="pending"))
        db.commit()
        db.close()

        code = pyotp.TOTP(secret).now()
        _totp_verify_attempts.clear()
        with patch("services.totp_service.SessionLocal", real_db):
            resp = client.post("/api/totp/verify", json={"agent_hash": "abcd", "code": code})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        token = data["token"]

        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        assert payload["typ"] == TOKEN_TYPE_SESSION
        assert payload["auth_method"] == "totp"
        assert payload["agent_hash"] == "abcd"

        # TOTP 登录得到的会话令牌可访问受保护接口
        r = client.get("/api/console/user", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text


# ======================================================================
# 6. 子域 SSO 同步流：会话 cookie → /api/auth/sync 302 带 token
# ======================================================================

class TestAuthSyncFlow:
    def test_sync_redirects_with_token(self, real_db, client):
        db = real_db()
        from models.database import User
        from utils.auth import hash_password
        u = User(username="q22sync", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add(u)
        db.commit()
        uid = u.id
        db.close()

        tok = create_jwt_token({"user_id": uid, "username": "q22sync"})
        resp = client.get("/api/auth/sync", params={"redirect": "/dashboard"},
                          cookies={"feclaw_jwt": tok}, follow_redirects=False)
        # 未配置 FECLAW_PUBLIC_URL ⇒ 同源跳转；有效会话 cookie ⇒ 302 带 #token=
        assert resp.status_code == 302
        assert "#token=" in resp.headers.get("location", "")
