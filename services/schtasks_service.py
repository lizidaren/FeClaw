"""
平台级定时任务服务（原名 HeartbeatService）
"""

import logging
import threading
from datetime import datetime
from typing import Dict, Any, Optional

from models.database import SessionLocal

logger = logging.getLogger(__name__)


# ============================================================================
# SchtasksService 类 - 平台级定时任务服务
# ============================================================================

class SchtasksService:
    """
    平台级定时任务服务

    提供统一的定时任务执行、健康检查、统计记录功能。
    支持任务超时控制，避免单个任务阻塞整个流程。
    """

    # 默认任务超时（秒）
    DEFAULT_TASK_TIMEOUT = 120

    # 内建任务列表
    DEFAULT_TASKS = [
        {"name": "health_check", "description": "检查服务健康状态"},
        {"name": "cleanup", "description": "清理过期数据"},
        {"name": "cleanup_stale_sandbox_fuses", "description": "清理30分钟未活跃的 FUSE 实例"},
    ]

    def __init__(self):
        """初始化定时任务服务"""
        self.task_timeout = self.DEFAULT_TASK_TIMEOUT
        self._last_run_stats: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------------
    # 核心执行方法
    # ------------------------------------------------------------------------

    def execute_heartbeat_tasks(self) -> Dict[str, Any]:
        """
        执行定时任务列表（带超时控制）

        Returns:
            执行结果
        """
        start_time = datetime.now()
        logger.info(f"[Schtasks] Starting tasks at {start_time.isoformat()}")

        results = {
            "executed": 0,
            "succeeded": 0,
            "failed": 0,
            "timed_out": 0,
            "tasks": [],
            "duration_ms": 0
        }

        for task in self.DEFAULT_TASKS:
            task_name = task.get("name", "unknown")
            task_desc = task.get("description", "")

            logger.info(f"[Schtasks] Executing task: {task_name} - {task_desc}")

            task_result = self._execute_single_task_with_timeout(task_name)
            results["executed"] += 1

            if task_result["status"] == "success":
                results["succeeded"] += 1
            elif task_result["status"] == "timeout":
                results["timed_out"] += 1
                results["failed"] += 1
            else:
                results["failed"] += 1

            results["tasks"].append({
                "name": task_name,
                "status": task_result["status"],
                "duration_ms": task_result.get("duration_ms", 0),
                "error": task_result.get("error")
            })

        end_time = datetime.now()
        results["duration_ms"] = int((end_time - start_time).total_seconds() * 1000)

        logger.info(f"[Schtasks] Completed: executed={results['executed']}, "
              f"succeeded={results['succeeded']}, failed={results['failed']}, "
              f"timed_out={results['timed_out']}, duration={results['duration_ms']}ms")

        self._last_run_stats = results
        return results

    def _execute_single_task_with_timeout(self, task_name: str) -> Dict[str, Any]:
        """
        带超时控制的单个任务执行

        Args:
            task_name: 任务名称

        Returns:
            执行结果字典
        """
        start_time = datetime.now()
        task_result: bool = False
        task_exception: Optional[str] = None

        def run_task():
            nonlocal task_result, task_exception
            try:
                task_result = self._execute_single_task(task_name)
            except Exception as e:
                task_exception = str(e)

        thread = threading.Thread(target=run_task)
        thread.daemon = True
        thread.start()
        thread.join(timeout=self.task_timeout)

        end_time = datetime.now()
        duration_ms = int((end_time - start_time).total_seconds() * 1000)

        if thread.is_alive():
            # 任务超时
            logger.warning(f"[Schtasks] Task {task_name} timed out after {self.task_timeout}s")
            return {
                "status": "timeout",
                "duration_ms": duration_ms,
                "error": f"Task exceeded timeout of {self.task_timeout}s"
            }

        if task_exception:
            return {
                "status": "error",
                "duration_ms": duration_ms,
                "error": task_exception
            }

        # 任务完成
        return {
            "status": "success" if task_result else "failed",
            "duration_ms": duration_ms
        }

    def _execute_single_task(self, task_name: str) -> bool:
        """
        执行单个定时任务（内部方法）

        Args:
            task_name: 任务名称

        Returns:
            是否成功
        """
        if task_name == "health_check":
            return _health_check_task()
        elif task_name == "cleanup":
            return _cleanup_task()
        elif task_name == "cleanup_stale_sandbox_fuses":
            result = _cleanup_stale_fuses_task()
            return result.get("status") == "success"
        else:
            logger.warning(f"[Schtasks] Unknown task: {task_name}")
            return False

    def get_last_run_stats(self) -> Optional[Dict[str, Any]]:
        """获取上次运行统计"""
        return self._last_run_stats

    def get_task_summary(self) -> str:
        """
        获取定时任务执行摘要

        Returns:
            格式化的任务执行摘要字符串，用于日志或记录
        """
        if not self._last_run_stats:
            return "No schtasks stats available"

        stats = self._last_run_stats
        lines = []

        # 整体状态
        total = stats.get("executed", 0)
        succeeded = stats.get("succeeded", 0)
        failed = stats.get("failed", 0)
        timed_out = stats.get("timed_out", 0)
        duration_ms = stats.get("duration_ms", 0)

        lines.append(f"Tasks: {succeeded}/{total} succeeded")
        if failed > 0:
            lines.append(f"Failed: {failed}")
        if timed_out > 0:
            lines.append(f"Timed out: {timed_out}")
        lines.append(f"Duration: {duration_ms}ms")

        # 详细任务状态
        tasks = stats.get("tasks", [])
        if tasks:
            task_details = []
            for task in tasks:
                name = task.get("name", "unknown")
                status = task.get("status", "unknown")
                duration = task.get("duration_ms", 0)
                error = task.get("error")

                status_icon = "✅" if status == "success" else "❌" if status in ("error", "failed") else "⏱️" if status == "timeout" else "❓"
                detail = f"{status_icon} {name} ({duration}ms)"
                if error:
                    detail += f" - {error[:50]}..."
                task_details.append(detail)

            lines.append("Details: " + " | ".join(task_details))

        return " | ".join(lines)

    def check_backend_health(
        self,
        backend_url: Optional[str] = None,
        timeout_seconds: int = 5,
        include_details: bool = False
    ) -> Dict[str, Any]:
        """
        检查后端服务健康状态

        这是一个公共 API 方法，供外部系统（监控面板、API调用）使用。
        与内部 _health_check_task() 不同，这个方法返回详细的健康报告。

        Args:
            backend_url: 后端健康检查 URL，如果为 None 则依次尝试本地和远程
            timeout_seconds: 单个检查的超时时间（秒）
            include_details: 是否包含详细的检查项结果

        Returns:
            健康报告字典，包含：
            - status: "healthy" / "unhealthy" / "degraded" / "error"
            - backend: {"url", "response", "status", "duration_ms"}
            - database: {"status", "duration_ms"}（仅当 include_details=True）
            - scheduler: {"status", "running"}（仅当 include_details=True）
            - timestamp: ISO 格式的时间戳
            - duration_ms: 总检查耗时
            - details: 详细检查项列表（仅当 include_details=True）
        """
        import subprocess
        import time

        start_time = time.time()
        timestamp = datetime.now().isoformat()

        report = {
            "status": "unhealthy",
            "backend": {
                "url": backend_url or "auto",
                "response": None,
                "status": "unknown",
                "duration_ms": 0
            },
            "timestamp": timestamp,
            "duration_ms": 0
        }

        if include_details:
            report["database"] = {"status": "unknown", "duration_ms": 0}
            report["scheduler"] = {"status": "unknown", "running": False}
            report["details"] = []

        # 1. 检查后端 API 响应
        urls_to_check = []
        if backend_url:
            urls_to_check = [backend_url]
        else:
            # 默认：先检查本地，再检查远程
            urls_to_check = [
                "http://localhost:8080/health",
                "http://localhost:8080/health"  # 默认本地健康检查，生产环境建议改为实际域名
            ]

        backend_healthy = False
        for url in urls_to_check:
            try:
                check_start = time.time()
                response = subprocess.run(
                    ["curl", "-s", "--max-time", str(timeout_seconds), url],
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds + 1
                )
                check_duration = int((time.time() - check_start) * 1000)

                response_text = response.stdout.strip()
                if "healthy" in response_text.lower() or response_text == "":
                    backend_healthy = True
                    report["backend"]["url"] = url
                    report["backend"]["response"] = response_text[:100] if response_text else "OK"
                    report["backend"]["status"] = "healthy"
                    report["backend"]["duration_ms"] = check_duration
                    break
                else:
                    report["backend"]["url"] = url
                    report["backend"]["response"] = response_text[:100]
                    report["backend"]["status"] = "unhealthy"
                    report["backend"]["duration_ms"] = check_duration

            except subprocess.TimeoutExpired:
                report["backend"]["status"] = "timeout"
                report["backend"]["duration_ms"] = timeout_seconds * 1000
            except Exception as e:
                report["backend"]["status"] = "error"
                report["backend"]["response"] = str(e)[:100]

        # 2. 检查数据库连接（如果 include_details=True）
        if include_details:
            try:
                db_start = time.time()
                db = SessionLocal()
                from sqlalchemy import text
                db.execute(text("SELECT 1"))
                db.close()
                db_duration = int((time.time() - db_start) * 1000)

                report["database"]["status"] = "healthy"
                report["database"]["duration_ms"] = db_duration
                report["details"].append({
                    "name": "database",
                    "status": "healthy",
                    "duration_ms": db_duration
                })
            except Exception as e:
                report["database"]["status"] = "error"
                report["database"]["error"] = str(e)[:100]
                report["details"].append({
                    "name": "database",
                    "status": "error",
                    "error": str(e)[:100]
                })

            # 3. 检查调度器状态
            try:
                from services.scheduler_service import _scheduler
                if _scheduler and _scheduler.running:
                    report["scheduler"]["status"] = "healthy"
                    report["scheduler"]["running"] = True
                    report["details"].append({
                        "name": "scheduler",
                        "status": "healthy",
                        "running": True
                    })
                else:
                    report["scheduler"]["status"] = "not_running"
                    report["scheduler"]["running"] = False
                    report["details"].append({
                        "name": "scheduler",
                        "status": "not_running",
                        "running": False
                    })
            except ImportError:
                report["scheduler"]["status"] = "not_available"
                report["scheduler"]["running"] = False
                report["details"].append({
                    "name": "scheduler",
                    "status": "not_available"
                })

        # 计算总耗时和最终状态
        total_duration = int((time.time() - start_time) * 1000)
        report["duration_ms"] = total_duration

        # 确定最终状态
        if backend_healthy:
            if include_details:
                # 如果有详细检查，综合判断
                db_ok = report["database"]["status"] == "healthy"
                scheduler_ok = report["scheduler"]["status"] in ("healthy", "not_available")

                if db_ok and scheduler_ok:
                    report["status"] = "healthy"
                elif db_ok or backend_healthy:
                    report["status"] = "degraded"
                else:
                    report["status"] = "unhealthy"
            else:
                report["status"] = "healthy"
        else:
            report["status"] = "unhealthy"

        logger.info(f"[Schtasks] Backend health: {report['status']} ({total_duration}ms)")
        return report

    def get_backend_health_summary(self) -> str:
        """
        获取后端健康摘要字符串

        用于日志输出或心跳记录。

        Returns:
            格式化的健康摘要，如 "healthy (backend OK, 23ms)"
        """
        report = self.check_backend_health(include_details=False)

        status = report["status"]
        backend_status = report["backend"]["status"]
        duration = report["duration_ms"]

        return f"{status} (backend {backend_status}, {duration}ms)"


# ============================================================================
# 内部任务实现（保持原有逻辑不变）
# ============================================================================

def _sync_memory_task() -> bool:
    """同步每日记忆到长期记忆"""
    try:
        from services.workspace_service import sync_daily_memory_to_memory

        db = SessionLocal()
        try:
            from models.database import User
            users = db.query(User).all()
            for user in users:
                try:
                    result = sync_daily_memory_to_memory(str(user.id), db)
                    logger.info(f"[Schtasks] Synced memory for user {user.id}: {result.get('status')}")
                except Exception as e:
                    logger.error(f"[Schtasks] Error syncing memory for user {user.id}: {e}")
            return True
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Schtasks] sync_memory error: {e}")
        return False


def _health_check_task() -> bool:
    """检查服务健康状态"""
    try:
        # 检查数据库连接
        db = SessionLocal()
        try:
            from sqlalchemy import text
            db.execute(text("SELECT 1"))
            logger.info("[Schtasks] Database: OK")
        finally:
            db.close()

        # 检查调度器状态
        try:
            from services.scheduler_service import _scheduler
            if _scheduler and _scheduler.running:
                logger.info("[Schtasks] Scheduler: OK")
            else:
                logger.warning("[Schtasks] Scheduler: Not running")
        except ImportError:
            logger.warning("[Schtasks] Scheduler: not available")

        return True
    except Exception as e:
        logger.error(f"[Schtasks] health_check error: {e}")
        return False


def _cleanup_task() -> bool:
    """清理过期数据"""
    try:
        from datetime import timedelta
        from models.database import ScheduledTask, cleanup_expired_share_mappings

        db = SessionLocal()
        try:
            # 清理已完成超过7天的任务
            cutoff = datetime.now() - timedelta(days=7)
            deleted_tasks = db.query(ScheduledTask).filter(
                ScheduledTask.status.in_(["done", "sent", "cancelled"]),
                ScheduledTask.created_at < cutoff
            ).delete()
            db.commit()
            logger.info(f"[Schtasks] Cleanup: deleted {deleted_tasks} old tasks")

            # 清理过期的分享链接
            deleted_shares = cleanup_expired_share_mappings(db)
            if deleted_shares > 0:
                logger.info(f"[Schtasks] Cleanup: deleted {deleted_shares} expired share mappings")

            return True
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Schtasks] cleanup error: {e}")
        return False


def _cleanup_stale_fuses_task() -> Dict:
    """清理僵尸 sandbox FUSE"""
    from services.vfs_fuse_daemon import cleanup_stale_fuses
    count = cleanup_stale_fuses(timeout=1800)
    return {"status": "success", "message": f"cleaned {count} stale FUSE mounts"}
