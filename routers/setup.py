"""
FeClaw 配置向导 — 路由

冷启动模式（SETUP_COMPLETE != true）：
- 所有 setup API 由 ?token=<SETUP_TOKEN> 鉴权，无需登录
- 新增 /setup/database（测试连接）、/setup/admin（建表 + 建 admin）
- /setup 页面 GET 由本模块直接渲染，鉴权 token 走查询参数

正常启动模式（SETUP_COMPLETE == true）：
- 所有 setup API 仍挂载（供已登录 admin 在管理后台调整配置）
- 鉴权降级为 get_admin_user（admin JWT）

API 列表：
- GET  /setup                       → 渲染 setup.html（冷启动时 ?token=xxx）
- GET  /setup/api/state             → 当前状态（admin 鉴权）
- GET  /setup/api/providers         → provider 列表（admin 鉴权）
- POST /setup/database              → 测试 DB 连接（token 鉴权）
- POST /setup/admin                 → 初始化 DB + 建 admin（token 鉴权）
- POST /setup/api-keys              → 保存 LLM API keys（admin 鉴权）
- POST /setup/storage               → 保存存储 / 数据库配置（admin 鉴权）
- POST /setup/verify                → 测试当前配置（admin 鉴权）
- POST /setup/verify/{provider_id}  → 测试单个 provider（admin 鉴权）
- POST /setup/complete              → 标记 SETUP_COMPLETE=true（admin 鉴权）
- POST /setup/admin-update          → 修改管理员邮箱 / 密码（admin 鉴权）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import settings
from models.database import get_db, User
from services.setup_service import (
    build_database_url,
    get_current_admin,
    get_partial_config,
    get_provider_list,
    init_database,
    is_setup_complete,
    test_db_connection,
    update_env,
    verify_config,
    verify_provider,
)
from utils.auth_dependencies import get_admin_user


async def _get_setup_auth(
    request: Request,
    token: str = Query("", alias="token"),
):
    """冷启动用 token 鉴权，正常模式用 JWT 管理员鉴权。"""
    if _is_cold_start():
        # 冷启动：验证 setup token
        expected = (settings.SETUP_TOKEN or "").strip()
        if not expected or not token or token.strip() != expected:
            from fastapi.responses import HTMLResponse
            return HTMLResponse(INVALID_TOKEN_HTML, status_code=403)
        return None  # 无 user 对象
    else:
        # 正常模式：走 JWT 管理员鉴权
        return await get_admin_user(request)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["Setup"])


# ───────────────────────────────────────────────────────────
# 鉴权：冷启动 token OR admin JWT
# ───────────────────────────────────────────────────────────

def _is_cold_start() -> bool:
    """冷启动 = SETUP_COMPLETE != true。"""
    if settings.SETUP_COMPLETE is True:
        return False
    # pydantic_settings 默认会把 "true"/"false" 转成 bool；兜底校验字符串
    return not bool(settings.SETUP_COMPLETE)


def verify_setup_token(token: str = Query("", alias="token")) -> bool:
    """冷启动鉴权：URL ?token=<SETUP_TOKEN>。

    非冷启动时（SETUP_COMPLETE=true）此依赖直接放行 —— 由后续 get_admin_user 接管。
    """
    if not _is_cold_start():
        return True
    expected = (settings.SETUP_TOKEN or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="冷启动 token 未生成，请重启后端服务",
        )
    if not token or token.strip() != expected:
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing setup token",
        )
    return True


# ───────────────────────────────────────────────────────────
# Pydantic models
# ───────────────────────────────────────────────────────────

class APIKeysPayload(BaseModel):
    """API key 表单提交。空字符串视为"保持现状"。"""
    keys: Dict[str, str] = Field(default_factory=dict)


class StoragePayload(BaseModel):
    """Step 5 提交：文件存储 + 向量搜索后端配置。"""
    storage_mode: str = "auto"
    database_url: str = ""
    local_storage_root: str = "./feclaw-storage"
    tencent_cos_secret_id: str = ""
    tencent_cos_secret_key: str = ""
    tencent_cos_bucket: str = ""
    vector_storage_backend: str = ""


class AdminPayload(BaseModel):
    """旧版 admin 提交（保留向后兼容以防被其他代码引用）。"""
    email: str = ""
    password: str = ""


class AdminUpdatePayload(BaseModel):
    """正常启动：管理员邮箱 / 密码修改。"""
    email: str = ""
    password: str = ""


class DatabasePayload(BaseModel):
    """Step 1 冷启动：MySQL 配置。"""
    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "FeClaw"


class AdminWithDbPayload(BaseModel):
    """Step 2 提交：admin 账号 + db 配置（前端从 Step 1 缓存）。"""
    # DB
    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "FeClaw"
    # admin
    admin_username: str = "admin"
    admin_password: str = ""


class CompletePayload(BaseModel):
    """最后一步：用户为每个能力选择的模型 + 搜索后端。"""
    feclaw_domain: str = ""
    cookie_secure: bool = False
    default_llm_model: str = ""
    default_vision_model: str = ""
    default_embedding_model: str = ""
    default_search_engine: str = ""


# ───────────────────────────────────────────────────────────
# 页面路由（仅冷启动时挂载 token 校验；正常启动时仍可访问）
# ───────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    token: str = Query("", alias="token"),
    reset: str = Query("", alias="reset"),
    db: Session = Depends(get_db),
):
    """配置向导页面。

    冷启动：URL 必须带 ?token=xxx（由 main.py 启动时打印到控制台）
    正常启动：默认渲染只读摘要；?reset=1 强制重走向导
    """
    return _render_setup_view(
        request=request,
        db=db,
        token=token,
        reset=reset,
    )


INVALID_TOKEN_HTML = """<!DOCTYPE html><html lang=zh-CN><head><meta charset=utf-8><title>无效令牌</title><style>body{background:#050510;color:#e0e0e0;display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:sans-serif;text-align:center;margin:0}h1{color:#f87171;font-size:2em}p{color:#888;margin-top:12px}.hint{color:#666;font-size:0.9em;margin-top:8px}a{color:#667eea}</style></head><body><div><h1>🔒 无效的配置令牌</h1><p>请使用控制台打印的完整 URL 访问</p><p class=hint>首次启动时管理地址会打印在终端中</p></div></body></html>"""


def _get_templates():
    """获取 Jinja2Templates 实例（带 FeClaw 模板目录）。"""
    from fastapi.templating import Jinja2Templates
    import os
    return Jinja2Templates(directory=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates",
    ))


def _mask_db_url(url: str) -> str:
    """脱敏显示数据库 URL（隐藏密码）。"""
    if not url:
        return "(未配置)"
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        if parsed.password:
            netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
            masked = urlunparse(parsed._replace(netloc=netloc))
            return masked
        return url
    except Exception:
        # 退化：粗暴地把 password=xxx 替换
        import re
        return re.sub(r"(password=)[^&;]+", r"\1***", url)


def _build_summary_data() -> Dict[str, Any]:
    """构建 setup 配置摘要（不依赖 DB）。"""
    partial = get_partial_config()
    env = partial
    api_keys = env.get("api_keys_present", {}) or {}

    # 已配置的 LLM Provider（id → name）
    from services.setup_service import PROVIDER_LIST
    providers_set = []
    for p in PROVIDER_LIST:
        key_name = p["api_key_name"]
        if api_keys.get(key_name):
            providers_set.append({"id": p["id"], "name": p["name"], "api_key_name": key_name})

    # 部署模式
    if settings.FECLAW_SUBDOMAIN_ENABLED and settings.FECLAW_PUBLIC_URL:
        deployment_mode = "subdomain"
    else:
        deployment_mode = "single-site"

    # OAuth 状态
    oauth_enabled = bool(settings.OAUTH_ENABLED and (settings.OAUTH_PROVIDER_URL or "").strip())

    # 存储模式（解析实际生效的）
    storage_mode = (settings.STORAGE_MODE or "auto").lower()
    cos_configured = all([
        settings.TENCENT_COS_SECRET_ID,
        settings.TENCENT_COS_SECRET_KEY,
        settings.TENCENT_COS_BUCKET,
    ])
    storage_active = storage_mode
    if storage_mode == "auto":
        storage_active = "cos" if cos_configured else "local"

    return {
        "setup_complete": True,
        "deployment_mode": deployment_mode,
        "feclaw_domain": settings.FECLAW_PUBLIC_URL or "(未设置)",
        "cookie_secure": bool(settings.COOKIE_SECURE),
        "providers_set": providers_set,
        "providers_total": len(PROVIDER_LIST),
        "llm_model": settings.MAIN_TEXT_MODEL or settings.AGENT_LLM_MODEL or "(未设置)",
        "vision_model": settings.MAIN_VISION_MODEL or "(未设置)",
        "embedding_model": settings.MAIN_EMBEDDING_MODEL or "(未设置)",
        "search_engine": settings.DEFAULT_SEARCH_ENGINE or "qwen",
        "storage_mode": storage_mode,
        "storage_active": storage_active,
        "local_storage_root": settings.LOCAL_STORAGE_ROOT or "./feclaw-storage",
        "cos_bucket": settings.TENCENT_COS_BUCKET or "(未配置)",
        "cos_configured": cos_configured,
        "database_url_masked": _mask_db_url(settings.DATABASE_URL or ""),
        "vector_storage_backend": settings.VECTOR_STORAGE_BACKEND or "cos",
        "oauth_enabled": oauth_enabled,
        "oauth_provider_name": settings.OAUTH_PROVIDER_NAME,
        "oauth_provider_url": settings.OAUTH_PROVIDER_URL or "",
    }


def _render_setup_page(request: Request, token: str = "", setup_done: bool = False) -> HTMLResponse:
    """实际渲染 setup.html（向导或摘要）。"""
    templates = _get_templates()
    resp = templates.TemplateResponse(
        request,
        "setup.html",
        {
            "request": request,
            "setup_token": token,
            "cold_start": _is_cold_start(),
            "setup_done": setup_done,
        },
    )
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def _render_setup_view(
    request: Request,
    db: Optional[Session] = None,
    token: str = "",
    reset: str = "",
) -> HTMLResponse:
    """根据 setup 状态选择渲染 wizard 或 summary。

    冷启动：总是 wizard
    ?reset=1：强制重走 wizard
    已完成：渲染只读 summary
    未完成：渲染 wizard
    """
    cold_start = _is_cold_start()
    force_wizard = reset.strip().lower() in ("1", "true", "yes")

    if cold_start:
        # 冷启动：token 鉴权由 setup_page 调用方前置完成；这里只渲染
        return _render_setup_page(request, token=token, setup_done=False)

    if force_wizard:
        return _render_setup_page(request, token="", setup_done=False)

    setup_done = False
    try:
        if db is not None:
            setup_done = is_setup_complete(db)
    except Exception as e:
        logger.warning(f"[Setup] is_setup_complete 检测失败: {e}")

    if setup_done:
        return _render_setup_page(request, token="", setup_done=True)

    return _render_setup_page(request, token="", setup_done=False)


# ───────────────────────────────────────────────────────────
# 冷启动专用 API
# ───────────────────────────────────────────────────────────

@router.post("/database")
async def setup_database(
    payload: DatabasePayload,
    _: bool = Depends(verify_setup_token),
):
    """Step 1：测试 MySQL 连接。不写 .env，只返回测试结果。"""
    ok, msg = test_db_connection(
        host=payload.host,
        port=payload.port,
        user=payload.user,
        password=payload.password,
        database=payload.database,
    )
    if not ok:
        return {"status": "error", "message": msg}
    return {
        "status": "ok",
        "message": msg,
        "database_url": build_database_url(
            payload.host, payload.port, payload.user, payload.password, payload.database
        ),
    }


@router.post("/admin")
async def setup_admin(
    payload: AdminWithDbPayload,
    _: bool = Depends(verify_setup_token),
):
    """Step 2：连接 DB + 建表 + 建 admin（一次性完成）。

    前端从 Step 1 缓存 DB 配置 + admin 表单 → 一次性提交。
    """
    username = (payload.admin_username or "").strip() or "admin"
    password = payload.admin_password or ""
    if len(password) < 8:
        return {"status": "error", "message": "密码至少 8 位"}

    db_url = build_database_url(
        payload.host, payload.port, payload.user, payload.password, payload.database
    )
    ok, msg = init_database(
        db_url=db_url,
        admin_username=username,
        admin_password=password,
    )
    if not ok:
        return {"status": "error", "message": msg}
    # init_database 已写 DATABASE_URL + JWT_SECRET 到 .env
    # 同时清掉 SETUP_TOKEN（冷启动结束后 token 失效）
    update_env({"SETUP_TOKEN": ""})
    return {"status": "ok", "message": msg}


# ───────────────────────────────────────────────────────────
# 正常启动 API（admin JWT 鉴权）
# ───────────────────────────────────────────────────────────

@router.get("/api/providers")
async def api_providers(
    _setup_user = Depends(_get_setup_auth),
):
    """返回 provider 列表 + 当前 .env 中各 key 的设置状态。"""
    return get_provider_list()


@router.post("/api-keys")
async def save_api_keys(
    payload: APIKeysPayload,
    _setup_user = Depends(_get_setup_auth),
):
    """保存用户填写的 LLM API key 到 .env。空值不覆盖。"""
    updates: Dict[str, str] = {}
    for k, v in (payload.keys or {}).items():
        if v and v.strip():
            updates[k.strip()] = v.strip()
    if updates:
        update_env(updates)
        logger.info(f"[Setup] admin={(_setup_user.username if _setup_user else "admin")} 更新了 {len(updates)} 个 API key")
    return {"status": "ok", "updated": list(updates.keys())}


@router.post("/storage")
async def save_storage(
    payload: StoragePayload,
    _setup_user = Depends(_get_setup_auth),
):
    """保存存储模式 + 数据库 + COS 凭证 + 向量后端配置。"""
    updates: Dict[str, str] = {}
    if payload.storage_mode:
        updates["STORAGE_MODE"] = payload.storage_mode
    if payload.local_storage_root:
        updates["LOCAL_STORAGE_ROOT"] = payload.local_storage_root
    if payload.database_url:
        updates["DATABASE_URL"] = payload.database_url
    if payload.tencent_cos_secret_id:
        updates["TENCENT_COS_SECRET_ID"] = payload.tencent_cos_secret_id.strip()
    if payload.tencent_cos_secret_key:
        updates["TENCENT_COS_SECRET_KEY"] = payload.tencent_cos_secret_key.strip()
    if payload.tencent_cos_bucket:
        updates["TENCENT_COS_BUCKET"] = payload.tencent_cos_bucket.strip()
    if payload.vector_storage_backend:
        updates["VECTOR_STORAGE_BACKEND"] = payload.vector_storage_backend.strip().lower()
    if updates:
        update_env(updates)
        logger.info(f"[Setup] admin={(_setup_user.username if _setup_user else "admin")} 更新了存储配置: {list(updates.keys())}")
    return {"status": "ok", "updated": list(updates.keys())}


@router.post("/verify")
async def verify(
    _setup_user = Depends(_get_setup_auth),
    
    db: Session = Depends(get_db),
):
    """测试当前配置是否可用。"""
    return await verify_config(db=db)


@router.post("/verify/{provider_id}")
async def verify_one(
    provider_id: str,
    _setup_user = Depends(_get_setup_auth),
):
    """测试某个 provider 的 API key。"""
    return await verify_provider(provider_id)


@router.post("/complete")
async def complete(
    payload: CompletePayload = CompletePayload(),
    _setup_user = Depends(_get_setup_auth),
):
    """标记 SETUP_COMPLETE=true，同时把模型选择写入 .env。"""
    updates: Dict[str, str] = {"SETUP_COMPLETE": "true"}
    if payload.feclaw_domain:
        updates["FECLAW_PUBLIC_URL"] = payload.feclaw_domain.strip()
    updates["COOKIE_SECURE"] = "true" if payload.cookie_secure else "false"
    if payload.default_llm_model:
        updates["MAIN_TEXT_MODEL"] = payload.default_llm_model.strip()
    if payload.default_vision_model:
        updates["MAIN_VISION_MODEL"] = payload.default_vision_model.strip()
    if payload.default_embedding_model:
        updates["MAIN_EMBEDDING_MODEL"] = payload.default_embedding_model.strip()
    if payload.default_search_engine:
        updates["DEFAULT_SEARCH_ENGINE"] = payload.default_search_engine.strip().lower()
    update_env(updates)
    logger.info(
        f"[Setup] admin={(_setup_user.username if _setup_user else "admin")} 完成配置: "
        f"llm={updates.get('MAIN_TEXT_MODEL')!r}, "
        f"vision={updates.get('MAIN_VISION_MODEL')!r}, "
        f"embedding={updates.get('MAIN_EMBEDDING_MODEL')!r}, "
        f"search={updates.get('DEFAULT_SEARCH_ENGINE')!r}"
    )
    return {
        "status": "ok",
        "setup_complete": True,
        "saved": {k: v for k, v in updates.items() if k != "SETUP_COMPLETE"},
        "message": "配置完成，请重启后端服务",
    }


@router.post("/admin-update")
async def update_admin_info(
    payload: AdminUpdatePayload,
    _setup_user = Depends(_get_setup_auth),
    
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
    _setup_user = Depends(_get_setup_auth),
    
    db: Session = Depends(get_db),
):
    """前端 Step 1 进入时拉取一次：用于预填邮箱 + 判断是否仍需 setup。"""
    partial = get_partial_config()
    return {
        "admin": get_current_admin(db),
        "providers": get_provider_list(),
        "storage": {
            "storage_mode": partial.get("storage_mode", "auto"),
            "vector_storage_backend": settings.VECTOR_STORAGE_BACKEND or "cos",
            "tencent_cos_bucket": settings.TENCENT_COS_BUCKET or "",
            "tencent_cos_secret_id_set": bool(
                (settings.TENCENT_COS_SECRET_ID or "").strip()
            ),
            "tencent_cos_secret_key_set": bool(
                (settings.TENCENT_COS_SECRET_KEY or "").strip()
            ),
        },
    }


@router.get("/api/summary")
async def api_summary(
    request: Request,
    db: Session = Depends(get_db),
):
    """只读 setup 配置摘要（无需鉴权，仅展示非敏感信息）。

    用于：
    1. /setup 页面在 setup_done 状态下渲染摘要视图
    2. 管理后台 widget（未来扩展）
    """
    summary = _build_summary_data()
    summary["setup_complete_flag"] = bool(settings.SETUP_COMPLETE)
    try:
        if db is not None:
            summary["setup_complete"] = is_setup_complete(db)
            summary["admin"] = get_current_admin(db)
        else:
            summary["admin"] = None
    except Exception:
        summary["admin"] = None
    return summary
