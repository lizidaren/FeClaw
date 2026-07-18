"""
Tests for Upload Router API - Phase 8 Engine
routers/upload.py
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient


class TestUploadRouterAuth:
    """Test auth requirements for upload endpoints"""

    def test_create_session_without_user_id_returns_400(self):
        """POST /api/desktop/upload_session without user_id returns 400"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/desktop/upload_session", json={})

        assert response.status_code == 400

    def test_upload_done_without_session_returns_404(self):
        """POST /api/desktop/upload_done with unknown session returns 404"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("services.upload_service.upload_service") as mock_svc:
            mock_svc.confirm_upload.return_value = None

            response = client.post(
                "/api/desktop/upload_done",
                json={"session_id": "nonexistent", "filename": "test.jpg"},
            )

            assert response.status_code == 404


class TestUploadRouterCreateSession:
    """Test POST /api/desktop/upload_session"""

    def test_create_session_success(self):
        """create_session returns session_id and presigned URL"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("services.upload_service.upload_service") as mock_svc:
            mock_svc.create_session.return_value = ("abc12345", "https://cos.example.com/put")

            response = client.post(
                "/api/desktop/upload_session",
                json={"user_id": 1},
            )

            assert response.status_code == 200
            data = response.json()
            assert "session_id" in data
            assert "presigned_put_url" in data
            assert data["session_id"] == "abc12345"
            assert data["presigned_put_url"] == "https://cos.example.com/put"

    def test_create_session_missing_user_id(self):
        """create_session returns 400 when user_id missing"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/desktop/upload_session",
            json={},
        )

        assert response.status_code == 400
        assert "user_id" in response.json().get("detail", "")

    def test_create_session_expires_in_value(self):
        """create_session returns expires_in of 600"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("services.upload_service.upload_service") as mock_svc:
            mock_svc.create_session.return_value = ("abc12345", "https://cos.example.com/put")

            response = client.post(
                "/api/desktop/upload_session",
                json={"user_id": 1},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["expires_in"] == 600


class TestUploadRouterUploadDone:
    """Test POST /api/desktop/upload_done"""

    def test_upload_done_success(self):
        """upload_done returns ok and triggers WS push"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("services.upload_service.upload_service") as mock_svc:
            mock_svc.confirm_upload.return_value = "https://cos.example.com/get"

            with patch("routers.desktop_ws.manager") as mock_manager:
                mock_manager.send = AsyncMock()

                response = client.post(
                    "/api/desktop/upload_done",
                    json={"session_id": "abc12345", "filename": "photo.jpg"},
                )

                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "ok"

    def test_upload_done_missing_session_id(self):
        """upload_done returns 400 when session_id missing"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/desktop/upload_done",
            json={"filename": "photo.jpg"},
        )

        assert response.status_code == 400
        assert "session_id" in response.json().get("detail", "")

    def test_upload_done_session_not_found(self):
        """upload_done returns 404 when session not found"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("services.upload_service.upload_service") as mock_svc:
            mock_svc.confirm_upload.return_value = None

            response = client.post(
                "/api/desktop/upload_done",
                json={"session_id": "nonexistent", "filename": "photo.jpg"},
            )

            assert response.status_code == 404

    def test_upload_done_session_expired(self):
        """upload_done returns 404 when session expired"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("services.upload_service.upload_service") as mock_svc:
            mock_svc.confirm_upload.return_value = None  # expired returns None

            response = client.post(
                "/api/desktop/upload_done",
                json={"session_id": "expired", "filename": "photo.jpg"},
            )

            assert response.status_code == 404

    def test_upload_done_ws_push_payload(self):
        """upload_done sends correct WS payload"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with patch("services.upload_service.upload_service") as mock_svc:
            mock_svc.confirm_upload.return_value = "https://cos.example.com/get"

            with patch("routers.desktop_ws.manager") as mock_manager:
                mock_manager.send = AsyncMock()

                response = client.post(
                    "/api/desktop/upload_done",
                    json={"session_id": "abc12345", "filename": "photo.jpg"},
                )

                # Verify WS push was called with correct payload structure
                mock_manager.send.assert_called_once()
                call_args = mock_manager.send.call_args[0][0]
                assert call_args["type"] == "upload_complete"
                assert call_args["session_id"] == "abc12345"
                assert call_args["filename"] == "photo.jpg"
                assert "presigned_get_url" in call_args
