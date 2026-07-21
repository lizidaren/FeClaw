"""工具调用日志路径、权限判断与保留期清理。"""

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, Optional

from config import settings
from services.file_storage import FileStorage, create_file_storage

logger = logging.getLogger(__name__)

TOOL_LOG_ROOT = ".logs/tools"
TOOL_LOG_RETENTION_DAYS = 7
_TOOL_LOG_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AGENT_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def canonicalize_tool_log_path(path: str) -> str:
    """将工具日志的 VFS 路径标准化为 Agent 根目录下的 ``.logs`` 路径。"""
    normalized = (path or "").strip().replace("\\", "/").lstrip("/")
    if normalized == "workspace/.logs":
        return ".logs"
    if normalized.startswith("workspace/.logs/"):
        return normalized[len("workspace/"):]
    return normalized


def is_tool_log_vfs_path(path: str) -> bool:
    """判断 VFS 路径是否属于只读工具日志目录。"""
    normalized = canonicalize_tool_log_path(path).rstrip("/")
    return normalized == ".logs" or normalized.startswith(".logs/")


def is_tool_log_cos_key(key: str) -> bool:
    """判断存储 key 是否属于任一 Agent 的只读 ``.logs`` 目录。"""
    normalized = (key or "").replace("\\", "/").strip("/")
    return bool(re.search(r"(?:^|/)agents/[^/]+/\.logs(?:/|$)", normalized))


def cleanup_tool_logs(
    agent_hash: Optional[str] = None,
    retention_days: int = TOOL_LOG_RETENTION_DAYS,
    *,
    storage: Optional[FileStorage] = None,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """删除日期目录早于保留期的工具日志。

    ``agent_hash`` 为空时清理所有 Agent；手动工具传入当前 Agent，避免跨 Agent
    删除。无法解析为 ``YYYY-MM-DD`` 的目录会被保留。
    """
    if retention_days < 0:
        raise ValueError("retention_days 不能小于 0")
    if agent_hash and not _AGENT_HASH_RE.fullmatch(agent_hash):
        raise ValueError("agent_hash 格式无效")

    storage = storage or create_file_storage(
        mode=getattr(settings, "STORAGE_MODE", "auto")
    )
    current = now or datetime.now()
    cutoff_date = (current - timedelta(days=retention_days)).date()

    agents_prefix = f"{settings.STORAGE_PREFIX}agents/"
    if agent_hash:
        scan_prefix = f"{agents_prefix}{agent_hash}/{TOOL_LOG_ROOT}/"
        date_pattern = re.compile(
            rf"^{re.escape(scan_prefix)}(\d{{4}}-\d{{2}}-\d{{2}})/"
        )
    else:
        scan_prefix = agents_prefix
        date_pattern = re.compile(
            rf"^{re.escape(agents_prefix)}[^/]+/{re.escape(TOOL_LOG_ROOT)}/"
            r"(\d{4}-\d{2}-\d{2})/"
        )

    objects = storage.list_objects(scan_prefix, max_keys=1_000_000) or []
    deleted = 0
    failed = 0
    skipped = 0

    for obj in objects:
        key = obj.get("Key", "")
        match = date_pattern.match(key)
        if not match or not _TOOL_LOG_DATE_RE.fullmatch(match.group(1)):
            skipped += 1
            continue
        try:
            log_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            skipped += 1
            continue
        if log_date >= cutoff_date:
            skipped += 1
            continue
        if storage.delete_file_by_key(key):
            deleted += 1
        else:
            failed += 1

    result: Dict[str, object] = {
        "scanned": len(objects),
        "deleted": deleted,
        "failed": failed,
        "skipped": skipped,
        "cutoff_date": cutoff_date.isoformat(),
        "retention_days": retention_days,
    }
    logger.info(
        "[ToolLogs] cleanup agent=%s scanned=%s deleted=%s failed=%s cutoff=%s",
        agent_hash or "*",
        result["scanned"],
        deleted,
        failed,
        result["cutoff_date"],
    )
    return result
