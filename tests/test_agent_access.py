"""
M1 / M2 收敛回归测试（FIX-H）

- M1：`user_owns_agent` / `agent_belongs_to_user` 的 `str()` 归一（int/str 两形态）
  —— 注入缺陷（退回原始 `==`）会让 str 形态用例报红。
- M2：`extract_hash_from_host` 同时接受 4 位 / 8 位 hex、拒绝非法长度；
  `generate_agent_hash` 唯一生成入口；share 子域名解析放开 8 位。
"""

import pytest
from unittest.mock import MagicMock

from utils.agent_access import (
    user_owns_agent,
    agent_belongs_to_user,
    extract_hash_from_host,
    generate_agent_hash,
)


# ─────────────────────────── M1：归属判定 str() 归一 ───────────────────────────

class TestUserOwnsAgent:
    def _db_returning(self, agent):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = agent
        return db

    def test_owns_with_int_user_id(self):
        agent = MagicMock()
        agent.user_id = 5
        assert user_owns_agent(self._db_returning(agent), "abcd", 5) is True

    def test_owns_with_str_user_id(self):
        # 注入缺陷：退回原始 `agent.user_id == user_id` 时，int 列 vs str 入参会判错
        agent = MagicMock()
        agent.user_id = 5
        assert user_owns_agent(self._db_returning(agent), "abcd", "5") is True

    def test_not_owns_when_mismatch(self):
        agent = MagicMock()
        agent.user_id = 5
        assert user_owns_agent(self._db_returning(agent), "abcd", 6) is False
        assert user_owns_agent(self._db_returning(agent), "abcd", "6") is False

    def test_not_owns_when_agent_missing(self):
        assert user_owns_agent(self._db_returning(None), "abcd", 5) is False

    def test_not_owns_when_hash_empty(self):
        db = MagicMock()
        assert user_owns_agent(db, "", 5) is False
        assert user_owns_agent(db, None, 5) is False


class TestAgentBelongsToUser:
    def test_int_and_str_both_match(self):
        agent = MagicMock()
        agent.user_id = 7
        assert agent_belongs_to_user(agent, 7) is True
        assert agent_belongs_to_user(agent, "7") is True

    def test_none_agent_false(self):
        assert agent_belongs_to_user(None, 7) is False

    def test_mismatch_false(self):
        agent = MagicMock()
        agent.user_id = 7
        assert agent_belongs_to_user(agent, 8) is False
        assert agent_belongs_to_user(agent, "8") is False


# ─────────────────────────── M2：hash 解析 / 生成 ─────────────────────────────

class TestExtractHashFromHost:
    def test_4_char_hash(self):
        assert extract_hash_from_host("8d85.feclaw.chat") == "8d85"

    def test_8_char_hash(self):
        # 注入缺陷：若退回「只认 4 位」，8 位新 Agent 子域会解析成 None
        assert extract_hash_from_host("1234abcd.feclaw.chat") == "1234abcd"

    def test_invalid_length_rejected(self):
        assert extract_hash_from_host("abc.feclaw.chat") is None        # 3 位
        assert extract_hash_from_host("123456789.feclaw.chat") is None  # 9 位

    def test_non_hex_rejected(self):
        assert extract_hash_from_host("zzzz.feclaw.chat") is None
        assert extract_hash_from_host("1234efgh.feclaw.chat") is None

    def test_bare_host_rejected(self):
        assert extract_hash_from_host("feclaw.chat") is None
        assert extract_hash_from_host("") is None
        assert extract_hash_from_host(None) is None


class TestGenerateAgentHash:
    def test_default_8_hex(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        h = generate_agent_hash(db)
        assert len(h) == 8
        int(h, 16)  # 必须是合法 hex

    def test_length_bytes_2_gives_4_hex(self):
        # 老入口（totp_service）传 length_bytes=2，行为保持 4 位
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        h = generate_agent_hash(db, length_bytes=2)
        assert len(h) == 4


class TestShareSubdomainParsing:
    @pytest.mark.asyncio
    async def test_8_char_subdomain_scopes_to_agent(self, monkeypatch):
        """M2 注入缺陷：share.py 若退回 `len(prefix) == 4`，8 位子域会落入全局查找。"""
        from config import settings
        from services import share_service
        from fastapi import HTTPException
        from routers.share import resolve_share_by_slug

        monkeypatch.setattr(settings, "FECLAW_SUBDOMAIN_ENABLED", True)
        monkeypatch.setattr(settings, "FECLAW_PUBLIC_URL", "feclaw.test")

        captured = {}

        def fake_resolve_slug(slug, agent_hash, db):
            captured["agent_hash"] = agent_hash
            return None

        monkeypatch.setattr(share_service, "resolve_slug", fake_resolve_slug)

        req = MagicMock()
        req.headers = {"host": "1234abcd.feclaw.test"}
        with pytest.raises(HTTPException) as exc_info:
            await resolve_share_by_slug("some-slug", req, db=MagicMock())
        assert exc_info.value.status_code == 404
        assert captured["agent_hash"] == "1234abcd"
