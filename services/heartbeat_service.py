"""
心跳服务 - 向后兼容别名

所有功能已迁移至 services.schtasks_service，本模块保留别名以确保向后兼容。
"""

import logging

logger = logging.getLogger(__name__)

# 从 schtasks_service 重新导出所有符号
from services.schtasks_service import (  # noqa: F401, E402
    SchtasksService,
    SchtasksService as HeartbeatService,
    _sync_memory_task,
    _health_check_task,
    _cleanup_task,
    _cleanup_stale_fuses_task,
)
