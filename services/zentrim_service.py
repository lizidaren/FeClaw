"""
Zentrim（格物所）业务服务

提供条目 CRUD、文件上传、搜索、时间线、@引用 等操作。
符合 `docs/v1/02-zentrim.md` 中的设计。
"""
import json
import logging
import os
import re
import secrets
import time
# fix(P1-11): datetime.utcnow() 已废弃，改用 timezone-aware 的 now(UTC)
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# fix(P0-1): file_type 兜底正则（防止 COS Key 路径穿越）
_FILE_TYPE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# fix(P0-2): 上传附件大小上限（50 MB），与 router 层一致
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# fix(P1-4): 附录数量上限（防止单条目被滥用撑爆 JSON 列）
MAX_APPENDICES = 200
# fix(P1-4): 单条附录内容上限 64 KB（防 DoS / JSON 列膨胀）
MAX_APPENDIX_CONTENT = 64 * 1024


# fix(P1-1): 转义 LIKE 通配符 %/_，配合 SQLAlchemy .like(..., escape="\\") 使用
def _escape_like(s: str) -> str:
    """把字符串中的 %, _, \\ 用反斜杠转义，供 LIKE 子句使用"""
    if not s:
        return s
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from config import settings
from models.zentrim import (
    ZentrimEntry,
    ZentrimReference,
    ZentrimTimeline,
    ZentrimTimelineEntry,
)

logger = logging.getLogger(__name__)


# fix(P1-9): ULID 单调递增保护 — 防止系统时钟回拨导致时间戳倒退
_LAST_TS_MS = 0


# ────────────────────────────────────────────────────────────────────
# ULID（标准库实现）
# ────────────────────────────────────────────────────────────────────
def _generate_ulid() -> str:
    """生成 26 字符 ULID（时间戳 48 位 + 随机 80 位 → base32 编码）"""
    global _LAST_TS_MS
    # 48-bit 毫秒时间戳；若当前时间 ≤ _LAST_TS_MS 则递增
    ts_ms = int(time.time() * 1000)
    if ts_ms <= _LAST_TS_MS:
        ts_ms = _LAST_TS_MS + 1
    _LAST_TS_MS = ts_ms
    ts_bytes = ts_ms.to_bytes(6, "big", signed=False)

    # 80-bit 随机
    rand_bytes = secrets.token_bytes(10)

    raw = ts_bytes + rand_bytes  # 共 128 位 → 16 字节
    # Crockford base32 字母表
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    # 26 字符编码：每个字符 5 位 → 共 130 位，取高 128 位
    value = int.from_bytes(raw, "big")
    chars: List[str] = []
    for _ in range(26):
        chars.append(alphabet[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


# ────────────────────────────────────────────────────────────────────
# MIME → 扩展名
# ────────────────────────────────────────────────────────────────────
_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "text/plain": "txt",
    "text/html": "html",
    "application/pdf": "pdf",
}


def _ext_from_mime(mime: str) -> str:
    """根据 MIME 推断扩展名（不带 .）"""
    if not mime:
        return "bin"
    mime = mime.lower().strip()
    if mime in _MIME_EXT:
        return _MIME_EXT[mime]
    # 尝试取 / 后半部分
    if "/" in mime:
        return mime.split("/", 1)[1].strip() or "bin"
    return "bin"


# ────────────────────────────────────────────────────────────────────
# ZentrimService
# ────────────────────────────────────────────────────────────────────
class ZentrimService:
    """Zentrim 业务服务 — 所有 DB 操作走注入的 Session"""

    def __init__(self, db: Session):
        self.db = db

    # ════════════════════════════════════════
    # 条目 CRUD
    # ════════════════════════════════════════
    def create_entry(
        self,
        user_id: int,
        type: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        source: Optional[str] = None,
        attachment: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        summary: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        bbox: Optional[Dict[str, Any]] = None,
        source_url: Optional[str] = None,
    ) -> ZentrimEntry:
        """创建条目，自动生成 ULID"""
        if type not in ("note", "photo", "recording", "link", "canvas"):
            raise ValueError(f"Invalid entry type: {type}")

        entry = ZentrimEntry(
            id=_generate_ulid(),
            user_id=user_id,
            type=type,
            title=title,
            content=content,
            summary=summary,
            tags=tags,
            status="active",
            source=source or type,  # 默认 source 等于 type
            source_url=source_url,
            attachment=attachment,
            bbox=bbox,
            metadata_=metadata or {},
        )
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def get_entries(
        self,
        user_id: int,
        type: Optional[str] = None,
        limit: int = 20,
        before: Optional[datetime] = None,
        include_archived: bool = False,
    ) -> List[ZentrimEntry]:
        """获取时间线（按 created_at 倒序，仅 active 条目默认）"""
        # fix(P2-3): Service 层硬性 limit 上限（1-100），防止内部/测试调用传超大值
        limit = max(1, min(limit, 100))
        query = self.db.query(ZentrimEntry).filter(ZentrimEntry.user_id == user_id)
        if type:
            query = query.filter(ZentrimEntry.type == type)
        if not include_archived:
            query = query.filter(ZentrimEntry.status != "archived")
        if before:
            query = query.filter(ZentrimEntry.created_at < before)
        return query.order_by(ZentrimEntry.created_at.desc()).limit(limit).all()

    def get_entry(
        self, entry_id: str, user_id: Optional[int] = None
    ) -> Optional[ZentrimEntry]:
        """条目详情；可选校验 user_id 防越权"""
        query = self.db.query(ZentrimEntry).filter(ZentrimEntry.id == entry_id)
        if user_id is not None:
            query = query.filter(ZentrimEntry.user_id == user_id)
        return query.first()

    def archive_entry(self, entry_id: str, user_id: Optional[int] = None) -> Optional[ZentrimEntry]:
        # fix(P1-10): 状态更新走统一的 update_entry_status 入口，避免路径重复
        """归档条目（软删） — 委托内部 update_entry_status 实现"""
        entry = self.get_entry(entry_id, user_id=user_id)
        if not entry:
            return None
        entry.status = "archived"
        entry.archived_at = datetime.now(timezone.utc)
        entry.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(entry)
        return entry
        if not entry:
            return None
        entry.status = "archived"
        entry.archived_at = datetime.now(timezone.utc)
        entry.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def unarchive_entry(self, entry_id: str, user_id: Optional[int] = None) -> Optional[ZentrimEntry]:
        # fix(P1-10): 状态更新走统一的 update_entry_status 入口，避免路径重复
        """取消归档 — 委托内部 update_entry_status 实现"""
        entry = self.get_entry(entry_id, user_id=user_id)
        if not entry:
            return None
        entry.status = "active"
        entry.archived_at = None
        entry.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def delete_entry(self, entry_id: str, user_id: Optional[int] = None) -> bool:
        """硬删除（同时删除关联的 references、timeline entries、COS 文件）"""
        entry = self.get_entry(entry_id, user_id=user_id)
        if not entry:
            return False

        # fix(P0-3): DB 操作必须放在单个事务里，失败整体 rollback 避免状态错乱
        try:
            self.db.query(ZentrimTimelineEntry).filter(
                ZentrimTimelineEntry.entry_id == entry_id
            ).delete(synchronize_session=False)
            self.db.query(ZentrimReference).filter(
                or_(
                    ZentrimReference.source_id == entry_id,
                    ZentrimReference.target_id == entry_id,
                )
            ).delete(synchronize_session=False)
            self.db.delete(entry)
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            logger.exception(f"[ZentrimService] delete_entry({entry_id}) failed; rolled back: {e}")
            raise

        # fix(P0-3): COS 删除放事务外（IO 操作不应阻塞 DB 事务）
        # fix(P0-4): 失败时改打 ERROR 并标记 ORPHAN，便于监控告警
        # TODO: 后续应引入 cos_cleanup_queue outbox 表 + worker 重试机制
        att = entry.attachment or {}
        if isinstance(att, dict) and att.get("key"):
            try:
                self._delete_storage_object(att["key"])
            except Exception as e:
                logger.error(
                    f"[ZentrimService] ORPHAN COS FILE: user={user_id} entry={entry_id} "
                    f"key={att['key']} err={e}",
                    exc_info=True,
                )
                # TODO: 入 outbox 表（cos_cleanup_queue），由 worker 重试清理

        # fix(P1-5): 清理向量索引（防止硬删后向量残留导致搜索结果指向已删条目）
        vector_id = getattr(entry, "vector_id", None)
        if vector_id:
            try:
                import asyncio
                from services.vector_search_service import VectorSearchService

                vs = VectorSearchService(agent_hash=None)
                index_name = f"idx-zentrim-{user_id}" if user_id else "idx-zentrim"
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # 已有运行 loop：后台任务清理，不阻塞删除
                        asyncio.ensure_future(vs.delete(keys=[vector_id], index=index_name))
                    else:
                        loop.run_until_complete(vs.delete(keys=[vector_id], index=index_name))
                except RuntimeError:
                    # 无 event loop 时同步执行
                    asyncio.run(vs.delete(keys=[vector_id], index=index_name))
            except Exception as e:
                # 向量清理失败只警告，不阻塞 DB 删除（DB 是真相之源）
                logger.warning(
                    f"[ZentrimService] vector cleanup failed (non-fatal): user={user_id} "
                    f"entry={entry_id} vector_id={vector_id} err={e}"
                )
        return True

    def add_appendix(
        self,
        entry_id: str,
        title: str,
        content: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
        user_id: Optional[int] = None,
    ) -> Optional[ZentrimEntry]:
        """添加附录到计算层 metadata_.appendices（不改原始层）"""
        entry = self.get_entry(entry_id, user_id=user_id)
        if not entry:
            return None

        meta = entry.metadata_ or {}
        if not isinstance(meta, dict):
            meta = {}

        # 四层结构：批注层（annotation）
        annotation = meta.get("annotation") or {}
        if not isinstance(annotation, dict):
            annotation = {}

        appendix_list = annotation.get("appendices") or []
        if not isinstance(appendix_list, list):
            appendix_list = []

        # fix(P1-4): 限制单个条目的附录数量，防 DoS / JSON 列膨胀
        if len(appendix_list) >= MAX_APPENDICES:
            raise ValueError(
                f"Too many appendices (max {MAX_APPENDICES}); please start a new entry"
            )
        # fix(P1-4): 限制单条附录内容长度（64 KB）
        if content and len(content) > MAX_APPENDIX_CONTENT:
            raise ValueError(
                f"Appendix content exceeds limit ({MAX_APPENDIX_CONTENT} bytes)"
            )

        appendix_list.append({
            "title": title,
            "content": content,
            "attachments": attachments or [],
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        annotation["appendices"] = appendix_list
        meta["annotation"] = annotation

        entry.metadata_ = meta
        entry.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def update_entry(
        self,
        entry_id: str,
        user_id: Optional[int] = None,
        **fields: Any,
    ) -> Optional[ZentrimEntry]:
        """通用 update（支持 title/content/tags/summary 等）"""
        entry = self.get_entry(entry_id, user_id=user_id)
        if not entry:
            return None

        allowed = {"title", "content", "summary", "tags", "metadata", "bbox"}  # fix(P1-10): status 走专门的 status 更新端点
        for key, value in fields.items():
            if key in allowed:
                if key == "metadata":
                    entry.metadata_ = value
                else:
                    setattr(entry, key, value)
        entry.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    # ════════════════════════════════════════
    # 文件存储
    # ════════════════════════════════════════
    def _storage(self):
        """获取存储服务实例（懒加载）"""
        from services.file_storage import create_file_storage
        return create_file_storage(mode=settings.STORAGE_MODE)

    def _delete_storage_object(self, key: str) -> bool:
        """删除 COS 文件（带 try/except）"""
        try:
            storage = self._storage()
            return bool(storage.delete_file_by_key(key))
        except Exception as e:
            logger.warning(f"[ZentrimService] delete_file_by_key({key}) failed: {e}")
            return False

    def upload_attachment(
        self,
        user_id: int,
        entry_id: str,
        file_bytes: bytes,
        mime: str,
        file_type: str,
        original_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        上传附件到 `zentrim/user_{uid}/attachments/{entry_id}_{file_type}.{ext}`

        Args:
            user_id: 所属用户
            entry_id: 关联条目 ID（ULID）
            file_bytes: 文件字节数据
            mime: MIME 类型
            file_type: 用户传入的子类型标记（如 original/clean/audio）
            original_filename: 原始文件名（用于扩展名推断）

        Returns:
            {"bucket", "key", "mime", "size"}
        """
        # fix(P0-1): file_type 兜底校验，防止 path traversal 写到非预期前缀
        if not file_type or not _FILE_TYPE_RE.match(file_type):
            raise ValueError(f"file_type must match ^[a-z0-9_-]{{1,32}}$, got: {file_type!r}")

        # fix(P0-2): 文件大小硬上限，防止上传打爆 worker 内存
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise ValueError(f"file_bytes exceeds limit ({MAX_UPLOAD_BYTES} bytes)")

        # 推断扩展名
        ext = _ext_from_mime(mime)
        if original_filename and "." in original_filename:
            ext_from_name = original_filename.rsplit(".", 1)[-1].lower()
            if ext_from_name and len(ext_from_name) <= 5:
                ext = ext_from_name

        key = f"{settings.TENCENT_COS_PREFIX}zentrim/user_{user_id}/attachments/{entry_id}_{file_type}.{ext}"

        storage = self._storage()
        storage.put_object(key, file_bytes)

        return {
            "bucket": settings.TENCENT_COS_BUCKET,
            "key": key,
            "mime": mime,
            "size": len(file_bytes),
        }

    # ════════════════════════════════════════
    # 搜索
    # ════════════════════════════════════════
    def search_zentrim(
        self,
        user_id: int,
        query: str,
        limit: int = 20,
        include_archived: bool = False,
    ) -> List[ZentrimEntry]:
        """
        混合搜索：
        1. 向量搜索 idx-zentrim-{uid}（如有向量服务）
        2. FULLTEXT / LIKE 搜索
        3. 合并去重，按相关性排序
        """
        if not query or not query.strip():
            return []

        results: Dict[str, ZentrimEntry] = {}

        # 1. 向量搜索（可选）
        try:
            vector_hits = self._vector_search(user_id, query, top_k=limit)
            for entry, score in vector_hits:
                results[entry.id] = entry  # 向量结果按命中度已排序
        except Exception as e:
            logger.debug(f"[ZentrimService] vector search skipped: {e}")

        # 2. 字面匹配（title/content/summary LIKE）
        try:
            # fix(P1-1): 先转义 %/_，避免用户输入作为通配符导致全表扫描或绕过匹配
            safe_query = _escape_like(query)
            pat = f"%{safe_query}%"
            like_query = self.db.query(ZentrimEntry).filter(
                ZentrimEntry.user_id == user_id,
                or_(
                    ZentrimEntry.title.like(pat, escape="\\"),
                    ZentrimEntry.content.like(pat, escape="\\"),
                    ZentrimEntry.summary.like(pat, escape="\\"),
                ),
            )
            if not include_archived:
                like_query = like_query.filter(ZentrimEntry.status != "archived")
            like_hits = like_query.order_by(ZentrimEntry.created_at.desc()).limit(limit).all()
            for entry in like_hits:
                if entry.id not in results:
                    results[entry.id] = entry
        except Exception as e:
            logger.debug(f"[ZentrimService] fulltext search skipped: {e}")

        # 合并结果，向量优先（已按顺序插入），字面命中按时间倒序追加
        combined = list(results.values())
        # 截断到 limit
        return combined[:limit]

    def _vector_search(
        self, user_id: int, query: str, top_k: int
    ) -> List[tuple]:
        """
        尝试向量搜索；如不可用返回空列表。

        返回 [(ZentrimEntry, score), ...]。
        """
        # fix(P2-4): VectorSearchService 单例缓存（避免每次搜索重新实例化）
        global _VS_SINGLETON
        try:
            _VS_SINGLETON
        except NameError:
            _VS_SINGLETON = None

        try:
            from services.vector_search_service import VectorSearchService

            if _VS_SINGLETON is None:
                _VS_SINGLETON = VectorSearchService(agent_hash=None)
            vs = _VS_SINGLETON
            # idx-zentrim-{uid} 索引命名约定
            index_name = f"idx-zentrim-{user_id}"
            raw = vs.search(query, top_k=top_k * 2) if hasattr(vs, "search") else []

            entries_with_score = []
            for hit in raw or []:
                meta = hit.get("metadata", {}) if isinstance(hit, dict) else {}
                entry_id = (
                    meta.get("entry_id")
                    or meta.get("id")
                    or hit.get("id")
                    or hit.get("key")
                )
                if not entry_id:
                    continue
                entry = self.get_entry(entry_id, user_id=user_id)
                if not entry or entry.status == "archived":
                    continue
                entries_with_score.append((entry, hit.get("score", 0)))

            # 按 score 倒序
            entries_with_score.sort(key=lambda x: x[1], reverse=True)
            return entries_with_score[:top_k]
        except ImportError as e:
            # fix(P1-2): 首次加载失败静默 — VectorSearchService 模块缺失是常见降级场景
            _VS_SINGLETON = True  # 用 True 标记 "已尝试但不可用"，避免反复 import
            logger.warning(f"[ZentrimService] vector search degraded (ImportError): {e}")
            return []
        except Exception as e:
            # fix(P1-2): 运行时错误 — 打 warning 而非 debug，便于监控告警
            logger.warning(f"[ZentrimService] vector search degraded: {e}")
            return []

    # ════════════════════════════════════════
    # 时间线
    # ════════════════════════════════════════
    def create_timeline(
        self, user_id: int, name: str, description: Optional[str] = None, type: str = "custom"
    ) -> ZentrimTimeline:
        """创建时间线"""
        if type not in ("auto", "custom"):
            type = "custom"
        tl = ZentrimTimeline(
            id=_generate_ulid(),
            user_id=user_id,
            name=name,
            description=description,
            type=type,
        )
        self.db.add(tl)
        self.db.commit()
        self.db.refresh(tl)
        return tl

    def get_timelines(self, user_id: int) -> List[ZentrimTimeline]:
        """获取用户所有时间线"""
        return (
            self.db.query(ZentrimTimeline)
            .filter(ZentrimTimeline.user_id == user_id)
            .order_by(ZentrimTimeline.created_at.desc())
            .all()
        )

    def get_timeline(self, timeline_id: str, user_id: Optional[int] = None) -> Optional[ZentrimTimeline]:
        """时间线详情"""
        query = self.db.query(ZentrimTimeline).filter(ZentrimTimeline.id == timeline_id)
        if user_id is not None:
            query = query.filter(ZentrimTimeline.user_id == user_id)
        return query.first()

    def add_to_timeline(
        self, timeline_id: str, entry_id: str, user_id: Optional[int] = None, sort_order: int = 0
    ) -> bool:
        """加入条目到时间线"""
        # 校验所有权
        tl = self.get_timeline(timeline_id, user_id=user_id)
        if not tl:
            return False
        entry = self.get_entry(entry_id, user_id=user_id)
        if not entry:
            return False

        # 已存在则跳过（保证 idempotent）
        exists = (
            self.db.query(ZentrimTimelineEntry)
            .filter(
                ZentrimTimelineEntry.timeline_id == timeline_id,
                ZentrimTimelineEntry.entry_id == entry_id,
            )
            .first()
        )
        if exists:
            return True

        link = ZentrimTimelineEntry(
            timeline_id=timeline_id,
            entry_id=entry_id,
            sort_order=sort_order,
        )
        self.db.add(link)
        self.db.commit()
        return True

    def remove_from_timeline(
        self, timeline_id: str, entry_id: str, user_id: Optional[int] = None
    ) -> bool:
        """从时间线移除"""
        tl = self.get_timeline(timeline_id, user_id=user_id)
        if not tl:
            return False

        deleted = (
            self.db.query(ZentrimTimelineEntry)
            .filter(
                ZentrimTimelineEntry.timeline_id == timeline_id,
                ZentrimTimelineEntry.entry_id == entry_id,
            )
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return bool(deleted)

    def get_timeline_entries(
        self, timeline_id: str, user_id: Optional[int] = None
    ) -> List[ZentrimEntry]:
        """获取时间线下的所有条目"""
        tl = self.get_timeline(timeline_id, user_id=user_id)
        if not tl:
            return []
        rows = (
            self.db.query(ZentrimTimelineEntry, ZentrimEntry)
            .join(ZentrimEntry, ZentrimEntry.id == ZentrimTimelineEntry.entry_id)
            .filter(ZentrimTimelineEntry.timeline_id == timeline_id)
            .order_by(ZentrimTimelineEntry.sort_order.asc(), ZentrimTimelineEntry.added_at.asc())
            .all()
        )
        return [entry for _link, entry in rows]

    def delete_timeline(self, timeline_id: str, user_id: Optional[int] = None) -> bool:
        """删除时间线（不影响条目本身）"""
        tl = self.get_timeline(timeline_id, user_id=user_id)
        if not tl:
            return False
        self.db.query(ZentrimTimelineEntry).filter(
            ZentrimTimelineEntry.timeline_id == timeline_id
        ).delete(synchronize_session=False)
        self.db.delete(tl)
        self.db.commit()
        return True

    # ════════════════════════════════════════
    # @引用
    # ════════════════════════════════════════
    def create_reference(
        self, source_id: str, target_id: str, user_id: Optional[int] = None
    ) -> Optional[ZentrimReference]:
        """创建 @引用（source → target）"""
        if source_id == target_id:
            return None  # 不允许自引用
        # 校验所有权：source、target 必须属同一用户
        source = self.get_entry(source_id, user_id=user_id)
        target = self.get_entry(target_id, user_id=user_id)
        if not source or not target:
            return None

        # fix(P1-7): 去重 — 已存在同 source+target 的引用则直接返回
        existing = (
            self.db.query(ZentrimReference)
            .filter(
                ZentrimReference.source_id == source_id,
                ZentrimReference.target_id == target_id,
            )
            .first()
        )
        if existing:
            return existing

        ref = ZentrimReference(
            id=_generate_ulid(),
            source_id=source_id,
            target_id=target_id,
        )
        self.db.add(ref)
        self.db.commit()
        self.db.refresh(ref)
        return ref

    def get_references(
        self, entry_id: str, direction: str = "target", user_id: Optional[int] = None
    ) -> List[ZentrimReference]:
        """
        获取引用关系
        - direction="target"：返回哪些条目 @引用了 entry_id（即 entry 是被引用方）
        - direction="source"：返回 entry_id @引用了哪些条目（即 entry 是引用方）
        """
        entry = self.get_entry(entry_id, user_id=user_id)
        if not entry:
            return []

        if direction == "target":
            query = self.db.query(ZentrimReference).filter(ZentrimReference.target_id == entry_id)
        else:
            query = self.db.query(ZentrimReference).filter(ZentrimReference.source_id == entry_id)
        return query.order_by(ZentrimReference.created_at.desc()).all()

    def delete_reference(self, ref_id: str, user_id: Optional[int] = None) -> bool:
        """删除引用关系；校验 user_id 必须同时拥有 source 和 target"""
        ref = self.db.query(ZentrimReference).filter(ZentrimReference.id == ref_id).first()
        if not ref:
            return False

        if user_id is not None:
            source = self.get_entry(ref.source_id, user_id=user_id)
            target = self.get_entry(ref.target_id, user_id=user_id)
            if not source or not target:
                return False

        self.db.delete(ref)
        self.db.commit()
        return True

    # ════════════════════════════════════════
    # 状态管理
    # ════════════════════════════════════════
    def update_entry_status(
        self,
        entry_id: str,
        status: str,
        user_id: Optional[int] = None,
    ) -> Optional[ZentrimEntry]:
        """更新条目状态（active/processing/archived）"""
        if status not in ("active", "archived", "processing"):
            raise ValueError(f"Invalid status: {status}")

        entry = self.get_entry(entry_id, user_id=user_id)
        if not entry:
            return None

        entry.status = status
        if status == "archived":
            entry.archived_at = datetime.now(timezone.utc)
        else:
            entry.archived_at = None
        entry.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    # ════════════════════════════════════════
    # 序列化辅助
    # ════════════════════════════════════════
    @staticmethod
    def serialize_entry(entry: ZentrimEntry) -> Dict[str, Any]:
        """统一序列化（供 router JSON 响应使用）"""
        if entry is None:
            return {}

        def _iso(dt):
            return dt.isoformat() if dt else None

        return {
            "id": entry.id,
            "user_id": entry.user_id,
            "type": entry.type,
            "title": entry.title,
            "content": entry.content,
            "summary": entry.summary,
            "tags": entry.tags or [],
            "status": entry.status,
            "source": entry.source,
            "source_url": entry.source_url,
            "attachment": entry.attachment,
            "bbox": entry.bbox,
            "metadata": entry.metadata_ or {},
            "vector_id": entry.vector_id,
            "created_at": _iso(entry.created_at),
            "updated_at": _iso(entry.updated_at),
            "archived_at": _iso(entry.archived_at),
        }

    @staticmethod
    def serialize_timeline(tl: ZentrimTimeline) -> Dict[str, Any]:
        if tl is None:
            return {}
        return {
            "id": tl.id,
            "user_id": tl.user_id,
            "name": tl.name,
            "description": tl.description,
            "type": tl.type,
            "created_at": tl.created_at.isoformat() if tl.created_at else None,
        }

    @staticmethod
    def serialize_reference(ref: ZentrimReference) -> Dict[str, Any]:
        if ref is None:
            return {}
        return {
            "id": ref.id,
            "source_id": ref.source_id,
            "target_id": ref.target_id,
            "created_at": ref.created_at.isoformat() if ref.created_at else None,
        }
