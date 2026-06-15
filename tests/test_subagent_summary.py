"""
测试子代理返回摘要功能
"""
import json
import pytest
from unittest.mock import Mock, patch, MagicMock


class TestSubagentSummary:
    """测试子代理摘要功能"""

    def test_summarize_output_structure(self):
        """测试结构化摘要生成"""
        # Mock 依赖
        with patch('services.agent_tools_service.VirtualFileSystem') as mock_vfs_class, \
             patch('services.agent_tools_service.PermissionService') as mock_perm_class, \
             patch('services.agent_tools_service.SessionLocal') as mock_session:

            # Mock 数据库查询
            mock_db = Mock()
            mock_agent = Mock()
            mock_agent.user_id = 1
            mock_db.query.return_value.filter.return_value.first.return_value = mock_agent
            mock_session.return_value = mock_db

            from services.agent_tools_service import AgentToolsService

            service = AgentToolsService(agent_hash="test")

            # Mock OpenAI 客户端 - 在方法内部创建时替换
            mock_response = Mock()
            mock_response.choices = [Mock()]
            mock_response.choices[0].message.content = json.dumps({
                "task_completed": True,
                "key_results": "成功创建了配置文件并完成了初始化",
                "files_created": ["workspace/config.yaml", "workspace/.env"],
                "files_modified": ["workspace/main.py"],
                "failure_reason": None
            })

            mock_client = Mock()
            mock_client.chat.completions.create.return_value = mock_response

            # 替换方法内部的 OpenAI 调用
            with patch.dict('sys.modules', {'openai': Mock(OpenAI=Mock(return_value=mock_client))}):
                # 调用方法
                result = service._summarize_subagent_output(
                    task="创建项目配置文件",
                    output="这是一个很长的输出..." * 100,
                    model="test-model",
                    elapsed_time=10.5
                )

            # 验证返回结构
            assert isinstance(result, dict)
            assert "task_completed" in result
            assert "key_results" in result
            assert "files_created" in result
            assert "files_modified" in result
            assert "failure_reason" in result

            assert result["task_completed"] == True
            assert len(result["files_created"]) == 2
            assert len(result["files_modified"]) == 1
            assert result["failure_reason"] is None

    def test_format_summary_output_success(self):
        """测试格式化摘要输出（成功情况）"""
        with patch('services.agent_tools_service.VirtualFileSystem') as mock_vfs_class, \
             patch('services.agent_tools_service.PermissionService') as mock_perm_class, \
             patch('services.agent_tools_service.SessionLocal') as mock_session:

            # Mock 数据库查询
            mock_db = Mock()
            mock_agent = Mock()
            mock_agent.user_id = 1
            mock_db.query.return_value.filter.return_value.first.return_value = mock_agent
            mock_session.return_value = mock_db

            from services.agent_tools_service import AgentToolsService

            service = AgentToolsService(agent_hash="test")

            summary = {
                "task_completed": True,
                "key_results": "成功创建了3个文件",
                "files_created": ["file1.py", "file2.py"],
                "files_modified": ["file3.py"],
                "failure_reason": None
            }

            result = service._format_summary_output(summary, 5000, "workspace/subagent_logs/test.md")

            # 验证输出格式
            assert "📋 **输出摘要**" in result
            assert "✅ **任务状态**: 完成" in result
            assert "📝 **关键结果**" in result
            assert "📁 **创建的文件**" in result
            assert "✏️ **修改的文件**" in result
            assert "workspace/subagent_logs/test.md" in result

    def test_format_summary_output_failure(self):
        """测试格式化摘要输出（失败情况）"""
        with patch('services.agent_tools_service.VirtualFileSystem') as mock_vfs_class, \
             patch('services.agent_tools_service.PermissionService') as mock_perm_class, \
             patch('services.agent_tools_service.SessionLocal') as mock_session:

            # Mock 数据库查询
            mock_db = Mock()
            mock_agent = Mock()
            mock_agent.user_id = 1
            mock_db.query.return_value.filter.return_value.first.return_value = mock_agent
            mock_session.return_value = mock_db

            from services.agent_tools_service import AgentToolsService

            service = AgentToolsService(agent_hash="test")

            summary = {
                "task_completed": False,
                "key_results": "任务执行失败",
                "files_created": [],
                "files_modified": [],
                "failure_reason": "权限不足，无法写入文件"
            }

            result = service._format_summary_output(summary, 3000, None)

            # 验证输出格式
            assert "❌ **任务状态**: 未完成" in result
            assert "⚠️ **失败原因**: 权限不足，无法写入文件" in result

    def test_json_extraction_from_markdown(self):
        """测试从 markdown 代码块中提取 JSON"""
        with patch('services.agent_tools_service.VirtualFileSystem') as mock_vfs_class, \
             patch('services.agent_tools_service.PermissionService') as mock_perm_class, \
             patch('services.agent_tools_service.SessionLocal') as mock_session:

            # Mock 数据库查询
            mock_db = Mock()
            mock_agent = Mock()
            mock_agent.user_id = 1
            mock_db.query.return_value.filter.return_value.first.return_value = mock_agent
            mock_session.return_value = mock_db

            from services.agent_tools_service import AgentToolsService

            service = AgentToolsService(agent_hash="test")

            # Mock OpenAI 返回 markdown 包裹的 JSON
            mock_response = Mock()
            mock_response.choices = [Mock()]
            mock_response.choices[0].message.content = '''```json
{
    "task_completed": true,
    "key_results": "测试成功",
    "files_created": [],
    "files_modified": [],
    "failure_reason": null
}
```'''

            mock_client = Mock()
            mock_client.chat.completions.create.return_value = mock_response

            with patch.dict('sys.modules', {'openai': Mock(OpenAI=Mock(return_value=mock_client))}):
                result = service._summarize_subagent_output(
                    task="测试任务",
                    output="长输出...",
                    model="test-model",
                    elapsed_time=5.0
                )

            assert result["task_completed"] == True
            assert result["key_results"] == "测试成功"

    def test_read_subagent_log(self):
        """测试读取子代理日志"""
        with patch('services.agent_tools_service.VirtualFileSystem') as mock_vfs_class, \
             patch('services.agent_tools_service.PermissionService') as mock_perm_class, \
             patch('services.agent_tools_service.SessionLocal') as mock_session:

            # Mock 数据库查询
            mock_db = Mock()
            mock_agent = Mock()
            mock_agent.user_id = 1
            mock_db.query.return_value.filter.return_value.first.return_value = mock_agent
            mock_session.return_value = mock_db

            from services.agent_tools_service import AgentToolsService

            service = AgentToolsService(agent_hash="test")

            # Mock file_read 方法
            service.file_read = Mock(return_value="# 日志内容\n测试日志...")

            # 测试有效路径
            result = service.read_subagent_log("workspace/subagent_logs/2024-01-15_10-30-00_test.md")
            assert result == "# 日志内容\n测试日志..."

            # 测试无效路径
            result = service.read_subagent_log("invalid/path.md")
            assert "Error: 无效的日志路径" in result

            # 测试非 .md 文件
            result = service.read_subagent_log("workspace/subagent_logs/test.txt")
            assert "Error: 日志文件应为 .md 格式" in result

    def test_fallback_on_json_parse_error(self):
        """测试 JSON 解析失败时的降级处理"""
        with patch('services.agent_tools_service.VirtualFileSystem') as mock_vfs_class, \
             patch('services.agent_tools_service.PermissionService') as mock_perm_class, \
             patch('services.agent_tools_service.SessionLocal') as mock_session:

            # Mock 数据库查询
            mock_db = Mock()
            mock_agent = Mock()
            mock_agent.user_id = 1
            mock_db.query.return_value.filter.return_value.first.return_value = mock_agent
            mock_session.return_value = mock_db

            from services.agent_tools_service import AgentToolsService

            service = AgentToolsService(agent_hash="test")

            # Mock OpenAI 返回无效 JSON
            mock_response = Mock()
            mock_response.choices = [Mock()]
            mock_response.choices[0].message.content = "这不是有效的 JSON"

            mock_client = Mock()
            mock_client.chat.completions.create.return_value = mock_response

            with patch.dict('sys.modules', {'openai': Mock(OpenAI=Mock(return_value=mock_client))}):
                result = service._summarize_subagent_output(
                    task="测试任务",
                    output="这是一个长输出..." * 100,
                    model="test-model",
                    elapsed_time=5.0
                )

            # 验证降级处理：返回基本摘要
            assert isinstance(result, dict)
            assert "task_completed" in result
            assert "key_results" in result
            # 降级处理时，key_results 应包含部分原始输出
            assert len(result["key_results"]) <= 500


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
