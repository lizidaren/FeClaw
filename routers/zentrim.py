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
- GET    /api/zentrim/entries/{entry_id}/blocks    读取条目所有 blocks
- GET    /api/zentrim/entries/{entry_id}/canvas    聚合端点（entry + blocks，供前端画布加载）
- POST   /api/zentrim/entries/{entry_id}/process       触发 AI 管线处理
- GET    /api/zentrim/entries/{entry_id}/status        获取管线处理状态

约定：
- 所有 endpoint 返回 JSON
- 错误格式：{"detail": "..."}
"""
import json
import logging
from datetime import datetime
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
from models.database import SessionLocal, get_db
from models.zentrim import ZentrimBlock
from services.zentrim_pipeline import pipeline as zentrim_pipeline
from services.zentrim_service import ZentrimService, _generate_ulid
from utils.auth import get_current_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/zentrim", tags=["Zentrim"])


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
    """精简版 entry 创建请求 — 实际内容由 blocks 承载"""
    # fix(P1-8): 限制字段最大长度，防止单请求打爆内存 / DB JSON 列
    title: Optional[str] = Field(default=None, max_length=512)
    tags: Optional[List[str]] = Field(default=None, max_length=64)
    metadata: Optional[Dict[str, Any]] = None


class EntryPatchRequest(BaseModel):
    """精简版 entry 更新请求 — 仅允许 title / tags / metadata"""
    # fix(P1-8): 限制字段最大长度，防止单请求打爆内存 / DB JSON 列
    title: Optional[str] = Field(default=None, max_length=512)
    tags: Optional[List[str]] = Field(default=None, max_length=64)
    metadata: Optional[Dict[str, Any]] = None


class BlocksPutRequest(BaseModel):
    """全量替换 blocks 请求体"""
    blocks: List[Dict[str, Any]] = Field(..., max_length=1000)


class AppendixRequest(BaseModel):
    # fix(P1-8): 配合 service 层 MAX_APPENDICES / MAX_APPENDIX_CONTENT 双层防御
    title: str = Field(..., max_length=512)
    content: str = Field(..., max_length=1_000_000)
    attachments: Optional[List[Dict[str, Any]]] = None


class PipelineProcessRequest(BaseModel):
    """触发管线处理请求"""
    block_id: str
    cos_key: str
    block_type: str  # "photo" | "audio" | "ink"


# fix(P0-3): cos_key 白名单正则 — 必须以 feclaw/zentrim/user_{uid}/ 开头，
# 后跟 blocks/{block_id}_* 或 attachments/{entry_id}_{file_type}.{ext} 形态。
# 客户端传入的其他路径（如其他用户的 COS key、feclaw/agents/ 下 Agent 私有文件）一律拒绝。
_COS_KEYPrefix_PATTERN_TEMPLATE = r"^feclaw/zentrim/user_{uid}/[A-Za-z0-9_\-/]+\.[a-z0-9]{{1,5}}$"


def _validate_cos_key(cos_key: str, user_id: int) -> None:
    """校验 cos_key 是否符合白名单格式且归属当前用户。

    防御 COS 路径穿越攻击（review P0-3）：任意 cos_key 可让 pipeline 借服务端凭证
    下载其他用户的 COS 文件。
    """
    import re
    pattern = _COS_KEYPrefix_PATTERN_TEMPLATE.format(uid=user_id)
    if not re.match(pattern, cos_key):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid cos_key: must start with "
                f"'feclaw/zentrim/user_{user_id}/' and match whitelist pattern."
            ),
        )
    # 额外防御：禁止 .., //, 空字节
    if ".." in cos_key or "//" in cos_key or "\x00" in cos_key:
        raise HTTPException(status_code=400, detail="Invalid cos_key: forbidden characters")


# ────────────────────────────────────────────────────────────────────
# 条目 CRUD
# ────────────────────────────────────────────────────────────────────
@router.post("/entries")
async def create_entry(
    body: EntryCreateRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    创建条目（精简版 — JSON body）

    - 旧字段（type/content/summary/source/source_url/attachment/bbox）已废弃，
      实际内容请通过 PUT /entries/{id}/blocks 写入。
    - 返回的 entry 不包含 blocks 数组；如有需要再单独 GET。
    """
    svc = ZentrimService(db)
    entry = svc.create_entry(
        user_id=user_id,
        title=body.title,
        tags=body.tags,
        metadata=body.metadata,
    )

    # fix(P1-3): 写操作审计日志
    logger.info(f"[audit] zentrim.create_entry user={user_id} entry={entry.id}")
    return ZentrimService.serialize_entry(entry)


@router.get("/entries")
async def list_entries(
    limit: int = Query(20, ge=1, le=100),
    before: Optional[str] = Query(None),
    include_archived: bool = Query(False),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """时间线分页（精简 schema 无 type 过滤参数）"""
    before_dt: Optional[datetime] = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'before' datetime")

    svc = ZentrimService(db)
    entries = svc.get_entries(
        user_id=user_id,
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
    """更新条目（title/tags/metadata）"""
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
# Blocks CRUD（迁移文档 §2.2）
# ────────────────────────────────────────────────────────────────────
@router.put("/entries/{entry_id}/blocks")
async def save_blocks(
    entry_id: str,
    body: BlocksPutRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """全量替换一个 entry 的所有 blocks。

    请求体：{"blocks": [{"type":"text", "data":{...}, "text":"..."}, ...]}
    返回：{"status": "ok", "block_count": N}
    """
    svc = ZentrimService(db)
    try:
        block_count = svc.save_blocks(entry_id, body.blocks, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if block_count < 0:
        raise HTTPException(status_code=404, detail="Entry not found")
    # fix(P1-3): 写操作审计日志
    logger.info(
        f"[audit] zentrim.save_blocks user={user_id} entry={entry_id} "
        f"block_count={block_count}"
    )
    return {"status": "ok", "block_count": block_count}


@router.get("/entries/{entry_id}/blocks")
async def load_blocks(
    entry_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """读取一个 entry 的所有 blocks（按 sort_order 升序）。

    返回：{"blocks": [{"id":"...", "type":"text", "data":{...}, "text":"..."}, ...]}
    """
    svc = ZentrimService(db)
    # 先确认 entry 存在 + 归属校验（404 比 200+[] 更明确）
    entry = svc.get_entry(entry_id, user_id=user_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    blocks = svc.load_blocks(entry_id, user_id=user_id)
    return {"blocks": blocks}


@router.get("/entries/{entry_id}/canvas")
async def get_entry_canvas(
    entry_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """聚合端点：一次调用返回 entry 基本信息 + blocks（按 sort_order 排序）。

    用于前端 Canvas 页面加载已有条目的画布数据。
    返回：
    {
      "entry": { id, title, tags, metadata: {...}, ... },
      "blocks": [
        { id, type, data, text, sort_order, ... },
        ...
      ]
    }
    """
    svc = ZentrimService(db)
    entry = svc.get_entry(entry_id, user_id=user_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    entry_data = ZentrimService.serialize_entry(entry)
    blocks = svc.load_blocks(entry_id, user_id=user_id)
    return {"entry": entry_data, "blocks": blocks}


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
    """把附件上传到 COS，返回 key 供前端通过 PUT /blocks 写入 block 引用"""
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
    # fix(P0-1): 不再回写 entry.attachment（列已删除）；返回 COS key 供前端通过 PUT /blocks 写入 block 引用
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


# ────────────────────────────────────────────────────────────────────
# AI Pipeline（§9 Pipeline）
# ────────────────────────────────────────────────────────────────────
@router.post("/entries/{entry_id}/process")
async def trigger_pipeline(
    entry_id: str,
    body: PipelineProcessRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """触发管线处理（后台异步，不阻塞）

    请求体：{"block_id": "...", "cos_key": "...", "block_type": "photo|audio|ink"}
    返回：{"status": "processing", "entry_id": "...", "block_id": "..."}
    """
    svc = ZentrimService(db)
    entry = svc.get_entry(entry_id, user_id=user_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    # fix(P0-2): 校验 block_id 真的属于该 entry_id + 归属当前用户
    # 防御攻击向量：用户 A 用自己合法的 entry_id，但编造 block_id + 任意 cos_key，
    # 借 pipeline 服务端凭证读取他人 COS 文件。
    block = (
        db.query(ZentrimBlock)
        .filter(
            ZentrimBlock.id == body.block_id,
            ZentrimBlock.entry_id == entry_id,
        )
        .first()
    )
    if not block:
        raise HTTPException(
            status_code=404,
            detail=f"Block {body.block_id} not found in entry {entry_id}",
        )

    # fix(P0-2): 从 DB 读 block.data.key 作为权威 cos_key，忽略前端传入的 cos_key
    # （如果前端传入的 cos_key 与 DB 不一致，记 warning 但以 DB 为准）
    block_data = block.data if isinstance(block.data, dict) else {}
    db_cos_key = block_data.get("key") or block_data.get("cos_key")
    if not db_cos_key:
        raise HTTPException(
            status_code=400,
            detail=f"Block {body.block_id} has no cos_key in data; cannot trigger pipeline",
        )
    if db_cos_key != body.cos_key:
        logger.warning(
            f"[audit] zentrim.pipeline.cos_key_mismatch user={user_id} entry={entry_id} "
            f"block={body.block_id} client_cos_key={body.cos_key!r} db_cos_key={db_cos_key!r}; using DB value"
        )
    # 以 DB 为真相之源
    cos_key = db_cos_key

    # fix(P0-3): cos_key 路径白名单 + 归属校验（防止路径穿越 / 跨用户读取）
    _validate_cos_key(cos_key, user_id)

    bt = body.block_type
    if bt == "photo":
        zentrim_pipeline.process_photo(entry_id, body.block_id, cos_key, user_id)
    elif bt == "audio":
        zentrim_pipeline.process_audio(entry_id, body.block_id, cos_key, user_id)
    elif bt == "ink":
        zentrim_pipeline.process_ink(entry_id, body.block_id, cos_key, user_id)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported block_type: {bt} (expected: photo/audio/ink)",
        )

    logger.info(
        f"[audit] zentrim.pipeline.trigger user={user_id} entry={entry_id} "
        f"block={body.block_id} type={bt}"
    )
    return {
        "status": "processing",
        "entry_id": entry_id,
        "block_id": body.block_id,
    }


@router.get("/entries/{entry_id}/status")
async def get_pipeline_status(
    entry_id: str,
    block_id: Optional[str] = Query(None),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """获取管线处理状态

    - 不传 block_id 时返回 entry 整体状态（来自 DB entry.status）
    - 传 block_id 时返回该 block 的管线任务状态（idle/processing/done）
    """
    svc = ZentrimService(db)
    entry = svc.get_entry(entry_id, user_id=user_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    if block_id:
        task_status = zentrim_pipeline.get_status(entry_id, block_id)
        return {
            "entry_id": entry_id,
            "block_id": block_id,
            "pipeline_status": task_status,
            "entry_status": entry.status,
        }

    return {
        "entry_id": entry_id,
        "entry_status": entry.status,
    }
