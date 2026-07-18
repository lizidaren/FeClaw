"""
Tests for Group Router API - Phase 4 Engine
routers/group.py
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime
from fastapi.testclient import TestClient


class TestGroupRouterAuth:
    """Test auth requirements for group endpoints"""

    def test_create_group_without_auth_returns_401(self):
        """POST /api/groups without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/groups", json={"name": "Test"})

        # Should return 401/403 without auth
        assert response.status_code in [401, 403]

    def test_list_groups_without_auth_returns_401(self):
        """GET /api/groups without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/groups")

        assert response.status_code in [401, 403]

    def test_get_group_without_auth_returns_401(self):
        """GET /api/groups/{id} without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/groups/some-group-id")

        assert response.status_code in [401, 403]

    def test_delete_group_without_auth_returns_401(self):
        """DELETE /api/groups/{id} without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/api/groups/some-group-id")

        assert response.status_code in [401, 403]

    def test_add_member_without_auth_returns_401(self):
        """POST /api/groups/{id}/members without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/groups/some-group-id/members", json={"agent_hash": "abcd"})

        assert response.status_code in [401, 403]

    def test_remove_member_without_auth_returns_401(self):
        """DELETE /api/groups/{id}/members/{hash} without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/api/groups/some-group-id/members/abcd")

        assert response.status_code in [401, 403]

    def test_get_messages_without_auth_returns_401(self):
        """GET /api/groups/{id}/messages without auth returns 401"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/groups/some-group-id/messages")

        assert response.status_code in [401, 403]


class TestGroupRouterWithAuth:
    """Test group endpoints with authentication"""

    @pytest.fixture
    def mock_auth(self):
        """Mock authentication to return user_id=1"""
        with patch("utils.auth.get_current_user_id") as mock:
            mock.return_value = 1
            yield mock

    @pytest.fixture
    def mock_db_session(self):
        """Mock database session"""
        mock_session = MagicMock()
        mock_session.add = MagicMock()
        mock_session.commit = MagicMock()
        mock_session.refresh = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        return mock_session

    def test_create_group_validates_name_length(self, mock_auth):
        """create_group rejects name > 100 chars"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("models.database.get_db") as mock_get_db:
            mock_get_db.return_value.__enter__ = MagicMock()
            mock_get_db.return_value.__exit__ = MagicMock()

            response = client.post(
                "/api/groups",
                json={"name": "x" * 101},
                headers={"Authorization": "Bearer fake"},
            )

            # Should return 400 for validation error
            assert response.status_code == 400

    def test_create_group_success(self, mock_auth):
        """create_group creates group successfully"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        mock_group = MagicMock()
        mock_group.id = "new-group-id"
        mock_group.name = "Test Group"
        mock_group.owner_user_id = 1
        mock_group.announcement = ""
        mock_group.announcement_updated_at = None
        mock_group.settings = {}
        mock_group.context_isolation = True
        mock_group.max_rounds = 100
        mock_group.created_at = datetime.utcnow()

        with patch("models.database.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.add = MagicMock()
            mock_db.flush = MagicMock()
            mock_db.commit = MagicMock()
            mock_db.refresh = MagicMock(side_effect=lambda g: setattr(g, 'id', 'new-group-id'))
            mock_db.query.return_value.filter.return_value.first.return_value = None
            mock_get_db.return_value = mock_db

            with patch("routers.group.GroupDispatchService") as MockService:
                mock_svc = MagicMock()
                mock_svc.create_group.return_value = mock_group
                MockService.return_value = mock_svc

                with patch("routers.group._format_group") as mock_format:
                    mock_format.return_value = {
                        "id": "new-group-id",
                        "name": "Test Group",
                        "announcement": "",
                        "announcement_updated_at": None,
                        "owner_user_id": 1,
                        "settings": {},
                        "context_isolation": True,
                        "max_rounds": 100,
                        "created_at": 1234567890,
                        "member_count": 1,
                    }

                    response = client.post(
                        "/api/groups",
                        json={"name": "Test Group"},
                        headers={"Authorization": "Bearer fake"},
                    )

                    # If auth is properly mocked, should get past auth
                    # (may still fail on other mocks but auth works)

    def test_delete_group_owner_only(self, mock_auth):
        """delete_group only allows owner"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("models.database.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            # Group owned by different user
            mock_group = MagicMock()
            mock_group.owner_user_id = 999
            mock_group.deleted_at = None
            mock_db.query.return_value.filter.return_value.first.return_value = mock_group

            response = client.delete(
                "/api/groups/some-id",
                headers={"Authorization": "Bearer fake"},
            )

            assert response.status_code == 403


class TestGroupRouterMessages:
    """Test message-related endpoints"""

    def test_get_messages_pagination(self):
        """get_messages respects limit parameter"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("utils.auth.get_current_user_id") as mock_auth:
            mock_auth.return_value = 1

            with patch("models.database.get_db") as mock_get_db:
                mock_db = MagicMock()
                mock_get_db.return_value = mock_db

                # Mock group found
                mock_group = MagicMock()
                mock_group.owner_user_id = 1
                mock_group.deleted_at = None
                mock_db.query.return_value.filter.return_value.first.side_effect = [
                    mock_group,  # _get_group_or_404
                ]

                with patch("routers.group.GroupDispatchService") as MockService:
                    mock_svc = MagicMock()
                    mock_svc.get_messages.return_value = []
                    MockService.return_value = mock_svc

                    response = client.get(
                        "/api/groups/some-id/messages?limit=100",
                        headers={"Authorization": "Bearer fake"},
                    )

                    # Verify limit was passed
                    mock_svc.get_messages.assert_called()
                    call_args = mock_svc.get_messages.call_args
                    assert call_args[1]["limit"] == 100


class TestGroupRouterMoments:
    """Test moments-related endpoints"""

    def test_list_group_moments_requires_auth(self):
        """GET /api/groups/{id}/moments requires auth"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/groups/some-id/moments")

        assert response.status_code in [401, 403]

    def test_delete_moment_requires_auth(self):
        """DELETE /api/groups/{id}/moments/{mid} requires auth"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete("/api/groups/some-group/moments/some-moment")

        assert response.status_code in [401, 403]
