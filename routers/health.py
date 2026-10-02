"""
健康检查 API 路由
提供系统健康状态的 REST API 端点
"""

from fastapi import APIRouter, Query, Depends, HTTPException
from typing import Optional

from services.heartbeat_service import HeartbeatService
from utils.auth_dependencies import get_admin_user
from models.database import User

router = APIRouter(prefix="/api", tags=["Health"])


# 创建全局 HeartbeatService 实例
_heartbeat_service = HeartbeatService()


@router.get("/health/backend")
async def get_backend_health(
    backend_url: Optional[str] = Query(None, description="自定义后端 URL"),
    include_details: bool = Query(False, description="是否包含详细检查项"),
    timeout_seconds: int = Query(5, description="检查超时时间（秒）"),
    user: User = Depends(get_admin_user),
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
    # Q20/H15：即使加了管理员鉴权，仍校验自定义 backend_url（防 SSRF + curl 参数注入）
    if backend_url:
        from utils.url_validation import validate_public_http_url
        if not validate_public_http_url(backend_url):
            raise HTTPException(status_code=400, detail="backend_url 非法（仅允许公网 http/https）")

    report = _heartbeat_service.check_backend_health(
        backend_url=backend_url,
        timeout_seconds=timeout_seconds,
        include_details=include_details
    )
    return report


@router.get("/heartbeat/stats")
async def get_heartbeat_stats(user: User = Depends(get_admin_user)) -> dict:
    """
    心跳执行统计（Q21/L11：加管理员鉴权，匿名不再可读心跳任务明细）

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