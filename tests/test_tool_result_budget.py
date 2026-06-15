"""
P0-Tool-Result-Budget 功能测试

测试工具结果截断功能：
- 超过 50KB 的结果保存到 VFS
- 仅保留 2KB 预览在上下文中
"""

import pytest
from unittest.mock import MagicMock, patch
from services.agent_tools_service import AgentToolsService


class TestToolResultBudget:
    """工具结果预算功能测试"""

    def setup_method(self):
        """测试前准备"""
        # Mock 依赖
        self.mock_storage = MagicMock()
        self.mock_vfs = MagicMock()

    def test_small_result_not_truncated(self):
        """测试小结果不被截断"""
        service = AgentToolsService(agent_hash="test")

        # 小于 50KB 的结果应该不被截断
        small_result = "这是一个小结果，不会触发截断机制。" * 100  # ~2KB
        truncated = service._truncate_tool_result(
            result=small_result,
            tool_name="test_tool"
        )

        assert truncated == small_result
        assert len(truncated.encode('utf-8')) < service.TOOL_RESULT_MAX_SIZE

    def test_large_result_truncated(self):
        """测试大结果被截断"""
        service = AgentToolsService(agent_hash="test")

        # Mock file_write 方法
        with patch.object(service, 'file_write', return_value="OK: 已写入 workspace/tool_results/test.md"):
            # 创建超过 50KB 的结果
            large_result = "这是一个大结果，会触发截断机制。" * 2000  # ~60KB

            truncated = service._truncate_tool_result(
                result=large_result,
                tool_name="test_tool",
                tool_args={"arg1": "value1"},
                call_id="test_123"
            )

            # 验证截断后的结果
            assert len(truncated.encode('utf-8')) < service.TOOL_RESULT_MAX_SIZE
            assert "50KB" in truncated or "已截断" in truncated
            assert "workspace/tool_results" in truncated

    def test_large_result_preview_size(self):
        """测试大结果的预览大小"""
        service = AgentToolsService(agent_hash="test")

        with patch.object(service, 'file_write', return_value="OK: 已写入 workspace/tool_results/test.md"):
            # 创建超过 50KB 的结果
            large_result = "测试内容" * 20000  # ~80KB

            truncated = service._truncate_tool_result(
                result=large_result,
                tool_name="bash",
                tool_args={"command": "ls -la"}
            )

            # 验证预览部分不超过 2KB
            preview_part = truncated.split("---")[0] if "---" in truncated else truncated
            assert len(preview_part.encode('utf-8')) <= service.TOOL_RESULT_PREVIEW_SIZE + 500  # 预览 + 提示信息

    def test_file_write_failure(self):
        """测试文件写入失败时的处理"""
        service = AgentToolsService(agent_hash="test")

        with patch.object(service, 'file_write', return_value="Error: 写入失败"):
            large_result = "测试内容" * 20000  # ~80KB

            truncated = service._truncate_tool_result(
                result=large_result,
                tool_name="test_tool"
            )

            # 即使写入失败，也应该返回截断的结果
            assert "失败" in truncated or "已截断" in truncated

    def test_none_result(self):
        """测试 None 结果"""
        service = AgentToolsService(agent_hash="test")

        truncated = service._truncate_tool_result(
            result=None,
            tool_name="test_tool"
        )

        assert truncated == "" or truncated == "None"

    def test_non_string_result(self):
        """测试非字符串结果"""
        service = AgentToolsService(agent_hash="test")

        # dict 结果
        truncated = service._truncate_tool_result(
            result={"key": "value"},
            tool_name="test_tool"
        )

        assert isinstance(truncated, str)

    def test_chinese_content_size(self):
        """测试中文内容的字节大小计算"""
        service = AgentToolsService(agent_hash="test")

        # 中文内容，每个字符约 3 bytes (UTF-8)
        chinese_result = "中文测试内容" * 15000  # 约 180KB

        with patch.object(service, 'file_write', return_value="OK: 已写入"):
            truncated = service._truncate_tool_result(
                result=chinese_result,
                tool_name="test_tool"
            )

            # 验证中文内容正确处理
            assert len(truncated.encode('utf-8')) < service.TOOL_RESULT_MAX_SIZE

    def test_call_id_generation(self):
        """测试调用 ID 自动生成"""
        service = AgentToolsService(agent_hash="abcd")

        with patch.object(service, 'file_write', return_value="OK: 已写入"):
            large_result = "测试" * 20000

            truncated = service._truncate_tool_result(
                result=large_result,
                tool_name="test_tool"
            )

            # 验证生成的路径包含日期格式
            assert "workspace/tool_results/" in truncated


class TestThresholdValues:
    """阈值值测试"""

    def test_max_size_threshold(self):
        """测试最大大小阈值"""
        service = AgentToolsService(agent_hash="test")

        assert service.TOOL_RESULT_MAX_SIZE == 50000  # 50KB
        assert service.TOOL_RESULT_PREVIEW_SIZE == 2000  # 2KB

    def test_exact_threshold_boundary(self):
        """测试阈值边界"""
        service = AgentToolsService(agent_hash="test")

        # 正好在阈值边界的结果
        boundary_result = "x" * 50000  # 正好 50KB

        with patch.object(service, 'file_write', return_value="OK: 已写入"):
            # 不应该被截断（因为 <= 阈值）
            truncated = service._truncate_tool_result(
                result=boundary_result,
                tool_name="test_tool"
            )

            assert truncated == boundary_result

        # 略超过阈值
        over_boundary = "x" * 50001  # 略超过 50KB

        with patch.object(service, 'file_write', return_value="OK: 已写入"):
            truncated = service._truncate_tool_result(
                result=over_boundary,
                tool_name="test_tool"
            )

            assert truncated != over_boundary
            assert len(truncated.encode('utf-8')) < service.TOOL_RESULT_MAX_SIZE