"""
PKCE-aware OAuth exchange tests (FeClaw-Mobile).

覆盖：
1. Happy path — mobile 发 {code, code_verifier} → FeClaw 转发到 Platform /token
2. Missing code_verifier → 400
3. Legacy {platform_token} 仍正常工作
4. 两者都缺 → 400
5. Platform /token 返回 invalid_grant → FeClaw 返回 502
6. /mobile-login 转发 code_challenge + method=S256 到 Platform /authorize

所有外部 HTTP 调用都用 mock，不依赖真实 Platform / DB。
"""

import base64
import hashlib
import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


# ────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────


def _make_oauth_service(**overrides):
    """构造一个 OAuthService，所有 settings 可覆盖"""
    defaults = {
        "OAUTH_PROVIDER_URL": "https://sso.example.com",
        "OAUTH_CLIENT_ID": "feclaw",
        "OAUTH_CLIENT_SECRET": "secret",
        "OAUTH_REDIRECT_URI": "https://feclaw.example.com/callback",
        "OAUTH_AUTHORIZE_URL": "",
        "OAUTH_TOKEN_URL": "https://sso.example.com/token",
        "OAUTH_USERINFO_URL": "https://sso.example.com/userinfo",
        "OAUTH_JWKS_URL": "",
        "OAUTH_END_SESSION_URL": "",
        "JWT_SECRET": "test_secret_key_for_pkce",
        "JWT_ALGORITHM": "HS256",
        "JWT_EXPIRE_HOURS": 168,
    }
    defaults.update(overrides)
    with patch("services.oauth_service.settings") as mock_settings:
        for k, v in defaults.items():
            setattr(mock_settings, k, v)
        from services.oauth_service import OAuthService
        return OAuthService()


def _compute_pkce_pair() -> tuple[str, str]:
    """生成一对真实可用的 PKCE verifier + S256 challenge（RFC 7636 §4）"""
    verifier = base64.urlsafe_b64encode(b"a-test-code-verifier-1234567890ab").decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode().rstrip("=")
    return verifier, challenge


class _FakeHTTPResult:
    """模拟 httpx 响应的最小可用对象"""
    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            # httpx 在 4xx/5xx 默认抛 HTTPStatusError；模拟真实行为
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=MagicMock(),
                response=MagicMock(status_code=self.status_code),
            )

    def json(self) -> dict:
        return self._json_data


def _patch_httpx_post(post_responses: list | None = None, post_side_effect=None):
    """
    Patch httpx.AsyncClient.post — 返回受控响应序列或抛指定异常。
    返回 (mock_client_factory, captured_calls) — captured_calls 记录每次 post 的 kwargs。
    """
    captured: list[dict] = []
    responses_iter = iter(post_responses or [])

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.append({"url": url, **kwargs})
            if post_side_effect is not None:
                raise post_side_effect
            try:
                return next(responses_iter)
            except StopIteration:
                return _FakeHTTPResult(200, {})

        async def get(self, url, **kwargs):
            captured.append({"url": url, "method": "GET", **kwargs})
            try:
                return next(responses_iter)
            except StopIteration:
                return _FakeHTTPResult(200, {})

    return _FakeClient, captured


# ────────────────────────────────────────────────────────────
# 单元测试：OAuthService.exchange_code_with_pkce
# ────────────────────────────────────────────────────────────


class TestExchangeCodeWithPkce:
    """OAuthService.exchange_code_with_pkce 的契约测试"""

    @pytest.mark.asyncio
    async def test_post_body_includes_code_and_verifier(self):
        """POST body 必须包含 grant_type/code/code_verifier/client_id/redirect_uri"""
        svc = _make_oauth_service()
        FakeClient, calls = _patch_httpx_post(
            post_responses=[_FakeHTTPResult(200, {"access_token": "x"})]
        )

        with patch("httpx.AsyncClient", FakeClient):
            await svc.exchange_code_with_pkce(
                code="auth_code_xyz",
                code_verifier="verifier_123abc",
                redirect_uri="feclaw://oauth/callback",
            )

        assert len(calls) == 1
        body = calls[0]["data"]
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "auth_code_xyz"
        assert body["code_verifier"] == "verifier_123abc"
        assert body["client_id"] == "feclaw"
        assert body["redirect_uri"] == "feclaw://oauth/callback"

    @pytest.mark.asyncio
    async def test_no_client_secret_in_post_body(self):
        """PKCE 流程 **不**发送 client_secret（PKCE 替代共享密钥信任）"""
        svc = _make_oauth_service()
        FakeClient, calls = _patch_httpx_post(
            post_responses=[_FakeHTTPResult(200, {"access_token": "x"})]
        )

        with patch("httpx.AsyncClient", FakeClient):
            await svc.exchange_code_with_pkce(
                code="auth_code_xyz",
                code_verifier="verifier_123abc",
            )

        body = calls[0]["data"]
        assert "client_secret" not in body, "PKCE flow must NOT include client_secret"

    @pytest.mark.asyncio
    async def test_uses_default_redirect_uri_when_not_given(self):
        """未指定 redirect_uri 时退化到 settings.OAUTH_REDIRECT_URI"""
        svc = _make_oauth_service()
        FakeClient, calls = _patch_httpx_post(
            post_responses=[_FakeHTTPResult(200, {"access_token": "x"})]
        )

        with patch("httpx.AsyncClient", FakeClient):
            await svc.exchange_code_with_pkce(
                code="c",
                code_verifier="v",
            )

        assert calls[0]["data"]["redirect_uri"] == "https://feclaw.example.com/callback"

    @pytest.mark.asyncio
    async def test_returns_token_dict_on_success(self):
        """2xx 响应 → 返回 token dict"""
        svc = _make_oauth_service()
        token_payload = {
            "access_token": "platform_acc",
            "id_token": "platform_id",
            "refresh_token": "platform_ref",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        FakeClient, _ = _patch_httpx_post(
            post_responses=[_FakeHTTPResult(200, token_payload)]
        )

        with patch("httpx.AsyncClient", FakeClient):
            result = await svc.exchange_code_with_pkce(code="c", code_verifier="v")

        assert result == token_payload
        assert result["access_token"] == "platform_acc"

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self):
        """网络/HTTP 错误 → 返回 None（与 exchange_code_for_token 行为一致）"""
        svc = _make_oauth_service()
        FakeClient, _ = _patch_httpx_post(
            post_side_effect=httpx.HTTPError("network down"),
        )

        with patch("httpx.AsyncClient", FakeClient):
            result = await svc.exchange_code_with_pkce(code="c", code_verifier="v")

        assert result is None


class TestExchangeCodeWithPkceVerbose:
    """verbose 版本区分 invalid_grant（4xx）和网络错误"""

    @pytest.mark.asyncio
    async def test_invalid_grant_returns_structured_error(self):
        """Platform 返回 400 invalid_grant → verbose 返回 ok=False + status + error"""
        svc = _make_oauth_service()
        FakeClient, _ = _patch_httpx_post(
            post_responses=[_FakeHTTPResult(400, {
                "error": "invalid_grant",
                "error_description": "code has expired",
            })],
        )

        with patch("httpx.AsyncClient", FakeClient):
            result = await svc.exchange_code_with_pkce_verbose(
                code="expired", code_verifier="v",
            )

        assert result["ok"] is False
        assert result["status"] == 400
        assert result["error"] == "invalid_grant"
        assert "expired" in result["error_description"]

    @pytest.mark.asyncio
    async def test_success_returns_data(self):
        """Platform 返回 2xx → verbose 返回 ok=True + data"""
        svc = _make_oauth_service()
        FakeClient, _ = _patch_httpx_post(
            post_responses=[_FakeHTTPResult(200, {"access_token": "ok"})],
        )

        with patch("httpx.AsyncClient", FakeClient):
            result = await svc.exchange_code_with_pkce_verbose(
                code="c", code_verifier="v",
            )

        assert result["ok"] is True
        assert result["data"]["access_token"] == "ok"


# ────────────────────────────────────────────────────────────
# 路由层测试：/api/oauth/exchange 集成（用 real_db fixture）
# ────────────────────────────────────────────────────────────


class TestOAuthExchangeRoutePkce:
    """/api/oauth/exchange 路由 PKCE 流程"""

    @pytest.fixture
    def client(self, real_db):
        """FastAPI TestClient + 内存 SQLite（real_db fixture）"""
        with patch("routers.oauth.oauth_service") as mock_svc:
            # 默认：PKCE 交换成功 + userinfo 成功
            mock_svc.exchange_code_with_pkce_verbose = AsyncMock(return_value={
                "ok": True,
                "data": {"access_token": "platform_acc_123"},
            })
            mock_svc.get_userinfo = AsyncMock(return_value={
                "id": "platform_user_42",
                "username": "alice",
                "email": "alice@example.com",
                "is_admin": False,
            })
            from main import app
            yield TestClient(app, raise_server_exceptions=False), mock_svc

    def test_happy_path_pkce_returns_feclaw_token_pair(self, client):
        """PKCE 流程：mobile 发 {code, code_verifier} → 返回 FeClaw access + refresh"""
        test_client, mock_svc = client
        resp = test_client.post(
            "/api/oauth/exchange",
            json={
                "code": "auth_code_xyz",
                "code_verifier": "verifier_with_enough_entropy_1234",
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "success"
        assert data["auth_method"] == "platform"
        assert "token" in data and data["token"]
        assert "refresh_token" in data and data["refresh_token"]
        assert "expires_in" in data
        assert "refresh_expires_in" in data
        assert data["user_id"]
        assert data["username"] == "alice"

        # 验证 FeClaw 确实调了 Platform /token（PKCE）
        mock_svc.exchange_code_with_pkce_verbose.assert_awaited_once()
        kwargs = mock_svc.exchange_code_with_pkce_verbose.await_args.kwargs
        assert kwargs["code"] == "auth_code_xyz"
        assert kwargs["code_verifier"] == "verifier_with_enough_entropy_1234"

        # 验证 FeClaw 调了 Platform /userinfo
        mock_svc.get_userinfo.assert_awaited_once_with("platform_acc_123")

    def test_pkce_path_creates_user_on_first_login(self, client, real_db):
        """首次登录应创建 FeClaw User + UserLink"""
        test_client, _ = client
        resp = test_client.post(
            "/api/oauth/exchange",
            json={
                "code": "code_abc",
                "code_verifier": "verifier_123",
            },
        )
        assert resp.status_code == 200, resp.text

        # 验证 DB 里有 user
        from models.database import User, UserLink
        db = real_db()
        try:
            user = db.query(User).filter(User.username == "alice").first()
            assert user is not None, "user should be created via find_or_create_user_from_platform"
            link = db.query(UserLink).filter(
                UserLink.user_id == user.id,
                UserLink.provider == "platform",
                UserLink.provider_user_id == "platform_user_42",
            ).first()
            assert link is not None, "UserLink should be created"
        finally:
            db.close()

    def test_missing_code_verifier_returns_400(self, client):
        """PKCE 流程缺 code_verifier → 400"""
        test_client, _ = client
        resp = test_client.post(
            "/api/oauth/exchange",
            json={"code": "code_only"},
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["status"] == "invalid_request"
        assert "together" in detail["message"].lower()

    def test_missing_code_returns_400(self, client):
        """PKCE 流程缺 code → 400"""
        test_client, _ = client
        resp = test_client.post(
            "/api/oauth/exchange",
            json={"code_verifier": "verifier_only"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["status"] == "invalid_request"

    def test_neither_field_returns_400(self, client):
        """code/code_verifier/platform_token 都没 → 400"""
        test_client, _ = client
        resp = test_client.post("/api/oauth/exchange", json={})
        assert resp.status_code == 400
        assert resp.json()["detail"]["status"] == "invalid_request"
        assert "either" in resp.json()["detail"]["message"].lower()

    def test_empty_body_returns_400(self, client):
        """完全空 body → 400（不是 422）"""
        test_client, _ = client
        resp = test_client.post("/api/oauth/exchange")
        assert resp.status_code == 400


class TestOAuthExchangeRoutePkceUpstreamErrors:
    """/api/oauth/exchange 路由 PKCE 流程的上游错误处理"""

    @pytest.fixture
    def client(self, real_db):
        with patch("routers.oauth.oauth_service") as mock_svc:
            mock_svc.exchange_code_with_pkce_verbose = AsyncMock(return_value={
                "ok": False,
                "status": 400,
                "error": "invalid_grant",
                "error_description": "authorization code expired",
            })
            from main import app
            yield TestClient(app, raise_server_exceptions=False), mock_svc

    def test_invalid_grant_returns_502(self, client):
        """Platform 返回 invalid_grant → FeClaw 返回 502 + 结构化 detail"""
        test_client, _ = client
        resp = test_client.post(
            "/api/oauth/exchange",
            json={"code": "expired_code", "code_verifier": "v"},
        )
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert detail["status"] == "platform_token_exchange_failed"
        assert detail["error"] == "invalid_grant"
        assert detail["upstream_status"] == 400

    def test_platform_5xx_returns_502(self, real_db):
        """Platform 返回 5xx → FeClaw 返回 502"""
        with patch("routers.oauth.oauth_service") as mock_svc:
            mock_svc.exchange_code_with_pkce_verbose = AsyncMock(return_value={
                "ok": False,
                "status": 503,
                "error": "temporarily_unavailable",
                "error_description": "platform down",
            })
            from main import app
            c = TestClient(app, raise_server_exceptions=False)
            resp = c.post(
                "/api/oauth/exchange",
                json={"code": "c", "code_verifier": "v"},
            )
        assert resp.status_code == 502
        assert resp.json()["detail"]["upstream_status"] == 503


class TestOAuthExchangeRouteLegacyCompat:
    """/api/oauth/exchange legacy platform_token 流程必须继续工作"""

    @pytest.fixture
    def client(self, real_db):
        with patch("routers.oauth.oauth_service") as mock_svc:
            # _verify_platform_token_via_me 在 oauth.py 里直接 httpx 调 Platform /api/auth/me
            # 这里直接 patch 那个 helper
            with patch(
                "routers.oauth._verify_platform_token_via_me",
                new=AsyncMock(return_value={
                    "id": "legacy_platform_user_99",
                    "username": "bob",
                    "email": "bob@example.com",
                }),
            ):
                from main import app
                yield TestClient(app, raise_server_exceptions=False)

    def test_legacy_platform_token_still_works(self, client):
        """仅发 platform_token → 仍走 legacy /api/auth/me → 返回 FeClaw JWT pair"""
        resp = client.post(
            "/api/oauth/exchange",
            json={"platform_token": "legacy_acc_token_abc"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "success"
        assert data["username"] == "bob"
        assert data["auth_method"] == "platform"
        assert data["token"]


# ────────────────────────────────────────────────────────────
# /api/oauth/mobile-login PKCE 参数透传
# ────────────────────────────────────────────────────────────


class TestOAuthMobileLoginPkceParams:
    """/mobile-login 必须把 code_challenge + method=S256 加到 Platform authorize URL"""

    @pytest.fixture
    def client(self):
        from main import app
        with patch("routers.oauth.settings") as mock_settings:
            mock_settings.OAUTH_PROVIDER_URL = "https://sso.example.com"
            mock_settings.OAUTH_AUTHORIZE_URL = ""
            mock_settings.OAUTH_CLIENT_ID = "feclaw"
            mock_settings.FECLAW_PUBLIC_URL = "feclaw.example.com"
            mock_settings.FECLAW_SUBDOMAIN_ENABLED = False
            yield TestClient(app, raise_server_exceptions=False)

    def test_mobile_login_forwards_pkce_challenge(self, client):
        """/mobile-login 收到 code_challenge 后会拼到 Platform authorize URL"""
        verifier, challenge = _compute_pkce_pair()

        resp = client.get(
            "/api/oauth/mobile-login",
            params={
                "scheme": "feclaw",
                "state": "csrf_state_1234567890",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.text
        location = resp.headers["location"]
        assert location.startswith("https://sso.example.com/authorize?")
        assert f"code_challenge={challenge}" in location
        assert "code_challenge_method=S256" in location
        # 其它既有参数必须保留
        assert "client_id=feclaw" in location
        assert "state=csrf_state_1234567890" in location
        assert "redirect_uri=feclaw%3A%2F%2Foauth%2Fcallback" in location

    def test_mobile_login_without_pkce_omits_params(self, client):
        """不带 PKCE 参数的旧 mobile 调用 → 仍走老路径（不传 challenge）"""
        resp = client.get(
            "/api/oauth/mobile-login",
            params={"scheme": "feclaw", "state": "csrf_state_1234567890"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "code_challenge=" not in location

    def test_mobile_login_rejects_invalid_pkce_method(self, client):
        """code_challenge_method 必须是 S256 或 plain"""
        resp = client.get(
            "/api/oauth/mobile-login",
            params={
                "scheme": "feclaw",
                "state": "csrf_state_1234567890",
                "code_challenge": "anychallenge",
                "code_challenge_method": "MD5",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["status"] == "invalid_pkce"

    def test_mobile_login_accepts_plain_method(self, client):
        """code_challenge_method=plain 也允许（challenge 长度必须 43-128）"""
        # RFC 7636 §4.1：code_verifier 长度 43-128 的 [A-Za-z0-9_-] 串
        plain_challenge = "a" * 50
        resp = client.get(
            "/api/oauth/mobile-login",
            params={
                "scheme": "feclaw",
                "state": "csrf_state_1234567890",
                "code_challenge": plain_challenge,
                "code_challenge_method": "plain",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "code_challenge_method=plain" in resp.headers["location"]
        assert f"code_challenge={plain_challenge}" in resp.headers["location"]


# ────────────────────────────────────────────────────────────
# 请求模型兼容性
# ────────────────────────────────────────────────────────────


class TestOAuthExchangeRequestModel:
    """OAuthExchangeRequest 必须接受所有 legacy + PKCE 字段，且不拒额外字段"""

    def test_accepts_pkce_fields_only(self):
        from routers.oauth import OAuthExchangeRequest
        req = OAuthExchangeRequest(code="abc", code_verifier="verifier_xyz")
        assert req.code == "abc"
        assert req.code_verifier == "verifier_xyz"
        assert req.platform_token is None

    def test_accepts_legacy_platform_token_only(self):
        from routers.oauth import OAuthExchangeRequest
        req = OAuthExchangeRequest(platform_token="legacy_token_abc")
        assert req.platform_token == "legacy_token_abc"
        assert req.code is None
        assert req.code_verifier is None

    def test_accepts_both_for_max_flexibility(self):
        """两端都给也不报错（router 会优先走 PKCE）"""
        from routers.oauth import OAuthExchangeRequest
        req = OAuthExchangeRequest(
            platform_token="legacy",
            code="c",
            code_verifier="v",
        )
        assert req.platform_token == "legacy"
        assert req.code == "c"

    def test_accepts_extra_fields_without_forbid(self):
        """mobile 可能顺手带 redirect_uri 等冗余字段 — 不能被 extra='forbid' 拦下"""
        from routers.oauth import OAuthExchangeRequest
        req = OAuthExchangeRequest(
            code="c",
            code_verifier="v",
            redirect_uri="feclaw://oauth/callback",
        )
        assert req.redirect_uri == "feclaw://oauth/callback"

    def test_all_fields_optional(self):
        """所有字段默认 None — body 必须显式提供至少一个流程"""
        from routers.oauth import OAuthExchangeRequest
        req = OAuthExchangeRequest()
        assert req.code is None
        assert req.code_verifier is None
        assert req.platform_token is None
        assert req.id_token is None
        assert req.redirect_uri is None