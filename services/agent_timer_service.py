"""
Agent 级独立定时器服务
每个 Agent 拥有独立的 APScheduler CronTrigger 任务
"""

import logging
from typing import Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# 全局调度器（与 schtasks 共用）
_scheduler: Optional[AsyncIOScheduler] = None
# Agent 定时器注册表: {agent_hash: job_id}
_agent_timers: Dict[str, str] = {}

# 默认间隔（秒）
DEFAULT_SYNC_MEMORY_INTERVAL = 900  # 15分钟


def set_scheduler(scheduler: AsyncIOScheduler) -> None:
    """设置全局调度器引用"""
    global _scheduler
    _scheduler = scheduler


def ensure_agent_timer(agent_hash: str, interval: int = DEFAULT_SYNC_MEMORY_INTERVAL) -> bool:
    """
    确保 Agent 有独立的定时器

    Args:
        agent_hash: Agent 的 4 位 hash
        interval: 执行间隔（秒），可通过 config 配置

    Returns:
        是否成功
    """
    global _agent_timers

    if not _scheduler or not _scheduler.running:
        logger.warning("Scheduler not running, cannot start agent timer")
        return False

    # 已存在则更新 last_active
    if agent_hash in _agent_timers:
        return True

    job_id = f"agent_sync_memory_{agent_hash}"

    try:
        _scheduler.add_job(
            _execute_agent_sync_memory,
            IntervalTrigger(seconds=interval),
            id=job_id,
            args=[agent_hash],
            replace_existing=True,
            misfire_grace_time=30,
        )
        _agent_timers[agent_hash] = job_id
        logger.info(f"[AgentTimer] Started timer for agent {agent_hash} (interval={interval}s)")
        return True
    except Exception as e:
        logger.error(f"[AgentTimer] Failed to start timer for {agent_hash}: {e}")
        return False


def stop_agent_timer(agent_hash: str) -> bool:
    """停止 Agent 的定时器"""
    global _agent_timers

    job_id = _agent_timers.pop(agent_hash, None)
    if job_id and _scheduler:
        try:
            _scheduler.remove_job(job_id)
            logger.info(f"[AgentTimer] Stopped timer for agent {agent_hash}")
            return True
        except Exception as e:
            logger.warning(f"[AgentTimer] Error stopping timer for {agent_hash}: {e}")
    return False


def _execute_agent_sync_memory(agent_hash: str):
    """执行 Agent 记忆同步（仅同步当前 Agent 对应用户的记忆，而非遍历所有用户）"""
    from services.workspace_service import sync_daily_memory_to_memory
    from models.database import SessionLocal, AgentProfile
    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        if not agent:
            logger.warning(f"[AgentTimer] Agent {agent_hash} not found, skipping memory sync")
            return
        user_id = str(agent.user_id)
        result = sync_daily_memory_to_memory(user_id, db)
        logger.info(f"[AgentTimer] Memory synced for agent {agent_hash} (user {user_id}): {result.get('status')}")
    except Exception as e:
        logger.error(f"[AgentTimer] Error syncing memory for {agent_hash}: {e}")
    finally:
        db.close()


def stop_all_agent_timers():
    """停止所有 Agent 定时器（服务关闭时调用）"""
    for agent_hash in list(_agent_timers.keys()):
        stop_agent_timer(agent_hash)
