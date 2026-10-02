"""
健康检查 API 测试

Q20/H15 + Q21/L11 之后：/api/health/backend 与 /api/heartbeat/stats 均要求
管理员 JWT。匿名访问必须 401（fail-closed），不再是原来的公开 200。
本文件原样保留旧的匿名 200 断言会与安全修复冲突，故同步为「匿名 ⇒ 401」。
"""

from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


class TestHealthAPI:
    """健康检查 API 测试（管理员鉴权后：匿名 ⇒ 401）"""

    def test_backend_health_endpoint_exists(self):
        """后端健康检查端点存在（匿名 ⇒ 401 而非 404）"""
        response = client.get("/api/health/backend")
        assert response.status_code == 401

    def test_backend_health_requires_admin(self):
        """匿名访问后端健康检查 ⇒ 401（Q20/H15）"""
        assert client.get("/api/health/backend").status_code == 401

    def test_backend_health_include_details_requires_admin(self):
        """带 include_details 也需管理员（匿名 ⇒ 401）"""
        assert client.get("/api/health/backend?include_details=true").status_code == 401

    def test_backend_health_custom_url_requires_admin(self):
        """带自定义 backend_url 也需管理员（匿名 ⇒ 401）"""
        assert client.get("/api/health/backend?backend_url=https://example.com/health").status_code == 401

    def test_heartbeat_stats_endpoint_exists(self):
        """心跳统计端点存在（匿名 ⇒ 401 而非 404）"""
        response = client.get("/api/heartbeat/stats")
        assert response.status_code == 401

    def test_heartbeat_stats_requires_admin(self):
        """匿名访问心跳统计 ⇒ 401（Q21/L11）"""
        assert client.get("/api/heartbeat/stats").status_code == 401
