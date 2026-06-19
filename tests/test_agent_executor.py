"""
Agent 执行引擎单元测试

测试覆盖：
1. 模块级常量 (SubagentPermission, SUBAGENT_PERMISSION_SETS, CONTEXT_LIMIT)
2. AgentExecutor.__init__ (agent_hash, tools, blocked_tools)
3. user_id / agent_profile 懒加载属性
4. _refresh_agent_profile 重新加载
5. _read_vfs_file (正常/Error前缀/异常/空内容)
6. _extract_known_image_paths (多模式匹配/去重/逆序)
7. _validate_path_with_hint (路径校验/提示注入)
8. _estimate_tokens 委托调用
9. _get_tool_definitions (过滤禁用工具)
10. _smart_crop (短文本/长文本头尾裁剪)
11. _build_knowledge_injection (分组/低分过滤/截断)
12. execute_tool (正常/禁用/未知/异常/list_subagent_roles/参数过滤)
13. chat_with_tools (pending/suspended/上下文压缩/SR direct_reply/inject_rules/熔断)

所有测试 mock 外部依赖，不真调 LLM/VFS/DB。
"""

import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from services.agent_executor import (
    AgentExecutor,
    SubagentPermission,
    SUBAGENT_PERMISSION_SETS,
    SUBAGENT_BLOCKED_TOOLS,
    CONTEXT_LIMIT,
)

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────


def _make_mock_tools(with_truncate_side_effect=True):
    """创建 mock AgentToolsService，带 vfs 和 _truncate_tool_result

    Args:
        with_truncate_side_effect: True 时 _truncate_tool_result 直接返回 result 参数值
    """
    tools = MagicMock()
    tools.vfs = MagicMock()
    if with_truncate_side_effect:
        tools._truncate_tool_result = MagicMock(
            side_effect=lambda result, tool_name, tool_args: result
        )
    else:
        tools._truncate_tool_result = MagicMock(return_value="truncated_result")
    return tools


def _make_mock_agent_profile(status="ready", sr_enabled=False, name="TestAgent", description=""):
    """创建 mock AgentProfile"""
    profile = MagicMock()
    profile.status = status
    profile.sr_enabled = sr_enabled
    profile.name = name
    profile.description = description
    return profile


# ─────────────────────────────────────────────
# 1. 模块级常量
# ─────────────────────────────────────────────


class TestModuleConstants:
    """模块级常量和枚举测试"""

    def test_subagent_permission_enum_values(self):
        """SubagentPermission 枚举应有 readonly / standard / full 三个值"""
        assert SubagentPermission.READONLY == "readonly"
        assert SubagentPermission.STANDARD == "standard"
        assert SubagentPermission.FULL == "full"

    def test_permission_sets_have_three_levels(self):
        """SUBAGENT_PERMISSION_SETS 应有三个权限等级"""
        assert len(SUBAGENT_PERMISSION_SETS) == 3
        assert SubagentPermission.READONLY in SUBAGENT_PERMISSION_SETS
        assert SubagentPermission.STANDARD in SUBAGENT_PERMISSION_SETS
        assert SubagentPermission.FULL in SUBAGENT_PERMISSION_SETS

    def test_blocked_tools_is_readonly_alias(self):
        """SUBAGENT_BLOCKED_TOOLS 是 READONLY 权限的别名"""
        assert SUBAGENT_BLOCKED_TOOLS == SUBAGENT_PERMISSION_SETS[SubagentPermission.READONLY]["blocked"]

    def test_context_limit_value(self):
        """CONTEXT_LIMIT 应为 110000"""
        assert CONTEXT_LIMIT == 110000

    def test_readonly_blocks_destructive_tools(self):
        """READONLY 权限应禁止 spawn_subagent / file_write / bash 等工具"""
        blocked = SUBAGENT_PERMISSION_SETS[SubagentPermission.READONLY]["blocked"]
        assert "spawn_subagent" in blocked
        assert "end_conversation" in blocked
        assert "file_write" in blocked
        assert "file_delete" in blocked
        assert "bash" in blocked

    def test_full_allows_most_tools(self):
        """FULL 权限仅禁止 spawn_subagent 和 end_conversation"""
        blocked = SUBAGENT_PERMISSION_SETS[SubagentPermission.FULL]["blocked"]
        assert "spawn_subagent" in blocked
        assert "end_conversation" in blocked
        assert "file_write" not in blocked
        assert "bash" not in blocked


# ─────────────────────────────────────────────
# 2. AgentExecutor.__init__
# ─────────────────────────────────────────────


class TestAgentExecutorInit:
    """AgentExecutor.__init__ 测试"""

    def test_init_stores_agent_hash_and_tools(self):
        """应存储 agent_hash 和 tools 实例"""
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        assert executor.agent_hash == "abcd"
        assert executor.tools is tools

    def test_init_default_blocked_tools_is_empty(self):
        """不传 blocked_tools 时默认 blocked_tools 为空集合"""
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        assert executor.blocked_tools == set()

    def test_init_blocked_tools_converted_to_set(self):
        """blocked_tools 列表应转换为 set"""
        tools = _make_mock_tools()
        blocked = ["bash", "file_write"]
        executor = AgentExecutor(agent_hash="abcd", tools=tools, blocked_tools=blocked)
        assert executor.blocked_tools == {"bash", "file_write"}

    def test_init_empty_blocked_tools_list(self):
        """空 blocked_tools 列表应产生空 set"""
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools, blocked_tools=[])
        assert executor.blocked_tools == set()

    def test_init_internal_state_defaults(self):
        """初始化时内部状态应为 None / 默认值"""
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        assert executor._user_id is None
        assert executor._agent_profile is None
        assert executor._skip_compact is False
        assert executor._skip_smart_router is False
        assert executor._typing_callback is None
        assert executor._cached_persona is None


# ─────────────────────────────────────────────
# 3. user_id 属性
# ─────────────────────────────────────────────


class TestAgentExecutorUserId:
    """AgentExecutor.user_id 属性测试"""

    @pytest.fixture
    def executor(self):
        return AgentExecutor(agent_hash="abcd", tools=_make_mock_tools())

    def test_user_id_lazy_loads_from_db(self, executor, mock_db):
        """首次访问 user_id 时应从 DB 查询 AgentProfile 并缓存结果"""
        mock_agent = _make_mock_agent_profile()
        mock_agent.user_id = 42
        mock_db.query.return_value.filter.return_value.first.return_value = mock_agent

        result = executor.user_id
        assert result == "42"
        assert executor._user_id == "42"
        assert executor._agent_profile is mock_agent

    def test_user_id_returns_cached_value(self, executor, mock_db):
        """已缓存 user_id 时应直接返回，不重复访问 DB"""
        executor._user_id = "99"

        result = executor.user_id
        assert result == "99"
        mock_db.query.assert_not_called()

    def test_user_id_agent_not_found_raises(self, executor, mock_db):
        """Agent 不存在时应抛出 ValueError"""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with pytest.raises(ValueError, match="not found"):
            executor.user_id


# ─────────────────────────────────────────────
# 4. agent_profile 属性
# ─────────────────────────────────────────────


class TestAgentExecutorAgentProfile:
    """AgentExecutor.agent_profile 属性测试"""

    @pytest.fixture
    def executor(self):
        return AgentExecutor(agent_hash="abcd", tools=_make_mock_tools())

    def test_agent_profile_lazy_loads_from_db(self, executor, mock_db):
        """首次访问 agent_profile 时应从 DB 查询"""
        mock_agent = _make_mock_agent_profile()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_agent

        result = executor.agent_profile
        assert result is mock_agent
        assert executor._agent_profile is mock_agent

    def test_agent_profile_returns_cached_value(self, executor, mock_db):
        """已缓存时应直接返回，不重复查 DB"""
        mock_agent = _make_mock_agent_profile()
        executor._agent_profile = mock_agent

        result = executor.agent_profile
        assert result is mock_agent
        mock_db.query.assert_not_called()

    def test_agent_profile_not_found_raises(self, executor, mock_db):
        """Agent 不存在时应抛出 ValueError"""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with pytest.raises(ValueError, match="not found"):
            executor.agent_profile


# ─────────────────────────────────────────────
# 5. _refresh_agent_profile()
# ─────────────────────────────────────────────


class TestAgentExecutorRefreshAgentProfile:
    """_refresh_agent_profile() 测试"""

    def test_refresh_reloads_from_db_and_resets_persona(self, mock_db):
        """应从 DB 重新加载 profile 并重置 persona 缓存"""
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        executor._cached_persona = "old persona"

        mock_agent = _make_mock_agent_profile()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_agent

        executor._refresh_agent_profile()

        assert executor._agent_profile is mock_agent
        assert executor._cached_persona is None

    def test_refresh_agent_not_found_does_not_crash(self, mock_db):
        """Agent 查不到时不应 crash，原 profile 保持不变"""
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        original_profile = _make_mock_agent_profile()
        executor._agent_profile = original_profile

        mock_db.query.return_value.filter.return_value.first.return_value = None
        executor._refresh_agent_profile()

        assert executor._agent_profile is original_profile


# ─────────────────────────────────────────────
# 6. _read_vfs_file()
# ─────────────────────────────────────────────


class TestAgentExecutorReadVfsFile:
    """_read_vfs_file() 测试"""

    @pytest.fixture
    def executor(self):
        tools = _make_mock_tools()
        return AgentExecutor(agent_hash="abcd", tools=tools)

    def test_read_vfs_file_returns_trimmed_content(self, executor):
        """正常读取时应返回 strip 后的内容"""
        executor.tools.vfs.cat.return_value = "  file content with spaces  \n"
        result = executor._read_vfs_file("/workspace/agent/soul.md")
        assert result == "file content with spaces"

    def test_read_vfs_file_returns_none_on_error_prefix(self, executor):
        """cat 返回以 Error 开头的内容时返回 None"""
        executor.tools.vfs.cat.return_value = "Error: file not found"
        result = executor._read_vfs_file("/nonexistent.md")
        assert result is None

    def test_read_vfs_file_returns_none_on_exception(self, executor):
        """cat 抛异常时返回 None，不向上传播"""
        executor.tools.vfs.cat.side_effect = RuntimeError("VFS unavailable")
        result = executor._read_vfs_file("/boom.md")
        assert result is None

    def test_read_vfs_file_returns_empty_on_whitespace_only(self, executor):
        """纯空白内容 strip 后返回空字符串"""
        executor.tools.vfs.cat.return_value = "   "
        result = executor._read_vfs_file("/empty.md")
        assert result == ""


# ─────────────────────────────────────────────
# 7. _extract_known_image_paths()
# ─────────────────────────────────────────────


class TestAgentExecutorExtractKnownImagePaths:
    """_extract_known_image_paths() 测试"""

    @pytest.fixture
    def executor(self):
        return AgentExecutor(agent_hash="abcd", tools=_make_mock_tools())

    def test_extracts_paths_chinese_colon_pattern(self, executor):
        """应匹配「图片路径：/workspace/images/xxx.png」中文冒号模式"""
        messages = [
            {"role": "assistant", "content": "图片路径：/workspace/images/photo.png"}
        ]
        paths = executor._extract_known_image_paths(messages)
        assert "/workspace/images/photo.png" in paths

    def test_extracts_paths_saved_pattern(self, executor):
        """应匹配 已保存到"/workspace/images/xxx.jpg" 模式"""
        messages = [
            {"role": "assistant", "content": '已保存到"/workspace/images/img.jpg"'}
        ]
        paths = executor._extract_known_image_paths(messages)
        assert "/workspace/images/img.jpg" in paths

    def test_extracts_paths_vfs_pattern(self, executor):
        """应匹配 已保存到VFS路径"/workspace/images/xxx.webp" 模式"""
        messages = [
            {"role": "assistant", "content": '已保存到VFS路径"/workspace/images/thumb.webp"'}
        ]
        paths = executor._extract_known_image_paths(messages)
        assert "/workspace/images/thumb.webp" in paths

    def test_deduplicates_duplicate_paths(self, executor):
        """应去重，同一路径只出现一次"""
        messages = [
            {"role": "assistant", "content": "图片路径：/workspace/images/a.png"},
            {"role": "assistant", "content": "图片路径：/workspace/images/a.png"},
        ]
        paths = executor._extract_known_image_paths(messages)
        assert paths.count("/workspace/images/a.png") == 1

    def test_reverse_order_from_end_of_messages(self, executor):
        """应从消息末尾向前遍历（逆序）"""
        messages = [
            {"role": "assistant", "content": "图片路径：/workspace/images/old.png"},
            {"role": "assistant", "content": "图片路径：/workspace/images/new.png"},
        ]
        paths = executor._extract_known_image_paths(messages)
        assert paths[0] == "/workspace/images/new.png"
        assert paths[1] == "/workspace/images/old.png"

    def test_ignores_non_string_content(self, executor):
        """应忽略 content 为 None 或 list 等非字符串的消息"""
        messages = [
            {"role": "user", "content": None},
            {"role": "user", "content": ["list", "content"]},
        ]
        paths = executor._extract_known_image_paths(messages)
        assert paths == []

    def test_returns_empty_when_no_matches(self, executor):
        """无匹配时应返回空列表"""
        messages = [{"role": "user", "content": "请描述图片"}]
        paths = executor._extract_known_image_paths(messages)
        assert paths == []

    def test_matches_multiple_image_formats(self, executor):
        """应匹配 png / jpg / jpeg / gif / webp / bmp 格式"""
        messages = [{"role": "assistant", "content": (
            "图片路径：/workspace/images/a.png "
            '已保存到"/workspace/images/b.jpg" '
            '已保存到VFS路径"/workspace/images/c.gif" '
            "图片路径: /workspace/images/d.bmp"
        )}]
        paths = executor._extract_known_image_paths(messages)
        assert len(paths) == 4


# ─────────────────────────────────────────────
# 8. _validate_path_with_hint()
# ─────────────────────────────────────────────


class TestAgentExecutorValidatePathWithHint:
    """_validate_path_with_hint() 测试"""

    @pytest.fixture
    def executor(self):
        return AgentExecutor(agent_hash="abcd", tools=_make_mock_tools())

    def test_returns_none_for_non_path_tool(self, executor):
        """web_search 等非文件操作工具不触发路径校验"""
        hint = executor._validate_path_with_hint("web_search", {"query": "test"}, [])
        assert hint is None

    def test_path_in_known_list_returns_none(self, executor):
        """路径在已知列表中时应返回 None（无提示）"""
        messages = [{"role": "assistant", "content": "图片路径：/workspace/images/real.png"}]
        hint = executor._validate_path_with_hint(
            "file_read", {"path": "/workspace/images/real.png"}, messages
        )
        assert hint is None

    def test_path_not_in_known_list_returns_hint(self, executor):
        """路径不在已知列表中时应返回路径校验提示"""
        messages = [{"role": "assistant", "content": "图片路径：/workspace/images/known.png"}]
        hint = executor._validate_path_with_hint(
            "file_read", {"path": "/workspace/images/wrong.png"}, messages
        )
        assert hint is not None
        assert "路径校验提示" in hint
        assert "wrong.png" in hint
        assert "/workspace/images/known.png" in hint

    def test_no_known_paths_returns_none(self, executor):
        """消息中无已知图片路径时返回 None"""
        messages = [{"role": "user", "content": "hello"}]
        hint = executor._validate_path_with_hint(
            "file_read", {"path": "/workspace/images/any.png"}, messages
        )
        assert hint is None

    def test_non_workspace_path_not_validated(self, executor):
        """非 /workspace 开头的路径不触发校验"""
        messages = [{"role": "assistant", "content": "图片路径：/workspace/images/real.png"}]
        hint = executor._validate_path_with_hint(
            "file_read", {"path": "/tmp/other.png"}, messages
        )
        assert hint is None

    def test_spawn_subagent_validates_image_path(self, executor):
        """spawn_subagent 工具应校验 image_path 参数"""
        messages = [{"role": "assistant", "content": "图片路径：/workspace/images/real.png"}]
        hint = executor._validate_path_with_hint(
            "spawn_subagent", {"image_path": "/workspace/images/fake.png"}, messages
        )
        assert hint is not None
        assert "fake.png" in hint

    def test_edit_tool_validates_path(self, executor):
        """edit 工具应校验 path 参数"""
        messages = [{"role": "assistant", "content": "图片路径：/workspace/images/real.png"}]
        hint = executor._validate_path_with_hint(
            "edit", {"path": "/workspace/images/wrong.png"}, messages
        )
        assert hint is not None


# ─────────────────────────────────────────────
# 9. _estimate_tokens()
# ─────────────────────────────────────────────


class TestAgentExecutorEstimateTokens:
    """_estimate_tokens() 测试"""

    def test_delegates_to_llm_service(self):
        """应委托 llm_service.estimate_tokens 并返回其结果"""
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        messages = [{"role": "user", "content": "test"}]

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = 42
            result = executor._estimate_tokens(messages)
            mock_llm.estimate_tokens.assert_called_once_with(messages)
            assert result == 42


# ─────────────────────────────────────────────
# 10. _get_tool_definitions()
# ─────────────────────────────────────────────


class TestAgentExecutorGetToolDefinitions:
    """_get_tool_definitions() 测试"""

    def test_returns_all_when_no_blocked_tools(self):
        """无禁用工具时应返回全部工具定义"""
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)

        mock_schemas = [
            {"function": {"name": "tool_a"}},
            {"function": {"name": "tool_b"}},
            {"function": {"name": "tool_c"}},
        ]
        with patch("services.tool_registry.get_tool_schemas", return_value=mock_schemas):
            result = executor._get_tool_definitions()
            assert len(result) == 3

    def test_filters_blocked_tools(self):
        """应过滤掉 blocked_tools 中的工具"""
        tools = _make_mock_tools()
        blocked = ["tool_b", "tool_d"]
        executor = AgentExecutor(agent_hash="abcd", tools=tools, blocked_tools=blocked)

        mock_schemas = [
            {"function": {"name": "tool_a"}},
            {"function": {"name": "tool_b"}},
            {"function": {"name": "tool_c"}},
        ]
        with patch("services.tool_registry.get_tool_schemas", return_value=mock_schemas):
            result = executor._get_tool_definitions()
            names = [s["function"]["name"] for s in result]
            assert "tool_a" in names
            assert "tool_b" not in names
            assert "tool_c" in names
            assert len(result) == 2

    def test_empty_schemas_returns_empty_list(self):
        """空 schemas 应返回空列表"""
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)

        with patch("services.tool_registry.get_tool_schemas", return_value=[]):
            result = executor._get_tool_definitions()
            assert result == []


# ─────────────────────────────────────────────
# 11. _smart_crop()
# ─────────────────────────────────────────────


class TestAgentExecutorSmartCrop:
    """_smart_crop() 静态方法测试"""

    def test_short_text_returned_verbatim(self):
        """短文本（<= 600 字符）应原样返回"""
        text = "short text"
        result = AgentExecutor._smart_crop(text)
        assert result == text

    def test_text_at_600_boundary(self):
        """恰好 600 字符应原样返回"""
        text = "x" * 600
        result = AgentExecutor._smart_crop(text)
        assert result == text

    def test_long_text_includes_omission_marker(self):
        """超过 600 字符的文本应含省略标记"""
        text = "x" * 1000
        result = AgentExecutor._smart_crop(text)
        assert "……（中间省略" in result
        assert "knowledge_get" in result
        assert len(result) < len(text)

    def test_601_chars_triggers_crop(self):
        """601 字符即触发裁剪"""
        text = "x" * 601
        result = AgentExecutor._smart_crop(text)
        assert "中间省略" in result


# ─────────────────────────────────────────────
# 12. _build_knowledge_injection()
# ─────────────────────────────────────────────


class TestAgentExecutorBuildKnowledgeInjection:
    """_build_knowledge_injection() 静态方法测试"""

    def test_groups_by_source(self):
        """应按来源分组为公共知识库/私有知识库/对话记忆"""
        results = [
            {"score": 0.8, "source": "textbook", "key": "key1", "metadata": {"text": "textbook content"}},
            {"score": 0.7, "source": "knowledge_base", "key": "key2", "metadata": {"text": "kb content"}},
        ]
        text = AgentExecutor._build_knowledge_injection(results)
        assert "公共知识库" in text
        assert "私有知识库" in text
        assert "key1" in text
        assert "key2" in text

    def test_filters_low_score_results(self):
        """score <= 0.3 的结果应被过滤"""
        results = [
            {"score": 0.2, "source": "textbook", "key": "low", "metadata": {"text": "low score"}},
            {"score": 0.8, "source": "textbook", "key": "high", "metadata": {"text": "high score"}},
        ]
        text = AgentExecutor._build_knowledge_injection(results)
        assert "low score" not in text
        assert "high score" in text

    def test_filters_empty_text(self):
        """metadata.text 为空的结果应被跳过"""
        results = [
            {"score": 0.8, "source": "textbook", "key": "empty", "metadata": {}},
            {"score": 0.7, "source": "knowledge_base", "key": "has_text", "metadata": {"text": "some text"}},
        ]
        text = AgentExecutor._build_knowledge_injection(results)
        assert "empty" not in text
        assert "has_text" in text

    def test_limits_to_top_5(self):
        """只取前 5 条结果"""
        results = [
            {"score": 0.9, "source": "textbook", "key": f"key{i}", "metadata": {"text": f"text{i}"}}
            for i in range(10)
        ]
        text = AgentExecutor._build_knowledge_injection(results)
        assert "key4" in text       # [:5] 取索引 0-4
        assert "key5" not in text   # 索引 5 超出 [:5]

    def test_empty_input_returns_empty(self):
        """空列表应返回空字符串"""
        text = AgentExecutor._build_knowledge_injection([])
        assert text == ""

    def test_conversation_memory_section(self):
        """conversation_memory 来源应显示【相关历史会话】"""
        results = [
            {"score": 0.8, "source": "conversation_memory", "key": "mem1",
             "metadata": {"text": "记忆内容"}},
        ]
        text = AgentExecutor._build_knowledge_injection(results)
        assert "相关历史会话" in text

    def test_gaokao_is_public_knowledge(self):
        """gaokao 来源应归入公共知识库"""
        results = [
            {"score": 0.8, "source": "gaokao", "key": "gk1",
             "metadata": {"text": "高考内容"}},
        ]
        text = AgentExecutor._build_knowledge_injection(results)
        assert "公共知识库" in text


# ─────────────────────────────────────────────
# 13. execute_tool()
# ─────────────────────────────────────────────


class TestAgentExecutorExecuteTool:
    """execute_tool() 测试"""

    @pytest.fixture
    def executor(self):
        tools = _make_mock_tools()
        # 给 tools 添加模拟工具方法
        tools.search_file = MagicMock(return_value="result: found file")
        tools.web_search = MagicMock(return_value="search result")
        return AgentExecutor(agent_hash="abcd", tools=tools)

    @pytest.mark.asyncio
    async def test_blocked_tool_returns_error(self, executor):
        """禁用工具应返回包含"已被禁用"的 Error 消息"""
        executor.blocked_tools = {"file_write"}
        result = await executor.execute_tool("file_write", {"path": "/test.txt"})
        assert result.startswith("Error:")
        assert "已被禁用" in result

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, executor):
        """未知工具应返回包含"未知工具"的 Error 消息"""
        with patch("services.tool_registry.get_tool", return_value=None):
            result = await executor.execute_tool("nonexistent", {})
            assert result.startswith("Error:")
            assert "未知工具" in result

    @pytest.mark.asyncio
    async def test_tool_method_not_on_service_returns_error(self, executor):
        """工具定义存在但 AgentToolsService 无对应方法时应返回 Error"""
        # MagicMock getattr 会自动创建属性，故显式设为 None 模拟方法不存在
        executor.tools.method_not_here = None
        mock_entry = {"param_names": ["query"]}
        with patch("services.tool_registry.get_tool", return_value=mock_entry):
            result = await executor.execute_tool("method_not_here", {})
            assert result.startswith("Error:")
            assert "不可用" in result

    @pytest.mark.asyncio
    async def test_sync_tool_execution(self, executor):
        """同步工具应在线程池执行并通过 _truncate_tool_result 截断"""
        mock_entry = {"param_names": ["query"]}
        with patch("services.tool_registry.get_tool", return_value=mock_entry):
            result = await executor.execute_tool("search_file", {"query": "test.py"})
            executor.tools.search_file.assert_called_once_with(query="test.py")
            executor.tools._truncate_tool_result.assert_called_once()
            assert result == "result: found file"

    @pytest.mark.asyncio
    async def test_async_tool_execution(self, executor):
        """异步工具应直接 await 并返回结果"""
        async def mock_async_tool(query=""):
            return f"async: {query}"
        executor.tools.async_search = mock_async_tool

        mock_entry = {"param_names": ["query"]}
        with patch("services.tool_registry.get_tool", return_value=mock_entry):
            result = await executor.execute_tool("async_search", {"query": "hello"})
            assert "async: hello" in result

    @pytest.mark.asyncio
    async def test_filters_extra_parameters(self, executor):
        """多余参数应被过滤，只传 tool_entry 声明的参数"""
        mock_entry = {"param_names": ["query"]}
        with patch("services.tool_registry.get_tool", return_value=mock_entry):
            await executor.execute_tool("search_file", {
                "query": "test", "extra": "ignored", "another": 123
            })
            executor.tools.search_file.assert_called_once_with(query="test")

    @pytest.mark.asyncio
    async def test_tool_exception_returns_error(self, executor):
        """工具执行异常时应返回 Error 字符串，不向上传播异常"""
        executor.tools.search_file.side_effect = RuntimeError("boom")

        mock_entry = {"param_names": ["query"]}
        with patch("services.tool_registry.get_tool", return_value=mock_entry):
            result = await executor.execute_tool("search_file", {"query": "test"})
            assert result == "Error: boom"

    @pytest.mark.asyncio
    async def test_list_subagent_roles_special_path(self, executor):
        """list_subagent_roles 应直接调用 tools.list_subagent_roles() 不走 get_tool"""
        executor.tools.list_subagent_roles = MagicMock(return_value='[{"role": "coder"}]')

        result = await executor.execute_tool("list_subagent_roles", {})
        executor.tools.list_subagent_roles.assert_called_once()
        assert "coder" in result

    @pytest.mark.asyncio
    async def test_injects_on_progress_callback_for_web_search(self, executor):
        """工具方法签名含 on_progress 时应注入回调"""
        mock_entry = {"param_names": ["query"]}
        progress_calls = []

        async def on_progress(chunk):
            progress_calls.append(chunk)

        with patch("services.tool_registry.get_tool", return_value=mock_entry):
            with patch("inspect.signature") as mock_sig:
                mock_sig.return_value.parameters = {
                    "query": MagicMock(), "on_progress": MagicMock()
                }
                await executor.execute_tool(
                    "search_file", {"query": "test"}, on_progress=on_progress
                )
                call_kwargs = executor.tools.search_file.call_args[1]
                assert "on_progress" in call_kwargs


# ─────────────────────────────────────────────
# 14. chat_with_tools() — 状态检查
# ─────────────────────────────────────────────


class TestAgentExecutorChatWithToolsStatus:
    """chat_with_tools() Agent 状态检查测试"""

    @pytest.fixture
    def executor(self):
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        executor._agent_profile = _make_mock_agent_profile(status="ready")
        return executor

    @pytest.mark.asyncio
    async def test_pending_agent_returns_error_step(self, executor):
        """Agent 状态为 pending 时应返回 error Step 并停止"""
        executor._agent_profile.status = "pending"
        messages = [{"role": "user", "content": "hello"}]

        steps = []
        async for step in executor.chat_with_tools(messages):
            steps.append(step)

        assert len(steps) == 1
        assert steps[0].step_type == "error"
        assert "尚未初始化" in steps[0].content
        assert executor.agent_hash in steps[0].content

    @pytest.mark.asyncio
    async def test_suspended_agent_returns_error_step(self, executor):
        """Agent 状态为 suspended 时应返回 error Step 并停止"""
        executor._agent_profile.status = "suspended"
        messages = [{"role": "user", "content": "hello"}]

        steps = []
        async for step in executor.chat_with_tools(messages):
            steps.append(step)

        assert len(steps) == 1
        assert steps[0].step_type == "error"
        assert "已被暂停" in steps[0].content


# ─────────────────────────────────────────────
# 15. chat_with_tools() — 上下文压缩
# ─────────────────────────────────────────────


class TestAgentExecutorChatWithToolsCompact:
    """chat_with_tools() 上下文压缩测试"""

    @pytest.fixture
    def executor(self):
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        executor._agent_profile = _make_mock_agent_profile(status="ready")
        executor._refresh_agent_profile = MagicMock()
        return executor

    @pytest.mark.asyncio
    async def test_compact_triggered_when_over_context_limit(self, executor):
        """token 数超过 CONTEXT_LIMIT 且 _skip_compact=False 时应触发压缩"""
        messages = [{"role": "user", "content": "x" * 100}]
        mock_compacted = [{"role": "system", "content": "compacted"}]

        decision = MagicMock()
        decision.direct_reply = "reply"
        decision.buffer_msg = None
        decision.thinking = False
        decision.prefetch = []
        decision.inject_rules = []

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = CONTEXT_LIMIT + 1
            with patch("services.agent_executor.MessageCompactor") as MockMC:
                MockMC.return_value.compact = AsyncMock(return_value=mock_compacted)
                with patch.object(executor, "_get_tool_definitions", return_value=[]):
                    with patch("services.agent_executor.SmartRouter") as MockSR:
                        MockSR.return_value.route = AsyncMock(return_value=decision)
                        with patch("services.vector_search_service.VectorSearchService") as MockVS:
                            MockVS.return_value.search_public_with_quality = AsyncMock(return_value=[])

                            steps = []
                            async for step in executor.chat_with_tools(messages):
                                steps.append(step)

        MockMC.return_value.compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_compact_skipped_when_skip_compact_is_true(self, executor):
        """_skip_compact=True 时不触发上下文压缩"""
        executor._skip_compact = True
        messages = [{"role": "user", "content": "test"}]

        decision = MagicMock()
        decision.direct_reply = "reply"
        decision.buffer_msg = None
        decision.thinking = False
        decision.prefetch = []
        decision.inject_rules = []

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = CONTEXT_LIMIT + 1
            with patch("services.agent_executor.MessageCompactor") as MockMC:
                with patch.object(executor, "_get_tool_definitions", return_value=[]):
                    with patch("services.agent_executor.SmartRouter") as MockSR:
                        MockSR.return_value.route = AsyncMock(return_value=decision)
                        with patch("services.vector_search_service.VectorSearchService") as MockVS:
                            MockVS.return_value.search_public_with_quality = AsyncMock(return_value=[])

                            steps = []
                            async for step in executor.chat_with_tools(messages):
                                steps.append(step)

        MockMC.return_value.compact.assert_not_called()


# ─────────────────────────────────────────────
# 16. chat_with_tools() — SmartRouter direct_reply
# ─────────────────────────────────────────────


class TestAgentExecutorChatWithToolsDirectReply:
    """chat_with_tools() SR direct_reply 测试"""

    @pytest.fixture
    def executor(self):
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        executor._agent_profile = _make_mock_agent_profile(
            status="ready", sr_enabled=True
        )
        executor._refresh_agent_profile = MagicMock()
        return executor

    @pytest.mark.asyncio
    async def test_direct_reply_shortcuts_main_model(self, executor):
        """SR direct_reply 不为空时应直接返回，不调用主模型"""
        decision = MagicMock()
        decision.direct_reply = "这是直接回答"
        decision.buffer_msg = None
        decision.thinking = False
        decision.prefetch = []
        decision.inject_rules = []

        messages = [{"role": "user", "content": "hello"}]

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = 500
            with patch.object(executor, "_get_tool_definitions", return_value=[]):
                with patch("services.agent_executor.SmartRouter") as MockSR:
                    MockSR.return_value.route = AsyncMock(return_value=decision)
                    with patch("services.vector_search_service.VectorSearchService") as MockVS:
                        MockVS.return_value.search_public_with_quality = AsyncMock(return_value=[])

                        steps = []
                        async for step in executor.chat_with_tools(messages):
                            steps.append(step)

        final_steps = [s for s in steps if s.step_type == "final"]
        assert len(final_steps) == 1
        assert final_steps[0].content == "这是直接回答"


# ─────────────────────────────────────────────
# 17. chat_with_tools() — SR disabled / skip
# ─────────────────────────────────────────────


class TestAgentExecutorChatWithToolsSrDisabled:
    """chat_with_tools() SR 禁用测试"""

    @pytest.fixture
    def executor(self):
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        executor._agent_profile = _make_mock_agent_profile(
            status="ready", sr_enabled=False
        )
        executor._refresh_agent_profile = MagicMock()
        return executor

    @pytest.mark.asyncio
    async def test_sr_disabled_skips_router(self, executor):
        """sr_enabled=False 时不应调用 SmartRouter"""
        messages = [{"role": "user", "content": "hello"}]

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = 500
            with patch.object(executor, "_get_tool_definitions", return_value=[]):
                with patch("services.agent_executor.SmartRouter") as MockSR:
                    with patch("services.vector_search_service.VectorSearchService") as MockVS:
                        MockVS.return_value.search_public_with_quality = AsyncMock(return_value=[])

                        async def mock_stream(*args, **kwargs):
                            yield {"type": "done", "content": "ok", "tool_calls": None}

                        mock_llm.chat_with_tools_stream = mock_stream

                        steps = []
                        async for step in executor.chat_with_tools(messages):
                            steps.append(step)

        MockSR.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_smart_router_override(self, executor):
        """_skip_smart_router=True 时即使 sr_enabled=True 也跳过 SR"""
        executor._skip_smart_router = True
        executor._agent_profile.sr_enabled = True
        messages = [{"role": "user", "content": "hello"}]

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = 500
            with patch.object(executor, "_get_tool_definitions", return_value=[]):
                with patch("services.agent_executor.SmartRouter") as MockSR:
                    with patch("services.vector_search_service.VectorSearchService") as MockVS:
                        MockVS.return_value.search_public_with_quality = AsyncMock(return_value=[])

                        async def mock_stream(*args, **kwargs):
                            yield {"type": "done", "content": "ok", "tool_calls": None}

                        mock_llm.chat_with_tools_stream = mock_stream

                        steps = []
                        async for step in executor.chat_with_tools(messages):
                            steps.append(step)

        MockSR.assert_not_called()


# ─────────────────────────────────────────────
# 18. chat_with_tools() — inject_rules
# ─────────────────────────────────────────────


class TestAgentExecutorChatWithToolsInjectRules:
    """chat_with_tools() inject_rules 测试"""

    @pytest.fixture
    def executor(self):
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        executor._agent_profile = _make_mock_agent_profile(
            status="ready", sr_enabled=True
        )
        executor._refresh_agent_profile = MagicMock()
        return executor

    @pytest.mark.asyncio
    async def test_inject_rules_appended_to_existing_system_message(self, executor):
        """inject_rules 应追加到现有 system 消息末尾"""
        decision = MagicMock()
        decision.direct_reply = None
        decision.buffer_msg = None
        decision.thinking = False
        decision.prefetch = []
        decision.inject_rules = ["规则1", "规则2"]

        messages = [
            {"role": "system", "content": "original system"},
            {"role": "user", "content": "hello"},
        ]

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = 500

            async def mock_stream(*args, **kwargs):
                for msg in kwargs["messages"]:
                    if msg["role"] == "system":
                        assert "【人设注入】" in msg["content"]
                        assert "规则1" in msg["content"]
                        assert "规则2" in msg["content"]
                yield {"type": "done", "content": "ok", "tool_calls": None}

            mock_llm.chat_with_tools_stream = mock_stream

            with patch.object(executor, "_get_tool_definitions", return_value=[]):
                with patch("services.agent_executor.SmartRouter") as MockSR:
                    MockSR.return_value.route = AsyncMock(return_value=decision)
                    with patch("services.vector_search_service.VectorSearchService") as MockVS:
                        MockVS.return_value.search_public_with_quality = AsyncMock(return_value=[])

                        steps = []
                        async for step in executor.chat_with_tools(messages):
                            steps.append(step)

    @pytest.mark.asyncio
    async def test_inject_rules_inserts_system_message_if_missing(self, executor):
        """无 system 消息时 inject_rules 应插入新的 system 消息"""
        decision = MagicMock()
        decision.direct_reply = None
        decision.buffer_msg = None
        decision.thinking = False
        decision.prefetch = []
        decision.inject_rules = ["规则A"]

        messages = [{"role": "user", "content": "hello"}]

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = 500

            async def mock_stream(*args, **kwargs):
                system_msgs = [m for m in kwargs["messages"] if m["role"] == "system"]
                assert len(system_msgs) == 1
                assert "规则A" in system_msgs[0]["content"]
                yield {"type": "done", "content": "ok", "tool_calls": None}

            mock_llm.chat_with_tools_stream = mock_stream

            with patch.object(executor, "_get_tool_definitions", return_value=[]):
                with patch("services.agent_executor.SmartRouter") as MockSR:
                    MockSR.return_value.route = AsyncMock(return_value=decision)
                    with patch("services.vector_search_service.VectorSearchService") as MockVS:
                        MockVS.return_value.search_public_with_quality = AsyncMock(return_value=[])

                        steps = []
                        async for step in executor.chat_with_tools(messages):
                            steps.append(step)


# ─────────────────────────────────────────────
# 19. chat_with_tools() — LLM error / 熔断器
# ─────────────────────────────────────────────


class TestAgentExecutorChatWithToolsErrorHandling:
    """chat_with_tools() 错误处理和熔断器测试"""

    @pytest.fixture
    def executor(self):
        tools = _make_mock_tools()
        executor = AgentExecutor(agent_hash="abcd", tools=tools)
        executor._agent_profile = _make_mock_agent_profile(status="ready")
        executor._refresh_agent_profile = MagicMock()
        return executor

    @pytest.mark.asyncio
    async def test_llm_api_error_returns_error_step(self, executor):
        """LLM API 调用异常时应返回 error Step 而非 crash"""
        messages = [{"role": "user", "content": "hello"}]

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = 500

            async def mock_stream_error(*args, **kwargs):
                raise RuntimeError("API connection refused")
                yield

            mock_llm.chat_with_tools_stream = mock_stream_error

            with patch.object(executor, "_get_tool_definitions", return_value=[]):
                with patch("services.agent_executor.SmartRouter") as MockSR:
                    MockSR.return_value.route = AsyncMock(return_value=MagicMock(
                        direct_reply=None, buffer_msg=None, thinking=False,
                        prefetch=[], inject_rules=[]
                    ))
                    with patch("services.vector_search_service.VectorSearchService") as MockVS:
                        MockVS.return_value.search_public_with_quality = AsyncMock(return_value=[])

                        steps = []
                        async for step in executor.chat_with_tools(messages):
                            steps.append(step)

        error_steps = [s for s in steps if s.step_type == "error"]
        assert len(error_steps) >= 1
        assert "API" in error_steps[0].content

    @pytest.mark.asyncio
    async def test_consecutive_errors_circuit_breaker(self, executor):
        """连续 3 次工具 Error 应触发熔断器并停止"""
        messages = [{"role": "user", "content": "hello"}]

        tool_calls = [
            {"id": "call_1", "type": "function",
             "function": {"name": "web_search", "arguments": '{"query":"test"}'}},
        ]

        with patch("services.agent_executor.llm_service") as mock_llm:
            mock_llm.estimate_tokens.return_value = 500

            async def mock_stream(*args, **kwargs):
                yield {"type": "done", "content": "", "tool_calls": tool_calls}

            mock_llm.chat_with_tools_stream = mock_stream

            with patch.object(executor, "_get_tool_definitions", return_value=[]):
                with patch.object(executor, "execute_tool", return_value="Error: tool failure"):
                    with patch("services.agent_executor.SmartRouter") as MockSR:
                        MockSR.return_value.route = AsyncMock(return_value=MagicMock(
                            direct_reply=None, buffer_msg=None, thinking=False,
                            prefetch=[], inject_rules=[]
                        ))
                        with patch("services.vector_search_service.VectorSearchService") as MockVS:
                            MockVS.return_value.search_public_with_quality = AsyncMock(return_value=[])

                            steps = []
                            async for step in executor.chat_with_tools(messages):
                                steps.append(step)

        final_steps = [s for s in steps if s.step_type == "final"]
        assert len(final_steps) == 1
        assert "连续失败" in final_steps[0].content
