"""
FastAPI 认证依赖（统一入口）

本模块只处理**全局 HS256 本地 JWT**（cookie `feclaw_jwt` 或 Bearer header）。
TOTP / 平台 session 等 agent 级 token 由 `routers/feclaw_domain._get_token_from_request`
单独处理，**不要**合并 —— 它们作用域不同、校验方式不同、生命周期不同。

设计要点：
- Token 来源顺序：Authorization Bearer header → `feclaw_jwt` cookie
- JWT 字段兼容：`user_id`（utils.auth.create_jwt_token 签发的）与 `sub`
  （oauth_service.create_local_jwt 签发的）都识别
- **吊销**：`jwt_version` 与库中不一致 ⇒ 401（登出后旧 token 立即失效）
- 4+1 依赖：required/optional × User/int + admin
"""
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from config import settings
from models.database import User, get_db
from utils.auth import decode_jwt_token, is_token_revoked, user_id_from_payload  # 低层原语，保持单点维护


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"status": "unauthorized"},
    headers={"WWW-Authenticate": "Bearer"},
)


def _extract_global_jwt(request: Request) -> Optional[str]:
    """提取全局 HS256 JWT。Bearer header 优先，其次 `feclaw_jwt` cookie。

    注意：**不**读取 TOTP cookie 或 `platform_session` —— 那些是 agent/平台作用域。
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.cookies.get("feclaw_jwt")


def _user_id_from_payload(payload: dict) -> Optional[int]:
    """从 JWT payload 取 user_id。兼容 `user_id` 与 `sub` 两种字段。

    委托给 `utils.auth.user_id_from_payload` —— 与吊销校验共用同一份解析逻辑。
    """
    return user_id_from_payload(payload)


def _decode_or_none(token: str, db: Optional[Session] = None) -> Optional[dict]:
    """解 JWT + 吊销校验；失败返回 None 而不抛异常（让调用方决定 raise/return None）。

    这是全局 HS256 JWT 的**唯一校验入口** —— 登出吊销（`jwt_version`）在这里生效，
    因此所有 `get_current_user*` 依赖自动获得「登出后旧 token 立即失效」。
    """
    payload = decode_jwt_token(token)
    if payload is None:
        return None
    if is_token_revoked(payload, db=db):
        return None
    return payload


# ────────────────────────────────────────────────────────────────────
# 公开依赖
# ────────────────────────────────────────────────────────────────────

async def get_current_user_id(
    request: Request,
    db: Session = Depends(get_db),
) -> int:
    """必须登录；只返回 user_id。"""
    token = _extract_global_jwt(request)
    if not token:
        raise _UNAUTHORIZED
    payload = _decode_or_none(token, db=db)
    if not payload:
        raise _UNAUTHORIZED
    user_id = _user_id_from_payload(payload)
    if user_id is None:
        raise _UNAUTHORIZED
    return user_id


async def get_current_user_id_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[int]:
    """可选登录；未登录返回 None。"""
    token = _extract_global_jwt(request)
    if not token:
        return None
    payload = _decode_or_none(token, db=db)
    if not payload:
        return None
    return _user_id_from_payload(payload)


async def get_current_token_payload(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """返回完整 JWT payload（用于需要 username/email/auth_method 等自定义字段的场景）。

    抛 401 同 `get_current_user_id`。
    """
    token = _extract_global_jwt(request)
    if not token:
        raise _UNAUTHORIZED
    payload = _decode_or_none(token, db=db)
    if not payload:
        raise _UNAUTHORIZED
    return payload


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """必须登录；返回完整 User 对象（含 is_admin 等字段）。"""
    token = _extract_global_jwt(request)
    if not token:
        raise _UNAUTHORIZED
    payload = _decode_or_none(token, db=db)
    if not payload:
        raise _UNAUTHORIZED
    user_id = _user_id_from_payload(payload)
    if user_id is None:
        raise _UNAUTHORIZED
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise _UNAUTHORIZED
    return user


async def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    """可选登录；未登录返回 None。"""
    token = _extract_global_jwt(request)
    if not token:
        return None
    payload = _decode_or_none(token, db=db)
    if not payload:
        return None
    user_id = _user_id_from_payload(payload)
    if user_id is None:
        return None
    return db.query(User).filter(User.id == user_id).first()


async def get_admin_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """必须登录 + 必须管理员。"""
    user = await get_current_user(request, db)
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"status": "forbidden", "message": "需要管理员权限"},
        )
    return user