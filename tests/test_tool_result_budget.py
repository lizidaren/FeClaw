"""工具调用结果统一日志与 50KB 上下文预算测试。"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.agent_tools_service import AgentToolsService


@pytest.fixture
def service():
    with patch("services.tools.base.VirtualFileSystem"), patch(
        "services.tools.base.PermissionService"
    ):
        instance = AgentToolsService(agent_hash="test")
    instance._vfs = MagicMock()
    return instance


@pytest.mark.asyncio
class TestToolResultBudget:
    async def test_small_result_is_logged_but_not_truncated(self, service):
        small_result = "这是一个小结果。" * 100
        service._log_tool_result = AsyncMock(
            return_value=".logs/tools/2026-07-19/test_tool/call-1.log"
        )

        result = await service._truncate_tool_result(
            result=small_result,
            tool_name="test_tool",
            call_id="call-1",
        )

        assert result == small_result
        service._log_tool_result.assert_awaited_once_with(
            tool_name="test_tool",
            result=small_result,
            call_id="call-1",
        )

    async def test_exact_threshold_is_logged_but_not_truncated(self, service):
        boundary_result = "x" * service.TOOL_RESULT_MAX_SIZE
        service._log_tool_result = AsyncMock(return_value=".logs/tools/x.log")

        result = await service._truncate_tool_result(
            result=boundary_result,
            tool_name="test_tool",
        )

        assert result == boundary_result
        service._log_tool_result.assert_awaited_once()

    async def test_large_result_returns_preview_and_new_path(self, service):
        large_result = "测试内容" * 20000
        log_path = ".logs/tools/2026-07-19/bash/call-2.log"
        service._log_tool_result = AsyncMock(return_value=log_path)

        result = await service._truncate_tool_result(
            result=large_result,
            tool_name="bash",
            tool_args={"command": "ls -la"},
            call_id="call-2",
        )

        preview = result.split("---", 1)[0]
        assert len(preview.encode("utf-8")) <= service.TOOL_RESULT_PREVIEW_SIZE + 2
        assert log_path in result
        assert "结果超过 50KB" in result
        assert large_result not in result

    async def test_large_result_reports_log_failure(self, service):
        large_result = "测试内容" * 20000
        service._log_tool_result = AsyncMock(return_value="")

        result = await service._truncate_tool_result(
            result=large_result,
            tool_name="test_tool",
        )

        assert "尝试保存到 VFS 失败" in result
        assert len(result.encode("utf-8")) < service.TOOL_RESULT_MAX_SIZE

    async def test_none_and_non_string_results_are_normalized_and_logged(self, service):
        service._log_tool_result = AsyncMock(return_value=".logs/tools/x.log")

        none_result = await service._truncate_tool_result(None, "test_tool")
        assert none_result == ""
        service._log_tool_result.assert_awaited_with(
            tool_name="test_tool", result="", call_id=None
        )

        dict_result = await service._truncate_tool_result(
            {"key": "value"}, "test_tool"
        )
        assert dict_result == "{'key': 'value'}"
        service._log_tool_result.assert_awaited_with(
            tool_name="test_tool",
            result="{'key': 'value'}",
            call_id=None,
        )

    async def test_log_path_uses_date_tool_and_sanitized_call_id(self, service):
        service._vfs.write_tool_log.return_value = "OK: 已写入"

        path = await service._log_tool_result(
            tool_name="web/search",
            result="完整结果",
            call_id="call/id:123",
        )

        assert re.fullmatch(
            r"\.logs/tools/\d{4}-\d{2}-\d{2}/web_search/call_id_123\.log",
            path,
        )
        service._vfs.write_tool_log.assert_called_once_with(path, "完整结果")

    async def test_log_path_uses_uuid_without_call_id(self, service):
        service._vfs.write_tool_log.return_value = "OK: 已写入"

        path = await service._log_tool_result("test_tool", "result")

        assert re.fullmatch(
            r"\.logs/tools/\d{4}-\d{2}-\d{2}/test_tool/[0-9a-f]{32}\.log",
            path,
        )


class TestThresholdValues:
    def test_threshold_constants(self, service):
        assert service.TOOL_RESULT_MAX_SIZE == 50000
        assert service.TOOL_RESULT_PREVIEW_SIZE == 2000
