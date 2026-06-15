"""
HeartbeatService 单元测试
"""

import pytest
from datetime import datetime
from unittest.mock import patch

from services.heartbeat_service import HeartbeatService


class TestHeartbeatTaskSummary:
    """心跳任务摘要测试"""

    def test_get_task_summary_no_stats(self):
        """测试没有统计时返回默认消息"""
        service = HeartbeatService()
        summary = service.get_task_summary()
        assert summary == "No schtasks stats available"

    def test_get_task_summary_with_stats(self):
        """测试有统计时的摘要格式"""
        service = HeartbeatService()
        service._last_run_stats = {
            "executed": 3,
            "succeeded": 2,
            "failed": 1,
            "timed_out": 0,
            "duration_ms": 150,
            "tasks": [
                {"name": "sync_memory", "status": "success", "duration_ms": 50},
                {"name": "health_check", "status": "success", "duration_ms": 30},
                {"name": "cleanup", "status": "failed", "duration_ms": 70, "error": "Some error"}
            ]
        }

        summary = service.get_task_summary()

        assert "2/3 succeeded" in summary
        assert "150ms" in summary
        assert "sync_memory" in summary
        assert "✅" in summary
        assert "❌" in summary


class TestBackendHealthCheck:
    """后端健康检查测试"""

    def test_check_backend_health_returns_dict(self):
        """测试 check_backend_health 返回字典"""
        service = HeartbeatService()
        report = service.check_backend_health()

        assert isinstance(report, dict)
        assert "status" in report
        assert "backend" in report
        assert "timestamp" in report
        assert "duration_ms" in report

        # backend 字段结构
        assert "url" in report["backend"]
        assert "status" in report["backend"]
        assert "duration_ms" in report["backend"]

    def test_check_backend_health_default_urls(self):
        """测试默认 URL 检查逻辑"""
        service = HeartbeatService()
        report = service.check_backend_health()

        # 默认应该尝试本地或远程
        assert report["backend"]["url"] in [
            "http://localhost:8080/health",
            "https://feclaw.chat/health",
            "auto"
        ]

    def test_check_backend_health_custom_url(self):
        """测试自定义 URL"""
        service = HeartbeatService()
        report = service.check_backend_health(backend_url="https://example.com/health")

        assert report["backend"]["url"] == "https://example.com/health"

    def test_check_backend_health_include_details(self):
        """测试 include_details=True 时返回详细信息"""
        service = HeartbeatService()
        report = service.check_backend_health(include_details=True)

        assert "database" in report
        assert "scheduler" in report
        assert "details" in report
        assert isinstance(report["details"], list)

        # database 字段结构
        assert "status" in report["database"]

        # scheduler 字段结构
        assert "status" in report["scheduler"]
        assert "running" in report["scheduler"]

    def test_check_backend_health_no_details_by_default(self):
        """测试默认不返回详细信息"""
        service = HeartbeatService()
        report = service.check_backend_health(include_details=False)

        # 不应该包含详细信息
        assert "database" not in report
        assert "scheduler" not in report
        assert "details" not in report

    def test_check_backend_health_status_values(self):
        """测试状态值有效性"""
        service = HeartbeatService()
        report = service.check_backend_health()

        # status 应该是有效值之一
        valid_statuses = ["healthy", "unhealthy", "degraded", "error"]
        assert report["status"] in valid_statuses

        # backend.status 应该是有效值之一
        valid_backend_statuses = ["healthy", "unhealthy", "timeout", "error", "unknown"]
        assert report["backend"]["status"] in valid_backend_statuses

    def test_get_backend_health_summary(self):
        """测试后端健康摘要格式"""
        service = HeartbeatService()

        # Mock check_backend_health 返回健康状态
        with patch.object(service, 'check_backend_health', return_value={
            "status": "healthy",
            "backend": {"url": "test", "status": "healthy", "duration_ms": 100},
            "timestamp": "2026-05-10T04:00:00",
            "duration_ms": 123
        }):
            summary = service.get_backend_health_summary()
            assert "healthy" in summary
            assert "backend" in summary

    def test_get_backend_health_summary_unhealthy(self):
        """测试不健康状态的摘要"""
        service = HeartbeatService()

        with patch.object(service, 'check_backend_health', return_value={
            "status": "unhealthy",
            "backend": {"url": "test", "status": "timeout", "response": None, "duration_ms": 5000},
            "timestamp": "2026-05-10T04:00:00",
            "duration_ms": 5000
        }):
            summary = service.get_backend_health_summary()
            assert "unhealthy" in summary
            assert "timeout" in summary
