"""
分享页引用令牌路由 — 创建和解析文本引用
"""

import secrets
import string
import time
import logging
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
_rate_limit: dict = {}
_RATE_LIMIT_WINDOW = 60       # 60 秒窗口
_RATE_LIMIT_MAX = 10           # 每窗口最多 10 次请求
_REF_EXPIRY_DAYS = 90


def _check_rate_limit(client_ip: str) -> bool:
    """简单内存速率限制，返回 True 表示允许"""
    now = time.time()
    entries = _rate_limit.get(client_ip, [])
    # 清理过期条目
    entries = [e for e in entries if now - e[0] < _RATE_LIMIT_WINDOW]
    total = sum(e[1] for e in entries)
    if total >= _RATE_LIMIT_MAX:
        return False
    # 添加到最近窗口
    if entries and now - entries[-1][0] < 1:
        entries[-1] = (entries[-1][0], entries[-1][1] + 1)
    else:
        entries.append((now, 1))
    _rate_limit[client_ip] = entries
    # 定期清理全局 dict（防止泄漏）
    if len(_rate_limit) > 10000:
        _rate_limit.clear()
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
    client_ip = request.client.host if request.client else None

    # 速率限制
    if client_ip and not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    # 校验 share_hash
    mapping = db.query(ShareMapping).filter(
        ShareMapping.share_hash == req.share_hash
    ).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="分享链接不存在或已过期")

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
async def get_share_reference(ref_hash: str, db: Session = Depends(get_db)):
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

    return {
        "selected_text": ref.selected_text,
        "context_before": ref.context_before or "",
        "context_after": ref.context_after or "",
        "vfs_path": ref.vfs_path,
    }
