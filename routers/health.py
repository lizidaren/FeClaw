"""
健康检查 API 路由
提供系统健康状态的 REST API 端点
"""

from fastapi import APIRouter, Query
from typing import Optional

from services.heartbeat_service import HeartbeatService

router = APIRouter(prefix="/api", tags=["Health"])


# 创建全局 HeartbeatService 实例
_heartbeat_service = HeartbeatService()


@router.get("/health/backend")
async def get_backend_health(
    backend_url: Optional[str] = Query(None, description="自定义后端 URL"),
    include_details: bool = Query(False, description="是否包含详细检查项"),
    timeout_seconds: int = Query(5, description="检查超时时间（秒）")
):
    """
    后端健康检查
    
    返回后端服务的健康状态，包括：
    - 后端 API 响应状态
    - 数据库连接状态（include_details=True）
    - 调度器状态（include_details=True）
    
    **状态值：**
    - healthy: 所有组件正常
    - unhealthy: 主要组件异常
    - degraded: 部分组件异常但仍可用
    - error: 检查过程出错
    """
    report = _heartbeat_service.check_backend_health(
        backend_url=backend_url,
        timeout_seconds=timeout_seconds,
        include_details=include_details
    )
    return report


@router.get("/heartbeat/stats")
async def get_heartbeat_stats() -> dict:
    """
    心跳执行统计
    
    返回最近一次心跳任务执行的统计信息，包括：
    - 执行的任务数
    - 成功/失败/超时数
    - 每个任务的执行结果
    - 总耗时
    """
    stats = _heartbeat_service.get_last_run_stats()
    
    if not stats:
        return {
            "status": "no_data",
            "message": "No heartbeat stats available",
            "summary": _heartbeat_service.get_task_summary()
        }
    
    return {
        "status": "available",
        "stats": stats,
        "summary": _heartbeat_service.get_task_summary()
    }