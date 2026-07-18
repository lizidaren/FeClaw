"""
Tests for UploadService - Phase 8 Engine
services/upload_service.py
"""

import pytest
import time
from unittest.mock import MagicMock, patch

from services.upload_service import UploadService, UploadSession, SESSION_TTL


class TestUploadServiceCreateSession:
    """Test create_session() method"""

    def test_create_session_returns_id_and_url(self):
        """create_session returns session_id and presigned_put_url"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"

            session_id, presigned_url = svc.create_session(user_id=1)

            assert session_id is not None
            assert len(session_id) == 8
            assert presigned_url == "https://cos.example.com/put"

    def test_create_session_stores_session(self):
        """create_session stores session in memory"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"

            session_id, _ = svc.create_session(user_id=42)

            stored = svc._sessions.get(session_id)
            assert stored is not None
            assert stored.user_id == 42
            assert stored.completed is False
            assert stored.is_expired() is False

    def test_create_session_unique_ids(self):
        """create_session generates unique IDs"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"

            ids = set()
            for _ in range(100):
                session_id, _ = svc.create_session(user_id=1)
                ids.add(session_id)

            # All IDs should be unique (probability of collision is negligible)
            assert len(ids) == 100


class TestUploadServiceConfirmUpload:
    """Test confirm_upload() method"""

    def test_confirm_upload_success(self):
        """confirm_upload returns presigned GET URL"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"
            mock_instance.generate_presigned_get_url.return_value = "https://cos.example.com/get"

            session_id, _ = svc.create_session(user_id=1)
            get_url = svc.confirm_upload(session_id, "photo.jpg")

            assert get_url == "https://cos.example.com/get"
            # Session should be cleaned up
            assert svc._sessions.get(session_id) is None

    def test_confirm_upload_not_found(self):
        """confirm_upload returns None for unknown session"""
        svc = UploadService()

        result = svc.confirm_upload("nonexistent", "file.jpg")

        assert result is None

    def test_confirm_upload_expired(self):
        """confirm_upload returns None for expired session"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"

            session_id, _ = svc.create_session(user_id=1)

            # Manually expire the session
            svc._sessions[session_id].created_at = time.time() - SESSION_TTL - 1

            result = svc.confirm_upload(session_id, "file.jpg")

            assert result is None
            # Session should be cleaned up
            assert svc._sessions.get(session_id) is None

    def test_confirm_upload_sets_filename(self):
        """confirm_upload sets filename on session"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"
            mock_instance.generate_presigned_get_url.return_value = "https://cos.example.com/get"

            session_id, _ = svc.create_session(user_id=1)

            # Expire session to prevent actual cleanup
            svc._sessions[session_id].created_at = time.time()

            svc.confirm_upload(session_id, "photo.jpg")

            # Session was deleted after confirm
            assert svc._sessions.get(session_id) is None


class TestUploadServiceGetSession:
    """Test get_session() method"""

    def test_get_session_exists(self):
        """get_session returns session when exists"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"

            session_id, _ = svc.create_session(user_id=1)

            session = svc.get_session(session_id)

            assert session is not None
            assert session.session_id == session_id

    def test_get_session_not_found(self):
        """get_session returns None for unknown session"""
        svc = UploadService()

        result = svc.get_session("nonexistent")

        assert result is None

    def test_get_session_expired(self):
        """get_session returns None and cleans up expired session"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"

            session_id, _ = svc.create_session(user_id=1)

            # Manually expire the session
            svc._sessions[session_id].created_at = time.time() - SESSION_TTL - 1

            result = svc.get_session(session_id)

            assert result is None
            assert svc._sessions.get(session_id) is None


class TestUploadSessionTTL:
    """Test UploadSession.is_expired() method"""

    def test_is_expired_new_session_false(self):
        """New session is not expired"""
        session = UploadSession(
            session_id="abc12345",
            user_id=1,
            presigned_put_url="http://example.com",
            cos_key="test/key",
            created_at=time.time(),
        )

        assert session.is_expired() is False

    def test_is_expired_old_session_true(self):
        """Old session is expired"""
        session = UploadSession(
            session_id="abc12345",
            user_id=1,
            presigned_put_url="http://example.com",
            cos_key="test/key",
            created_at=time.time() - SESSION_TTL - 1,
        )

        assert session.is_expired() is True

    def test_session_ttl_is_600(self):
        """SESSION_TTL is 600 seconds (10 minutes)"""
        assert SESSION_TTL == 600


class TestUploadServiceEdgeCases:
    """Edge case tests for UploadService"""

    def test_confirm_upload_cleans_up_after(self):
        """confirm_upload removes session from storage"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"
            mock_instance.generate_presigned_get_url.return_value = "https://cos.example.com/get"

            session_id, _ = svc.create_session(user_id=1)
            assert len(svc._sessions) == 1

            svc.confirm_upload(session_id, "file.jpg")

            assert len(svc._sessions) == 0

    def test_multiple_sessions_coexist(self):
        """Multiple sessions can coexist"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"

            id1, _ = svc.create_session(user_id=1)
            id2, _ = svc.create_session(user_id=2)
            id3, _ = svc.create_session(user_id=3)

            assert len(svc._sessions) == 3
            assert id1 != id2 != id3

    def test_cos_key_format(self):
        """COS key follows expected format"""
        svc = UploadService()

        with patch("services.upload_service.CosStorage") as mock_cos:
            mock_instance = MagicMock()
            mock_cos.return_value = mock_instance
            mock_instance.generate_presigned_put_url.return_value = "https://cos.example.com/put"

            session_id, _ = svc.create_session(user_id=1)

            session = svc._sessions[session_id]
            assert session.cos_key.startswith("feclaw/uploads/")
            assert session_id in session.cos_key
