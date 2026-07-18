"""
管理后台 — 配置 + 统计 (admin-only)

路由：
- GET  /admin                  → 管理员后台首页（重定向到 /admin/settings）
- GET  /admin/settings         → 渲染 admin_settings.html
- GET  /admin/config           → 读取当前 .env 配置（仅 is_admin）
- POST /admin/config           → 更新 .env 配置（sectioned, 仅 is_admin）
- POST /admin/config/test/{provider_id} → 测试 provider API key（仅 is_admin）
- GET  /admin/stats            → 系统统计数据（仅 is_admin）

复用 services/setup_service 的 .env 读写、provider 列表与 verify。
注意：本模块使用 /admin/* 前缀，与 routers/admin.py 的 /api/admin/* 不冲突。
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import settings
from models.database import (
    AgentProfile,
    AgentUsageLog,
    ChatHistory,
    ConversationSession,
    SessionLocal,
    User,
    get_db,
)
from services.setup_service import (
    PROVIDER_LIST,
    _parse_env_file,
    ENV_FILE,
    get_provider_list,
    update_env,
    verify_provider as svc_verify_provider,
)
from utils.auth_dependencies import get_admin_user

logger = logging.getLogger(__name__)

FORBIDDEN_PAGE_HTML = """<!DOCTYPE html><html lang=zh-CN><head><meta charset=utf-8><title>权限不足</title><style>body{background:#050510;color:#e0e0e0;display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:sans-serif;text-align:center;margin:0}h1{font-size:3em;margin:0}h2{color:#f87171;margin:8px 0 0}p{color:#888;margin-top:20px}a{color:#667eea;text-decoration:none}.btn{display:inline-block;margin-top:24px;padding:10px 24px;background:#667eea;color:#fff;border-radius:8px;text-decoration:none}</style></head><body><div><div style="font-size:64px;margin-bottom:16px">🔒</div><h1>403</h1><h2>需要管理员权限</h2><p>你的账户没有访问管理后台的权限。</p><a href="/dashboard" class="btn">返回控制台</a></div></body></html>"""

router = APIRouter(prefix="/admin", tags=["Admin Panel"])


# ───────────────────────────────────────────────────────────
# 页面
# ───────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_index(_admin: User = Depends(get_admin_user)):
    """管理后台首页 → 重定向到 /admin/settings"""
    return RedirectResponse(url="/admin/settings", status_code=302)


@router.get("/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request, _admin: User = Depends(get_admin_user)):
    """渲染 admin_settings.html。前端通过 JS fetch /admin/config 和 /admin/stats 加载数据。"""
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(
        directory=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "templates",
        )
    )
    resp = templates.TemplateResponse(
        request,
        "admin_settings.html",
        {"request": request, "current_user": _admin},
    )
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ───────────────────────────────────────────────────────────
# 辅助
# ───────────────────────────────────────────────────────────

def _mask_key(value: str) -> str:
    """显示 API key：保留前 2 后 4，中间用 • 代替。空值返回空串。"""
    if not value:
        return ""
    v = value.strip()
    if len(v) <= 6:
        return "•" * len(v)
    return f"{v[:2]}{'•' * max(4, len(v) - 6)}{v[-4:]}"


def _read_env() -> Dict[str, str]:
    """读取 .env 原始 dict（用于 GET /admin/config）。"""
    return _parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}


# ───────────────────────────────────────────────────────────
# GET /admin/config
# ───────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(_admin: User = Depends(get_admin_user)):
    """返回当前 .env 中所有配置项 + 运行时 settings 合并值。

    敏感字段（API keys、SECRET 类）只返回是否存在标记 + 占位显示。
    """
    env = _read_env()

    # 1. 部署模式（推断）
    feclaw_domain = (env.get("FECLAW_DOMAIN", "") or settings.FECLAW_DOMAIN or "").strip()
    # 推断：单站点 = FECLAW_DOMAIN 为空
    deploy_mode = "subdomain" if feclaw_domain else "single"

    # 2. Cookie secure：pydantic 把 "true"/"false" 解析为 bool，env 中可能仍是字符串
    cookie_secure_raw = env.get("COOKIE_SECURE", "")
    if cookie_secure_raw == "":
        cookie_secure = bool(settings.COOKIE_SECURE) if isinstance(settings.COOKIE_SECURE, bool) else False
    else:
        cookie_secure = cookie_secure_raw.strip().lower() in ("true", "1", "yes")

    # 3. 数据库 URL（只读展示，可脱敏但保留 host/port/db）
    db_url = env.get("DATABASE_URL", "") or settings.DATABASE_URL or ""

    # 4. 存储
    storage_mode = (env.get("STORAGE_MODE", "") or settings.STORAGE_MODE or "auto").strip()
    local_storage_root = (env.get("LOCAL_STORAGE_ROOT", "") or settings.LOCAL_STORAGE_ROOT or "./feclaw-storage").strip()
    vector_backend = (env.get("VECTOR_STORAGE_BACKEND", "") or settings.VECTOR_STORAGE_BACKEND or "cos").strip().lower()

    # 5. COS
    cos_secret_id = env.get("TENCENT_COS_SECRET_ID", "") or settings.TENCENT_COS_SECRET_ID or ""
    cos_secret_key = env.get("TENCENT_COS_SECRET_KEY", "") or settings.TENCENT_COS_SECRET_KEY or ""
    cos_bucket = (env.get("TENCENT_COS_BUCKET", "") or settings.TENCENT_COS_BUCKET or "").strip()

    # 6. Provider 列表 + key 状态
    providers_data = get_provider_list()
    providers_out: List[Dict[str, Any]] = []
    for p in providers_data["providers"]:
        key_name = p["api_key_name"]
        raw_value = env.get(key_name, "") or getattr(settings, key_name, "") or ""
        providers_out.append({
            "id": p["id"],
            "name": p["name"],
            "description": p.get("description", ""),
            "badge": p.get("badge"),
            "api_key_name": key_name,
            "covers": p.get("covers", []),
            "has_key": bool(raw_value.strip()),
            "key_display": _mask_key(raw_value),
        })

    # 7. 模型选择
    models = {
        "main_text": (env.get("MAIN_TEXT_MODEL", "") or settings.MAIN_TEXT_MODEL or "").strip(),
        "main_vision": (env.get("MAIN_VISION_MODEL", "") or settings.MAIN_VISION_MODEL or "").strip(),
        "main_embedding": (env.get("MAIN_EMBEDDING_MODEL", "") or settings.MAIN_EMBEDDING_MODEL or "").strip(),
    }

    # 8. 搜索后端
    search_engine = (env.get("DEFAULT_SEARCH_ENGINE", "") or settings.DEFAULT_SEARCH_ENGINE or "qwen").strip().lower()

    return {
        "deploy": {
            "mode": deploy_mode,
            "feclaw_domain": feclaw_domain,
            "cookie_secure": cookie_secure,
        },
        "database": {
            "url": db_url,
            "url_display": _mask_db_url(db_url),
            "read_only": True,  # 数据库连接串修改需重启，不允许在线改
            "note": "数据库 URL 修改需重启后端服务才能生效，因此不提供在线编辑",
        },
        "storage": {
            "storage_mode": storage_mode,
            "local_storage_root": local_storage_root,
            "vector_backend": vector_backend,
            "cos_secret_id_set": bool(cos_secret_id.strip()),
            "cos_secret_key_set": bool(cos_secret_key.strip()),
            "cos_bucket": cos_bucket,
            "cos_secret_id_display": _mask_key(cos_secret_id),
            "cos_secret_key_display": _mask_key(cos_secret_key),
        },
        "providers": providers_out,
        "models": models,
        "search_engine": search_engine,
    }


def _mask_db_url(url: str) -> str:
    """脱敏 DATABASE_URL 中的密码部分，保留 schema://user:****@host:port/db。"""
    if not url:
        return ""
    try:
        # mysql+pymysql://user:password@host:port/db?params
        if "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        if "@" not in rest:
            return url
        auth, host_part = rest.split("@", 1)
        if ":" in auth:
            user, _pwd = auth.split(":", 1)
            return f"{scheme}://{user}:****@{host_part}"
        return url
    except Exception:
        return url


# ───────────────────────────────────────────────────────────
# POST /admin/config  (sectioned)
# ───────────────────────────────────────────────────────────

class DeploySection(BaseModel):
    """部署模式 section"""
    mode: str = "single"  # "single" | "subdomain"
    feclaw_domain: str = ""
    cookie_secure: bool = False


class KeysSection(BaseModel):
    """API keys section. 空字符串视为"保持现状"。

    前端发送 `{ keys: { "QWEN_API_KEY": "sk-xxx" } }` —— 顶层就是 key→value 字典。
    """
    keys: Dict[str, str] = Field(default_factory=dict)

    # 接受平铺写法：把 "QWEN_API_KEY" 等顶层 key 视作本节
    class Config:
        extra = "allow"


class ModelsSection(BaseModel):
    """模型选择 section"""
    main_text: str = ""
    main_vision: str = ""
    main_embedding: str = ""


class StorageSection(BaseModel):
    """存储 section"""
    storage_mode: str = ""
    local_storage_root: str = ""
    vector_backend: str = ""
    tencent_cos_secret_id: str = ""
    tencent_cos_secret_key: str = ""
    tencent_cos_bucket: str = ""


class AdminConfigPayload(BaseModel):
    """统一入口：所有 section 合并在一个 payload 中。

    `search_engine` 是顶层字段（不是 section），便于与 `models` 一起提交。
    空字符串视为保持现状。
    """
    deploy: Optional[DeploySection] = None
    keys: Optional[KeysSection] = None
    models: Optional[ModelsSection] = None
    storage: Optional[StorageSection] = None
    search_engine: Optional[str] = None


@router.post("/config")
async def update_config(
    payload: AdminConfigPayload,
    admin: User = Depends(get_admin_user),
):
    """保存一个或多个 section。每个 section 独立处理，可单独或批量提交。"""
    updates: Dict[str, str] = {}
    updated_sections: List[str] = []

    if payload.deploy is not None:
        # 部署模式
        if payload.deploy.mode == "subdomain":
            if not payload.deploy.feclaw_domain or not payload.deploy.feclaw_domain.strip():
                raise HTTPException(status_code=400, detail="子域名模式必须填写 FECLAW_DOMAIN")
            updates["FECLAW_DOMAIN"] = payload.deploy.feclaw_domain.strip()
        else:
            # 单站点：清空 FECLAW_DOMAIN
            updates["FECLAW_DOMAIN"] = ""
        updates["COOKIE_SECURE"] = "true" if payload.deploy.cookie_secure else "false"
        updated_sections.append("deploy")

    if payload.keys is not None:
        for k, v in (payload.keys.keys or {}).items():
            if v and v.strip():
                updates[k.strip()] = v.strip()
        if any((payload.keys.keys or {}).values()):
            updated_sections.append("keys")

    if payload.models is not None:
        if payload.models.main_text is not None:
            updates["MAIN_TEXT_MODEL"] = payload.models.main_text.strip()
        if payload.models.main_vision is not None:
            updates["MAIN_VISION_MODEL"] = payload.models.main_vision.strip()
        if payload.models.main_embedding is not None:
            updates["MAIN_EMBEDDING_MODEL"] = payload.models.main_embedding.strip()
        updated_sections.append("models")

    if payload.storage is not None:
        if payload.storage.storage_mode:
            updates["STORAGE_MODE"] = payload.storage.storage_mode.strip()
        if payload.storage.local_storage_root:
            updates["LOCAL_STORAGE_ROOT"] = payload.storage.local_storage_root.strip()
        if payload.storage.vector_backend:
            updates["VECTOR_STORAGE_BACKEND"] = payload.storage.vector_backend.strip().lower()
        if payload.storage.tencent_cos_secret_id and payload.storage.tencent_cos_secret_id.strip():
            updates["TENCENT_COS_SECRET_ID"] = payload.storage.tencent_cos_secret_id.strip()
        if payload.storage.tencent_cos_secret_key and payload.storage.tencent_cos_secret_key.strip():
            updates["TENCENT_COS_SECRET_KEY"] = payload.storage.tencent_cos_secret_key.strip()
        if payload.storage.tencent_cos_bucket and payload.storage.tencent_cos_bucket.strip():
            updates["TENCENT_COS_BUCKET"] = payload.storage.tencent_cos_bucket.strip()
        if any([
            payload.storage.storage_mode,
            payload.storage.local_storage_root,
            payload.storage.vector_backend,
            payload.storage.tencent_cos_secret_id,
            payload.storage.tencent_cos_secret_key,
            payload.storage.tencent_cos_bucket,
        ]):
            updated_sections.append("storage")

    # 搜索后端（顶层字段）
    if payload.search_engine is not None and payload.search_engine.strip():
        updates["DEFAULT_SEARCH_ENGINE"] = payload.search_engine.strip().lower()
        updated_sections.append("search")

    if updates:
        update_env(updates)
        logger.info(
            f"[Admin] admin={admin.username} 更新了 sections={updated_sections} keys={list(updates.keys())}"
        )

    return {
        "status": "ok",
        "updated_sections": updated_sections,
        "updated_keys": list(updates.keys()),
        "message": "配置已保存，部分项需重启后端服务才能生效" if updates else "无变更",
    }


# ───────────────────────────────────────────────────────────
# POST /admin/config/test/{provider_id}
# ───────────────────────────────────────────────────────────

@router.post("/config/test/{provider_id}")
async def test_provider_config(
    provider_id: str,
    _admin: User = Depends(get_admin_user),
):
    """测试某个 provider 的 API key 是否已设置且格式合法。"""
    return await svc_verify_provider(provider_id)


# ───────────────────────────────────────────────────────────
# GET /admin/stats
# ───────────────────────────────────────────────────────────

def _get_storage_size(root: str) -> str:
    """获取本地存储目录大小的人类可读字符串。失败返回 'N/A'。"""
    if not root:
        return "N/A"
    p = Path(root)
    if not p.exists():
        return "0B"
    try:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(p):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return _humanize_bytes(total)
    except Exception as e:
        logger.debug(f"[Admin] storage size 计算失败: {e}")
        return "N/A"


def _humanize_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}MB"
    return f"{n / (1024 * 1024 * 1024):.2f}GB"


@router.get("/stats")
async def get_stats(_admin: User = Depends(get_admin_user)):
    """系统运行统计：用户数、Agent 数、消息数、模型调用、存储、系统状态、最近活动。"""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)

        # ── 数据库状态
        try:
            db.execute(func.now())  # 任何 SELECT 都行
            db_status = "connected"
        except Exception as e:
            db_status = f"error: {e}"

        # ── 基础计数
        # 用 func.count(<pk>) 而非 .count()，避免 ORM 拉取所有列导致
        # 模型中声明但 DB 不存在的列报错（如 ChatHistory.tool_call_id）
        def _safe_count(model) -> int:
            try:
                return db.query(func.count(model.id)).scalar() or 0
            except Exception:
                try:
                    return db.query(model).count()
                except Exception:
                    return 0

        user_count = _safe_count(User)
        agent_count = _safe_count(AgentProfile)
        chat_count = _safe_count(ChatHistory)
        session_count = _safe_count(ConversationSession)

        # ── 模型调用（agent_usage_log）
        # 表可能不存在（旧版未建），做容错
        model_calls_today = 0
        model_calls_total = 0
        try:
            model_calls_today = db.query(AgentUsageLog).filter(
                AgentUsageLog.created_at >= today_start
            ).count()
            model_calls_total = db.query(AgentUsageLog).count()
        except Exception:
            pass

        # ── LLM provider 状态（每个 provider 的 key 存在性）
        llm_status: Dict[str, str] = {}
        for p in PROVIDER_LIST:
            key_name = p["api_key_name"]
            value = getattr(settings, key_name, "") or ""
            llm_status[p["id"]] = "ok" if value.strip() else "no_key"

        # ── 存储用量
        local_root = (settings.LOCAL_STORAGE_ROOT or "./feclaw-storage").strip()
        storage_used = _get_storage_size(local_root)
        storage_mode = (settings.STORAGE_MODE or "auto").strip()

        # ── 今日新增
        try:
            new_users_today = db.query(func.count(User.id)).filter(User.created_at >= today_start).scalar() or 0
        except Exception:
            new_users_today = 0
        try:
            new_agents_today = db.query(func.count(AgentProfile.id)).filter(AgentProfile.created_at >= today_start).scalar() or 0
        except Exception:
            new_agents_today = 0
        try:
            messages_today = db.query(func.count(ChatHistory.id)).filter(ChatHistory.created_at >= today_start).scalar() or 0
        except Exception:
            messages_today = 0

        # ── 最近活动：最近 5 条 ChatHistory
        # 注意：必须 select 显式字段，避免 SQLAlchemy 拉取模型中声明但 DB 不存在的列
        # （如 tool_call_id 字段在新版本加入但生产库未迁移）
        try:
            recent_rows = db.query(
                ChatHistory.id,
                ChatHistory.user_id,
                ChatHistory.agent_hash,
                ChatHistory.channel,
                ChatHistory.created_at,
            ).order_by(ChatHistory.created_at.desc()).limit(5).all()
        except Exception:
            # 最差情况只查 created_at + channel
            recent_rows = db.query(
                ChatHistory.id,
                ChatHistory.user_id,
                ChatHistory.agent_hash,
                ChatHistory.channel,
            ).order_by(ChatHistory.id.desc()).limit(5).all()
        recent_activity: List[Dict[str, Any]] = []
        for row in recent_rows:
            mid, muid, mahash, mchannel, mcreated = (
                row[0], row[1], row[2], row[3],
                row[4] if len(row) > 4 else None,
            )
            user_name = None
            if muid:
                u = db.query(User).filter(User.id == muid).first()
                if u:
                    user_name = u.username
            agent_label = mahash or "-"
            if mahash:
                ap = db.query(AgentProfile).filter(AgentProfile.hash == mahash).first()
                if ap and ap.name:
                    agent_label = ap.name

            recent_activity.append({
                "time": mcreated.isoformat() if mcreated else "",
                "user": user_name or "anonymous",
                "action": f"chat with {agent_label}",
                "channel": mchannel or "web",
            })

        # ── 用户增长（最近 7 天，按天）
        user_growth: List[Dict[str, Any]] = []
        for i in range(6, -1, -1):
            day_start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            count = db.query(User).filter(
                User.created_at >= day_start, User.created_at < day_end
            ).count()
            user_growth.append({
                "date": day_start.strftime("%m-%d"),
                "count": count,
            })

        # ── 消息按渠道分布（今日）
        try:
            channel_rows = db.query(
                ChatHistory.channel,
                func.count(ChatHistory.id).label("count"),
            ).filter(
                ChatHistory.created_at >= today_start,
                ChatHistory.channel.isnot(None),
            ).group_by(ChatHistory.channel).all()
            channel_distribution = {ch: cnt for ch, cnt in channel_rows}
        except Exception:
            channel_distribution = {}

        return {
            "user_count": user_count,
            "agent_count": agent_count,
            "chat_count": chat_count,
            "session_count": session_count,
            "model_calls_today": model_calls_today,
            "model_calls_total": model_calls_total,
            "new_users_today": new_users_today,
            "new_agents_today": new_agents_today,
            "messages_today": messages_today,
            "storage_used": storage_used,
            "storage_mode": storage_mode,
            "local_storage_root": local_root,
            "db_status": db_status,
            "llm_status": llm_status,
            "recent_activity": recent_activity,
            "user_growth": user_growth,
            "channel_distribution": channel_distribution,
        }
    finally:
        db.close()
