"""
OAuth Client 服务
处理与 FirstEntrancePlatform 的 OAuth 认证流程
"""

import secrets
import httpx
from typing import Optional, Dict, Any
from urllib.parse import urlencode, urljoin
from jose import jwt, JWTError
from datetime import datetime, timedelta
import logging

from config import settings

logger = logging.getLogger(__name__)


class OAuthService:
    """OAuth Client 服务类"""

    # JWKS 缓存
    _jwks_cache: Optional[Dict[str, Any]] = None
    _jwks_cache_time: Optional[datetime] = None
    _jwks_cache_ttl: int = 300  # 秒

    def __init__(self):
        self.provider_url = settings.OAUTH_PROVIDER_URL
        self.client_id = settings.OAUTH_CLIENT_ID
        self.client_secret = settings.OAUTH_CLIENT_SECRET
        self.redirect_uri = settings.OAUTH_REDIRECT_URI
        self._oauth_configured = bool(self.provider_url and self.client_id)

        # OAuth 端点：优先使用显式配置，否则使用 OIDC 标准路径
        self.authorize_url = settings.OAUTH_AUTHORIZE_URL or urljoin(self.provider_url, "/authorize")
        self.token_url = settings.OAUTH_TOKEN_URL or urljoin(self.provider_url, "/token")
        self.userinfo_url = settings.OAUTH_USERINFO_URL or urljoin(self.provider_url, "/userinfo")
        self.jwks_url = settings.OAUTH_JWKS_URL or urljoin(self.provider_url, "/.well-known/jwks.json")
        self.end_session_endpoint = settings.OAUTH_END_SESSION_URL or urljoin(self.provider_url, "/oauth/end-session")

    def build_logout_url(self, id_token: str = "", post_logout_redirect_uri: str = "") -> str:
        """构建 OIDC RP-Initiated Logout URL"""
        params = {}
        if id_token:
            params["id_token_hint"] = id_token
        if post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = post_logout_redirect_uri
            params["state"] = secrets.token_urlsafe(16)
        if params:
            return f"{self.end_session_endpoint}?{urlencode(params)}"
        return self.end_session_endpoint

    def get_authorize_url(self, state: str) -> Optional[str]:
        """生成授权 URL，重定向到 Platform 登录"""
        if not self._oauth_configured:
            return None
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "scope": "openid profile email"
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    async def exchange_code_for_token(self, code: str) -> Optional[Dict[str, Any]]:
        """用授权码换取 token（legacy: client_secret 鉴权）"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "redirect_uri": self.redirect_uri
                    },
                    headers={"Accept": "application/json"}
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to exchange code for token: {e}")
            return None

    async def exchange_code_with_pkce(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        PKCE-aware authorization code exchange (RFC 7636).

        POST {token_url} with:
          - grant_type=authorization_code
          - code
          - code_verifier
          - client_id
          - redirect_uri

        **No client_secret** — PKCE replaces the shared-secret trust with
        per-flow challenge/verifier binding. Returns the raw token dict on
        non-2xx responses (so the caller can surface invalid_grant etc.) by
        raising the underlying HTTPStatusError; callers should inspect
        response.json() themselves when they need error detail.

        For most callers, returning None on HTTP error (matching the existing
        exchange_code_for_token behavior) is sufficient; the router layer
        will surface a 502 in that case. Use `exchange_code_with_pkce_verbose`
        when you need the response body on failure (e.g., to distinguish
        invalid_grant from a network outage).
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "code_verifier": code_verifier,
                        "client_id": self.client_id,
                        "redirect_uri": redirect_uri or self.redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to exchange code with PKCE: {e}")
            return None

    async def exchange_code_with_pkce_verbose(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        PKCE-aware exchange that returns a structured result on failure.

        Returns one of:
          {"ok": True,  "data": {<token dict>}}
          {"ok": False, "status": <int>, "error": <str>, "error_description": <str>}

        Use this when the caller needs to distinguish invalid_grant (4xx) from
        upstream outage (5xx / network).
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "code_verifier": code_verifier,
                        "client_id": self.client_id,
                        "redirect_uri": redirect_uri or self.redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as e:
            logger.error(f"PKCE exchange network error: {e}")
            return {"ok": False, "status": 0, "error": "network_error", "error_description": str(e)}

        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            logger.warning(
                f"PKCE exchange rejected by Platform: status={response.status_code} "
                f"error={payload.get('error')} description={payload.get('error_description')}"
            )
            return {
                "ok": False,
                "status": response.status_code,
                "error": payload.get("error", "http_error"),
                "error_description": payload.get("error_description", ""),
            }

        return {"ok": True, "data": response.json()}

    async def refresh_token(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        """刷新 token"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret
                    },
                    headers={"Accept": "application/json"}
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to refresh token: {e}")
            return None

    async def get_userinfo(self, access_token: str) -> Optional[Dict[str, Any]]:
        """获取用户信息"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    self.userinfo_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json"
                    }
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to get userinfo: {e}")
            return None

    @staticmethod
    def _b64_decode_as_int(s: str) -> int:
        """urlsafe base64 解码为整数，自动补齐 padding"""
        from base64 import urlsafe_b64decode
        padding = 4 - len(s) % 4
        if padding == 4:
            padding = 0
        return int.from_bytes(urlsafe_b64decode(s + "=" * padding), "big")

    async def _fetch_jwks(self) -> Optional[Dict[str, Any]]:
        """获取 JWKS（带缓存，TTL 300 秒）"""
        now = datetime.utcnow()
        if (OAuthService._jwks_cache is not None
                and OAuthService._jwks_cache_time is not None
                and (now - OAuthService._jwks_cache_time).total_seconds() < OAuthService._jwks_cache_ttl):
            return OAuthService._jwks_cache

        try:
            async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
                response = await client.get(self.jwks_url)
                response.raise_for_status()
                OAuthService._jwks_cache = response.json()
                OAuthService._jwks_cache_time = now
                return OAuthService._jwks_cache
        except Exception as e:
            logger.error(f"Failed to fetch JWKS: {e}")
            return None

    async def verify_platform_jwt(self, token: str) -> Optional[Dict[str, Any]]:
        """
        验证 Platform 签发的 JWT（ID Token）
        使用 RS256 + JWKS 公钥验证，无需共享密钥
        """
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        try:
            # 从 JWT header 提取 kid
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")

            # 获取 JWKS
            jwks = await self._fetch_jwks()
            if not jwks:
                return None

            # P2-8 修复：按 kid 匹配密钥，不匹配时直接返回 None（禁止 fallback 到第一个 key）
            key_data = None
            if kid:
                key_data = next((k for k in jwks["keys"] if k.get("kid") == kid), None)
                if not key_data:
                    logger.warning(f"No JWKS key matched kid={kid}, rejecting token")
                    return None
            else:
                key_data = jwks["keys"][0]

            # 解析 RSA 公钥
            n = self._b64_decode_as_int(key_data["n"])
            e = self._b64_decode_as_int(key_data["e"])
            public_key = rsa.RSAPublicNumbers(e, n).public_key()
            public_pem = public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )

            # 验证 JWT（含 issuer 校验，防止跨颁发者攻击）
            payload = jwt.decode(
                token,
                public_pem,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.provider_url,
                options={"verify_exp": True, "verify_aud": True, "verify_iss": True}
            )
            return payload
        except JWTError as e:
            logger.error(f"Failed to verify platform JWT: {e}")
            return None

    def create_local_jwt(self, user_info: Dict[str, Any]) -> str:
        """
        根据 Platform 返回的用户信息创建本地 JWT
        用于后续 API 认证

        自动写入 `jwt_version`（当前用户吊销版本号），与
        `verify_local_jwt` 的校验配套 —— 登出后旧 token 立即失效。
        """
        user_id = user_info.get("sub")
        payload = {
            "sub": str(user_id) if user_id else "",
            "user_id": user_id,  # get_current_user 依赖此 key
            "username": user_info.get("username") or user_info.get("name"),
            "email": user_info.get("email"),
            # platform_user_id 已移除：外部身份绑定改由 UserLink 表维护（见 models.database.UserLink）
            "auth_method": user_info.get("auth_method", "local"),
            "iat": datetime.utcnow(),
            "exp": datetime.utcnow() + timedelta(hours=settings.JWT_EXPIRE_HOURS)
        }
        from utils.auth import attach_jwt_version
        attach_jwt_version(payload)
        return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

    def verify_local_jwt(self, token: str) -> Optional[Dict[str, Any]]:
        """验证由 create_local_jwt 签发的本地 JWT（含登出吊销校验）"""
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        except Exception:
            return None
        from utils.auth import is_token_revoked
        if is_token_revoked(payload):
            return None
        return payload


# 全局服务实例
oauth_service = OAuthService()