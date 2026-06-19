"""
分享页引用令牌路由 — 创建和解析文本引用
"""

import secrets
import string
import time
import logging
from collections import defaultdict
from threading import Lock
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from models.database import get_db, ShareReference, ShareMapping

logger = logging.getLogger(__name__)

router = APIRouter(tags=["share-reference"])

# base62 字符集
_BASE62 = string.digits + string.ascii_lowercase + string.ascii_uppercase

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


def _generate_ref_hash() -> str:
    """生成 8 位 base62 随机 hash"""
    return "".join(secrets.choice(_BASE62) for _ in range(8))


class ShareRefRequest(BaseModel):
    ref_hash: str | None = None
    share_hash: str
    vfs_path: str
    selected_text: str = Field(..., min_length=1, max_length=2000)
    context_before: str = Field(default="", max_length=500)
    context_after: str = Field(default="", max_length=500)
    selection_start: int | None = None
    selection_end: int | None = None


@router.post("/api/share/reference")
async def create_share_reference(req: ShareRefRequest, request: Request, db: Session = Depends(get_db)):
    """创建分享页引用令牌"""
    client_ip = _get_client_ip(request)

    # 速率限制
    if client_ip and not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    # 校验 share_hash 并获取 agent_hash
    mapping = db.query(ShareMapping).filter(
        ShareMapping.share_hash == req.share_hash
    ).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="分享链接不存在或已过期")

    # 记录当前 Agent 的 hash（用于跨 Agent 隐私隔离）
    agent_hash = mapping.agent_hash

    # 校验 selected_text
    text = req.selected_text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="选中文本不能为空")

    # 生成 ref_hash（重试防碰撞）
    ref_hash = req.ref_hash
    if ref_hash:
        if len(ref_hash) != 8:
            raise HTTPException(status_code=422, detail="ref_hash 长度必须为 8 位")
    else:
        for _ in range(10):
            ref_hash = _generate_ref_hash()
            existing = db.query(ShareReference).filter(
                ShareReference.ref_hash == ref_hash
            ).first()
            if not existing:
                break
        else:
            raise HTTPException(status_code=500, detail="生成引用标识失败，请重试")

    ref = ShareReference(
        ref_hash=ref_hash,
        share_hash=req.share_hash,
        agent_hash=agent_hash,
        vfs_path=req.vfs_path,
        selected_text=text,
        context_before=req.context_before[:500],
        context_after=req.context_after[:500],
        creator_ip=client_ip,
        expires_at=datetime.utcnow() + timedelta(days=_REF_EXPIRY_DAYS),
    )
    db.add(ref)
    db.commit()

    logger.info(f"Share reference created: ref_hash={ref_hash}, share_hash={req.share_hash[:8]}...")
    return {"ref_hash": ref_hash}


@router.get("/api/share/reference/{ref_hash}")
async def get_share_reference(ref_hash: str, request: Request, db: Session = Depends(get_db)):
    """解析分享页引用令牌"""
    # 恒定延迟防侧信道（无论是否存在都等待同样时间）
    import asyncio
    t0 = time.time()

    ref = db.query(ShareReference).filter(
        ShareReference.ref_hash == ref_hash
    ).first()

    elapsed = time.time() - t0
    if elapsed < 0.05:
        await asyncio.sleep(0.05 - elapsed)

    if not ref:
        raise HTTPException(status_code=404, detail="引用不存在或已过期")

    # 审计日志：记录谁在查询哪个引用（引用本身是公开的，但需要可追溯）
    caller_ip = _get_client_ip(request)
    logger.info(f"Share reference accessed: ref_hash={ref_hash}, caller_ip={caller_ip}, "
                f"agent_hash={ref.agent_hash}, share_hash={ref.share_hash[:8]}...")

    return {
        "selected_text": ref.selected_text,
        "context_before": ref.context_before or "",
        "context_after": ref.context_after or "",
        "vfs_path": ref.vfs_path,
    }
