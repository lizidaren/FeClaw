"""
Zentrim（格物所）API 路由

所有端点通过 JWT 认证获取 user_id：
- POST   /api/zentrim/entries              创建条目（支持 multipart 文件上传）
- GET    /api/zentrim/entries              时间线分页
- GET    /api/zentrim/entries/{entry_id}   条目详情
- PATCH  /api/zentrim/entries/{entry_id}   更新条目（title/content/tags 等）
- POST   /api/zentrim/entries/{entry_id}/archive    归档
- POST   /api/zentrim/entries/{entry_id}/unarchive  取消归档
- DELETE /api/zentrim/entries/{entry_id}   硬删除
- POST   /api/zentrim/entries/{entry_id}/appendix   添加附录（计算层）

- POST   /api/zentrim/timelines                       创建时间线
- GET    /api/zentrim/timelines                       时间线列表
- GET    /api/zentrim/timelines/{timeline_id}         时间线详情（含条目）
- DELETE /api/zentrim/timelines/{timeline_id}         删除时间线
- POST   /api/zentrim/timelines/{timeline_id}/entries     加入条目
- DELETE /api/zentrim/timelines/{timeline_id}/entries/{entry_id}  从时间线移除

- POST   /api/zentrim/references                       创建 @引用
- GET    /api/zentrim/references?entry_id=&direction=  查询引用
- DELETE /api/zentrim/references/{ref_id}              删除引用

- GET    /api/zentrim/search?q=...                     搜索
- POST   /api/zentrim/attachments                      通用附件上传（用户先创建条目 → 再上传）

约定：
- 所有 endpoint 返回 JSON
- 错误格式：{"detail": "..."}
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import settings
from models.database import SessionLocal
from services.zentrim_service import ZentrimService, _generate_ulid
from utils.auth import get_current_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/zentrim", tags=["Zentrim"])


# ────────────────────────────────────────────────────────────────────
# DB Dependency
# ────────────────────────────────────────────────────────────────────
def get_db():
    """数据库会话（与 `models/database.py` 保持一致）"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ────────────────────────────────────────────────────────────────────
# 请求/响应模型
# ────────────────────────────────────────────────────────────────────
class TimelineCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    type: str = "custom"


class TimelineEntryRequest(BaseModel):
    entry_id: str
    sort_order: int = 0


class ReferenceCreateRequest(BaseModel):
    source_id: str
    target_id: str


class EntryCreateRequest(BaseModel):
    # fix(P1-8): 限制字段最大长度，防止单请求打爆内存 / DB JSON 列
    type: str = Field(..., max_length=16)
    title: Optional[str] = Field(default=None, max_length=512)
    content: Optional[str] = Field(default=None, max_length=1_000_000)
    source: Optional[str] = Field(default=None, max_length=32)
    source_url: Optional[str] = Field(default=None, max_length=4096)
    summary: Optional[str] = Field(default=None, max_length=4096)
    tags: Optional[List[str]] = Field(default=None, max_length=64)
    metadata: Optional[Dict[str, Any]] = None
    bbox: Optional[Dict[str, Any]] = None
    attachment: Optional[Dict[str, Any]] = None


class EntryPatchRequest(BaseModel):
    # fix(P1-8): 限制字段最大长度，防止单请求打爆内存 / DB JSON 列
    title: Optional[str] = Field(default=None, max_length=512)
    content: Optional[str] = Field(default=None, max_length=1_000_000)
    summary: Optional[str] = Field(default=None, max_length=4096)
    tags: Optional[List[str]] = Field(default=None, max_length=64)
    metadata: Optional[Dict[str, Any]] = None
    bbox: Optional[Dict[str, Any]] = None


class AppendixRequest(BaseModel):
    # fix(P1-8): 配合 service 层 MAX_APPENDICES / MAX_APPENDIX_CONTENT 双层防御
    title: str = Field(..., max_length=512)
    content: str = Field(..., max_length=1_000_000)
    attachments: Optional[List[Dict[str, Any]]] = None


# ────────────────────────────────────────────────────────────────────
# 条目 CRUD
# ────────────────────────────────────────────────────────────────────
@router.post("/entries")
async def create_entry(
    type: str = Form(...),
    title: Optional[str] = Form(None),
    content: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    summary: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),  # JSON 字符串
    metadata: Optional[str] = Form(None),  # JSON 字符串
    bbox: Optional[str] = Form(None),  # JSON 字符串
    file: Optional[UploadFile] = File(None),
    file_type: Optional[str] = Form(None, pattern=r"^[a-z0-9_-]{1,32}$"),  # fix(P0-1): 校验 file_type 防止 COS Key 穿越
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    创建条目 + 可选上传文件

    - multipart/form-data
    - tags / metadata / bbox 接受 JSON 字符串
    - 若带 file，必须同时给 file_type
    """
    if type not in ("note", "photo", "recording", "link", "canvas"):
        raise HTTPException(status_code=400, detail=f"Invalid type: {type}")

    # 解析 JSON 字段
    parsed_tags: Optional[List[str]] = None
    if tags:
        try:
            parsed_tags = json.loads(tags)
            if not isinstance(parsed_tags, list):
                raise ValueError("tags must be a JSON array")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid tags JSON: {e}")

    parsed_metadata: Optional[Dict[str, Any]] = None
    if metadata:
        try:
            parsed_metadata = json.loads(metadata)
            if not isinstance(parsed_metadata, dict):
                raise ValueError("metadata must be a JSON object")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid metadata JSON: {e}")

    parsed_bbox: Optional[Dict[str, Any]] = None
    if bbox:
        try:
            parsed_bbox = json.loads(bbox)
            if not isinstance(parsed_bbox, dict):
                raise ValueError("bbox must be a JSON object")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid bbox JSON: {e}")

    svc = ZentrimService(db)

    # 先创建条目，拿到 id
    entry = svc.create_entry(
        user_id=user_id,
        type=type,
        title=title,
        content=content,
        source=source,
        source_url=source_url,
        summary=summary,
        tags=parsed_tags,
        metadata=parsed_metadata,
        bbox=parsed_bbox,
    )

    # 可选上传附件
    attachment: Optional[Dict[str, Any]] = None
    if file is not None:
        if not file_type:
            raise HTTPException(status_code=400, detail="file_type is required when file is provided")
        try:
            file_bytes = await file.read()
        finally:
            await file.close()

        # fix(P0-2): 限制单文件 ≤ 50MB，防止 OOM
        if len(file_bytes) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large (> 50MB)")

        # fix(P0-5): 上传 COS 拿 key；失败则 delete_entry 回滚 DB 记录
        try:
            attachment = svc.upload_attachment(
                user_id=user_id,
                entry_id=entry.id,
                file_bytes=file_bytes,
                mime=file.content_type or "application/octet-stream",
                file_type=file_type,
                original_filename=file.filename,
            )
        except Exception as e:
            logger.exception(f"[Zentrim] upload_attachment failed; rolling back entry {entry.id}: {e}")
            # COS 上传失败 → 删 entry（补偿）
            try:
                svc.delete_entry(entry.id, user_id=user_id)
            except Exception:
                logger.exception(f"[Zentrim] rollback delete_entry({entry.id}) also failed")
            raise HTTPException(status_code=502, detail="Upload to COS failed")

        # 回写 attachment 到条目
        entry.attachment = attachment
        entry.updated_at = datetime.now(timezone.utc)
        # fix(P0-5): 第二次 commit 失败则补偿删除已上传的 COS
        try:
            db.commit()
        except Exception as e:
            logger.exception(f"[Zentrim] db.commit failed after COS upload; compensating: {e}")
            db.rollback()
            try:
                svc._delete_storage_object(attachment["key"])
            except Exception:
                logger.exception(f"[Zentrim] compensating delete of {attachment['key']} failed")
            raise HTTPException(status_code=500, detail="DB commit failed after upload")
        db.refresh(entry)

    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.create_entry user={user_id} entry={entry.id} type={type}")
    return ZentrimService.serialize_entry(entry)


@router.get("/entries")
async def list_entries(
    type: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    before: Optional[str] = Query(None),
    include_archived: bool = Query(False),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """时间线分页"""
    before_dt: Optional[datetime] = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'before' datetime")

    svc = ZentrimService(db)
    entries = svc.get_entries(
        user_id=user_id,
        type=type,
        limit=limit,
        before=before_dt,
        include_archived=include_archived,
    )
    return [ZentrimService.serialize_entry(e) for e in entries]


@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """条目详情"""
    svc = ZentrimService(db)
    entry = svc.get_entry(entry_id, user_id=user_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return ZentrimService.serialize_entry(entry)


@router.patch("/entries/{entry_id}")
async def patch_entry(
    entry_id: str,
    body: EntryPatchRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """更新条目（title/content/tags/summary/metadata/bbox）"""
    svc = ZentrimService(db)
    fields = body.dict(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    entry = svc.update_entry(entry_id, user_id=user_id, **fields)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    # fix(P1-3): 写操作审计日志（便于事后追溯 / 合规审查）
    logger.info(f"[audit] zentrim.update_entry user={user_id} entry={entry_id} fields={sorted(fields.keys())}")
    return ZentrimService.serialize_entry(entry)


@router.post("/entries/{entry_id}/archive")
async def archive_entry(
    entry_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """归档条目（软删）"""
    svc = ZentrimService(db)
    entry = svc.archive_entry(entry_id, user_id=user_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.archive_entry user={user_id} entry={entry_id}")
    return {"status": "ok", "entry": ZentrimService.serialize_entry(entry)}


@router.post("/entries/{entry_id}/unarchive")
async def unarchive_entry(
    entry_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """取消归档"""
    svc = ZentrimService(db)
    entry = svc.unarchive_entry(entry_id, user_id=user_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.unarchive_entry user={user_id} entry={entry_id}")
    return {"status": "ok", "entry": ZentrimService.serialize_entry(entry)}


@router.delete("/entries/{entry_id}")
async def delete_entry(
    entry_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """硬删除（同时清理 references / timeline links）"""
    svc = ZentrimService(db)
    success = svc.delete_entry(entry_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Entry not found")
    # fix(P1-3): 写操作审计日志（硬删是高敏感操作，必须留痕）
    logger.info(f"[audit] zentrim.delete_entry user={user_id} entry={entry_id}")
    return {"status": "ok"}


@router.post("/entries/{entry_id}/appendix")
async def add_appendix(
    entry_id: str,
    body: AppendixRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """添加附录到 metadata.annotation.appendices（不改原始层）"""
    svc = ZentrimService(db)
    entry = svc.add_appendix(
        entry_id=entry_id,
        title=body.title,
        content=body.content,
        attachments=body.attachments,
        user_id=user_id,
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return ZentrimService.serialize_entry(entry)


# ────────────────────────────────────────────────────────────────────
# 附件上传（条目已存在后单独上传）
# ────────────────────────────────────────────────────────────────────
@router.post("/attachments")
async def upload_attachment(
    entry_id: str = Form(...),
    file_type: str = Form(..., pattern=r"^[a-z0-9_-]{1,32}$"),  # fix(P0-1): 校验 file_type
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """把附件上传到 `zentrim/user_{uid}/attachments/{entry_id}_{file_type}.{ext}`，并回写到 entry.attachment"""
    svc = ZentrimService(db)
    entry = svc.get_entry(entry_id, user_id=user_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    try:
        file_bytes = await file.read()
    finally:
        await file.close()

    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    # fix(P0-2): 限制单文件 ≤ 50MB，防止 OOM
    if len(file_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (> 50MB)")

    attachment = svc.upload_attachment(
        user_id=user_id,
        entry_id=entry_id,
        file_bytes=file_bytes,
        mime=file.content_type or "application/octet-stream",
        file_type=file_type,
        original_filename=file.filename,
    )
    entry.attachment = attachment
    entry.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(entry)

    return {"status": "ok", "entry_id": entry_id, "attachment": attachment}


# ────────────────────────────────────────────────────────────────────
# 时间线
# ────────────────────────────────────────────────────────────────────
@router.post("/timelines")
async def create_timeline(
    body: TimelineCreateRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """创建时间线"""
    svc = ZentrimService(db)
    tl = svc.create_timeline(
        user_id=user_id,
        name=body.name,
        description=body.description,
        type=body.type,
    )
    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.create_timeline user={user_id} timeline={tl.id} name={body.name!r}")
    return ZentrimService.serialize_timeline(tl)


@router.get("/timelines")
async def list_timelines(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """时间线列表"""
    svc = ZentrimService(db)
    tls = svc.get_timelines(user_id=user_id)
    return [ZentrimService.serialize_timeline(t) for t in tls]


@router.get("/timelines/{timeline_id}")
async def get_timeline(
    timeline_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """时间线详情（含条目）"""
    svc = ZentrimService(db)
    tl = svc.get_timeline(timeline_id, user_id=user_id)
    if not tl:
        raise HTTPException(status_code=404, detail="Timeline not found")
    entries = svc.get_timeline_entries(timeline_id, user_id=user_id)
    return {
        **ZentrimService.serialize_timeline(tl),
        "entries": [ZentrimService.serialize_entry(e) for e in entries],
        "entry_count": len(entries),
    }


@router.delete("/timelines/{timeline_id}")
async def delete_timeline(
    timeline_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """删除时间线（不影响条目本身）"""
    svc = ZentrimService(db)
    ok = svc.delete_timeline(timeline_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Timeline not found")
    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.delete_timeline user={user_id} timeline={timeline_id}")
    return {"status": "ok"}


@router.post("/timelines/{timeline_id}/entries")
async def add_to_timeline(
    timeline_id: str,
    body: TimelineEntryRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """将条目加入时间线"""
    svc = ZentrimService(db)
    ok = svc.add_to_timeline(
        timeline_id=timeline_id,
        entry_id=body.entry_id,
        user_id=user_id,
        sort_order=body.sort_order,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Timeline or entry not found")
    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.add_to_timeline user={user_id} timeline={timeline_id} entry={body.entry_id}")
    return {"status": "ok"}


@router.delete("/timelines/{timeline_id}/entries/{entry_id}")
async def remove_from_timeline(
    timeline_id: str,
    entry_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """从时间线移除条目"""
    svc = ZentrimService(db)
    ok = svc.remove_from_timeline(timeline_id, entry_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Timeline not found")
    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.remove_from_timeline user={user_id} timeline={timeline_id} entry={entry_id}")
    return {"status": "ok"}


# ────────────────────────────────────────────────────────────────────
# @引用
# ────────────────────────────────────────────────────────────────────
@router.post("/references")
async def create_reference(
    body: ReferenceCreateRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """创建 @引用"""
    svc = ZentrimService(db)
    ref = svc.create_reference(
        source_id=body.source_id,
        target_id=body.target_id,
        user_id=user_id,
    )
    if not ref:
        raise HTTPException(
            status_code=400,
            detail="Cannot create reference (self-reference or entry not found)",
        )
    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.create_reference user={user_id} ref={ref.id} source={body.source_id} target={body.target_id}")
    return ZentrimService.serialize_reference(ref)


@router.get("/references")
async def list_references(
    entry_id: str = Query(...),
    direction: str = Query("target", pattern="^(target|source)$"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """查询引用关系

    - direction=target：返回哪些条目 @引用了 entry_id（被引用方）
    - direction=source：返回 entry_id @引用了哪些条目（引用方）
    """
    svc = ZentrimService(db)
    refs = svc.get_references(entry_id, direction=direction, user_id=user_id)
    return [ZentrimService.serialize_reference(r) for r in refs]


@router.delete("/references/{ref_id}")
async def delete_reference(
    ref_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """删除 @引用"""
    svc = ZentrimService(db)
    ok = svc.delete_reference(ref_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Reference not found")
    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.delete_reference user={user_id} ref={ref_id}")
    return {"status": "ok"}


# ────────────────────────────────────────────────────────────────────
# 搜索
# ────────────────────────────────────────────────────────────────────
@router.get("/search")
async def search_entries(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    include_archived: bool = Query(False),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """混合搜索（向量优先 + LIKE 兜底合并）"""
    svc = ZentrimService(db)
    entries = svc.search_zentrim(
        user_id=user_id,
        query=q,
        limit=limit,
        include_archived=include_archived,
    )
    return {
        "query": q,
        "count": len(entries),
        "results": [ZentrimService.serialize_entry(e) for e in entries],
    }
