"""
Tests for FeHub Router API - Phase 6 Engine
routers/fehub.py

注：`get_db` / `get_current_user_id` 在 routers/fehub.py 是模块级 `from ... import`，
patch 源模块（utils.auth / models.database）不会影响已经绑定的依赖对象。
FastAPI 的正解是 `app.dependency_overrides[...]`，本文件统一用它注入用户与 DB。
"""

import pytest
from contextlib import contextmanager
from unittest.mock import MagicMock
from datetime import datetime
from fastapi.testclient import TestClient


@contextmanager
def _fehub_deps(session=None, user_id=1):
    """覆盖 FeHub 路由的两个依赖：登录用户 + 数据库会话。

    用法：
        with _fehub_deps() as mock_db:
            mock_db.query.return_value... = ...
    """
    from main import app
    from models.database import get_db
    from utils.auth import get_current_user_id

    db = session if session is not None else MagicMock()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
        app.dependency_overrides.pop(get_db, None)


class TestFeHubRouterAuth:
    """Test auth requirements for FeHub endpoints"""

    def test_list_apps_without_auth_returns_401(self):
        """GET /api/fehub/apps without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/fehub/apps")

        assert response.status_code in [401, 403]

    def test_get_app_without_auth_returns_401(self):
        """GET /api/fehub/apps/{id} without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/fehub/apps/some-app-id")

        assert response.status_code in [401, 403]

    def test_set_app_data_without_auth_returns_401(self):
        """POST /api/fehub/apps/{id}/data without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/fehub/apps/some-app-id/data",
            json={"key": "test", "value": {}},
        )

        assert response.status_code in [401, 403]

    def test_get_app_data_without_auth_returns_401(self):
        """GET /api/fehub/apps/{id}/data without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/fehub/apps/some-app-id/data?key=test")

        assert response.status_code in [401, 403]

    def test_delete_app_data_without_auth_returns_401(self):
        """DELETE /api/fehub/apps/{id}/data without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/api/fehub/apps/some-app-id/data?key=test")

        assert response.status_code in [401, 403]


class TestFeHubRouterListApps:
    """Test GET /api/fehub/apps"""

    def test_list_apps_empty(self):
        """list_apps returns empty when no apps"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _fehub_deps() as mock_db:
            mock_db.query.return_value.filter.return_value.all.return_value = []

            response = client.get("/api/fehub/apps")

            # Should return 200 with empty apps
            assert response.status_code == 200
            data = response.json()
            assert data["apps"] == []

    def test_list_apps_with_publishes(self):
        """list_apps returns published apps"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        mock_agent = MagicMock()
        mock_agent.hash = "abcd"

        mock_publish = MagicMock()
        mock_publish.id = "pub-123"
        mock_publish.agent_hash = "abcd"
        mock_publish.app_name = "TestApp"
        mock_publish.tag = "v1"
        mock_publish.is_public = False
        mock_publish.created_at = datetime(2024, 1, 1)

        with _fehub_deps() as mock_db:
            def query_side_effect(model):
                mock_q = MagicMock()
                if model.__name__ == "AgentProfile":
                    mock_q.filter.return_value.all.return_value = [mock_agent]
                elif model.__name__ == "FePublish":
                    mock_q.filter.return_value.order_by.return_value.all.return_value = [mock_publish]
                return mock_q

            mock_db.query.side_effect = query_side_effect

            response = client.get("/api/fehub/apps")

            assert response.status_code == 200
            data = response.json()
            assert len(data["apps"]) == 1
            assert data["apps"][0]["app_id"] == "abcd-v1"


class TestFeHubRouterAppData:
    """Test AppData CRUD endpoints"""

    def test_get_app_data_requires_key_or_prefix(self):
        """get_app_data returns 400 without key or prefix"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _fehub_deps() as mock_db:
            # 归属校验 agent_belongs_to_user 比较 str(agent.user_id) == str(user_id)
            mock_agent = MagicMock()
            mock_agent.user_id = 1
            mock_db.query.return_value.filter.return_value.first.return_value = mock_agent

            response = client.get("/api/fehub/apps/abcd-v1/data")

            assert response.status_code == 400
            assert "key" in response.json()["detail"]

    def test_delete_app_data_requires_key_or_prefix(self):
        """delete_app_data returns 400 without key or prefix"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _fehub_deps() as mock_db:
            mock_agent = MagicMock()
            mock_agent.user_id = 1
            mock_db.query.return_value.filter.return_value.first.return_value = mock_agent

            response = client.delete("/api/fehub/apps/abcd-v1/data")

            assert response.status_code == 400
            assert "key" in response.json()["detail"]

    def test_get_app_data_invalid_app_id_format(self):
        """get_app_data returns 400 for invalid app_id format

        app_id 契约是 `agent_hash-tag`，`rsplit("-", 1)` 后必须正好两段；
        因此「无效格式」= 完全不含 '-' 的 app_id（含 '-' 的会被拆成两段放行）。
        """
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _fehub_deps() as mock_db:
            response = client.get("/api/fehub/apps/invalidformat/data?key=test")

            assert response.status_code == 400
            assert "Invalid app_id" in response.json()["detail"]


class TestFeHubRouterMiniAppSDK:
    """Test MiniApp JS SDK compatible endpoints"""

    def test_miniapp_set_data_requires_auth(self):
        """POST /apps/{hash}/{id}/data requires auth"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/apps/abcd/app-id/data",
            json={"key": "test", "value": {}},
        )

        assert response.status_code in [401, 403]

    def test_miniapp_get_data_requires_auth(self):
        """GET /apps/{hash}/{id}/data requires auth"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/apps/abcd/app-id/data?key=test")

        assert response.status_code in [401, 403]

    def test_miniapp_delete_data_requires_auth(self):
        """DELETE /apps/{hash}/{id}/data requires auth"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/apps/abcd/app-id/data?key=test")

        assert response.status_code in [401, 403]
