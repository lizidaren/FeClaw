"""
Upload Session Service
管理手机端直传 COS 的预签名 URL 会话
"""

import logging
import time
import secrets
from typing import Optional
from dataclasses import dataclass

from config import settings

logger = logging.getLogger(__name__)

# TTL: 10 minutes in seconds
SESSION_TTL = 600


@dataclass
class UploadSession:
    session_id: str
    user_id: int
    presigned_put_url: str
    cos_key: str
    filename: Optional[str] = None
    completed: bool = False
    created_at: float = 0.0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > SESSION_TTL


class UploadService:
    """
    In-memory upload session manager.
    Sessions expire after SESSION_TTL seconds.
    """

    def __init__(self):
        self._sessions: dict[str, UploadSession] = {}

    def _generate_session_id(self) -> str:
        """Generate a unique 8-char session ID."""
        for _ in range(10):
            sid = secrets.token_hex(4)  # 8 hex chars
            if sid not in self._sessions:
                return sid
        raise RuntimeError("Failed to generate unique session ID")

    def create_session(self, user_id: int) -> tuple[str, str]:
        """
        Create a new upload session.

        Returns:
            (session_id, presigned_put_url)
        """
        session_id = self._generate_session_id()
        cos_key = f"feclaw/uploads/{session_id}/file"

        # Generate presigned PUT URL via CosStorage
        from services.storage_service import CosStorage
        storage = CosStorage()
        presigned_put_url = storage.generate_presigned_put_url(cos_key, expired=SESSION_TTL)

        session = UploadSession(
            session_id=session_id,
            user_id=user_id,
            presigned_put_url=presigned_put_url,
            cos_key=cos_key,
            created_at=time.time(),
        )
        self._sessions[session_id] = session
        logger.info(f"[UploadService] Session created: {session_id} for user {user_id}")
        return session_id, presigned_put_url

    def confirm_upload(self, session_id: str, filename: str) -> Optional[str]:
        """
        Mark session as complete and return presigned GET URL.
        Returns None if session not found or expired.
        """
        session = self._sessions.get(session_id)
        if session is None:
            logger.warning(f"[UploadService] confirm_upload: session {session_id} not found")
            return None
        if session.is_expired():
            logger.warning(f"[UploadService] confirm_upload: session {session_id} expired")
            del self._sessions[session_id]
            return None

        session.filename = filename
        session.completed = True

        # Build full COS URL and generate presigned GET URL
        cos_url = (
            f"https://{settings.TENCENT_COS_BUCKET}"
            f".cos.{settings.TENCENT_COS_REGION}.myqcloud.com/{session.cos_key}"
        )
        from services.storage_service import CosStorage
        storage = CosStorage()
        presigned_get_url = storage.generate_presigned_get_url(cos_url, expired=3600)

        logger.info(f"[UploadService] Upload confirmed: {session_id} -> {filename}")
        # Clean up completed session
        del self._sessions[session_id]
        return presigned_get_url

    def get_session(self, session_id: str) -> Optional[UploadSession]:
        """Get session info (does not mark as complete)."""
        session = self._sessions.get(session_id)
        if session and not session.is_expired():
            return session
        if session:
            del self._sessions[session_id]
        return None


# Global singleton
upload_service = UploadService()
