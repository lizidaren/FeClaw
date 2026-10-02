"""
Q15：微信 bot_token 凭证持久化 + 启动恢复（键 = internal_user_id ↔ agent_hash）专项测试。

覆盖：
- save_sdk_credentials 落库到 wechat_binding.ilink_token（按 (user, agent) 键）
- restore_login_state_from_db 必须显式给 key；给 key 后内存 token == 持久层 token
- 发送凭证解析 _resolve_send_base_info：有 agent_hash 时绝不优先用单例 _login_state
- 崩溃演练：ilink_token 半截/坏 JSON 不崩，优雅回落（要么全有要么全无）
"""
import os
import sys
import json

import pytest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture
def wechat_db():
    """SQLite in-memory DB，并把 services.wechat_service / sdk_adapter 的 SessionLocal 指过去。"""
    from models.database import Base
    import models.database as _db  # noqa: F401  (User / WeChatBinding / ...)
    from models.agent_profile import AgentProfile  # noqa: F401
    from models.agent_buffer import AgentBuffer  # noqa: F401
    from models.group import Group, GroupMember, GroupMessage, GroupMoments  # noqa: F401
    from models.fehub import FePublish, AppData  # noqa: F401
    from models.zentrim import ZentrimEntry, ZentrimTimeline, ZentrimTimelineEntry, ZentrimReference  # noqa: F401
    from models.organization import Organization  # noqa: F401  (groups.organization_id FK)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def factory():
        return TestSessionLocal()

    from services import wechat_service as ws
    from services.wechat import sdk_adapter as sdk

    with patch.object(ws, "SessionLocal", factory), patch.object(sdk, "SessionLocal", factory):
        yield factory


def _fresh_service():
    """复位单例，拿到一个干净的 WeChatService。"""
    from services.wechat_service import WeChatService
    WeChatService._instance = None
    return WeChatService()


def _make_binding(factory, *, user_id, agent_hash, openid, bot_token, ilink_token=None,
                  ilink_bot_id=None, ilink_user_id=None, base_url=None):
    from models.database import WeChatBinding
    db = factory()
    try:
        b = WeChatBinding(
            user_id=user_id,
            agent_hash=agent_hash,
            wx_openid=openid,
            bot_token=bot_token,
            ilink_bot_id=ilink_bot_id,
            ilink_user_id=ilink_user_id or openid,
            base_url=base_url,
            status="active",
            ilink_token=ilink_token,
        )
        db.add(b)
        db.commit()
        return b.id
    finally:
        db.close()


def _get_binding(factory, binding_id):
    from models.database import WeChatBinding
    db = factory()
    try:
        return db.query(WeChatBinding).filter(WeChatBinding.id == binding_id).first()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. 落库：save_sdk_credentials 键 = (user_id, agent_hash)
# ---------------------------------------------------------------------------

class TestSaveSdkCredentials:
    def test_persists_ilink_token_for_correct_agent(self, wechat_db):
        svc = _fresh_service()
        bid_aaaa = _make_binding(wechat_db, user_id=100, agent_hash="aaaa", openid="openid1", bot_token="legacy-aaaa")
        bid_bbbb = _make_binding(wechat_db, user_id=100, agent_hash="bbbb", openid="openid2", bot_token="legacy-bbbb")

        svc.save_sdk_credentials(
            user_id=100, bot_token="fresh-token-aaaa", ilink_bot_id="bot1",
            ilink_user_id="openid1", base_url="https://api", agent_hash="aaaa",
        )

        b_aaaa = _get_binding(wechat_db, bid_aaaa)
        cred = json.loads(b_aaaa.ilink_token)
        assert cred["token"] == "fresh-token-aaaa"
        assert cred["account_id"] == "bot1"
        assert cred["user_id"] == "openid1"

        # 别的 Agent 的绑定不被误写
        b_bbbb = _get_binding(wechat_db, bid_bbbb)
        assert b_bbbb.ilink_token is None

    def test_missing_binding_is_noop(self, wechat_db):
        svc = _fresh_service()
        svc.save_sdk_credentials(
            user_id=999, bot_token="x", ilink_bot_id="b", ilink_user_id="u",
            base_url="https://api", agent_hash="zzzz",
        )  # 不应抛异常
        assert True


# ---------------------------------------------------------------------------
# 2. 恢复：restore_login_state_from_db 必须显式给 key
# ---------------------------------------------------------------------------

class TestRestoreLoginState:
    def test_requires_explicit_key(self, wechat_db):
        svc = _fresh_service()
        _make_binding(wechat_db, user_id=100, agent_hash="aaaa", openid="openid1",
                      bot_token="tok-aaaa",
                      ilink_token=json.dumps({"token": "tok-aaaa", "account_id": "b1", "user_id": "openid1"}))
        # 不传 key（无法确定哪条）→ 不去猜
        assert svc.restore_login_state_from_db() is False
        assert svc._login_state.get("bot_token") is None

    def test_restores_token_for_specific_agent(self, wechat_db):
        svc = _fresh_service()
        _make_binding(wechat_db, user_id=100, agent_hash="aaaa", openid="openid1", bot_token="legacy",
                      ilink_token=json.dumps({"token": "tok-aaaa", "account_id": "b1", "user_id": "openid1"}))
        _make_binding(wechat_db, user_id=100, agent_hash="bbbb", openid="openid1", bot_token="legacy",
                      ilink_token=json.dumps({"token": "tok-bbbb", "account_id": "b2", "user_id": "openid1"}))

        assert svc.restore_login_state_from_db(100, "aaaa") is True
        assert svc._login_state["bot_token"] == "tok-aaaa"

        assert svc.restore_login_state_from_db(100, "bbbb") is True
        assert svc._login_state["bot_token"] == "tok-bbbb"

    def test_no_binding_returns_false(self, wechat_db):
        svc = _fresh_service()
        assert svc.restore_login_state_from_db(100, "nope") is False


# ---------------------------------------------------------------------------
# 3. 发送凭证解析：有 agent_hash 时绝不优先用单例 _login_state（§AC 根因）
# ---------------------------------------------------------------------------

class TestResolveSendBaseInfo:
    def test_agent_hash_wins_over_singleton(self, wechat_db):
        svc = _fresh_service()
        _make_binding(wechat_db, user_id=100, agent_hash="aaaa", openid="openid1", bot_token="legacy",
                      ilink_token=json.dumps({"token": "tok-aaaa", "account_id": "b1", "user_id": "openid1"}))
        _make_binding(wechat_db, user_id=100, agent_hash="bbbb", openid="openid1", bot_token="legacy",
                      ilink_token=json.dumps({"token": "tok-bbbb", "account_id": "b2", "user_id": "openid1"}))

        # 单例里装的是 aaaa 的 token（模拟重启后错误的「取最新一条」）
        svc._login_state["bot_token"] = "tok-aaaa"
        svc._login_state["ilink_user_id"] = "openid1"

        base_info = svc._resolve_send_base_info("openid1", agent_hash="bbbb")
        assert base_info["bot_token"] == "tok-bbbb"  # 绝不误用单例里的 aaaa

    def test_unknown_agent_returns_empty(self, wechat_db):
        svc = _fresh_service()
        base_info = svc._resolve_send_base_info("openid1", agent_hash="nope")
        assert base_info.get("bot_token") is None

    def test_legacy_no_agent_uses_singleton(self, wechat_db):
        svc = _fresh_service()
        svc._login_state["bot_token"] = "singleton-token"
        svc._login_state["ilink_user_id"] = "u"
        base_info = svc._resolve_send_base_info("openid1")  # 无 agent 上下文 → 遗留路径
        assert base_info["bot_token"] == "singleton-token"


# ---------------------------------------------------------------------------
# 4. 失效处理：-14 标记 binding 过期（键 = user ↔ agent）
# ---------------------------------------------------------------------------

class TestMarkBindingExpired:
    def test_marks_only_target_agent_expired(self, wechat_db):
        svc = _fresh_service()
        _make_binding(wechat_db, user_id=100, agent_hash="aaaa", openid="openid1", bot_token="tok-aaaa")
        bid_bbbb = _make_binding(wechat_db, user_id=100, agent_hash="bbbb", openid="openid1", bot_token="tok-bbbb")

        svc._mark_binding_expired("openid1", agent_hash="aaaa")

        from models.database import WeChatBinding
        db = wechat_db()
        try:
            a = db.query(WeChatBinding).filter(WeChatBinding.agent_hash == "aaaa").first()
            b = db.query(WeChatBinding).filter(WeChatBinding.agent_hash == "bbbb").first()
            assert a.status == "expired"
            assert b.status == "active"  # 别的 Agent 不受影响
        finally:
            db.close()


class TestSessionExpiredOnSend:
    @pytest.mark.asyncio
    async def test_send_14_marks_binding_expired(self, wechat_db):
        """发消息遇 -14/session timeout ⇒ 降级为需重新扫码（标记 binding expired，不静默）。"""
        svc = _fresh_service()
        _make_binding(wechat_db, user_id=100, agent_hash="aaaa", openid="openid1", bot_token="tok-aaaa",
                      ilink_token=json.dumps({"token": "tok-aaaa", "account_id": "b",
                                              "user_id": "openid1", "base_url": "https://api"}))

        class _FakeResp:
            status = 200
            async def text(self):
                return '{"errcode": -14, "errmsg": "session timeout"}'
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            def post(self, *a, **kw):
                return _FakeResp()

        async def _fake_get_session():
            return _FakeSession()

        svc._get_session = _fake_get_session

        ok = await svc._send_single_message("openid1", "hi", agent_hash="aaaa")
        assert ok is False

        from models.database import WeChatBinding
        db = wechat_db()
        try:
            b = db.query(WeChatBinding).filter(WeChatBinding.agent_hash == "aaaa").first()
            assert b.status == "expired"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 5. 崩溃演练：坏 / 半截 ilink_token 不崩（要么全有要么全无）
# ---------------------------------------------------------------------------

class TestCrashSafety:
    def test_corrupted_ilink_token_falls_back_to_legacy_field(self, wechat_db):
        svc = _fresh_service()
        _make_binding(wechat_db, user_id=100, agent_hash="aaaa", openid="openid1",
                      bot_token="legacy-token", ilink_token="{invalid json")
        assert svc.restore_login_state_from_db(100, "aaaa") is True
        assert svc._login_state["bot_token"] == "legacy-token"

    def test_no_usable_token_returns_false_no_crash(self, wechat_db):
        svc = _fresh_service()
        _make_binding(wechat_db, user_id=100, agent_hash="aaaa", openid="openid1",
                      bot_token="", ilink_token="{invalid json")
        assert svc.restore_login_state_from_db(100, "aaaa") is False

    def test_save_writes_single_valid_json(self, wechat_db):
        """ilink_token 是单条 JSON 字符串、单次 commit —— 不存在「半个凭证」的中间态。"""
        svc = _fresh_service()
        bid = _make_binding(wechat_db, user_id=100, agent_hash="aaaa", openid="openid1", bot_token="legacy")
        svc.save_sdk_credentials(
            user_id=100, bot_token="full-token", ilink_bot_id="b", ilink_user_id="openid1",
            base_url="https://api", agent_hash="aaaa",
        )
        raw = _get_binding(wechat_db, bid).ilink_token
        cred = json.loads(raw)  # 要么整条合法，要么（未写时）为 None —— 不会解析出半个
        assert cred["token"] == "full-token"
        assert cred["account_id"] == "b"
