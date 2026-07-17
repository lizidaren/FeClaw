"""
FeClaw 首次启动配置向导 — 路由

- GET  /setup                       → 渲染 setup.html（仅 admin）
- GET  /setup/api/providers          → 获取 provider 列表 + 当前 key 状态（仅 admin）
- POST /setup/api-keys               → 保存 LLM API keys（仅 admin）
- POST /setup/storage                → 保存存储 / 数据库配置（仅 admin）
- POST /setup/verify                 → 测试当前配置（仅 admin）
- POST /setup/complete               → 标记 SETUP_COMPLETE=true（仅 admin）
- POST /setup/admin                  → 修改管理员邮箱 / 密码（仅 admin）

`/setup` 的页面 GET 重定向到 `routers/feclaw_domain.setup_page`（保留同源路径），
但本模块也直接导出一个同名的 HTML 路由作为兜底。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from config import settings
from models.database import get_db, User
from services.setup_service import (
    get_current_admin,
    get_provider_list,
    update_env,
    verify_config,
    verify_provider,
)
from utils.auth_dependencies import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["Setup"])


# ───────────────────────────────────────────────────────────
# Pydantic models
# ───────────────────────────────────────────────────────────

class APIKeysPayload(BaseModel):
    """API key 表单提交。空字符串视为"保持现状"。"""
    keys: Dict[str, str] = Field(default_factory=dict)


class StoragePayload(BaseModel):
    storage_mode: str = "auto"             # auto | cos | local
    database_url: str = ""                  # 可选：留空表示不修改
    local_storage_root: str = "./feclaw-storage"


class AdminPayload(BaseModel):
    email: str = ""                         # 可选
    password: str = ""                      # 可选；为空表示不修改


# ───────────────────────────────────────────────────────────
# API 路由（仅 admin 可访问）
# ───────────────────────────────────────────────────────────

@router.get("/api/providers")
async def api_providers(
    user: User = Depends(get_admin_user),
):
    """返回 provider 列表 + 当前 .env 中各 key 的设置状态。"""
    return get_provider_list()


@router.post("/api-keys")
async def save_api_keys(
    payload: APIKeysPayload,
    user: User = Depends(get_admin_user),
):
    """保存用户填写的 LLM API key 到 .env。

    跳过空值（不覆盖已有）。"""
    updates: Dict[str, str] = {}
    for k, v in (payload.keys or {}).items():
        if v and v.strip():
            updates[k.strip()] = v.strip()
    if updates:
        update_env(updates)
        logger.info(f"[Setup] admin={user.username} 更新了 {len(updates)} 个 API key")
    return {"status": "ok", "updated": list(updates.keys())}


@router.post("/storage")
async def save_storage(
    payload: StoragePayload,
    user: User = Depends(get_admin_user),
):
    """保存存储模式 + 数据库配置。"""
    updates: Dict[str, str] = {}
    if payload.storage_mode:
        updates["STORAGE_MODE"] = payload.storage_mode
    if payload.local_storage_root:
        updates["LOCAL_STORAGE_ROOT"] = payload.local_storage_root
    if payload.database_url:
        updates["DATABASE_URL"] = payload.database_url
    if updates:
        update_env(updates)
        logger.info(f"[Setup] admin={user.username} 更新了存储配置: {list(updates.keys())}")
    return {"status": "ok", "updated": list(updates.keys())}


@router.post("/verify")
async def verify(
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """测试当前配置是否可用。"""
    return await verify_config(db=db)


@router.post("/verify/{provider_id}")
async def verify_one(
    provider_id: str,
    user: User = Depends(get_admin_user),
):
    """测试某个 provider 的 API key。"""
    return await verify_provider(provider_id)


@router.post("/complete")
async def complete(
    user: User = Depends(get_admin_user),
):
    """标记 SETUP_COMPLETE=true。前端在 Step 4 调一次。"""
    update_env({"SETUP_COMPLETE": "true"})
    return {"status": "ok", "setup_complete": True}


@router.post("/admin")
async def update_admin_info(
    payload: AdminPayload,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """更新管理员邮箱 / 密码。"""
    from utils.auth import hash_password

    admin = db.query(User).filter(User.username == "admin").first()
    if not admin:
        raise HTTPException(status_code=404, detail="admin 用户不存在")

    if payload.email and payload.email.strip():
        admin.email = payload.email.strip()
    if payload.password and payload.password.strip():
        if len(payload.password) < 6:
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        admin.password_hash = hash_password(payload.password)
        admin.salt = None
        admin.password_version = 2

    db.commit()
    db.refresh(admin)
    return {"status": "ok", "username": admin.username, "email": admin.email}


@router.get("/api/state")
async def state(
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """前端 Step 1 进入时拉取一次：用于预填邮箱 + 判断是否仍需 setup。"""
    return {
        "admin": get_current_admin(db),
        "providers": get_provider_list(),
    }
