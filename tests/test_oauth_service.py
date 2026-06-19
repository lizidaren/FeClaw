"""
OAuth 服务单元测试

测试覆盖：
1. OAuthService 初始化和配置检查
2. get_authorize_url 授权 URL 生成
3. build_logout_url 登出 URL 构建
4. exchange_code_for_token Token 交换
5. refresh_token Token 刷新
6. get_userinfo 用户信息获取
7. create_local_jwt / verify_local_jwt

所有测试 mock 外部依赖，不真调 HTTP/DB。
"""

import httpx
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime

pytestmark = pytest.mark.unit


def _make_svc(**overrides):
    """创建 OAuthService 实例，可覆盖 settings 属性"""
    defaults = {
        "OAUTH_PROVIDER_URL": "https://sso.example.com",
        "OAUTH_CLIENT_ID": "feclaw",
        "OAUTH_CLIENT_SECRET": "secret",
        "OAUTH_REDIRECT_URI": "https://feclaw.example.com/callback",
        "OAUTH_AUTHORIZE_URL": "",
        "OAUTH_TOKEN_URL": "",
        "OAUTH_USERINFO_URL": "",
        "OAUTH_JWKS_URL": "",
        "OAUTH_END_SESSION_URL": "",
        "JWT_SECRET": "test_secret_key",
        "JWT_ALGORITHM": "HS256",
        "JWT_EXPIRE_HOURS": 168,
    }
    defaults.update(overrides)
    with patch("services.oauth_service.settings") as mock_settings:
        for k, v in defaults.items():
            setattr(mock_settings, k, v)
        from services.oauth_service import OAuthService
        return OAuthService()


class TestOAuthServiceInit:
    """OAuthService 初始化测试"""

    def test_init_with_config(self):
        """配置完整时应设置 _oauth_configured 为 True"""
        svc = _make_svc()
        assert svc._oauth_configured is True
        assert svc.provider_url == "https://sso.example.com"

    def test_init_without_config(self):
        """无 provider_url 时应设置 _oauth_configured 为 False"""
        svc = _make_svc(OAUTH_PROVIDER_URL="", OAUTH_CLIENT_ID="")
        assert svc._oauth_configured is False

    def test_url_derivation(self):
        """未配置端点 URL 时应从 provider_url 推导 OIDC 标准路径"""
        svc = _make_svc()
        assert "/authorize" in svc.authorize_url
        assert "/token" in svc.token_url
        assert "/userinfo" in svc.userinfo_url
        assert "/.well-known/jwks.json" in svc.jwks_url


class TestGetAuthorizeUrl:
    """get_authorize_url 测试"""

    def test_returns_url_when_configured(self):
        """配置完整时应返回授权 URL"""
        svc = _make_svc()
        url = svc.get_authorize_url("test_state_123")
        assert url is not None
        assert "response_type=code" in url
        assert "client_id=feclaw" in url
        assert "state=test_state_123" in url

    def test_returns_none_when_not_configured(self):
        """未配置时应返回 None"""
        svc = _make_svc(OAUTH_PROVIDER_URL="", OAUTH_CLIENT_ID="")
        assert svc.get_authorize_url("state") is None


class TestBuildLogoutUrl:
    """build_logout_url 测试"""

    def test_basic_logout_url(self):
        """无参数时应返回基本登出 URL"""
        svc = _make_svc(OAUTH_END_SESSION_URL="https://sso.example.com/oauth/end-session")
        url = svc.build_logout_url()
        assert url == "https://sso.example.com/oauth/end-session"

    def test_logout_with_id_token(self):
        """带 id_token_hint 时应包含在 URL 中"""
        svc = _make_svc(OAUTH_END_SESSION_URL="https://sso.example.com/oauth/end-session")
        url = svc.build_logout_url(id_token="my_id_token")
        assert "id_token_hint=my_id_token" in url

    def test_logout_with_redirect(self):
        """带 post_logout_redirect_uri 时应包含 state"""
        svc = _make_svc(OAUTH_END_SESSION_URL="https://sso.example.com/oauth/end-session")
        url = svc.build_logout_url(
            id_token="my_token",
            post_logout_redirect_uri="https://feclaw.example.com"
        )
        assert "post_logout_redirect_uri" in url
        assert "state=" in url


class TestExchangeCodeForToken:
    """exchange_code_for_token 测试"""

    @pytest.mark.asyncio
    async def test_successful_exchange(self, mock_httpx_client):
        """成功交换 code 应返回 token 数据"""
        mock_httpx_client["set_post_response"]({
            "access_token": "acc_token",
            "id_token": "id_token",
            "token_type": "Bearer",
        })
        svc = _make_svc()
        result = await svc.exchange_code_for_token("auth_code_123")
        assert result is not None
        assert result["access_token"] == "acc_token"

    @pytest.mark.asyncio
    async def test_failed_exchange(self):
        """HTTP 错误时应返回 None"""
        svc = _make_svc()
        with patch.object(svc, "token_url", "https://sso.example.com/token"):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(side_effect=httpx.HTTPError("Connection error"))
                result = await svc.exchange_code_for_token("bad_code")
                assert result is None


class TestRefreshToken:
    """refresh_token 测试"""

    @pytest.mark.asyncio
    async def test_successful_refresh(self, mock_httpx_client):
        """成功刷新 token 应返回新 token 数据"""
        mock_httpx_client["set_post_response"]({
            "access_token": "new_acc_token",
            "token_type": "Bearer",
        })
        svc = _make_svc()
        result = await svc.refresh_token("old_refresh_token")
        assert result is not None
        assert result["access_token"] == "new_acc_token"

    @pytest.mark.asyncio
    async def test_failed_refresh(self):
        """HTTP 错误时应返回 None"""
        svc = _make_svc()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(side_effect=httpx.HTTPError("Network error"))
            result = await svc.refresh_token("bad_token")
            assert result is None


class TestGetUserinfo:
    """get_userinfo 测试（用户信息通过 GET 请求获取）"""

    @pytest.mark.asyncio
    async def test_successful_userinfo(self):
        """成功获取用户信息应返回用户数据"""
        svc = _make_svc()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {"sub": "user_123", "username": "testuser"}
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await svc.get_userinfo("valid_token")
            assert result is not None
            assert result["sub"] == "user_123"

    @pytest.mark.asyncio
    async def test_failed_userinfo(self):
        """HTTP 错误时应返回 None"""
        svc = _make_svc()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("HTTP error"))
            result = await svc.get_userinfo("bad_token")
            assert result is None


class TestLocalJWT:
    """create_local_jwt / verify_local_jwt 测试"""

    def test_create_local_jwt(self):
        """create_local_jwt 应生成包含用户信息的 JWT"""
        svc = _make_svc()
        user_info = {
            "sub": "user_42",
            "username": "testuser",
            "email": "test@example.com",
        }
        token = svc.create_local_jwt(user_info)
        assert token is not None
        assert isinstance(token, str)
        assert token.count(".") == 2

    def test_verify_local_jwt_valid(self):
        """verify_local_jwt 应验证自己签发的 token"""
        svc = _make_svc()
        user_info = {"sub": "user_42", "username": "testuser", "email": "test@example.com"}
        token = svc.create_local_jwt(user_info)
        payload = svc.verify_local_jwt(token)
        assert payload is not None
        assert payload.get("user_id") == "user_42"

    def test_verify_local_jwt_invalid(self):
        """无效 token 应返回 None"""
        svc = _make_svc()
        payload = svc.verify_local_jwt("invalid.token.here")
        assert payload is None
