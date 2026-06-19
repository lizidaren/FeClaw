"""
记忆系统单元测试（SessionMemoryService）

测试覆盖：
1. should_extract 阈值判断（未初始化/初始化后/增量不足）
2. is_memory_initialized 记忆文件检查
3. DISTILL_INTERVAL 和模块级计数器

所有测试 mock 外部依赖，不真调 LLM/VFS。
"""

import pytest
from unittest.mock import MagicMock, patch

from services.session_memory_service import (
    SessionMemoryService,
    SessionMemoryConfig,
    DISTILL_INTERVAL,
    MEMORY_FILE_PATH,
)

pytestmark = pytest.mark.unit


class TestShouldExtractUninitialized:
    """未初始化时的 should_extract 测试"""

    def test_not_enough_messages(self):
        """消息不足 MIN_MESSAGES_TO_INIT 时不应提取"""
        result = SessionMemoryService.should_extract(
            messages=[{"role": "user", "content": "hi"}],
            is_initialized=False,
        )
        assert result["should_extract"] is False
        assert "消息不足" in result["reason"]

    def test_reaches_init_threshold(self):
        """消息达到 MIN_MESSAGES_TO_INIT 时应提取"""
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(SessionMemoryConfig.MIN_MESSAGES_TO_INIT)]
        result = SessionMemoryService.should_extract(
            messages=messages,
            is_initialized=False,
        )
        assert result["should_extract"] is True
        assert "首次提取" in result["reason"]
        assert result["initialized"] is False

    def test_exactly_at_threshold(self):
        """消息数刚好等于阈值时也应提取"""
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(3)]
        result = SessionMemoryService.should_extract(
            messages=messages,
            is_initialized=False,
        )
        assert result["should_extract"] is True


class TestShouldExtractInitialized:
    """已初始化时的 should_extract 测试"""

    def test_no_new_messages(self):
        """没有新消息时不应提取"""
        result = SessionMemoryService.should_extract(
            messages=[{"role": "user", "content": "hi"}],
            is_initialized=True,
            last_extract_msg_index=1,
        )
        assert result["should_extract"] is False
        assert "增量不足" in result["reason"]

    def test_new_messages_with_tool_calls(self):
        """新消息数和工具调用数均达标时应提取"""
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
        result = SessionMemoryService.should_extract(
            messages=messages,
            is_initialized=True,
            last_extract_msg_index=0,
            tool_calls_since=3,
        )
        assert result["should_extract"] is True
        assert "增量更新" in result["reason"]

    def test_natural_break(self):
        """自然断点（新消息达标但工具调用不足）时应提取"""
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
        result = SessionMemoryService.should_extract(
            messages=messages,
            is_initialized=True,
            last_extract_msg_index=0,
            tool_calls_since=1,
        )
        assert result["should_extract"] is True
        assert "自然断点" in result["reason"]

    def test_insufficient_new_messages(self):
        """新消息不足 MIN_MESSAGES_BETWEEN_UPDATES 时不应提取"""
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(4)]
        result = SessionMemoryService.should_extract(
            messages=messages,
            is_initialized=True,
            last_extract_msg_index=2,
            tool_calls_since=0,
        )
        assert result["should_extract"] is False

    def test_custom_last_extract_index(self):
        """应从 last_extract_msg_index 开始计算新消息数"""
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
        # 从索引 7 开始，新消息 = 3，刚好达到阈值
        result = SessionMemoryService.should_extract(
            messages=messages,
            is_initialized=True,
            last_extract_msg_index=7,
            tool_calls_since=0,
        )
        assert result["should_extract"] is True
        assert result["new_msg_count"] == 3


class TestIsMemoryInitialized:
    """is_memory_initialized 测试"""

    def test_file_exists_with_content(self):
        """文件存在且有内容时应返回 True"""
        with patch("services.session_memory_service.VirtualFileSystem") as mock_vfs_cls:
            mock_vfs = MagicMock()
            mock_vfs_cls.return_value = mock_vfs
            mock_vfs.cat.return_value = "# Session Memory\n\nSome content here"
            svc = SessionMemoryService(agent_hash="abcd")
            assert svc.is_memory_initialized() is True
            mock_vfs.cat.assert_called_with(MEMORY_FILE_PATH)

    def test_file_is_empty(self):
        """文件为 (空 开头时应返回 False"""
        with patch("services.session_memory_service.VirtualFileSystem") as mock_vfs_cls:
            mock_vfs = MagicMock()
            mock_vfs_cls.return_value = mock_vfs
            mock_vfs.cat.return_value = "(空 — 文件不存在)"
            svc = SessionMemoryService(agent_hash="abcd")
            assert svc.is_memory_initialized() is False

    def test_file_error(self):
        """文件读取错误时应返回 False"""
        with patch("services.session_memory_service.VirtualFileSystem") as mock_vfs_cls:
            mock_vfs = MagicMock()
            mock_vfs_cls.return_value = mock_vfs
            mock_vfs.cat.return_value = "Error: file not found"
            svc = SessionMemoryService(agent_hash="abcd")
            assert svc.is_memory_initialized() is False


class TestDistillInterval:
    """蒸馏间隔测试"""

    def test_distill_interval_constant(self):
        """DISTILL_INTERVAL 应为 5"""
        assert DISTILL_INTERVAL == 5

    def test_config_constants(self):
        """配置常量应有合理的默认值"""
        assert SessionMemoryConfig.MIN_MESSAGES_TO_INIT >= 1
        assert SessionMemoryConfig.MIN_MESSAGES_BETWEEN_UPDATES >= 1
        assert SessionMemoryConfig.NATURAL_BREAK_MIN_MESSAGES >= 1
