"""Error hierarchy for the WeChat Bot SDK."""


class WeChatBotError(Exception):
    """Base error for all SDK errors."""

    def __init__(self, message: str, code: str = "UNKNOWN") -> None:
        super().__init__(message)
        self.code = code


class ApiError(WeChatBotError):
    """Returned when the iLink API returns an error."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int = 0,
        errcode: int = 0,
        payload: object = None,
    ) -> None:
        super().__init__(message, "API_ERROR")
        self.http_status = http_status
        self.errcode = errcode
        self.payload = payload

    @property
    def is_session_expired(self) -> bool:
        return self.errcode == -14


class AuthError(WeChatBotError):
    """Authentication errors (QR expired, login failed, etc.)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "AUTH_ERROR")


class NoContextError(WeChatBotError):
    """No context_token available for a user."""

    def __init__(self, user_id: str) -> None:
        super().__init__(
            f"No context_token for user {user_id}. "
            "A message from this user must be received first.",
            "NO_CONTEXT",
        )
        self.user_id = user_id


class MediaError(WeChatBotError):
    """Media processing errors (encryption, upload, download)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "MEDIA_ERROR")
