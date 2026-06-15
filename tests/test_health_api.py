"""
健康检查 API 测试
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from main import app
from services.heartbeat_service import HeartbeatService


client = TestClient(app)


class TestHealthAPI:
    """健康检查 API 测试"""

    def test_backend_health_endpoint_exists(self):
        """测试后端健康检查端点存在"""
        response = client.get("/api/health/backend")
        # 不应该返回 404
        assert response.status_code != 404

    def test_backend_health_returns_dict(self):
        """测试后端健康检查返回字典"""
        response = client.get("/api/health/backend")
        assert response.status_code == 200
        data = response.json()

        assert isinstance(data, dict)
        assert "status" in data
        assert "backend" in data
        assert "timestamp" in data
        assert "duration_ms" in data

    def test_backend_health_valid_status(self):
        """测试后端健康状态值有效"""
        response = client.get("/api/health/backend")
        data = response.json()

        valid_statuses = ["healthy", "unhealthy", "degraded", "error"]
        assert data["status"] in valid_statuses

    def test_backend_health_include_details(self):
        """测试 include_details 参数"""
        response = client.get("/api/health/backend?include_details=true")
        data = response.json()

        # 应该包含详细信息
        assert "database" in data
        assert "scheduler" in data
        assert "details" in data

    def test_backend_health_no_details_by_default(self):
        """测试默认不包含详细信息"""
        response = client.get("/api/health/backend")
        data = response.json()

        # 不应该包含详细信息
        assert "database" not in data
        assert "scheduler" not in data
        assert "details" not in data

    def test_backend_health_custom_url(self):
        """测试自定义 URL 参数"""
        response = client.get("/api/health/backend?backend_url=https://example.com/health")
        data = response.json()

        assert data["backend"]["url"] == "https://example.com/health"

    def test_heartbeat_stats_endpoint_exists(self):
        """测试心跳统计端点存在"""
        response = client.get("/api/heartbeat/stats")
        # 不应该返回 404
        assert response.status_code != 404

    def test_heartbeat_stats_returns_dict(self):
        """测试心跳统计返回字典"""
        response = client.get("/api/heartbeat/stats")
        assert response.status_code == 200
        data = response.json()

        assert isinstance(data, dict)
        assert "status" in data
