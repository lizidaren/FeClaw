"""
全局测试配置和 Fixture
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# 自动添加项目根目录到 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture
def mock_db():
    """Mock 数据库 SessionLocal，返回 MagicMock 实例。

    使用方式：
        def test_something(mock_db):
            from models.database import SessionLocal
            # SessionLocal() 返回 mock_db.session
            mock_db.session.add.assert_called_once()
    """

    mock_session = MagicMock()
    mock_session.add = MagicMock()
    mock_session.commit = MagicMock()
    mock_session.close = MagicMock()

    with patch("models.database.SessionLocal", return_value=mock_session):
        # 同时 patch 各 service 中 import 的 SessionLocal（模块级 import 需要单独 patch）
        with patch("services.llm_service.SessionLocal", return_value=mock_session):
            with patch("services.permission_service.SessionLocal", return_value=mock_session):
                    with patch("services.agent_executor.SessionLocal", return_value=mock_session):
                        yield mock_session


@pytest.fixture
def mock_httpx_client():
    """Mock httpx.AsyncClient，返回可控制响应的 client 实例。

    用法：
        def test(mock_httpx_client):
            mock_httpx_client["set_post_response"]({"usage": {...}})
    """

    container = {}

    class FakeHttpxResponse:
        """模拟 httpx.Response，同步 raise_for_status 和 json"""
        def __init__(self, json_data=None):
            self._json_data = json_data or {}
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return self._json_data

    class FakeStreamResponse:
        """模拟流式 httpx 响应"""
        def __init__(self, sse_lines=None):
            self._sse_lines = sse_lines or []
            self.status_code = 200
            self.is_closed = False

        def raise_for_status(self):
            pass

        async def aclose(self):
            self.is_closed = True

        async def aiter_lines(self):
            for line in self._sse_lines:
                yield line

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    def set_post_response(json_data):
        container["post_response"] = json_data

    def set_stream_response(sse_lines):
        container["stream_response"] = sse_lines

    container["set_post_response"] = set_post_response
    container["set_stream_response"] = set_stream_response

    mock_client = MagicMock()

    # 关键在于：MagicMock.__aenter__ 默认返回新 mock，
    # 必须设 __aenter__.return_value = mock_client 让 async with 拿到同一个实例
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    async def mock_post(*args, **kwargs):
        return FakeHttpxResponse(json_data=container.get("post_response", {}))

    def mock_stream(*args, **kwargs):
        return FakeStreamResponse(sse_lines=container.get("stream_response", []))

    mock_client.post = mock_post
    mock_client.stream = mock_stream

    with patch("httpx.AsyncClient", return_value=mock_client):
        yield container
