from __future__ import annotations

"""
分享页引用令牌路由 — 创建和解析文本引用

T17a「分享边界」（本线程 S3）：
- 引用路径**只**从父 `ShareMapping` 推导；请求里的 `vfs_path` 仅为兼容旧前端保留，
  一律忽略（禁止任意路径引用）。
- 创建/解析都必须校验父 share：存在、未过期、未撤销、密码已验证。
- cookie 解锁（POST /s/{slug}/unlock）与 `?password=` 兼容通道都可用于证明，
  后者会被标记 deprecated。
"""

import time
import logging
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from models.database import get_db
from services import share_service as share_svc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["share-reference"])

# 内存速率限制：{ip: [(timestamp, count)]}
_rate_limit: dict = defaultdict(list)
_rate_lock = Lock()
_RATE_LIMIT_WINDOW = 60       # 60 秒窗口
_RATE_LIMIT_MAX = 10           # 每窗口最多 10 次请求
_REF_EXPIRY_DAYS = 90

# 受信任的反代 IP 段（内网），仅在这些 IP 来源时使用 X-Forwarded-For
_TRUSTED_PROXIES = ["127.0.0.1", "::1"]


def _get_client_ip(request: Request) -> str:
    """提取真实客户端 IP（仅信任内网反代）"""
    peer = request.client.host if request.client else ""
    if peer in _TRUSTED_PROXIES:
        xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if xff:
            return xff
    return peer


def _check_rate_limit(client_ip: str) -> bool:
    """线程安全的内存速率限制，返回 True 表示允许"""
    now = time.time()
    with _rate_lock:
        entries = [(t, c) for t, c in _rate_limit.get(client_ip, [])
                   if now - t < _RATE_LIMIT_WINDOW]
        total = sum(c for _, c in entries)
        if total >= _RATE_LIMIT_MAX:
            # 写回清理后的 entries（避免泄漏）
            _rate_limit[client_ip] = entries[-200:] if entries else []
            return False
        if entries and now - entries[-1][0] < 1:
            entries[-1] = (entries[-1][0], entries[-1][1] + 1)
        else:
            entries.append((now, 1))
        _rate_limit[client_ip] = entries[-200:]

        # 选择性清理：只清除已过期的 IP 条目，不再整体清空
        if len(_rate_limit) > 10000:
            cutoff = now - _RATE_LIMIT_WINDOW
            expired = [k for k, v in _rate_limit.items()
                       if not [(t, c) for t, c in v if t > cutoff]]
            for k in expired:
                del _rate_limit[k]
        return True


class ShareRefRequest(BaseModel):
    ref_hash: str | None = None
    share_hash: str
    # T17a：仅为兼容旧前端（static/js/share-reference.js 仍会发送）而保留，
    # 服务端一律忽略，路径取自父 share mapping。
    vfs_path: str | None = None
    selected_text: str = Field(..., min_length=1, max_length=2000)
    context_before: str = Field(default="", max_length=500)
    context_after: str = Field(default="", max_length=500)
    selection_start: int | None = None
    selection_end: int | None = None


def _boundary_http(exc: share_svc.ShareBoundaryError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _safe_get_mapping(db, share_hash: str):
    """按 share_hash 查父分享；数据库不可用 → 503（不 500、不静默当不存在）。"""
    try:
        return share_svc.get_mapping_by_share_hash(share_hash, db=db)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Share] parent share lookup failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail="分享引用服务当前不可用，请稍后再试")


def _request_password_proof(request: Request) -> dict:
    """本次请求携带的密码证明（cookie 优先；query 仅兼容）。"""
    return {
        "cookie_value": request.cookies.get(share_svc.unlock_cookie_name()),
        "query_password": request.query_params.get("password"),
    }


@router.post("/api/share/reference")
async def create_share_reference(req: ShareRefRequest, request: Request, db: Session = Depends(get_db)):
    """创建分享页引用令牌（路径由父 share 决定，客户端不能指定）"""
    client_ip = _get_client_ip(request)

    # 速率限制
    if client_ip and not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    mapping = _safe_get_mapping(db, req.share_hash)
    if not mapping:
        raise HTTPException(status_code=404, detail="分享链接不存在或已过期")

    proof = _request_password_proof(request)
    try:
        share_svc.assert_mapping_live(mapping)
        proof_status = share_svc.password_proof_status(
            mapping,
            cookie_value=proof["cookie_value"],
            query_password=proof["query_password"],
        )
        if not proof_status["ok"]:
            raise share_svc.ShareBoundaryError(
                401, "share_password_required", "分享需要密码，请先解锁后再引用"
            )
        if req.vfs_path and req.vfs_path.strip() != (mapping.vfs_path or "").strip():
            logger.warning(
                "[Share] reference vfs_path ignored (client=%r mapping=%r)",
                (req.vfs_path or "")[:120], (mapping.vfs_path or "")[:120],
            )
        ref_hash = share_svc.create_reference_record(
            db, mapping, req.selected_text,
            ref_hash=req.ref_hash,
            context_before=req.context_before,
            context_after=req.context_after,
            creator_ip=client_ip,
            expires_days=_REF_EXPIRY_DAYS,
        )
    except share_svc.ShareBoundaryError as exc:
        logger.warning(
            "[Share] reference create rejected share=%s reason=%s",
            (req.share_hash or "")[:8], exc.reason,
        )
        raise _boundary_http(exc)
    except HTTPException:
        raise
    except Exception as exc:
        # 数据库不可用等基础设施故障：明确 503，不 500、也不放行
        logger.error("[Share] reference create failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail="分享引用服务当前不可用，请稍后再试")

    return {
        "ref_hash": ref_hash,
        # 返回服务端推导出的共享路径（不是客户端提交的值）
        "vfs_path": mapping.vfs_path,
    }


@router.get("/api/share/reference/{ref_hash}")
async def get_share_reference(ref_hash: str, request: Request, db: Session = Depends(get_db)):
    """解析分享页引用令牌（父 share 过期/撤销/需要密码时拒绝）"""
    # 恒定延迟防侧信道（无论是否存在都等待同样时间）
    import asyncio
    t0 = time.time()

    proof = _request_password_proof(request)
    try:
        payload = share_svc.load_authorized_reference(
            db, ref_hash,
            cookie_value=proof["cookie_value"],
            query_password=proof["query_password"],
        )
        error = None
    except share_svc.ShareBoundaryError as exc:
        payload = None
        error = exc
    except Exception as exc:
        # 数据库不可用等基础设施故障：明确 503，不当成“引用不存在”
        logger.error("[Share] reference lookup failed ref=%s: %s", ref_hash, exc, exc_info=True)
        raise HTTPException(status_code=503, detail="分享引用服务当前不可用，请稍后再试")

    elapsed = time.time() - t0
    if elapsed < 0.05:
        await asyncio.sleep(0.05 - elapsed)

    if error is not None or payload is None:
        logger.info(
            "[Share] reference access denied ref=%s caller_ip=%s reason=%s",
            ref_hash, _get_client_ip(request), getattr(error, "reason", "unknown"),
        )
        raise _boundary_http(error or share_svc.ShareBoundaryError(404, "reference_missing"))

    # 审计日志：记录谁在查询哪个引用（引用本身是公开的，但需要可追溯）
    logger.info(
        "[Share] reference accessed ref=%s caller_ip=%s agent_hash=%s share_hash=%s",
        ref_hash, _get_client_ip(request), payload["agent_hash"], (payload["share_hash"] or "")[:8],
    )

    return {
        "selected_text": payload["selected_text"],
        "context_before": payload["context_before"],
        "context_after": payload["context_after"],
        # 只返回父 share 的路径（服务端推导），不返回 ShareReference 里的历史值
        "vfs_path": payload["vfs_path"],
    }
