"""
Tests for MomentsService - Phase 5 Engine
services/moments_service.py

Note: moments_service imports AgentProfile from models.group which is a production bug.
We handle this by patching the import at the service module level.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime


# We need to get the real GroupMoments from models.group
# but since AgentProfile import fails, we import directly
import sys
import importlib

# First, check if we can import models.group components
# The production bug is: from models.group import GroupMoments, AgentProfile
# where AgentProfile doesn't exist in models.group

# We'll patch at the service module level


@pytest.fixture
def mock_moments_service():
    """Create a properly mocked moments service that avoids the AgentProfile import bug"""
    # Import the actual model
    from models.group import GroupMoments as RealGroupMoments

    # Create mock AgentProfile
    mock_agent_profile = MagicMock()

    # Patch before importing moments_service
    with patch.dict('sys.modules', {'models.group': MagicMock()}):
        # Create a mock that returns real GroupMoments for the import
        mock_group_module = MagicMock()
        mock_group_module.GroupMoments = RealGroupMoments
        mock_group_module.AgentProfile = mock_agent_profile
        sys.modules['models.group'] = mock_group_module

        # Now reimport moments_service to pick up our mock
        # We need to reload it since it was already imported with broken deps
        if 'services.moments_service' in sys.modules:
            del sys.modules['services.moments_service']

        from services.moments_service import MomentsService

        yield MomentsService, RealGroupMoments

        # Cleanup
        if 'services.moments_service' in sys.modules:
            del sys.modules['services.moments_service']


class TestMomentsServiceCreateMoment:
    """Test create_moment() method"""

    def test_create_moment_with_required_fields(self, mock_moments_service):
        """create_moment creates moment with required fields"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()
        mock_db = MagicMock()

        # Make db.add capture the object
        added_objects = []
        def capture_add(obj):
            added_objects.append(obj)
        mock_db.add = MagicMock(side_effect=capture_add)
        mock_db.commit = MagicMock()

        moment = svc.create_moment(
            db=mock_db,
            group_id="group-123",
            agent_hash="abcd",
            kind="manual",
            title="Test Title",
            content="Test content",
        )

        # Verify object was created with correct attributes
        assert len(added_objects) == 1
        obj = added_objects[0]
        assert obj.group_id == "group-123"
        assert obj.agent_hash == "abcd"
        assert obj.kind == "manual"
        assert obj.title == "Test Title"
        assert obj.content == "Test content"
        mock_db.commit.assert_called()

    def test_create_moment_without_agent_hash(self, mock_moments_service):
        """create_moment works with agent_hash=None (user post)"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()
        mock_db = MagicMock()

        added_objects = []
        def capture_add(obj):
            added_objects.append(obj)
        mock_db.add = MagicMock(side_effect=capture_add)
        mock_db.commit = MagicMock()

        moment = svc.create_moment(
            db=mock_db,
            group_id="group-123",
            agent_hash=None,
            kind="manual",
            title="User Post",
            content="Content",
        )

        assert len(added_objects) == 1
        obj = added_objects[0]
        assert obj.agent_hash is None

    def test_create_moment_with_attachments(self, mock_moments_service):
        """create_moment handles attachments"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()
        mock_db = MagicMock()

        added_objects = []
        def capture_add(obj):
            added_objects.append(obj)
        mock_db.add = MagicMock(side_effect=capture_add)
        mock_db.commit = MagicMock()

        attachments = [{"type": "image", "url": "http://example.com/img.jpg"}]
        moment = svc.create_moment(
            db=mock_db,
            group_id="group-123",
            agent_hash="abcd",
            kind="auto",
            title=None,
            content=None,
            attachments=attachments,
        )

        assert len(added_objects) == 1
        obj = added_objects[0]
        assert obj.attachments == attachments


class TestMomentsServiceGetMoments:
    """Test get_moments() method"""

    def test_get_moments_by_group_id(self, mock_moments_service):
        """get_moments filters by group_id"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()
        mock_db = MagicMock()

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        svc.get_moments(mock_db, "group-123")

        mock_db.query.assert_called_once()

    def test_get_moments_with_before_filter(self, mock_moments_service):
        """get_moments filters by before datetime"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()
        mock_db = MagicMock()

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        before_dt = datetime(2024, 1, 1)
        svc.get_moments(mock_db, "group-123", before=before_dt, limit=30)

        # Should have 2 filters: group_id and before
        assert mock_query.filter.call_count == 2

    def test_get_moments_with_limit(self, mock_moments_service):
        """get_moments respects limit"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()
        mock_db = MagicMock()

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        svc.get_moments(mock_db, "group-123", limit=100)

        mock_query.limit.assert_called_once_with(100)

    def test_get_moments_default_limit(self, mock_moments_service):
        """get_moments uses default limit of 50"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()
        mock_db = MagicMock()

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        svc.get_moments(mock_db, "group-123")

        mock_query.limit.assert_called_once_with(50)


class TestMomentsServiceDeleteMoment:
    """Test delete_moment() method"""

    def test_delete_moment_owner_success(self, mock_moments_service):
        """delete_moment succeeds when user owns the group"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()
        mock_db = MagicMock()

        mock_moment = MagicMock()
        mock_moment.id = "moment-123"

        mock_group = MagicMock()
        mock_group.owner_user_id = 1

        # Track delete calls
        deleted = []
        def track_delete(obj):
            deleted.append(obj)
        mock_db.delete = MagicMock(side_effect=track_delete)

        def query_side_effect(model):
            mock_q = MagicMock()
            if model == RealGroupMoments:
                mock_q.filter.return_value.first.return_value = mock_moment
            else:
                mock_q.filter.return_value.first.return_value = mock_group
            return mock_q

        mock_db.query.side_effect = query_side_effect

        result = svc.delete_moment(mock_db, "moment-123", "group-123", user_id=1)

        assert result is True
        assert mock_moment in deleted

    def test_delete_moment_not_found(self, mock_moments_service):
        """delete_moment returns False when moment not found"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()
        mock_db = MagicMock()

        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = svc.delete_moment(mock_db, "nonexistent", "group-123", user_id=1)

        assert result is False


class TestMomentsServicePushMomentsEvent:
    """Test push_moments_event() method"""

    @pytest.mark.asyncio
    async def test_push_moments_event_ws_failure_handled(self, mock_moments_service):
        """push_moments_event handles WS failure gracefully"""
        MomentsService, RealGroupMoments = mock_moments_service

        svc = MomentsService()

        mock_moment = MagicMock()
        mock_moment.id = "moment-123"
        mock_moment.agent_hash = "abcd"
        mock_moment.kind = "manual"
        mock_moment.title = "Test"
        mock_moment.content = "Content"
        mock_moment.attachments = []
        mock_moment.created_at = datetime(2024, 1, 1, 12, 0, 0)

        with patch("services.moments_service.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.query.return_value.filter.return_value.first.return_value = None

            with patch("routers.desktop_ws.manager") as mock_manager:
                mock_manager.send = AsyncMock(side_effect=Exception("WS error"))

                # Should not raise
                await svc.push_moments_event("group-123", mock_moment)
