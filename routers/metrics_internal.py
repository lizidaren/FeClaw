"""
P1.5 最小 metrics endpoint（admin-only）

目的：为运维提供 worker / 缓存 / LLM 用量 / 活跃 Agent 的快速可观测性入口。
不做：全栈 Prometheus / OpenTelemetry 接入（Phase 2+ 路线图）。

端点：GET /internal/metrics
认证：admin-only（Depends(get_admin_user)）
"""
import logging
import os
from datetime import datetime, timedelta

import psutil
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func

from models import database as _db_module
from models.database import LLMStat, User
from services.active_tracker import get_recent
from utils.auth_dependencies import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["Metrics (admin-only)"])


@router.get("/metrics")
async def get_metrics(
    request: Request,
    admin: User = Depends(get_admin_user),
) -> dict:
    """聚合 worker / cache / llm / agent 指标，便于运维快速排查。

    Returns:
        {
            "worker": {"pid", "rss_mb", "cpu_percent", "threads"},
            "caches": {"vfs_file_cache", "rate_limit_buckets", "web_search_cache": {...}},
            "llm_usage": {"total_tokens", "by_provider": [...], "by_day_last7": [...]},
            "active_agents": [...],
        }
    """
    return {
        "worker": _worker_stats(),
        "caches": _cache_stats(),
        "llm_usage": _llm_usage_rollup(),
        "active_agents": _active_agents(),
    }


def _worker_stats() -> dict:
    """当前 worker 进程 PID / RSS / CPU / threads（psutil）。"""
    try:
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        return {
            "pid": proc.pid,
            "rss_mb": round(mem.rss / (1024 * 1024), 2),
            "cpu_percent": proc.cpu_percent(interval=0.0),  # 非阻塞：取上次采样
            "threads": proc.num_threads(),
        }
    except Exception as e:
        logger.warning(f"[metrics] worker stats failed: {e}")
        return {"error": str(e)}


def _cache_stats() -> dict:
    """散落全局字典 / 缓存大小。失败容错 —— 单个缓存报错不影响其他。"""
    result = {}

    # 1. VFS file cache（chat_service._VFS_FILE_CACHE）
    try:
        from services.chat_service import _VFS_FILE_CACHE
        result["vfs_file_cache"] = {"size": len(_VFS_FILE_CACHE)}
    except Exception as e:
        result["vfs_file_cache"] = {"error": str(e)}

    # 2. Rate limit buckets（apps_service._rate_limit_buckets）
    try:
        from services.apps_service import _rate_limit_buckets
        result["rate_limit_buckets"] = {"size": len(_rate_limit_buckets)}
    except Exception as e:
        result["rate_limit_buckets"] = {"error": str(e)}

    # 3. Web search cache（WebToolsMixin class attr）
    try:
        from services.tools.web_tools import WebToolsMixin
        result["web_search_cache"] = WebToolsMixin.get_search_cache_stats()
    except Exception as e:
        result["web_search_cache"] = {"error": str(e)}

    return result


def _llm_usage_rollup() -> dict:
    """LLM 用量 rollup：total + 按 provider 聚合 + 最近 7 天每日聚合。

    故意用独立 SessionLocal 而非 Depends(get_db)：本端点要能独立排查，
    即使 db 连接有问题也能返回部分数据。

    用 `_db_module.SessionLocal()` 而不是模块级 import 的 SessionLocal：
    后者在 import 时被缓存，real_db fixture 的 patch 不会生效。
    """
    db = _db_module.SessionLocal()
    try:
        # 1. Total tokens
        total_tokens = db.query(func.coalesce(func.sum(LLMStat.tokens_used), 0)).scalar() or 0

        # 2. By provider (聚合 provider/model/total tokens)
        by_provider_rows = db.query(
            LLMStat.provider,
            LLMStat.model,
            func.coalesce(func.sum(LLMStat.tokens_used), 0).label("total"),
            func.count(LLMStat.id).label("calls"),
        ).group_by(LLMStat.provider, LLMStat.model).all()

        by_provider = [
            {
                "provider": row.provider or "unknown",
                "model": row.model or "unknown",
                "tokens": int(row.total or 0),
                "calls": int(row.calls or 0),
            }
            for row in by_provider_rows
        ]
        # 按 tokens 降序
        by_provider.sort(key=lambda x: x["tokens"], reverse=True)

        # 3. By day (最近 7 天)
        cutoff = datetime.utcnow() - timedelta(days=7)
        by_day_rows = db.query(
            func.date(LLMStat.created_at).label("day"),
            func.coalesce(func.sum(LLMStat.tokens_used), 0).label("total"),
            func.count(LLMStat.id).label("calls"),
        ).filter(LLMStat.created_at >= cutoff).group_by(
            func.date(LLMStat.created_at)
        ).order_by(func.date(LLMStat.created_at).desc()).all()

        by_day_last7 = [
            {
                "day": str(row.day) if row.day else None,
                "tokens": int(row.total or 0),
                "calls": int(row.calls or 0),
            }
            for row in by_day_rows
        ]

        return {
            "total_tokens": int(total_tokens),
            "by_provider": by_provider,
            "by_day_last7": by_day_last7,
        }
    except Exception as e:
        logger.warning(f"[metrics] llm_usage rollup failed: {e}")
        return {"error": str(e)}
    finally:
        db.close()


def _active_agents() -> list:
    """最近 30 分钟活跃 Agent 列表（按 agent_hash:channel 分组 + count）。"""
    try:
        groups = get_recent(minutes=30)
        result = []
        for key, info in groups.items():
            result.append({
                "agent_hash": info.get("agent_hash"),
                "channel": info.get("channel"),
                "active_requests": info.get("count", 0),
                "last_started_at": info.get("last_at", 0),
            })
        # 按活跃请求数降序
        result.sort(key=lambda x: x["active_requests"], reverse=True)
        return result
    except Exception as e:
        logger.warning(f"[metrics] active_agents failed: {e}")
        return []