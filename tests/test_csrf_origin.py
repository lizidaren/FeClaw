"""
T18a —— CSRF / Origin 与 redirect 白名单（安全集成测试）

覆盖（spec §5 T18 第 4/5 条、§9 A21）：

1. `safe_internal_redirect_path` 白名单：`//`、`\\`、控制字符、`javascript:`/`data:`、
   百分号编码绕过全部回落默认值。
2. **前端镜像一致性**：`static/js/auth.js` 的 `Auth.safeInternalPath` 与后端
   同一套向量结果一致（node 可用时执行，否则 skip —— 浏览器里没法 import Python）。
3. Origin / Referer 校验：外域 Origin 拒、`null` 拒、同源放行、
   `X-Forwarded-*` 不越权。
4. 双提交 token：cookies 认证的 mutation 缺 token / token 不匹配 → 403；
   Bearer 客户端豁免；安全方法（GET）不受影响。
5. **跨站 mutation 拒**：登录 / 注册被跨站 Origin 挡住（登录 CSRF）。
6. WS 握手：恶意 Origin 拒、同源放行、cookie 认证可用、逐条消息复核吊销。

运行说明：需要完整依赖；`.venv` 在交付时仍在安装，用例与实现一起交付，
执行记录见 T18a 报告。
"""

import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from config import settings
from main import app
from utils.auth_dependencies import (
    is_safe_internal_path,
    safe_internal_redirect_path,
)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_limiters():
    from services import rate_limiter as rl

    for limiter in (
        rl.login_limiter, rl.login_ip_limiter, rl.register_limiter,
        rl.password_change_limiter, rl.totp_verify_limiter,
    ):
        limiter.clear()
    yield


@pytest.fixture(autouse=True)
def _csrf_defaults(monkeypatch):
    monkeypatch.setattr(settings, "CSRF_ENABLED", True)
    monkeypatch.setattr(settings, "CSRF_REQUIRE_ORIGIN", False)
    monkeypatch.setattr(settings, "FECLAW_PUBLIC_URL", "")
    monkeypatch.setattr(settings, "OAUTH_ENABLED", False)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "open")
    yield


# ======================================================================
# 1. redirect 白名单（后端唯一实现）
# ======================================================================

# (input, expected_safe?) —— 与前端镜像测试共用同一组向量
REDIRECT_VECTORS = [
    ("/dashboard", True),
    ("/settings", True),
    ("/agent/abcd?tab=2#x", True),
    ("/a/b/c", True),
    ("", False),
    ("dashboard", False),
    ("//evil.example/pwn", False),
    ("///evil.example", False),
    ("/\\evil.example/pwn", False),
    ("\\\\evil.example", False),
    ("https://evil.example/", False),
    ("http://evil.example", False),
    ("javascript:alert(1)", False),
    ("data:text/html,<script>alert(1)</script>", False),
    ("/ok\r\nSet-Cookie: x=1", False),
    ("/ok\nLocation: https://evil", False),
    ("/ok\t/evil", False),
    ("/ok /evil", False),
    ("/%2f%2fevil.example", False),
    ("/%09//evil.example", False),
    ("/%255c%255cevil.example", False),
    ("/..%2f..%2fetc", True),      # 仍是内部相对路径（由各路由自己再做路径校验）
    ("/＼evil", True),             # 全角反斜杠不是路径分隔符
]


class TestRedirectWhitelist:
    @pytest.mark.parametrize("candidate,expected", REDIRECT_VECTORS)
    def test_is_safe_internal_path(self, candidate, expected):
        assert is_safe_internal_path(candidate) is expected, candidate

    @pytest.mark.parametrize("candidate,expected", REDIRECT_VECTORS)
    def test_safe_internal_redirect_path_falls_back(self, candidate, expected):
        got = safe_internal_redirect_path(candidate, default="/dashboard")
        if expected:
            assert got == candidate, candidate
        else:
            assert got == "/dashboard", candidate

    def test_none_and_whitespace(self):
        assert safe_internal_redirect_path(None) == "/dashboard"
        assert safe_internal_redirect_path("   ") == "/dashboard"

    def test_oauth_safe_redirect_helper_uses_same_rules(self):
        from routers.oauth import _is_safe_redirect

        assert _is_safe_redirect("/dashboard") == "/dashboard"
        assert _is_safe_redirect("//evil.example") == "/"
        assert _is_safe_redirect("/\\evil.example") == "/"
        assert _is_safe_redirect("https://evil.example/x") == "/"


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 才能校验前端 JS 镜像")
class TestFrontendMirrorParity:
    """`Auth.safeInternalPath` 必须与后端同一套规则（同一组向量）。"""

    def test_js_mirror_matches_python(self, tmp_path):
        import json

        vectors = [c for c, _ in REDIRECT_VECTORS]
        # 向量走文件而不是 argv：Windows 控制台代码页会破坏非 ASCII 字符
        vectors_file = tmp_path / "vectors.json"
        vectors_file.write_text(json.dumps(vectors, ensure_ascii=False), encoding="utf-8")

        harness = tmp_path / "parity.js"
        harness.write_text(
            """
const fs = require('fs');
const path = require('path');

// 最小 DOM/window 桩：auth.js 顶层会读 window.ROOT_DOMAIN / 安装 fetch 包装
global.window = {
  ROOT_DOMAIN: '',
  location: { pathname: '/login', href: 'http://localhost/login', search: '', hash: '',
              hostname: 'localhost', origin: 'http://localhost' },
  fetch: () => Promise.resolve({ ok: false, status: 401 }),
};
global.document = { cookie: '', createElement: () => ({ style: {}, innerHTML: '' }),
                    getElementById: () => null, body: { appendChild() {} } };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.fetch = () => Promise.resolve({ ok: false, status: 401 });

const repo = process.argv[2];
const vectors = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const src = fs.readFileSync(path.join(repo, 'static', 'js', 'auth.js'), 'utf8');
eval(src + '\\n;global.__Auth = Auth;');

const out = vectors.map(v => global.__Auth.safeInternalPath(v, '/dashboard'));
fs.writeFileSync(process.argv[4], JSON.stringify(out), 'utf8');
""",
            encoding="utf-8",
        )
        out_file = tmp_path / "out.json"
        result = subprocess.run(
            ["node", str(harness), ROOT, str(vectors_file), str(out_file)],
            capture_output=True, text=True, encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        js_results = json.loads(out_file.read_text(encoding="utf-8"))

        expected = [safe_internal_redirect_path(v, default="/dashboard") for v in vectors]
        mismatches = [
            (v, js, py) for v, js, py in zip(vectors, js_results, expected) if js != py
        ]
        assert not mismatches, f"前端/后端 redirect 白名单不一致: {mismatches}"


# ======================================================================
# 2. Origin / Referer 校验（HTTP 层）
# ======================================================================

def _seed_user(real_db, name="csrfuser", password="pw123456"):
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


def _login(client, name="csrfuser", password="pw123456", **kw):
    return client.post("/api/user/login", json={"username": name, "password": password}, **kw)


class TestOriginChecks:
    def test_cross_site_origin_rejected_on_login(self, real_db, client):
        _seed_user(real_db)
        resp = _login(client, headers={"Origin": "https://evil.example"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["status"] == "origin_rejected"
        # 拒绝时不能种下会话 cookie
        assert "feclaw_jwt=" not in resp.headers.get("set-cookie", "")

    def test_null_origin_rejected(self, real_db, client):
        _seed_user(real_db)
        resp = _login(client, headers={"Origin": "null"})
        assert resp.status_code == 403, resp.text

    def test_same_origin_allowed(self, real_db, client):
        _seed_user(real_db)
        resp = _login(client, headers={"Origin": "http://testserver"})
        assert resp.status_code == 200, resp.text

    def test_cross_site_referer_rejected_when_origin_missing(self, real_db, client):
        _seed_user(real_db)
        resp = _login(client, headers={"Referer": "https://evil.example/login"})
        assert resp.status_code == 403, resp.text

    def test_same_origin_referer_allowed(self, real_db, client):
        _seed_user(real_db)
        resp = _login(client, headers={"Referer": "http://testserver/login"})
        assert resp.status_code == 200, resp.text

    def test_forwarded_host_outside_public_domain_is_not_trusted(self, real_db, client):
        """`X-Forwarded-Host` 只有落在 FECLAW_PUBLIC_URL 域下才被当作同源。"""
        _seed_user(real_db)
        resp = _login(client, headers={
            "Origin": "https://evil.example",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
        })
        assert resp.status_code == 403, resp.text

    def test_cross_site_register_rejected(self, real_db, client):
        resp = client.post(
            "/api/user/register",
            json={"username": "xsrf", "password": "pw123456"},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403, resp.text

    def test_require_origin_mode_rejects_missing_origin(self, real_db, client, monkeypatch):
        monkeypatch.setattr(settings, "CSRF_REQUIRE_ORIGIN", True)
        _seed_user(real_db)
        resp = _login(client)
        assert resp.status_code == 403, resp.text

    def test_csrf_disabled_allows_cross_site(self, real_db, client, monkeypatch):
        """总开关只用于完全非浏览器部署；这里锁住「关掉才放行」的行为。"""
        monkeypatch.setattr(settings, "CSRF_ENABLED", False)
        _seed_user(real_db)
        resp = _login(client, headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200, resp.text


# ======================================================================
# 3. 双提交 token
# ======================================================================

class TestDoubleSubmitToken:
    def test_cookie_session_mutation_requires_token(self, real_db, client):
        _seed_user(real_db)
        assert _login(client).status_code == 200

        resp = client.post("/api/user/change-password",
                           json={"current_password": "pw123456", "new_password": "newpw123"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["status"] == "csrf_failed"

    def test_token_mismatch_rejected(self, real_db, client):
        _seed_user(real_db)
        assert _login(client).status_code == 200

        resp = client.post(
            "/api/user/change-password",
            json={"current_password": "pw123456", "new_password": "newpw123"},
            headers={"X-CSRF-Token": "deadbeef"},
        )
        assert resp.status_code == 403, resp.text

    def test_matching_token_allowed(self, real_db, client):
        _seed_user(real_db)
        assert _login(client).status_code == 200
        csrf = client.cookies.get("feclaw_csrf")
        assert csrf

        resp = client.post(
            "/api/user/change-password",
            json={"current_password": "pw123456", "new_password": "newpw123"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text

    def test_get_requests_do_not_require_token(self, real_db, client):
        _seed_user(real_db)
        assert _login(client).status_code == 200
        resp = client.get("/api/console/user")
        assert resp.status_code == 200, resp.text

    def test_bearer_exempt_from_token(self, real_db, client):
        _seed_user(real_db)
        token = _login(client).json()["token"]
        client.cookies.clear()

        resp = client.post(
            "/api/user/change-password",
            json={"current_password": "pw123456", "new_password": "newpw123"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    def test_tokens_match_helper_rejects_empty(self):
        class _Req:
            method = "POST"
            headers = {}
            cookies = {}
            url = type("U", (), {"path": "/x"})()

        from utils.csrf import csrf_token_matches, requires_csrf_token

        assert csrf_token_matches(_Req()) is False
        assert requires_csrf_token(_Req()) is False  # 没有会话 cookie ⇒ 不要求 token


# ======================================================================
# 4. WebSocket：Origin 允许列表 + cookie 认证 + 逐条复核
# ======================================================================

def _mk_token(typ="session", user_id=1, **extra):
    import datetime as _dt
    from jose import jwt

    payload = {
        "user_id": user_id,
        "exp": _dt.datetime.utcnow() + _dt.timedelta(hours=1),
    }
    if typ is not None:
        payload["typ"] = typ
    payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def _seed_ws_owner(real_db):
    from models.database import User
    from models.agent_profile import AgentProfile
    from utils.auth import hash_password

    db = real_db()
    user = User(username="ws_t18", password_hash=hash_password("pw"), salt=None, is_admin=False)
    db.add(user)
    db.commit()
    uid = user.id
    db.add(AgentProfile(user_id=uid, hash="8d85", totp_secret="X" * 32, name="a", status="pending"))
    db.commit()
    db.close()
    return uid


def _first_ws_event(ws):
    try:
        return ws.receive_json()
    except WebSocketDisconnect:
        return {"type": "__closed__"}


class TestWebSocketOriginAndAuth:
    def test_malicious_origin_rejected(self, real_db, client):
        uid = _seed_ws_owner(real_db)
        token = _mk_token(user_id=uid)
        from unittest.mock import patch

        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(
                "/api/chat/ws?agent_hash=8d85",
                headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
            ) as ws:
                event = _first_ws_event(ws)
        assert event.get("type") != "ping", event
        assert event.get("code") == "ORIGIN_REJECTED", event

    def test_same_origin_bearer_allowed(self, real_db, client):
        uid = _seed_ws_owner(real_db)
        token = _mk_token(user_id=uid)
        from unittest.mock import patch

        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(
                "/api/chat/ws?agent_hash=8d85",
                headers={"Authorization": f"Bearer {token}", "Origin": "http://testserver"},
            ) as ws:
                event = _first_ws_event(ws)
        assert event.get("type") == "ping", event

    def test_cookie_auth_works_without_query_token(self, real_db, client):
        """浏览器场景：只有 cookie，没有 ?token= —— 这是 T18a 的目标路径。"""
        uid = _seed_ws_owner(real_db)
        token = _mk_token(user_id=uid)
        client.cookies.set("feclaw_jwt", token)
        from unittest.mock import patch

        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(
                "/api/chat/ws?agent_hash=8d85",
                headers={"Origin": "http://testserver"},
            ) as ws:
                event = _first_ws_event(ws)
        client.cookies.clear()
        assert event.get("type") == "ping", event

    def test_query_token_ignored_for_browser_origin(self, real_db, client):
        """带 Origin 的握手 = 浏览器 ⇒ query token 不被接受（避免 JWT 进 URL）。"""
        uid = _seed_ws_owner(real_db)
        token = _mk_token(user_id=uid)
        from unittest.mock import patch

        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(
                f"/api/chat/ws?token={token}&agent_hash=8d85",
                headers={"Origin": "http://testserver"},
            ) as ws:
                event = _first_ws_event(ws)
        assert event.get("type") != "ping", event
        assert event.get("code") == "AUTH_ERROR", event

    def test_message_recheck_detects_revocation(self, real_db, client):
        """连接后登出（jwt_version +1）⇒ 下一条消息必须被拒并断开。"""
        from unittest.mock import patch
        from utils.auth import bump_user_jwt_version

        uid = _seed_ws_owner(real_db)
        token = _mk_token(user_id=uid, jwt_version=0)
        from models.database import User

        db = real_db()
        try:
            db.query(User).filter(User.id == uid).update({"jwt_version": 0})
            db.commit()
        finally:
            db.close()

        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(
                "/api/chat/ws?agent_hash=8d85",
                headers={"Authorization": f"Bearer {token}", "Origin": "http://testserver"},
            ) as ws:
                assert _first_ws_event(ws).get("type") == "ping"
                # 模拟登出吊销
                db = real_db()
                try:
                    db.query(User).filter(User.id == uid).update({"jwt_version": 1})
                    db.commit()
                finally:
                    db.close()
                ws.send_json({"content": "hello"})
                event = _first_ws_event(ws)
        assert event.get("type") == "error", event
        assert event.get("code") == "AUTH_ERROR", event


# ======================================================================
# 5. SSE 聊天 POST：JS 可触发的 mutation 必须过 CSRF
# ======================================================================

class TestChatStreamCsrf:
    _BODY = {
        "content": "hi",
        "channel": "web",
        "agent_hash": "ffffffff",   # 故意不存在：阻断在归属校验，不会真的调模型
    }

    def test_anonymous_is_401(self, client):
        resp = client.post("/api/chat/stream", json=self._BODY)
        assert resp.status_code == 401, resp.text

    def test_cookie_without_csrf_token_is_403(self, real_db, client):
        _seed_user(real_db)
        assert _login(client).status_code == 200

        resp = client.post("/api/chat/stream", json=self._BODY)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["status"] == "csrf_failed"

    def test_bearer_client_passes_csrf_layer(self, real_db, client):
        """Bearer 客户端豁免 CSRF：应当走到归属校验（403 属主），而不是 csrf_failed。"""
        _seed_user(real_db)
        token = _login(client).json()["token"]
        client.cookies.clear()

        resp = client.post(
            "/api/chat/stream", json=self._BODY,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code in (403, 404), resp.text
        assert "csrf_failed" not in resp.text


# ======================================================================
# 6. 静态断言：Origin/CSRF 逻辑只有一份实现
# ======================================================================

class TestNoDuplicateOriginLogic:
    def test_owned_routers_do_not_hand_roll_origin_checks(self):
        """T18a 拥有的 router 不应再手写 `headers.get('origin') == ...` 比较。

        只扫本切片负责的文件：其他 router 的 Origin 需求应在拿到「跨界请求」后
        统一改走 `utils.csrf`，不要让跨线程文件互相卡红。
        """
        import re

        owned = ["user.py", "oauth.py", "feclaw_chat.py"]
        offenders = []
        for name in owned:
            path = os.path.join(ROOT, "routers", name)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            if re.search(r"headers\.get\(['\"]origin['\"]\)\s*==", text):
                offenders.append(name)
        assert not offenders, f"这些 router 手写了 Origin 比较，请改用 utils.csrf: {offenders}"

    def test_ws_origin_check_goes_through_shared_helper(self):
        text = open(os.path.join(ROOT, "routers", "feclaw_chat.py"), encoding="utf-8").read()
        assert "check_ws_origin(" in text

    def test_csrf_module_is_the_single_entry(self):
        from utils import csrf

        assert hasattr(csrf, "csrf_protect")
        assert hasattr(csrf, "check_request_origin")
        assert hasattr(csrf, "check_ws_origin")
        assert hasattr(csrf, "csrf_token_matches")
