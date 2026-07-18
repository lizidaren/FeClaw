"""
P1.4 real_db fixture 测试

验证 opt-in 真实 SQLite 数据库能正确建立 schema + 支持基本 CRUD。
覆盖 User + AgentProfile + ChatHistory 三张关键表。
"""
import pytest
from datetime import datetime


class TestRealDBUserCRUD:
    """User 表 CRUD 真实 SQL。"""

    def test_user_create_and_query(self, real_db):
        """创建用户 + 立即查回。"""
        from models.database import User
        db = real_db()
        user = User(
            username="alice",
            password_hash="$2b$12$dummyhash",
            salt=None,
            password_version=2,
            is_admin=False,
        )
        db.add(user)
        db.commit()

        # 重新查回
        fetched = db.query(User).filter(User.username == "alice").first()
        assert fetched is not None
        assert fetched.username == "alice"
        assert fetched.password_hash == "$2b$12$dummyhash"
        assert fetched.salt is None
        assert fetched.password_version == 2
        assert fetched.is_admin is False
        assert isinstance(fetched.created_at, datetime)

    def test_user_unique_username(self, real_db):
        """username 唯一约束生效。"""
        from sqlalchemy.exc import IntegrityError
        from models.database import User
        db = real_db()
        db.add(User(username="bob", password_hash="h", salt=None))
        db.commit()

        db2 = real_db()  # 新 session 但同一 in-memory DB
        db2.add(User(username="bob", password_hash="h2", salt=None))
        with pytest.raises(IntegrityError):
            db2.commit()

    def test_user_count(self, real_db):
        """批量插入 + count 正确。"""
        from models.database import User
        db = real_db()
        for i in range(5):
            db.add(User(username=f"user_{i}", password_hash="h", salt=None))
        db.commit()
        assert db.query(User).count() == 5


class TestRealDBAgentProfileCRUD:
    """AgentProfile 表 CRUD 真实 SQL。"""

    def test_agent_create_with_p1_3_8char_hash(self, real_db):
        """P1.3: 8 字符 hash 可以正常创建。"""
        from models.database import AgentProfile
        db = real_db()
        agent = AgentProfile(
            user_id=1,
            hash="12345678",  # 8 字符新 hash
            totp_secret="BASE32SECRET",
            name="Test Agent",
        )
        db.add(agent)
        db.commit()

        fetched = db.query(AgentProfile).filter(AgentProfile.hash == "12345678").first()
        assert fetched is not None
        assert fetched.name == "Test Agent"

    def test_agent_4char_hash_still_works(self, real_db):
        """老 4 字符 hash 仍可存储（P1.3 不破坏老数据）。"""
        from models.database import AgentProfile
        db = real_db()
        # 老的 4 字符 hash（如 5656、8d85）
        agent = AgentProfile(
            user_id=1,
            hash="5656",
            totp_secret="x",
            name="Old Agent",
        )
        db.add(agent)
        db.commit()

        fetched = db.query(AgentProfile).filter(AgentProfile.hash == "5656").first()
        assert fetched is not None
        assert fetched.name == "Old Agent"

    def test_agent_hash_unique(self, real_db):
        """hash 唯一约束。"""
        from sqlalchemy.exc import IntegrityError
        from models.database import AgentProfile
        db = real_db()
        db.add(AgentProfile(user_id=1, hash="abcd", totp_secret="x"))
        db.commit()

        db2 = real_db()
        db2.add(AgentProfile(user_id=2, hash="abcd", totp_secret="y"))
        with pytest.raises(IntegrityError):
            db2.commit()


class TestRealDBChatHistoryCRUD:
    """ChatHistory 表 CRUD + JSON 列验证（P1.4 重点验证 SQLite 处理 JSON）。"""

    def test_chat_history_with_json_column(self, real_db):
        """JSON 列（tool_args / attachments / meta）能正常读写。"""
        from models.database import AgentProfile, ChatHistory, User
        db = real_db()
        user = User(username="tester", password_hash="h", salt=None)
        db.add(user)
        db.commit()
        agent = AgentProfile(user_id=user.id, hash="5656", totp_secret="x")
        db.add(agent)
        db.commit()

        chat = ChatHistory(
            user_id=user.id,
            agent_hash="5656",
            role="assistant",
            content="hello",
            tool_args={"query": "test.py", "depth": 2},  # dict
            attachments=[{"type": "image", "url": "x.png"}],  # list
            meta={"wechat_metadata": {"msg_id": "abc"}},
        )
        db.add(chat)
        db.commit()

        fetched = db.query(ChatHistory).first()
        assert fetched.tool_args == {"query": "test.py", "depth": 2}
        assert fetched.attachments == [{"type": "image", "url": "x.png"}]
        assert fetched.meta == {"wechat_metadata": {"msg_id": "abc"}}

    def test_chat_history_incremental_query(self, real_db):
        """after_id 增量查询（P1.4 模拟 desktop_api 真实查询）。"""
        from models.database import AgentProfile, ChatHistory, User
        db = real_db()
        user = User(username="u", password_hash="h", salt=None)
        db.add(user)
        db.commit()
        db.add(AgentProfile(user_id=user.id, hash="abcd", totp_secret="x"))
        db.commit()

        for i in range(10):
            db.add(ChatHistory(
                user_id=user.id, agent_hash="abcd",
                role="user", content=f"msg-{i}",
            ))
        db.commit()

        # 模拟 desktop_api.py 的增量查询逻辑
        all_msgs = db.query(ChatHistory).filter(
            ChatHistory.agent_hash == "abcd",
            ChatHistory.user_id == user.id,
        ).order_by(ChatHistory.id.asc()).all()

        assert len(all_msgs) == 10
        assert all_msgs[0].content == "msg-0"
        assert all_msgs[-1].content == "msg-9"

        # after_id=5 查询
        after = db.query(ChatHistory).filter(
            ChatHistory.agent_hash == "abcd",
            ChatHistory.id > 5,
        ).order_by(ChatHistory.id.asc()).all()
        assert len(after) == 5


class TestRealDBSchemaCompleteness:
    """P1.4 验证：所有 FeClaw 表都能在 SQLite 中创建成功。"""

    def test_all_tables_created(self, real_db):
        """所有 model 表都已创建（通过 query count = 0 验证 schema）。"""
        from sqlalchemy import inspect
        # 通过 patch 拿到 engine（real_db fixture 内的 engine 是局部的，这里通过创建另一个 session 间接验证）
        db = real_db()

        # 通过 query 验证关键表都存在（若表不存在会抛 OperationalError）
        from models.database import User, AgentProfile, ChatHistory, ShareMapping
        assert db.query(User).count() == 0
        assert db.query(AgentProfile).count() == 0
        assert db.query(ChatHistory).count() == 0
        assert db.query(ShareMapping).count() == 0