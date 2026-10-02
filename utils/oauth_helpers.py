"""
OAuth 辅助工具

集中 FeClaw 后端的 OAuth token 签发 / 用户匹配逻辑，供以下场景复用：
- `routers/oauth.py`  -- Web 回调、Mobile `/exchange`、`/refresh`、`/mobile-login`
- `routers/desktop_api.py` -- Desktop client `auth_exchange`

依赖：
- `config.settings` 提供 `JWT_SECRET` / `JWT_ALGORITHM` / `JWT_EXPIRE_HOURS`
- `utils.auth.hash_password` 兼容 legacy SHA-256 + bcrypt
- `models.database.UserLink` 记录 (provider, provider_user_id) 外部身份绑定（取代 User.platform_user_id 单字段）

签名设计：
- access_token：HS256，`sub=user_id`，`type="access"`，过期 = JWT_EXPIRE_HOURS
- refresh_token：HS256，`sub=user_id`，`type="refresh"`，过期 = JWT_EXPIRE_HOURS * 4
  （refresh 通常给 30 天；这里复用 settings 系数，保持简洁，可后续独立成 REFRESH_EXPIRE_HOURS）

只 export 同步 / 异步 helper；不做路由注册。
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from config import settings
from models.database import User, UserLink
from utils.auth import (
    attach_jwt_version,
    is_token_revoked,
    user_id_from_payload,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
# JWT 签发 / 解码（access + refresh 通用底层）
# ────────────────────────────────────────────────────────────

_REFRESH_EXPIRE_HOURS_MULTIPLIER = 4  # refresh = 4 × access（默认 28 天 vs 7 天）


def _encode_token(payload: Dict[str, Any]) -> str:
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _decode_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as e:
        logger.debug(f"[oauth_helpers] JWT decode failed: {e}")
        return None


def sign_access_token(
    user_id: int,
    username: Optional[str] = None,
    email: Optional[str] = None,
    auth_method: str = "platform",
) -> Tuple[str, int]:
    """
    签发 FeClaw 短期 access token。

    返回 (token, expires_in_seconds)
    """
    expire_hours = settings.JWT_EXPIRE_HOURS
    now = datetime.utcnow()
    payload = {
        "sub": str(user_id),
        "user_id": user_id,  # 兼容 utils/auth_dependencies._user_id_from_payload
        "username": username,
        "email": email,
        "auth_method": auth_method,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(hours=expire_hours),
    }
    # 登出吊销版本号：缺失会被 is_token_revoked 误判为已吊销，必须写
    attach_jwt_version(payload)
    return _encode_token(payload), expire_hours * 3600


def sign_refresh_token(user_id: int) -> Tuple[str, int]:
    """
    签发 FeClaw 长期 refresh token（HS256，type="refresh"）。

    返回 (token, expires_in_seconds)
    """
    expire_hours = settings.JWT_EXPIRE_HOURS * _REFRESH_EXPIRE_HOURS_MULTIPLIER
    now = datetime.utcnow()
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(hours=expire_hours),
    }
    # 同上：refresh token 也必须携带吊销版本号，登出后不能再换新 access token
    attach_jwt_version(payload)
    return _encode_token(payload), expire_hours * 3600


def decode_refresh_token(token: str) -> Optional[int]:
    """
    解码 refresh token，返回 user_id。

    校验失败（签名错 / 过期 / type 不对 / 已登出吊销） -> 返回 None。
    调用方应自行决定是否 raise 401。
    """
    payload = _decode_token(token)
    if not payload:
        return None
    if payload.get("type") != "refresh":
        logger.warning("[oauth_helpers] token type != 'refresh'")
        return None
    if is_token_revoked(payload):
        logger.info("[oauth_helpers] refresh token 已被登出吊销")
        return None
    return user_id_from_payload(payload)


# ────────────────────────────────────────────────────────────
# 用户匹配 / 创建（抽自 desktop_api + oauth_callback）
# ────────────────────────────────────────────────────────────


def _dummy_bcrypt_hash() -> str:
    """为 OAuth 创建的账户生成不可登录的密码 hash（bcrypt）。"""
    return bcrypt.hashpw(secrets.token_hex(32).encode(), bcrypt.gensalt(rounds=10)).decode()


def find_or_create_user_from_platform(
    db: Session,
    *,
    platform_user_id: str,
    username: str,
    email: Optional[str] = None,
    is_admin: bool = False,
) -> User:
    """
    按 Platform 维度匹配或创建 FeClaw User。
    使用 UserLink 表代替旧的 User.platform_user_id 字段。

    Q20/H16：只按 (provider, provider_user_id) 匹配/关联本地账号 ——
      * 不再按 username 匹配（杜绝「IdP 显示名=admin 预劫持本地 admin 账号」）；
      * `is_admin` 不来自 IdP 声明（参数保留仅为兼容，实际不再使用）——
        新建恒 False，匹配到已有用户时不改其 is_admin。

    副作用：commit + refresh；调用方不要再 commit 同一行。
    """
    # 1. UserLink 精确匹配 —— 唯一复用路径
    existing_link = (
        db.query(UserLink)
        .filter(UserLink.provider == "platform", UserLink.provider_user_id == platform_user_id)
        .first()
    )
    if existing_link:
        user = db.query(User).filter(User.id == existing_link.user_id).first()
        if user:
            if email and email != user.email:
                user.email = email
            # Q20/H16：不覆盖 is_admin（管理员身份只来自本地 DB）
            existing_link.provider_username = username
            db.commit()
            db.refresh(user)
            logger.info(f"[oauth_helpers] updated existing user via UserLink platform_user_id={platform_user_id}")
            return user

    # 2. 全新用户。username 可能已被本地账号占用（如 "admin"），冲突时追加 platform_user_id
    final_username = username
    if db.query(User).filter(User.username == username).first() is not None:
        final_username = f"{username}_{platform_user_id}"
        logger.warning(f"[oauth_helpers] username collision with local account, using {final_username}")

    new_user = User(
        username=final_username,
        password_hash=_dummy_bcrypt_hash(),
        salt=None,
        password_version=2,
        is_admin=False,
    )
    if email:
        new_user.email = email
    db.add(new_user)
    db.flush()
    link = UserLink(
        user_id=new_user.id,
        provider="platform",
        provider_user_id=platform_user_id,
        provider_username=username,
    )
    db.add(link)
    db.commit()
    db.refresh(new_user)
    logger.info(f"[oauth_helpers] created new user {final_username} (platform via UserLink)")
    return new_user


# ────────────────────────────────────────────────────────────
# 一站式：platform user_info -> FeClaw access + refresh
# ────────────────────────────────────────────────────────────


def issue_token_pair_for_platform_user(
    db: Session,
    user_info: Dict[str, Any],
) -> Dict[str, Any]:
    """
    把 Platform 返回的 user_info 字典（至少含 id/username，可选 email/is_admin）
    转换为 FeClaw 侧的 (access, refresh) token pair。

    返回字段：
    {
      "token": <access_token>,
      "refresh_token": <refresh_token>,
      "expires_in": <access expires_in_seconds>,
      "refresh_expires_in": <refresh expires_in_seconds>,
      "user_id": <int>,
      "username": <str>,
      "auth_method": "platform",
    }
    """
    platform_user_id = str(user_info.get("id") or user_info.get("sub") or "")
    if not platform_user_id:
        raise ValueError("user_info missing 'id' / 'sub'")

    username = user_info.get("username") or user_info.get("name") or f"platform_{platform_user_id}"
    email = user_info.get("email")

    # Q20/H16：is_admin 不来自 IdP 声明（不再读 user_info["is_admin"]）
    user = find_or_create_user_from_platform(
        db,
        platform_user_id=platform_user_id,
        username=username,
        email=email,
    )

    access_token, access_expires = sign_access_token(
        user_id=user.id,
        username=user.username,
        email=email or user.email,
        auth_method="platform",
    )
    refresh_token, refresh_expires = sign_refresh_token(user_id=user.id)

    return {
        "token": access_token,
        "refresh_token": refresh_token,
        "expires_in": access_expires,
        "refresh_expires_in": refresh_expires,
        "user_id": user.id,
        "username": user.username,
        "auth_method": "platform",
    }
