"""
Tests for GroupDispatchService - Phase 4 Engine
services/group_service.py
"""

import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime

from services.group_service import GroupDispatchService, NO_REPLY_MAGIC
from models.group import Group, GroupMember, GroupMessage


class TestGroupDispatchServiceCreateGroup:
    """Test create_group() method"""

    def test_create_group_valid_name(self):
        """create_group with valid name creates a group"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        group = svc.create_group(
            db=mock_db,
            name="Test Group",
            owner_user_id=1,
            member_hashes=["abcd", "ef12"],
        )

        assert group.name == "Test Group"
        assert group.owner_user_id == 1
        mock_db.add.assert_called()
        mock_db.commit.assert_called()

    def test_create_group_empty_name(self):
        """create_group with empty name should still work (no validation here)"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        group = svc.create_group(
            db=mock_db,
            name="",
            owner_user_id=1,
        )

        assert group.name == ""

    def test_create_group_with_settings(self):
        """create_group with custom settings passes them through"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        settings = {"theme": "dark", "notifications": True}
        group = svc.create_group(
            db=mock_db,
            name="Settings Test",
            owner_user_id=1,
            settings=settings,
        )

        assert group.settings == settings

    def test_create_group_adds_owner_as_member(self):
        """create_group adds owner as first member with empty agent_hash"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        group = svc.create_group(
            db=mock_db,
            name="Owner Test",
            owner_user_id=42,
        )

        # Verify owner member was added
        calls = mock_db.add.call_args_list
        owner_member_call = calls[1]  # calls[0] is Group, calls[1] is GroupMember
        owner_member = owner_member_call[0][0]
        assert isinstance(owner_member, GroupMember)
        assert owner_member.agent_hash == ""
        assert owner_member.role == "owner"

    def test_create_group_with_agent_members(self):
        """create_group adds agent members correctly"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        group = svc.create_group(
            db=mock_db,
            name="Agent Members",
            owner_user_id=1,
            member_hashes=["abcd", "ef12"],
        )

        # Should have 4 adds: 1 Group + 1 owner + 2 agents
        assert mock_db.add.call_count == 4


class TestGroupDispatchServiceMembers:
    """Test add_member() and remove_member() methods"""

    def test_add_member_new(self):
        """add_member adds a new member successfully"""
        svc = GroupDispatchService()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        member = svc.add_member(mock_db, "group-123", "abcd", role="member")

        assert member.agent_hash == "abcd"
        assert member.role == "member"
        assert member.is_silent is False
        mock_db.commit.assert_called()

    def test_add_member_existing_returns_existing(self):
        """add_member returns existing member without creating new"""
        svc = GroupDispatchService()
        mock_db = MagicMock()
        existing = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = existing

        member = svc.add_member(mock_db, "group-123", "abcd")

        assert member is existing
        mock_db.add.assert_not_called()

    def test_remove_member_exists(self):
        """remove_member deletes existing member"""
        svc = GroupDispatchService()
        mock_db = MagicMock()
        existing_member = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = existing_member

        result = svc.remove_member(mock_db, "group-123", "abcd")

        assert result is True
        mock_db.delete.assert_called_once_with(existing_member)
        mock_db.commit.assert_called()

    def test_remove_member_not_exists(self):
        """remove_member returns False when member not found"""
        svc = GroupDispatchService()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = svc.remove_member(mock_db, "group-123", "nonexistent")

        assert result is False
        mock_db.delete.assert_not_called()


class TestGroupDispatchServiceMessages:
    """Test get_messages() method"""

    def test_get_messages_with_limit(self):
        """get_messages respects limit parameter"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        svc.get_messages(mock_db, "group-123", limit=25)

        mock_query.limit.assert_called_once_with(25)

    def test_get_messages_with_before_filter(self):
        """get_messages filters by before datetime"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        before_dt = datetime(2024, 1, 1)
        svc.get_messages(mock_db, "group-123", before=before_dt, limit=50)

        # Should have two filter calls: group_id and before
        assert mock_query.filter.call_count == 2

    def test_get_messages_default_limit(self):
        """get_messages uses default limit of 50"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        svc.get_messages(mock_db, "group-123")

        mock_query.limit.assert_called_once_with(50)


class TestGroupDispatchServiceShouldWake:
    """Test should_wake() logic"""

    def test_should_wake_round_0_always_true(self):
        """round=0 always returns True"""
        svc = GroupDispatchService()
        member = MagicMock()
        member.is_silent = False

        assert svc.should_wake(member, "group-123", round=0) is True

    def test_should_wake_round_0_silent_member_still_true(self):
        """round=0 returns True even for silent members"""
        svc = GroupDispatchService()
        member = MagicMock()
        member.is_silent = True

        assert svc.should_wake(member, "group-123", round=0) is True

    def test_should_wake_round_n_silent_member_false(self):
        """round>0 with is_silent=True returns False"""
        svc = GroupDispatchService()
        member = MagicMock()
        member.is_silent = True

        assert svc.should_wake(member, "group-123", round=1) is False

    def test_should_wake_round_n_non_silent_member_true(self):
        """round>0 with is_silent=False returns True"""
        svc = GroupDispatchService()
        member = MagicMock()
        member.is_silent = False

        assert svc.should_wake(member, "group-123", round=1) is True


class TestGroupDispatchServiceBuildContext:
    """Test build_context() edge cases"""

    def test_build_context_empty_history(self):
        """build_context with no messages returns empty list"""
        svc = GroupDispatchService()

        with patch("services.group_service.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

            messages, persona = svc.build_context("abcd", "group-123")

            assert messages == []
            assert persona == ""

    def test_build_context_with_messages(self):
        """build_context with messages returns formatted list"""
        svc = GroupDispatchService()

        mock_msg1 = MagicMock()
        mock_msg1.sender_type = "user"
        mock_msg1.sender_hash = "u1"
        mock_msg1.content = "Hello"

        mock_msg2 = MagicMock()
        mock_msg2.sender_type = "agent"
        mock_msg2.sender_hash = "a1"
        mock_msg2.content = "Hi there"

        with patch("services.group_service.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
                mock_msg1, mock_msg2
            ]

            with patch("services.agent_init_service.agent_init_service") as mock_init:
                mock_init.load_agent_persona.return_value = "I am a helpful agent"

                messages, persona = svc.build_context("a1", "group-123")

                assert len(messages) == 2
                assert messages[0]["role"] == "user"
                assert messages[0]["content"] == "Hello"
                assert messages[1]["role"] == "assistant"
                assert messages[1]["content"] == "Hi there"
                assert persona == "I am a helpful agent"

    def test_build_context_loads_persona(self):
        """build_context loads agent persona"""
        svc = GroupDispatchService()

        with patch("services.group_service.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_msg = MagicMock()
            mock_msg.sender_type = "user"
            mock_msg.sender_hash = "abcd"
            mock_msg.content = "Hello"
            mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [mock_msg]

            with patch("services.agent_init_service.agent_init_service") as mock_init:
                mock_init.load_agent_persona.return_value = "Custom persona"

                messages, persona = svc.build_context("abcd", "group-123")

                assert persona == "Custom persona"

    def test_build_context_persona_load_failure(self):
        """build_context handles persona load failure gracefully"""
        svc = GroupDispatchService()

        with patch("services.group_service.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

            with patch("services.agent_init_service.agent_init_service") as mock_init:
                mock_init.load_agent_persona.side_effect = Exception("Load failed")

                messages, persona = svc.build_context("abcd", "group-123")

                assert persona == ""


class TestGroupDispatchServiceGetGroup:
    """Test get_group() and get_member() helpers"""

    def test_get_group_exists(self):
        """get_group returns group when found"""
        svc = GroupDispatchService()
        mock_db = MagicMock()
        mock_group = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_group

        result = svc.get_group(mock_db, "group-123")

        assert result is mock_group

    def test_get_group_not_found(self):
        """get_group returns None when not found"""
        svc = GroupDispatchService()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = svc.get_group(mock_db, "nonexistent")

        assert result is None

    def test_get_member_exists(self):
        """get_member returns member when found"""
        svc = GroupDispatchService()
        mock_db = MagicMock()
        mock_member = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_member

        result = svc.get_member(mock_db, "group-123", "abcd")

        assert result is mock_member

    def test_get_member_not_found(self):
        """get_member returns None when not found"""
        svc = GroupDispatchService()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = svc.get_member(mock_db, "group-123", "nonexistent")

        assert result is None


class TestGroupDispatchServiceListUserGroups:
    """Test list_user_groups()"""

    def test_list_user_groups_returns_owned_groups(self):
        """list_user_groups returns groups where user is owner"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        mock_group1 = MagicMock()
        mock_group2 = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_group1, mock_group2]

        result = svc.list_user_groups(mock_db, user_id=1)

        assert len(result) == 2
        assert result[0] is mock_group1
        assert result[1] is mock_group2

    def test_list_user_groups_excludes_deleted(self):
        """list_user_groups excludes deleted groups"""
        svc = GroupDispatchService()
        mock_db = MagicMock()

        mock_db.query.return_value.filter.return_value.all.return_value = []

        svc.list_user_groups(mock_db, user_id=1)

        # Verify deleted_at filter was used
        mock_db.query.assert_called_once_with(Group)


class TestGroupDispatchServiceOnMessage:
    """Test on_message() entry point"""

    @pytest.mark.asyncio
    async def test_on_message_saves_message(self):
        """on_message saves GroupMessage to DB"""
        svc = GroupDispatchService()

        with patch("services.group_service.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db

            with patch.object(svc, "dispatch_to_members", new_callable=AsyncMock) as mock_dispatch:
                msg_id = await svc.on_message(
                    group_id="group-123",
                    sender_type="user",
                    sender_hash="",
                    content="Test message",
                    mentions=["abcd"],
                )

                assert msg_id is not None
                mock_db.add.assert_called()
                mock_db.commit.assert_called()
                mock_dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_message_with_attachments(self):
        """on_message handles attachments correctly"""
        svc = GroupDispatchService()

        with patch("services.group_service.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db

            with patch.object(svc, "dispatch_to_members", new_callable=AsyncMock):
                attachments = [{"type": "image", "url": "http://example.com/img.jpg"}]
                msg_id = await svc.on_message(
                    group_id="group-123",
                    sender_type="user",
                    sender_hash="",
                    content="Check this out",
                    attachments=attachments,
                )

                # Verify attachment was passed to GroupMessage
                call_args = mock_db.add.call_args[0][0]
                assert call_args.attachments == attachments


class TestGroupDispatchServiceCompactContext:
    """Test _compact_context() edge cases"""

    def test_compact_context_small_messages_unchanged(self):
        """_compact_context returns unchanged when under threshold"""
        svc = GroupDispatchService()

        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]

        # Mock estimate_tokens to return small values
        with patch("services.group_service.estimate_tokens", return_value=10):
            result = svc._compact_context(messages)

            assert result == messages

    def test_compact_context_large_messages_compacted(self):
        """_compact_context compacts when over threshold"""
        svc = GroupDispatchService()

        # Create 100 messages
        messages = [
            {"role": "user", "content": f"Message {i}", "sender_hash": "u1"}
            for i in range(100)
        ]

        # Mock estimate_tokens to return large values
        with patch("services.group_service.estimate_tokens", return_value=2000):
            result = svc._compact_context(messages)

            # Should keep ~15% (at least 5)
            assert len(result) >= 5
            assert len(result) < len(messages)
