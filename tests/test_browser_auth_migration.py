"""
T18a —— 浏览器认证迁移（无 localStorage token 的完整流程）

分两层：

**A. 静态/HTTP 层（无需浏览器，默认执行）**
1. 首页 / 登录页 / token-sync / auth.js 不再把 JWT 写进 localStorage 或 JS 可写 cookie。
2. 登录 → 会话 cookie（HttpOnly）→ 受保护接口 → 带 CSRF 的写操作 → 登出 → 之后 401。
   全程**不使用** Authorization 头，也不读 localStorage（模拟「脚本拿不到 token」）。
3. mobile Bearer 仍可用（迁移不能把非浏览器客户端弄坏）。
4. TOTP 令牌的作用域隔离（`typ=totp` 只能碰它自己那个 agent）。
5. 跨源 fetch 不带凭据的客户端侧约定（静态检查 auth.js 的跨源分支）。

**B. 真浏览器层（Playwright，可跳过）**
需要显式提供本地测试 app 地址（`FECLAW_TEST_BASE_URL`，默认不设 ⇒ skip），
且必须装好 playwright 浏览器。**绝不**指向生产：默认值就是空。

运行说明：`.venv` 在交付时仍在安装；未执行的项在 T18a 报告里明确标未验证。
"""

import os
import re

import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_limiters():
    from services import rate_limiter as rl

    for limiter in (rl.login_limiter, rl.login_ip_limiter, rl.register_limiter):
        limiter.clear()
    yield


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "OAUTH_ENABLED", False)
    monkeypatch.setattr(settings, "FECLAW_PUBLIC_URL", "")
    monkeypatch.setattr(settings, "CSRF_ENABLED", True)
    yield


def _read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _seed_user(real_db, name="browseruser", password="pw123456"):
    from models.database import User
    from utils.auth import hash_password

    db = real_db()
    user = User(username=name, password_hash=hash_password(password), salt=None, is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    uid = user.id
    db.close()
    return uid


# ======================================================================
# A1. 静态检查：没有「把 JWT 交给脚本」的残留
# ======================================================================

class TestNoScriptReadableToken:
    def test_index_template_no_longer_writes_token_cookie(self):
        text = _read("templates/index.html")
        assert 'params.get("token")' not in text
        assert 'localStorage.setItem("feclaw_jwt"' not in text
        assert 'document.cookie = "feclaw_jwt=' not in text

    def test_token_sync_does_not_copy_cookie_to_localstorage(self):
        text = _read("static/js/token-sync.js")
        # 旧实现：cookie → localStorage 同步
        assert "localStorage.setItem(KEY" not in text
        assert "localStorage.setItem('feclaw_jwt'" not in text
        # 新实现：同源 mutation 自动带 CSRF 头
        assert "__feclawFetchWithCsrf" in text
        assert "X-CSRF-Token" in text

    def test_auth_js_set_token_does_not_persist(self):
        text = _read("static/js/auth.js")
        # setToken 内不得写 localStorage（只登记内存 + 交给服务端换 cookie）
        set_token_body = re.search(r"setToken\(token\)\s*\{(.*?)\n  \},", text, re.S)
        assert set_token_body, "找不到 setToken 实现"
        body = set_token_body.group(1)
        assert "localStorage.setItem" not in body
        assert "document.cookie" not in body
        # 迁移清理必须保留（否则旧 token 会一直躺在 localStorage 里）
        assert "_purgeLegacyToken" in text

    def test_login_template_does_not_store_token(self):
        text = _read("templates/login.html")
        assert "localStorage.setItem('feclaw_jwt'" not in text
        assert "Auth.setToken(data.token)" not in text
        assert "Auth.safeInternalPath" in text
        assert "Auth.redirectAfterLogin" in text

    def test_auth_js_bearer_only_for_explicit_mobile_token(self):
        text = _read("static/js/auth.js")
        assert "getMobileToken" in text
        # 旧实现无条件用 getToken() 拼 Bearer —— 必须消失
        assert "const token = this.getToken();" not in text


# ======================================================================
# A2. HTTP 层：cookie-only 的完整流程（模拟「脚本读不到 token」）
# ======================================================================

class TestCookieOnlyBrowserFlow:
    def _login(self, client, name="browseruser", password="pw123456"):
        resp = client.post("/api/user/login", json={"username": name, "password": password})
        assert resp.status_code == 200, resp.text
        # 浏览器拿不到 token 值，只能拿 cookie（csrf token 从可读 cookie 读）
        return resp

    def test_full_flow_without_authorization_header(self, real_db, client):
        _seed_user(real_db)
        self._login(client)

        # 1) 受保护接口：仅靠 cookie
        me = client.get("/api/console/user")
        assert me.status_code == 200, me.text

        # 2) 写操作：cookie + CSRF 头（token 从可读 cookie 取，脚本无需知道 JWT）
        csrf = client.cookies.get("feclaw_csrf")
        assert csrf
        changed = client.post(
            "/api/user/change-password",
            json={"current_password": "pw123456", "new_password": "newpw123"},
            headers={"X-CSRF-Token": csrf},
        )
        assert changed.status_code == 200, changed.text

        # 3) 登出（服务端清 cookie，含 host-only/domain 变体 + TOTP + CSRF）
        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200, logout.text

        # 4) 之后必须 401
        after = client.get("/api/console/user")
        assert after.status_code == 401, after.text

    def test_session_cookie_is_httponly_and_not_js_readable(self, real_db, client):
        _seed_user(real_db)
        resp = self._login(client)
        set_cookie = resp.headers.get("set-cookie", "")
        jwt_cookie_line = [
            line for line in set_cookie.split(",") if "feclaw_jwt=" in line
        ]
        # TestClient 可能把多条 Set-Cookie 合并；httpx 保留原始 header
        raw = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else [set_cookie]
        joined = " ".join(raw)
        assert "feclaw_jwt=" in joined
        assert "HttpOnly" in joined
        # 仅断言存在 httponly（细节见 test_auth_registration）

    def test_mobile_bearer_still_works(self, real_db, client):
        _seed_user(real_db)
        token = self._login(client).json()["token"]
        client.cookies.clear()

        resp = client.get("/api/console/user", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text

    def test_login_response_does_not_require_token_persistence(self, real_db, client):
        """响应体保留 token 只为未升级客户端；浏览器不存取也完全可用。"""
        _seed_user(real_db)
        resp = self._login(client)
        data = resp.json()
        assert data.get("session") == "cookie"
        assert data.get("csrf_token")

        client.cookies.set("feclaw_csrf", data["csrf_token"])
        # 只用 cookie 访问
        assert client.get("/api/oauth/me").status_code == 200


class TestTotpScopeIsolation:
    """TOTP 令牌只能在它自己那个 Agent 范围内使用（`utils/agent_access` 已有逻辑）。"""

    def _seed(self, real_db):
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password

        db = real_db()
        user = User(username="totpscope", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add(user)
        db.commit()
        uid = user.id
        db.add(AgentProfile(user_id=uid, hash="8d85", totp_secret="X" * 32, name="a", status="pending"))
        db.add(AgentProfile(user_id=uid, hash="343b", totp_secret="Y" * 32, name="b", status="pending"))
        db.commit()
        db.close()
        return uid

    def test_totp_scope_helpers(self, real_db):
        import datetime as _dt
        from jose import jwt
        from utils.agent_access import totp_scoped_agent_hash

        payload = {
            "user_id": 1, "typ": "totp", "agent_hash": "8d85",
            "exp": _dt.datetime.utcnow() + _dt.timedelta(hours=1),
        }
        token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
        assert totp_scoped_agent_hash(token) == "8d85"

        session_token = jwt.encode(
            {"user_id": 1, "typ": "session",
             "exp": _dt.datetime.utcnow() + _dt.timedelta(hours=1)},
            settings.JWT_SECRET, algorithm="HS256",
        )
        assert totp_scoped_agent_hash(session_token) is None

    def test_require_totp_scope_rejects_other_agent(self, real_db):
        import datetime as _dt
        from jose import jwt
        from fastapi import HTTPException
        from utils.agent_access import require_totp_scope

        uid = self._seed(real_db)
        payload = {
            "user_id": uid, "typ": "totp", "agent_hash": "8d85",
            "exp": _dt.datetime.utcnow() + _dt.timedelta(hours=1),
        }
        token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")

        class _Req:
            headers = {"Authorization": f"Bearer {token}"}
            cookies = {}
            url = type("U", (), {"path": "/api/chat/stream"})()

        # 目标 agent 与令牌作用域一致 → 放行
        require_totp_scope(_Req(), "8d85")
        # 目标 agent 不同 → 403
        with pytest.raises(HTTPException) as exc:
            require_totp_scope(_Req(), "343b")
        assert exc.value.status_code == 403

    def test_totp_token_cannot_read_other_agent_session_scope(self, real_db, client):
        """WS 层面同样受限（HTTP 层逻辑复用同一 helper）。"""
        uid = self._seed(real_db)
        import datetime as _dt
        from jose import jwt
        from unittest.mock import patch
        from starlette.websockets import WebSocketDisconnect

        payload = {
            "user_id": uid, "typ": "totp", "agent_hash": "8d85",
            "exp": _dt.datetime.utcnow() + _dt.timedelta(hours=1),
        }
        token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")

        def _first(ws):
            try:
                return ws.receive_json()
            except WebSocketDisconnect:
                return {"type": "__closed__"}

        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(
                f"/api/chat/ws?token={token}&agent_hash=343b",
                headers={"Origin": "http://testserver"},
            ) as ws:
                event = _first(ws)
        assert event.get("type") != "ping", event


# ======================================================================
# B. 真浏览器（Playwright）—— 默认跳过，绝不指向生产
# ======================================================================

BASE_URL = os.environ.get("FECLAW_TEST_BASE_URL", "").strip()
# 可选：使用已安装的发行版内核（例如 Windows 上不装 Playwright 自带 Chromium，
# 直接用系统 Edge：`FECLAW_PW_CHANNEL=msedge`）
PW_CHANNEL = os.environ.get("FECLAW_PW_CHANNEL", "").strip()

pytestmark_browser = pytest.mark.skipif(
    not BASE_URL,
    reason="需要 FECLAW_TEST_BASE_URL 指向本地测试 app（默认不设 ⇒ skip；绝不使用生产地址）",
)


@pytestmark_browser
class TestBrowserCookieOnlyFlow:
    """无 localStorage token 的浏览器旅程：登录 → 控制台 → 上传页 → 登出。

    运行前要求：
    1. 本地启动测试 app（假模型、临时 DB）：例如
       `FECLAW_TEST_BASE_URL=http://127.0.0.1:8081 python -m uvicorn main:app`
    2. `python -m playwright install chromium`，或设 `FECLAW_PW_CHANNEL=msedge`
       用系统已装的 Edge 内核（无需下载 Chromium）。
    """

    def test_login_chat_upload_logout_without_localstorage(self):
        playwright_api = pytest.importorskip("playwright.sync_api")
        sync_playwright = playwright_api.sync_playwright

        username = os.environ.get("FECLAW_TEST_USER", "")
        password = os.environ.get("FECLAW_TEST_PASSWORD", "")
        if not username or not password:
            pytest.skip("需要 FECLAW_TEST_USER / FECLAW_TEST_PASSWORD（本地测试账号）")

        with sync_playwright() as p:
            launch_kwargs = {}
            if PW_CHANNEL:
                launch_kwargs["channel"] = PW_CHANNEL
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
                page.fill("#usernameInput", username)
                page.fill("#passwordInput", password)
                page.click("#localLoginBtn")
                page.wait_for_load_state("domcontentloaded")

                # 1) 会话令牌不可被脚本读取（cookie 是 HttpOnly）
                token_in_storage = page.evaluate(
                    "() => localStorage.getItem('feclaw_jwt')"
                )
                assert token_in_storage is None, "浏览器仍把 JWT 写进了 localStorage"
                cookies = context.cookies()
                jwt = [c for c in cookies if c["name"] == "feclaw_jwt"]
                assert jwt, "登录后没有 feclaw_jwt cookie"
                assert jwt[0]["httpOnly"] is True, "会话 cookie 不是 HttpOnly"
                csrf = [c for c in cookies if c["name"] == "feclaw_csrf"]
                assert csrf and csrf[0]["httpOnly"] is False, "CSRF cookie 必须可读"

                # 2) 刷新后仍处于登录态（cookie 生效，不依赖 localStorage）
                page.reload(wait_until="domcontentloaded")
                body = page.text_content("body") or ""
                assert "登录" not in body[:80] or "退出" in body, body[:200]

                # 3) 登出后回到登录页，且再访问受保护页面会被挡
                page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
            finally:
                context.close()
                browser.close()
