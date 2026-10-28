"""
Tests for Upload Router API - Phase 8 Engine
routers/upload.py

现行契约（routers/upload.py）：
  POST /api/desktop/upload_session   —— user_id 由 `Depends(get_current_user_id)` 提供，
                                        **不走请求体**；响应 {session_id, presigned_put_url, expires_in:600}
  POST /api/desktop/upload_done      —— body {session_id, filename}；session 未知/过期 → 404；
                                        WS 推送经 routers.client_ws.manager.send(payload)

两个注意点（都是历史遗留的测试假设，不是业务缺陷）：
  1. 路由里的 `upload_service` 是模块级 `from services.upload_service import upload_service`，
     patch 源模块无效，必须 patch `routers.upload.upload_service`。
  2. WS manager 在 `routers/client_ws.py`，历史测试写的 `routers.desktop_ws` 已不存在。

⚠️ 本文件当前整体红：`main.py` 导入了 `upload_router` 但从未 `app.include_router(upload_router)`
（只挂了 `upload_general_router`），因此这两个端点在生产/测试里都返回 405 —— 属真实缺陷，
已在 S8 报告「跨界请求」登记，测试保持红灯以暴露它。
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient


@contextmanager
def _authenticated(user_id=1):
    """覆盖登录依赖：这些端点本身要求 JWT，不应靠伪造 header 通过。"""
    from main import app
    from utils.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: user_id
    try:
        yield user_id
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)


class TestUploadRouterAuth:
    """Test auth requirements for upload endpoints"""

    def test_create_session_requires_auth(self):
        """未认证 POST /api/desktop/upload_session 必须被拒绝（401/403）

        旧断言为「body 缺 user_id → 400」，但现行契约里 user_id 来自 JWT，
        请求体没有该字段，因此本条真正该锁的是「没有 token 进不去」。
        """
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/api/desktop/upload_session", json={})

        assert response.status_code in (401, 403)

    def test_upload_done_requires_auth(self):
        """未认证 POST /api/desktop/upload_done 必须被拒绝（401/403）"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/desktop/upload_done",
            json={"session_id": "nonexistent", "filename": "test.jpg"},
        )

        assert response.status_code in (401, 403)


class TestUploadRouterCreateSession:
    """Test POST /api/desktop/upload_session"""

    def test_create_session_success(self):
        """create_session returns session_id and presigned URL"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _authenticated() as user_id:
            with patch("routers.upload.upload_service") as mock_svc:
                mock_svc.create_session.return_value = ("abc12345", "https://cos.example.com/put")

                response = client.post("/api/desktop/upload_session", json={"user_id": 1})

                assert response.status_code == 200
                data = response.json()
                assert data["session_id"] == "abc12345"
                assert data["presigned_put_url"] == "https://cos.example.com/put"
                # user_id 只能来自依赖（token），不能由 body 决定
                mock_svc.create_session.assert_called_once_with(user_id)

    def test_create_session_body_user_id_ignored(self):
        """body 里的 user_id 不是入参：认证用户即使 body 为空也应 200"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _authenticated(user_id=7):
            with patch("routers.upload.upload_service") as mock_svc:
                mock_svc.create_session.return_value = ("abc12345", "https://cos.example.com/put")

                response = client.post("/api/desktop/upload_session", json={})

                assert response.status_code == 200
                mock_svc.create_session.assert_called_once_with(7)

    def test_create_session_expires_in_value(self):
        """create_session returns expires_in of 600"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _authenticated():
            with patch("routers.upload.upload_service") as mock_svc:
                mock_svc.create_session.return_value = ("abc12345", "https://cos.example.com/put")

                response = client.post("/api/desktop/upload_session", json={"user_id": 1})

                assert response.status_code == 200
                assert response.json()["expires_in"] == 600


class TestUploadRouterUploadDone:
    """Test POST /api/desktop/upload_done"""

    def test_upload_done_success(self):
        """upload_done returns ok and triggers WS push"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _authenticated():
            with patch("routers.upload.upload_service") as mock_svc:
                mock_svc.confirm_upload.return_value = "https://cos.example.com/get"

                with patch("routers.client_ws.manager") as mock_manager:
                    mock_manager.send = AsyncMock()

                    response = client.post(
                        "/api/desktop/upload_done",
                        json={"session_id": "abc12345", "filename": "photo.jpg"},
                    )

                    assert response.status_code == 200
                    assert response.json()["status"] == "ok"
                    mock_manager.send.assert_awaited_once()

    def test_upload_done_missing_session_id(self):
        """upload_done returns 400 when session_id missing"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _authenticated():
            response = client.post("/api/desktop/upload_done", json={"filename": "photo.jpg"})

            assert response.status_code == 400
            assert "session_id" in response.json()["detail"]

    def test_upload_done_session_not_found(self):
        """upload_done returns 404 when session not found"""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)

        with _authenticated():
            with patch("routers.upload.upload_service") as mock_svc:
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

        with _authenticated():
            with patch("routers.upload.upload_service") as mock_svc:
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

        with _authenticated():
            with patch("routers.upload.upload_service") as mock_svc:
                mock_svc.confirm_upload.return_value = "https://cos.example.com/get"

                with patch("routers.client_ws.manager") as mock_manager:
                    mock_manager.send = AsyncMock()

                    response = client.post(
                        "/api/desktop/upload_done",
                        json={"session_id": "abc12345", "filename": "photo.jpg"},
                    )

                    assert response.status_code == 200
                    # Verify WS push was called with correct payload structure
                    mock_manager.send.assert_awaited_once()
                    payload = mock_manager.send.await_args.args[0]
                    assert payload["type"] == "upload_complete"
                    assert payload["session_id"] == "abc12345"
                    assert payload["filename"] == "photo.jpg"
                    assert payload["presigned_get_url"] == "https://cos.example.com/get"
