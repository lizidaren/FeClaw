"""
Q20 安全审计 High 组（H2、H4–H11、H15–H20）修复回归测试

每个修过的点至少一条「未授权 / 非法输入 ⇒ 拒绝」断言：
- H2  子域名页面归属（非 owner ⇒ 403）
- H4  python_background fail-closed（bwrap 不可用 ⇒ 拒绝启动）
- H5  沙箱文案诚实化（SIGSYS 提示不再谎称已阻断网络/子进程）
- H6  bwrap --clearenv + 白名单 env（不再继承 os.environ）
- H7  web_fetch/parse_file 重定向逐跳校验（302 → 内网 ⇒ 阻断）
- H8  微信 base_url 由服务端固定（忽略客户端指定）
- H9  Desktop relay 身份绑定（无路由信息 ⇒ 不广播；channel 白名单）
- H10 会话 Cookie HttpOnly（TOTP 登录下发 HttpOnly cookie）
- H11 COOKIE_SECURE 默认 None（HTTPS 自动探测不再死代码）
- H15 /api/health/backend 管理员限定（匿名 ⇒ 401）
- H16 OAuth 只按 (provider, provider_user_id) 关联，is_admin 不来自 IdP
- H17 登录限流（IP+username 维度）
- H18 分享密码真生效 + token 路径过期校验
- H19 分享 slug CSPRNG + share_hash 唯一约束
- H20 沙箱内部 VFS /public 只读
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


# ======================================================================
# H2 —— 子域名页面只验登录不验归属 ⇒ 非 owner 403
# ======================================================================

class TestSubdomainPageOwnership:
    def test_verify_agent_ownership_non_owner_false(self, real_db):
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password
        from routers.feclaw_domain import _verify_agent_ownership

        db = real_db()
        owner = User(username="h2owner", password_hash=hash_password("pw"), salt=None, is_admin=False)
        attacker = User(username="h2attacker", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add_all([owner, attacker])
        db.commit()
        owner_id, attacker_id = owner.id, attacker.id
        db.add(AgentProfile(user_id=owner_id, hash="abcd", totp_secret="X" * 32, name="a", status="pending"))
        db.commit()
        db.close()

        with patch("routers.feclaw_domain.SessionLocal", real_db):
            assert _verify_agent_ownership("abcd", str(attacker_id)) is False
            assert _verify_agent_ownership("abcd", str(owner_id)) is True

    def test_dashboard_non_owner_returns_403(self, real_db, client):
        import datetime
        import jwt as pyjwt
        from config import settings
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password

        db = real_db()
        owner = User(username="h2owner2", password_hash=hash_password("pw"), salt=None, is_admin=False)
        attacker = User(username="h2attacker2", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add_all([owner, attacker])
        db.commit()
        owner_id, attacker_id = owner.id, attacker.id
        db.add(AgentProfile(user_id=owner_id, hash="abcd", totp_secret="X" * 32, name="a", status="pending"))
        db.commit()
        db.close()

        tok = pyjwt.encode(
            {"user_id": attacker_id, "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1)},
            settings.JWT_SECRET, algorithm="HS256",
        )
        with patch("routers.feclaw_domain.SessionLocal", real_db):
            resp = client.get("/dashboard", headers={"Host": "abcd.example.com"},
                              cookies={"feclaw_jwt": tok})
        assert resp.status_code == 403


# ======================================================================
# H4 —— python_background fail-closed
# ======================================================================

class TestBackgroundFailClosed:
    def test_bwrap_unavailable_refuses_background(self):
        from services.sandbox_manager import SandboxManager
        with patch("services.sandbox_manager._global_concurrency_limiter") as lim:
            lim.acquire.return_value = True
            mgr = object.__new__(SandboxManager)
            mgr._bwrap_available = False
            result = mgr.start_background("print(1)", name="t")
            assert isinstance(result, str)
            assert "拒绝" in result and "bwrap" in result


# ======================================================================
# H5 —— 沙箱提示诚实化（不再谎称已阻断网络/子进程）
# ======================================================================

class TestSandboxHonestMessage:
    def test_sigsys_message_no_false_claim(self):
        from services.sandbox_manager import SandboxManager
        mgr = object.__new__(SandboxManager)
        result = mgr._make_result("", "", -31, "sid")  # -31 = SIGSYS
        assert "does not guarantee network isolation" in result.stderr
        # 关键：不再出现「已阻断 Network access / System command execution」的假声明
        assert "Network access (HTTP requests" not in result.stderr


# ======================================================================
# H6 —— bwrap --clearenv + 白名单 env
# ======================================================================

class TestBwrapClearenv:
    def test_sandbox_env_whitelist_excludes_secrets(self):
        from services.sandbox_manager import SandboxManager
        mgr = object.__new__(SandboxManager)
        mgr._is_fuse_ready = MagicMock(return_value=False)
        envs = mgr._build_sandbox_env()
        keys = {e.split("=", 1)[0] for e in envs}
        # 只注入白名单变量，绝不透传 JWT_SECRET/DB 口令/API Key
        assert keys <= {"PATH", "HOME", "PYTHONDONTWRITEBYTECODE",
                        "PYTHONWARNINGS", "LANG", "LC_ALL", "FECLAW_USE_FUSE"}
        assert "JWT_SECRET" not in keys
        assert "DATABASE_URL" not in keys
        assert "PATH" in keys

    def test_bwrap_entry_uses_env_minus_i_clearenv(self):
        from services.sandbox_manager import SandboxManager
        mgr = object.__new__(SandboxManager)
        mgr._is_fuse_ready = MagicMock(return_value=False)
        # FIX-E（复审残余）：netns 缺失时 builder 现在直接 raise（fail-closed）。
        # 本测试只验「clearenv 等价 env -i」前缀，故 mock 出可用 netns 前缀再断言。
        with patch("services.sandbox_manager.NetworkIsolationManager.get_netns_prefix",
                   return_value=["/usr/local/libexec/feclaw/helper"]):
            cmd = mgr._build_bwrap_command("/tmp/x.py", None)
        # 本机 bwrap 0.4.0 不支持 --clearenv，改由 env -i 前缀实现等价 clearenv
        assert "/usr/bin/env" in cmd
        assert "-i" in cmd
        assert "--clearenv" not in cmd


# ======================================================================
# H7 —— web_fetch / parse_file 重定向逐跳校验
# ======================================================================

class TestRedirectHopValidation:
    def test_url_redirect_to_loopback_blocked(self):
        from services.tools.universal_parser import ParseFileMixin

        class FakeResp:
            status_code = 302
            headers = {"location": "http://127.0.0.1/secret"}

        # 首跳 example.com 合法（patch DNS 到公网 IP），302 → 127.0.0.1 必须被拦
        with patch("httpx.get", return_value=FakeResp()), \
             patch("utils.url_validation.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            import asyncio
            mgr = object.__new__(ParseFileMixin)
            result = asyncio.run(mgr._handle_url("https://example.com/a", ""))
        assert "URL 校验失败" in result


# ======================================================================
# H8 —— 微信 base_url 由服务端固定
# ======================================================================

class TestWechatBaseUrl:
    def test_bind_ignores_client_base_url(self, real_db, client):
        from config import settings
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        owner = User(username="h8owner", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add(owner)
        db.commit()
        owner_id = owner.id
        db.add(AgentProfile(user_id=owner_id, hash="abcd", totp_secret="X" * 32, name="a", status="pending"))
        db.commit()
        db.close()

        token = create_jwt_token({"sub": str(owner_id)})
        saved = {}

        def _fake_save(user_id, bot_token, ilink_bot_id, ilink_user_id, base_url, agent_hash):
            saved["base_url"] = base_url
            return MagicMock()

        with patch("routers.wechat.wechat_service.update_login_state"), \
             patch("routers.wechat.wechat_service.bind_user",
                   return_value=MagicMock(id=1)), \
             patch("routers.wechat.wechat_service.save_sdk_credentials",
                   side_effect=_fake_save), \
             patch("routers.wechat.wechat_service.start_polling",
                   new=AsyncMock()):
            resp = client.post(
                "/api/wechat/bind",
                json={"ilink_user_id": "u1", "agent_hash": "abcd",
                      "bot_token": "tok", "base_url": "http://attacker.com/evil"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        # 客户端提交的 base_url 必须被忽略，改用服务端配置。
        # FIX-F G5（测试/代码漂移）：2026-10-02 代码已把空配置回退到官方端点
        # ILINK_API_BASE（修复 -14 session timeout），安全属性不变（仍忽略客户端提交值）。
        # 旧断言 `== (settings.WECHAT_ILINK_BASE_URL or "")` 期望落库空串，已过时。
        from services.wechat.models import ILINK_API_BASE
        assert saved.get("base_url") != "http://attacker.com/evil"
        assert saved.get("base_url") == (settings.WECHAT_ILINK_BASE_URL or ILINK_API_BASE).strip()


# ======================================================================
# H9 —— Desktop relay 身份绑定（删单 socket 兜底 + channel 白名单）
# ======================================================================

class TestDesktopRelayRouting:
    def test_unroutable_message_not_broadcast(self):
        import asyncio
        from routers.client_ws import manager
        sent = asyncio.run(manager.send({"type": "command_exec_request"}))
        assert sent is False

    def test_invalid_channel_rejected(self, client):
        import datetime
        import jwt as pyjwt
        from config import settings
        from starlette.websockets import WebSocketDisconnect
        # 带合法 token：若 channel 白名单被移除，该连接会保持打开；现在必须被拒绝
        tok = pyjwt.encode(
            {"user_id": 1, "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1)},
            settings.JWT_SECRET, algorithm="HS256",
        )
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/client?channel=evil&token={tok}"):
                pass


# ======================================================================
# H10 —— TOTP 登录下发 HttpOnly 会话 Cookie
# ======================================================================

class TestTotpHttpOnlyCookie:
    def test_verify_totp_sets_httponly_cookie(self, real_db, client):
        import pyotp
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password
        from routers.feclaw_domain import _totp_verify_attempts

        db = real_db()
        u = User(username="h10owner", password_hash=hash_password("pw"), salt=None, is_admin=False)
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
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert "feclaw_jwt" in set_cookie
        assert "HttpOnly" in set_cookie


# ======================================================================
# H11 —— COOKIE_SECURE 默认 None（自动探测生效）
# ======================================================================

class TestCookieSecureDefault:
    def test_default_is_none(self):
        from config import Settings
        assert Settings.model_fields["COOKIE_SECURE"].default is None


# ======================================================================
# H15 —— /api/health/backend 管理员限定
# ======================================================================

class TestHealthBackendAuth:
    def test_unauthenticated_returns_401(self, client):
        resp = client.get("/api/health/backend")
        assert resp.status_code == 401


# ======================================================================
# H16 —— OAuth 只按 (provider, provider_user_id) 关联，is_admin 不来自 IdP
# ======================================================================

class TestOAuthIdentityBinding:
    def test_platform_admin_name_does_not_grant_admin(self, real_db):
        from utils.oauth_helpers import find_or_create_user_from_platform

        db = real_db()
        # 预置一个本地 admin 账号（无 UserLink）—— 名字碰撞不该被劫持
        from models.database import User
        from utils.auth import hash_password
        db.add(User(username="admin", password_hash=hash_password("pw"), salt=None, is_admin=True))
        db.commit()

        user = find_or_create_user_from_platform(
            db, platform_user_id="plat-123", username="admin", email="x@y.z",
        )
        # 只按 (provider, provider_user_id) 关联：绝不绑定到已有 admin
        assert user.username != "admin" or user.is_admin is False
        assert user.is_admin is False
        db.close()

    def test_is_admin_never_from_idp_claim(self, real_db):
        from utils.oauth_helpers import find_or_create_user_from_platform

        db = real_db()
        # 即使调用方误传 is_admin=True（旧接口兼容参数），也不得提权
        user = find_or_create_user_from_platform(
            db, platform_user_id="plat-456", username="newbie", email="a@b.c",
            is_admin=True,
        )
        assert user.is_admin is False
        db.close()


# ======================================================================
# H17 —— 登录限流
# ======================================================================

class TestLoginRateLimit:
    def test_blocks_after_max_attempts(self):
        from routers.user import _login_rate_limited, _login_attempts, _LOGIN_MAX
        _login_attempts.clear()
        key = "203.0.113.9:alice"
        for _ in range(_LOGIN_MAX):
            assert _login_rate_limited(key) is False
        assert _login_rate_limited(key) is True


# ======================================================================
# H18 —— 分享密码真生效 + token 路径过期校验
# ======================================================================

class TestSharePassword:
    def test_password_hash_roundtrip(self):
        from services.share_service import _hash_share_password, verify_share_password
        h = _hash_share_password("s3cret")
        assert h is not None and h.startswith("$2")
        assert verify_share_password("s3cret", h) is True
        assert verify_share_password("wrong", h) is False
        assert verify_share_password("", h) is False
        assert _hash_share_password(None) is None

    def test_slug_resolver_rejects_wrong_password(self, real_db, client):
        from models.database import ShareMapping
        from services.share_service import _hash_share_password

        db = real_db()
        db.add(ShareMapping(
            user_id="1", agent_hash="abcd", vfs_path="/workspace/a.md",
            share_hash="abcd1234abcd1234", slug="test-pwd-slug", mode="share",
            password=_hash_share_password("right"),
        ))
        db.commit()
        db.close()

        empty_storage = MagicMock()
        empty_storage.get_file_content = MagicMock(return_value=None)
        # 无密码 / 错密码 ⇒ 401；正确密码 ⇒ 通过密码门禁（进入文件解析，mock 存储返回空 → 404）
        with patch("services.file_storage.create_file_storage", return_value=empty_storage):
            r1 = client.get("/s/test-pwd-slug")
            assert r1.status_code == 401
            r2 = client.get("/s/test-pwd-slug?password=wrong")
            assert r2.status_code == 401
            r3 = client.get("/s/test-pwd-slug?password=right")
            # 密码正确后不再 401（进入文件解析，因 mock 存储为空返回 404）
            assert r3.status_code == 404

    def test_token_resolver_rejects_wrong_password(self, real_db, client):
        import base64
        import time as _t
        import hmac as _hmac
        import hashlib as _h
        from config import settings
        from models.database import ShareMapping
        from services.share_service import _hash_share_password

        db = real_db()
        share_hash = "1111222233334444"
        expires_at = int(_t.time()) + 3600
        db.add(ShareMapping(
            user_id="1", agent_hash="abcd", vfs_path="/workspace/a.md",
            share_hash=share_hash, slug="test-tok-slug", mode="share",
            password=_hash_share_password("right"),
            expires_at=__import__("datetime").datetime.utcfromtimestamp(expires_at),
        ))
        db.commit()
        db.close()

        # 用与 _encode_share_token 相同的方式伪造一个合法 token
        secret = _h.sha256(f"share:{settings.JWT_SECRET}".encode()).digest()
        payload = f"{expires_at}|{share_hash}"
        sig = _hmac.new(secret, payload.encode(), _h.sha256).hexdigest()
        token = base64.urlsafe_b64encode(f"{expires_at}|{share_hash}|{sig}".encode()).decode().rstrip("=")

        empty_storage = MagicMock()
        empty_storage.get_file_content = MagicMock(return_value=None)
        with patch("services.file_storage.create_file_storage", return_value=empty_storage):
            r1 = client.get(f"/share/{token}")
            assert r1.status_code == 401
            r2 = client.get(f"/share/{token}?password=right")
            assert r2.status_code == 404


# ======================================================================
# H19 —— slug CSPRNG + share_hash 唯一约束
# ======================================================================

class TestShareSlugEntropy:
    def test_slug_has_csprng_suffix(self):
        from services.share_service import _generate_slug
        s = _generate_slug()
        # 3 词 + CSPRNG token_urlsafe(16)（≥128 bit）后缀
        assert s.count("-") >= 3
        assert len(s) >= 40

    def test_share_hash_unique_constraint(self):
        from models.database import ShareMapping
        col = ShareMapping.__table__.columns["share_hash"]
        assert col.unique is True


# ======================================================================
# H20 —— 沙箱内部 VFS /public 只读
# ======================================================================

class TestSandboxVfsPublicReadonly:
    def test_upload_to_public_blocked(self):
        from services.sandbox_manager import SandboxManager
        mgr = object.__new__(SandboxManager)
        vfs = MagicMock()
        vfs._is_public_path.return_value = True
        vfs._resolve_path.return_value = ("feclaw/public/evil.md", None)
        mgr.vfs = vfs

        import base64
        resp = mgr._vfs_file_handler("/public/evil.md", {}, {
            "mode": "upload",
            "content": base64.b64encode(b"x").decode(),
        })
        assert "read-only" in resp.get("error", "")


# ======================================================================
# 冒烟 —— 非法输入 ⇒ 4xx（H15 URL 校验、H16/其它）
# ======================================================================

class TestHealthBackendUrl:
    def test_private_backend_url_rejected_by_validator(self):
        from utils.url_validation import validate_public_http_url
        assert validate_public_http_url("http://127.0.0.1:8080/x") is False
        assert validate_public_http_url("http://169.254.169.254/latest") is False
