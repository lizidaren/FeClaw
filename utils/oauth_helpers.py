"""
OAuth 辅助工具

集中 FeClaw 后端的 OAuth token 签发 / 用户匹配逻辑，供以下场景复用：
- `routers/oauth.py`  —— Web 回调、Mobile `/exchange`、`/refresh`、`/mobile-login`
- `routers/desktop_api.py` —— Desktop client `auth_exchange`

依赖：
- `config.settings` 提供 `JWT_SECRET` / `JWT_ALGORITHM` / `JWT_EXPIRE_HOURS`
- `utils.auth.hash_password` 兼容 legacy SHA-256 + bcrypt
- `models.database.User` 含 `platform_user_id` 字段（OAuth 维度唯一键）

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
from models.database import User

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
    return _encode_token(payload), expire_hours * 3600


def decode_refresh_token(token: str) -> Optional[int]:
    """
    解码 refresh token，返回 user_id。

    校验失败（签名错 / 过期 / type 不对） → 返回 None。
    调用方应自行决定是否 raise 401。
    """
    payload = _decode_token(token)
    if not payload:
        return None
    if payload.get("type") != "refresh":
        logger.warning("[oauth_helpers] token type != 'refresh'")
        return None
    raw = payload.get("sub") or payload.get("user_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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

    匹配优先级（与原 oauth_callback 一致，避免回归）：
    1. platform_user_id 精确匹配 → 复用 + 更新 email/is_admin
    2. username 匹配且未绑 Platform → 绑 Platform
    3. username 匹配但已绑别的 Platform → 强制创建独立账号 `{username}_{platform_user_id}`
    4. 全新 → 创建

    副作用：commit + refresh；调用方不要再 commit 同一行。
    """
    # 1. platform_user_id 精确匹配
    existing = (
        db.query(User).filter(User.platform_user_id == platform_user_id).first()
    )
    if existing:
        if email and email != existing.email:
            existing.email = email
        existing.is_admin = bool(is_admin) or existing.username == "admin"
        db.commit()
        db.refresh(existing)
        logger.info(f"[oauth_helpers] updated existing user platform_user_id={platform_user_id}")
        return existing

    # 2. username 匹配
    by_username = db.query(User).filter(User.username == username).first()
    if by_username and by_username.platform_user_id is None:
        by_username.platform_user_id = platform_user_id
        if email:
            by_username.email = email
        by_username.is_admin = bool(is_admin) or username == "admin"
        db.commit()
        db.refresh(by_username)
        logger.info(f"[oauth_helpers] linked local user {username} -> platform_user_id={platform_user_id}")
        return by_username

    if by_username and by_username.platform_user_id != platform_user_id:
        # 3. username 撞库 → 强制独立账号，避免账户劫持
        logger.warning(
            f"[oauth_helpers] username collision: {username} owned by "
            f"platform_user_id={by_username.platform_user_id}, "
            f"incoming platform_user_id={platform_user_id}"
        )
        new_user = User(
            username=f"{username}_{platform_user_id}",
            platform_user_id=platform_user_id,
            password_hash=_dummy_bcrypt_hash(),
            salt=None,
            password_version=2,
            is_admin=False,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        return new_user

    # 4. 全新
    new_user = User(
        username=username,
        platform_user_id=platform_user_id,
        password_hash=_dummy_bcrypt_hash(),
        salt=None,
        password_version=2,
        is_admin=bool(is_admin) or username == "admin",
    )
    if email:
        new_user.email = email
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    logger.info(f"[oauth_helpers] created new user {username} (platform_user_id={platform_user_id})")
    return new_user


# ────────────────────────────────────────────────────────────
# 一站式：platform user_info → FeClaw access + refresh
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
    is_admin = bool(user_info.get("is_admin", False))

    user = find_or_create_user_from_platform(
        db,
        platform_user_id=platform_user_id,
        username=username,
        email=email,
        is_admin=is_admin,
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