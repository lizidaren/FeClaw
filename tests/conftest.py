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

# T19/T01：以下不是 pytest 用例，而是需要真实服务/参数的“手工 E2E 脚本”，
# 它们在**模块级**就执行 argparse 或发起 HTTP 请求 —— pytest 一收集就会
# SystemExit / 连不上 localhost:8080 / 依赖缺失，导致整套测试无法收集。
# 显式跳过收集；手工运行方式写在各自文件头部。
collect_ignore = [
    "stress_test.py",              # FUSE 手工压测（模块级建目录）
    "test_zentrim_api.py",         # 模块级 argparse + 对运行中服务发请求
    "test_subagent_presets.py",    # 依赖 PRESET_ROLES（运行期配置）
    "test_subagent_presets_api.py",
    "test_subagent_summary.py",
]


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
    from models.organization import Organization  # noqa: F401
    from models.zentrim import ZentrimEntry, ZentrimTimeline, ZentrimTimelineEntry, ZentrimReference  # noqa: F401
    from models.kaoxiang_models import KaoxiangKaodian  # noqa: F401

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

    # Patch 所有 import SessionLocal 的位置（与服务模块级 import 兼容）。
    # T19a：仓库里 40+ 个模块在 import 时把 SessionLocal 绑到本地名字上，只 patch
    # 三五个模块会让其余模块（如 routers/*）继续连真实 MySQL —— 在无 MySQL 的
    # 环境里表现为 ConnectionRefused / 0 行导致 404。这里扫描 sys.modules，把
    # 所有本仓库模块上的 `SessionLocal` 名字统一替换成测试工厂。
    import sys as _sys
    from contextlib import ExitStack

    _repo_prefix = _project_root.replace("\\", "/").lower() + "/"
    with ExitStack() as stack:
        for _name, _mod in list(_sys.modules.items()):
            if _mod is None or _name.startswith(("tests", "_pytest", "pytest")):
                continue
            _file = getattr(_mod, "__file__", None)
            if not _file:
                continue
            _norm = _file.replace("\\", "/").lower()
            if _repo_prefix not in _norm:
                continue
            if getattr(_mod, "SessionLocal", None) is None:
                continue
            try:
                stack.enter_context(patch.object(_mod, "SessionLocal", session_factory))
            except Exception:  # 只读属性/异常模块跳过，不影响其余 patch
                continue
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


# ── pytest 标记与真实 MySQL fixture（T19a/T20a 的门禁基础）───────────────────

_MARKERS = {
    "unit": "纯单元测试，不依赖外部服务",
    "integration": "跨模块/接口集成测试",
    "real_db": "使用真实 SQL（SQLite in-memory），不是 MagicMock 会话",
    "mysql": "需要真实 MySQL（并发/行锁/迁移语义）；缺 TEST_DATABASE_URL 时 skip",
    "browser": "需要浏览器（Playwright + 已安装的 Edge）",
    "slow": "耗时较长，可单独排除",
    "network": "需要出网（默认不应在离线环境跑）",
}


def pytest_configure(config):
    """注册标记，消除 `PytestUnknownMarkWarning`（基线里 20+ 条噪声）。"""
    for name, description in _MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")


def _mysql_url():
    """取真实 MySQL 测试库地址；未配置返回空串。

    优先 `TEST_DATABASE_URL`（测试专用库），其次 `DATABASE_URL`（本地开发库）。
    两者都必须是 mysql/mariadb，避免把 SQLite 当成 MySQL 门禁。
    """
    for key in ("TEST_DATABASE_URL", "DATABASE_URL"):
        value = (os.environ.get(key) or "").strip()
        if value.startswith(("mysql", "mariadb")):
            return value
    return ""


@pytest.fixture(scope="session")
def mysql_engine():
    """真实 MySQL 引擎（session 级）。

    用途：T04/T09/T20 的并发、行锁、唯一约束、迁移语义 —— 这些不能拿 SQLite 冒充。
    缺配置时 **skip 而不是伪造通过**；调用方应在报告里标注 "MySQL 未验证"。
    """
    url = _mysql_url()
    if not url:
        pytest.skip("需要 TEST_DATABASE_URL/DATABASE_URL 指向真实 MySQL（mysql/mariadb）")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from models.database import Base, load_all_models

    load_all_models()
    engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def mysql_session(mysql_engine):
    """真实 MySQL 上的独立会话（用例结束回滚未提交内容并关闭）。"""
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=mysql_engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
