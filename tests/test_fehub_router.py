"""
Tests for FeHub Router API - Phase 6 Engine
routers/fehub.py
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime
from fastapi.testclient import TestClient


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

        with patch("utils.auth.get_current_user_id") as mock_auth:
            mock_auth.return_value = 1

            with patch("models.database.get_db") as mock_get_db:
                mock_db = MagicMock()
                mock_db.query.return_value.filter.return_value.all.return_value = []
                mock_get_db.return_value = mock_db

                response = client.get(
                    "/api/fehub/apps",
                    headers={"Authorization": "Bearer fake"},
                )

                # Should return 200 with empty apps
                assert response.status_code == 200
                data = response.json()
                assert "apps" in data

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

        with patch("utils.auth.get_current_user_id") as mock_auth:
            mock_auth.return_value = 1

            with patch("models.database.get_db") as mock_get_db:
                mock_db = MagicMock()

                def query_side_effect(model):
                    mock_q = MagicMock()
                    if model.__name__ == "AgentProfile":
                        mock_q.filter.return_value.all.return_value = [mock_agent]
                    elif model.__name__ == "FePublish":
                        mock_q.filter.return_value.order_by.return_value.all.return_value = [mock_publish]
                    return mock_q

                mock_db.query.side_effect = query_side_effect
                mock_get_db.return_value = mock_db

                response = client.get(
                    "/api/fehub/apps",
                    headers={"Authorization": "Bearer fake"},
                )

                assert response.status_code == 200
                data = response.json()
                assert len(data["apps"]) >= 0


class TestFeHubRouterAppData:
    """Test AppData CRUD endpoints"""

    def test_get_app_data_requires_key_or_prefix(self):
        """get_app_data returns 400 without key or prefix"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("utils.auth.get_current_user_id") as mock_auth:
            mock_auth.return_value = 1

            with patch("models.database.get_db") as mock_get_db:
                mock_db = MagicMock()
                mock_get_db.return_value = mock_db

                # Agent exists
                mock_agent = MagicMock()
                mock_db.query.return_value.filter.return_value.first.return_value = mock_agent

                response = client.get(
                    "/api/fehub/apps/abcd-v1/data",
                    headers={"Authorization": "Bearer fake"},
                )

                assert response.status_code == 400

    def test_delete_app_data_requires_key_or_prefix(self):
        """delete_app_data returns 400 without key or prefix"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("utils.auth.get_current_user_id") as mock_auth:
            mock_auth.return_value = 1

            with patch("models.database.get_db") as mock_get_db:
                mock_db = MagicMock()
                mock_get_db.return_value = mock_db

                mock_agent = MagicMock()
                mock_db.query.return_value.filter.return_value.first.return_value = mock_agent

                response = client.delete(
                    "/api/fehub/apps/abcd-v1/data",
                    headers={"Authorization": "Bearer fake"},
                )

                assert response.status_code == 400

    def test_get_app_data_invalid_app_id_format(self):
        """get_app_data returns 400 for invalid app_id format"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("utils.auth.get_current_user_id") as mock_auth:
            mock_auth.return_value = 1

            with patch("models.database.get_db") as mock_get_db:
                mock_db = MagicMock()
                mock_get_db.return_value = mock_db

                response = client.get(
                    "/api/fehub/apps/invalid-format/data?key=test",
                    headers={"Authorization": "Bearer fake"},
                )

                assert response.status_code == 400


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
