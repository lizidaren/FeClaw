"""活跃请求跟踪器 — 实时跟踪正在被 LLM 处理的消息"""

import json
import uuid
import time as _time
import logging
from typing import Optional, List, Dict, Any

from config import settings

logger = logging.getLogger(__name__)

# Redis key 前缀
_PREFIX = "feclaw:active:"
_ACTIVE_TTL = 180  # 活跃 TTL 秒（最多看 3 分钟前的）

_redis = None


def _get_redis():
    """惰性初始化 Redis（每次调用 ping 健康检查）"""
    global _redis
    if _redis is None:
        try:
            import redis as _redis_mod
            _redis = _redis_mod.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                username=settings.REDIS_USERNAME or None,
                password=settings.REDIS_PASSWORD or None,
                db=settings.REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=2,
            )
        except Exception as e:
            logger.warning(f"[ActiveTracker] Redis init failed: {e}")
            return None
    # 每次调用 ping 检查连接健康
    try:
        _redis.ping()
    except Exception as e:
        logger.warning(f"[ActiveTracker] Redis ping failed: {e}")
        _redis = None
        return None
    return _redis


def _is_enabled() -> bool:
    return settings.REDIS_ENABLED


def track_start(agent_hash: str, channel: str) -> Optional[str]:
    """记录请求开始，返回 request_id（UUID）"""
    if not _is_enabled():
        return None
    r = _get_redis()
    if not r:
        return None
    request_id = str(uuid.uuid4())
    now = _time.time()
    data = json.dumps({
        "agent_hash": agent_hash,
        "channel": channel,
        "started_at": now,
    })
    key = f"{_PREFIX}{request_id}"
    try:
        r.setex(key, _ACTIVE_TTL, data)
    except Exception as e:
        logger.warning(f"[ActiveTracker] setex failed: {e}")
    return request_id


def track_end(request_id: Optional[str]):
    """记录请求结束（删除活跃记录）"""
    if not _is_enabled() or not request_id:
        return
    r = _get_redis()
    if not r:
        return
    key = f"{_PREFIX}{request_id}"
    try:
        r.delete(key)
    except Exception as e:
        logger.warning(f"[ActiveTracker] delete failed: {e}")


def get_active() -> List[Dict[str, Any]]:
    """获取当前所有活跃请求详情"""
    if not _is_enabled():
        return []
    r = _get_redis()
    if not r:
        return []
    try:
        cursor = 0
        keys = []
        while True:
            cursor, batch = r.scan(cursor, match=f"{_PREFIX}*", count=1000)
            keys.extend(batch)
            if cursor == 0:
                break
        if not keys:
            return []
        results = []
        for key in keys:
            data = r.get(key)
            if data:
                record = json.loads(data)
                record["request_id"] = key.replace(_PREFIX, "")
                results.append(record)
        results.sort(key=lambda x: x.get("started_at", 0), reverse=True)
        return results
    except Exception as e:
        logger.warning(f"[ActiveTracker] get_active failed: {e}")
        return []


def get_recent(minutes: int = 30) -> Dict[str, Dict[str, Any]]:
    """获取最近活跃的 Agent 统计，按 agent_hash:channel 分组"""
    actives = get_active()
    cutoff = _time.time() - minutes * 60
    groups: Dict[str, Dict[str, Any]] = {}
    for req in actives:
        if req.get("started_at", 0) < cutoff:
            continue
        key = f"{req['agent_hash']}:{req['channel']}"
        if key not in groups:
            groups[key] = {
                "agent_hash": req["agent_hash"],
                "channel": req["channel"],
                "count": 0,
                "last_at": 0,
            }
        groups[key]["count"] += 1
        if req.get("started_at", 0) > groups[key]["last_at"]:
            groups[key]["last_at"] = req["started_at"]
    return groups
