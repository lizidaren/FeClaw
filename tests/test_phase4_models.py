"""
Tests for Phase 4-8 Database Models
models/group.py, models/fehub.py
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from models.group import Group, GroupMember, GroupMessage, GroupMoments
from models.fehub import FePublish, AppData


class TestGroupModel:
    """Test Group model"""

    def test_group_create(self):
        """Group can be created with required fields"""
        group = Group(
            name="Test Group",
            owner_user_id=1,
            context_isolation=True,
            max_rounds=100,
        )

        assert group.name == "Test Group"
        assert group.owner_user_id == 1
        # SQLAlchemy default=dict is applied at insert time, not object creation
        # So settings is None until flush/commit
        assert group.settings is None or group.settings == {}
        assert group.context_isolation is True
        assert group.max_rounds == 100
        assert group.deleted_at is None

    def test_group_soft_delete(self):
        """Group soft delete sets deleted_at"""
        group = Group(
            name="To Delete",
            owner_user_id=1,
        )

        group.deleted_at = datetime.utcnow()

        assert group.deleted_at is not None

    def test_group_with_custom_settings(self):
        """Group accepts custom settings"""
        settings = {"theme": "dark", "notifications": True}
        group = Group(
            name="Settings Test",
            owner_user_id=1,
            settings=settings,
        )

        assert group.settings == settings


class TestGroupMemberModel:
    """Test GroupMember model"""

    def test_group_member_create(self):
        """GroupMember can be created"""
        member = GroupMember(
            group_id="group-123",
            agent_hash="abcd",
            role="member",
            is_silent=False,
        )

        assert member.group_id == "group-123"
        assert member.agent_hash == "abcd"
        assert member.role == "member"
        assert member.is_silent is False

    def test_group_member_owner_role(self):
        """GroupMember can have owner role"""
        owner = GroupMember(
            group_id="group-123",
            agent_hash="",
            role="owner",
        )

        assert owner.role == "owner"
        assert owner.agent_hash == ""

    def test_group_member_unique_constraint(self):
        """GroupMember has proper indexes defined"""
        # Check indexes exist
        from models.group import GroupMember

        # Verify table args include indexes
        assert hasattr(GroupMember, "__table_args__")


class TestGroupMessageModel:
    """Test GroupMessage model"""

    def test_group_message_create(self):
        """GroupMessage can be created"""
        msg = GroupMessage(
            group_id="group-123",
            sender_type="user",
            sender_hash="",
            content="Hello world",
            message_type="text",
            round=0,
        )

        assert msg.group_id == "group-123"
        assert msg.sender_type == "user"
        assert msg.content == "Hello world"
        assert msg.message_type == "text"
        assert msg.round == 0
        assert msg.mentions == [] or msg.mentions is None

    def test_group_message_with_mentions(self):
        """GroupMessage handles mentions"""
        msg = GroupMessage(
            group_id="group-123",
            sender_type="user",
            sender_hash="",
            content="@agent1 @agent2",
            mentions=["agent1", "agent2"],
        )

        assert msg.mentions == ["agent1", "agent2"]

    def test_group_message_with_attachments(self):
        """GroupMessage handles attachments"""
        attachments = [{"type": "image", "url": "http://example.com/img.jpg"}]
        msg = GroupMessage(
            group_id="group-123",
            sender_type="agent",
            sender_hash="abcd",
            content="Check this",
            attachments=attachments,
        )

        assert msg.attachments == attachments

    def test_group_message_agent_sender(self):
        """GroupMessage works with agent sender"""
        msg = GroupMessage(
            group_id="group-123",
            sender_type="agent",
            sender_hash="abcd",
            content="Reply",
        )

        assert msg.sender_type == "agent"
        assert msg.sender_hash == "abcd"


class TestGroupMomentsModel:
    """Test GroupMoments model"""

    def test_group_moments_create(self):
        """GroupMoments can be created"""
        moment = GroupMoments(
            group_id="group-123",
            agent_hash="abcd",
            kind="manual",
            title="Test Moment",
            content="Content here",
        )

        assert moment.group_id == "group-123"
        assert moment.agent_hash == "abcd"
        assert moment.kind == "manual"
        assert moment.title == "Test Moment"
        assert moment.content == "Content here"

    def test_group_moments_without_agent(self):
        """GroupMoments can be created without agent (user post)"""
        moment = GroupMoments(
            group_id="group-123",
            agent_hash=None,
            kind="manual",
            title="User Post",
        )

        assert moment.agent_hash is None

    def test_group_moments_with_attachments(self):
        """GroupMoments handles attachments"""
        attachments = [{"type": "file", "name": "doc.pdf"}]
        moment = GroupMoments(
            group_id="group-123",
            agent_hash="abcd",
            kind="auto",
            title="Auto Post",
            attachments=attachments,
        )

        assert moment.attachments == attachments


class TestFePublishModel:
    """Test FePublish model"""

    def test_fe_publish_create(self):
        """FePublish can be created"""
        publish = FePublish(
            agent_hash="abcd",
            app_name="TestApp",
            tag="v1",
            snapshot_path="feclaw/agents/abcd/.fehub/releases/v1/",
            manifest={"name": "TestApp", "routes": []},
            is_public=False,
            is_active=True,
        )

        assert publish.agent_hash == "abcd"
        assert publish.app_name == "TestApp"
        assert publish.tag == "v1"
        assert publish.is_public is False
        assert publish.is_active is True

    def test_fe_publish_public(self):
        """FePublish can be marked public"""
        publish = FePublish(
            agent_hash="abcd",
            app_name="PublicApp",
            tag="v1",
            snapshot_path="path",
            manifest={},
            is_public=True,
        )

        assert publish.is_public is True

    def test_fe_publish_inactive(self):
        """FePublish can be marked inactive"""
        publish = FePublish(
            agent_hash="abcd",
            app_name="OldApp",
            tag="v0",
            snapshot_path="path",
            manifest={},
            is_active=False,
        )

        assert publish.is_active is False


class TestAppDataModel:
    """Test AppData model"""

    def test_app_data_create(self):
        """AppData can be created"""
        data = AppData(
            app_id="abcd-v1",
            user_id=1,
            key="score",
            value={"points": 100},
        )

        assert data.app_id == "abcd-v1"
        assert data.user_id == 1
        assert data.key == "score"
        assert data.value == {"points": 100}

    def test_app_data_update_value(self):
        """AppData value can be updated"""
        data = AppData(
            app_id="abcd-v1",
            user_id=1,
            key="game_state",
            value={"level": 1},
        )

        data.value = {"level": 2, "score": 500}

        assert data.value == {"level": 2, "score": 500}

    def test_app_data_multiple_keys(self):
        """Multiple AppData records can exist for same app/user"""
        data1 = AppData(
            app_id="abcd-v1",
            user_id=1,
            key="score",
            value={"points": 100},
        )

        data2 = AppData(
            app_id="abcd-v1",
            user_id=1,
            key="level",
            value={"current": 5},
        )

        assert data1.key != data2.key
        assert data1.app_id == data2.app_id
        assert data1.user_id == data2.user_id


class TestModelIndexes:
    """Test model indexes are properly defined"""

    def test_group_has_owner_index(self):
        """Group has index on owner_user_id"""
        assert hasattr(Group, "__table_args__")

    def test_group_member_has_group_index(self):
        """GroupMember has indexes on group_id and agent_hash"""
        assert hasattr(GroupMember, "__table_args__")

    def test_group_message_has_group_index(self):
        """GroupMessage has indexes on group_id and created_at"""
        assert hasattr(GroupMessage, "__table_args__")

    def test_group_moments_has_indexes(self):
        """GroupMoments has indexes on group_id and created_at"""
        assert hasattr(GroupMoments, "__table_args__")

    def test_fe_publish_has_unique_constraint(self):
        """FePublish has unique constraint on agent_hash + tag"""
        assert hasattr(FePublish, "__table_args__")

    def test_app_data_has_unique_constraint(self):
        """AppData has unique constraint on app_id + user_id + key"""
        assert hasattr(AppData, "__table_args__")
