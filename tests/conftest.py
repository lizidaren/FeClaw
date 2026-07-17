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
def real_db():
    """P1.4: 真实 SQLite in-memory 数据库 fixture。

    替代 mock_db，让测试跑真实的 SQL（commit/rollback/foreign keys/JSON 列等），
    同时不依赖外部 MySQL/Redis 服务。

    默认 opt-in：只在显式声明 `def test_x(real_db):` 时才使用。
    CI 策略：`pytest -m "not real_db"` 跑全套；`real_db` 用例仅 main 分支必跑。

    用法示例：
        def test_user_creation(real_db):
            db = real_db()
            user = User(username="alice", password_hash="x", salt=None)
            db.add(user)
            db.commit()
            assert db.query(User).count() == 1
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    # 延迟导入：避免 conftest.py 加载时触发 main.py 副作用
    from models.database import Base

    # 确保所有模型都被 import（Base.metadata 才能识别全部表）
    # 这些 import 触发 model class 注册到 Base
    from models import database as _db_mod  # noqa: F401
    from models.agent_profile import AgentProfile  # noqa: F401
    from models.agent_buffer import AgentBuffer  # noqa: F401
    from models.group import Group, GroupMember, GroupMessage, GroupMoments  # noqa: F401
    from models.fehub import FePublish, AppData  # noqa: F401
    from models.zentrim import ZentrimEntry, ZentrimTimeline, ZentrimTimelineEntry, ZentrimReference  # noqa: F401

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 共享单连接：让多次 SessionLocal() 看到同一份 in-memory DB
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # 模拟 SessionLocal 工厂：返回新 session（与真实 SessionLocal 行为一致）
    def session_factory():
        return TestSessionLocal()

    # Patch 所有 import SessionLocal 的位置（与服务模块级 import 兼容）
    with patch("models.database.SessionLocal", session_factory):
        with patch("services.llm_service.SessionLocal", session_factory):
            with patch("services.permission_service.SessionLocal", session_factory):
                with patch("services.agent_executor.SessionLocal", session_factory):
                    yield session_factory


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
