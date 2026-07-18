"""
Agent 初始化服务单元测试

测试覆盖：
1. AgentInitService 初始化和 storage 属性
2. create_agent Agent 创建
3. initialize_agent Agent 初始化
4. get_agent_status Agent 状态查询
5. validate_tools_config / validate_style 验证
6. load/save agent persona/tools/config
7. reload_agent_config 重新加载配置

所有测试 mock 外部依赖，不真调 COS/DB。
"""

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

pytestmark = pytest.mark.unit


class TestAgentInitServiceInit:
    """AgentInitService 初始化测试"""

    def test_init(self):
        """初始化应设置 storage 为 None（懒加载）"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        assert svc._storage is None

    def test_storage_lazy_loaded(self):
        """storage 属性应懒加载"""
        with patch("services.storage_service.StorageService") as mock_storage_cls:
            mock_storage_cls.return_value = "storage_instance"
            from services.agent_init_service import AgentInitService
            svc = AgentInitService()
            assert svc.storage == "storage_instance"
            assert svc._storage == "storage_instance"

    def test_available_tools(self):
        """AVAILABLE_TOOLS 应包含核心工具"""
        from services.agent_init_service import AgentInitService
        assert "file_read" in AgentInitService.AVAILABLE_TOOLS
        assert "file_write" in AgentInitService.AVAILABLE_TOOLS
        assert "bash" in AgentInitService.AVAILABLE_TOOLS
        assert "web_search" in AgentInitService.AVAILABLE_TOOLS

    def test_valid_styles(self):
        """VALID_STYLES 应包含所有有效风格"""
        from services.agent_init_service import AgentInitService
        assert "professional" in AgentInitService.VALID_STYLES
        assert "friendly" in AgentInitService.VALID_STYLES
        assert "casual" in AgentInitService.VALID_STYLES


class TestCreateAgent:
    """create_agent 测试"""

    def test_create_agent_success(self):
        """创建 Agent 应返回 AgentProfile 实例"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.query.return_value.filter.return_value.scalar.return_value = 0

        agent = svc.create_agent(
            db=mock_db,
            user_id=1,
            name="Test Agent",
            description="A test agent",
            hash_value="test",
        )

        assert agent.hash == "test"
        assert agent.user_id == 1
        assert agent.status == "pending"
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()

    def test_create_agent_limit(self):
        """超过 Agent 数量限制时应抛出 ValueError"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.scalar.return_value = 5

        with pytest.raises(ValueError, match="最多创建 5 个"):
            svc.create_agent(db=mock_db, user_id=1, hash_value="test")

    def test_create_agent_generates_hash(self):
        """未指定 hash_value 时应自动生成"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.scalar.return_value = 0
        mock_db.query.return_value.filter.return_value.first.side_effect = [None]

        agent = svc.create_agent(db=mock_db, user_id=1, name="Auto Agent")
        assert len(agent.hash) == 4


class TestInitializeAgent:
    """initialize_agent 测试"""

    def test_initialize_agent_basic(self):
        """初始化 Agent 应设置状态为 initialized 并创建文件"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        mock_db = MagicMock()
        agent = MagicMock()
        agent.hash = "abcd"
        agent.user_id = 42
        agent.status = "pending"

        with patch.object(svc, "_write_config_db") as mock_write:
            with patch.object(svc, "_read_config_db", return_value=None):
                svc._storage = MagicMock()
                with patch("services.agent_init_service.settings") as mock_settings:
                    mock_settings.MAIN_TEXT_MODEL = "deepseek-v4-flash"
                    mock_settings.STORAGE_PREFIX = "feclaw/"
                    with patch("services.vector_search_service.VectorSearchService") as mock_vs:
                        mock_vs.return_value.ensure_index.return_value = None

                        result = svc.initialize_agent(db=mock_db, agent=agent)

                        assert result["status"] == "success"
                        assert result["agent_hash"] == "abcd"
                        assert agent.status == "initialized"
                        assert mock_db.commit.called

    def test_initialize_agent_writes_persona(self):
        """初始化 Agent 应写入 persona 配置"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        mock_db = MagicMock()
        agent = MagicMock()
        agent.hash = "abcd"
        agent.user_id = 42
        agent.status = "pending"

        with patch.object(svc, "_write_config_db") as mock_write:
            with patch.object(svc, "_read_config_db", return_value=None):
                svc._storage = MagicMock()
                with patch("services.agent_init_service.settings") as mock_settings:
                    mock_settings.MAIN_TEXT_MODEL = "deepseek-v4-flash"
                    mock_settings.STORAGE_PREFIX = "feclaw/"
                    with patch("services.vector_search_service.VectorSearchService") as mock_vs:
                        mock_vs.return_value.ensure_index.return_value = None

                        svc.initialize_agent(db=mock_db, agent=agent)
                        persona_call = any(
                            "persona" in str(args) for args in mock_write.call_args_list
                        )
                        assert persona_call


class TestValidateToolsConfig:
    """validate_tools_config 测试"""

    def test_valid_config(self):
        """有效配置应返回 (True, None)"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        config = {
            "enabled": ["file_read", "file_write"],
            "disabled": ["bash"],
        }
        valid, error = svc.validate_tools_config(config)
        assert valid is True
        assert error is None

    def test_invalid_tool_name(self):
        """无效工具名应返回错误"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        config = {
            "enabled": ["file_read"],
            "disabled": ["nonexistent_tool"],
        }
        valid, error = svc.validate_tools_config(config)
        assert valid is False
        assert "Invalid tool" in error

    def test_tool_in_both_lists(self):
        """同一工具同时在 enabled 和 disabled 中应报错"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        config = {
            "enabled": ["file_read", "bash"],
            "disabled": ["bash"],
        }
        valid, error = svc.validate_tools_config(config)
        assert valid is False
        assert "both enabled and disabled" in error

    def test_not_a_dict(self):
        """非 dict 参数应返回错误"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        valid, error = svc.validate_tools_config("invalid")
        assert valid is False

    def test_enabled_not_a_list(self):
        """enabled 非 list 应返回错误"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        valid, error = svc.validate_tools_config({"enabled": "not_a_list", "disabled": []})
        assert valid is False


class TestValidateStyle:
    """validate_style 测试"""

    def test_valid_style(self):
        """有效风格应返回 (True, None)"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        valid, error = svc.validate_style("professional")
        assert valid is True
        assert error is None

    def test_invalid_style(self):
        """无效风格应返回错误"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        valid, error = svc.validate_style("invalid")
        assert valid is False
        assert "Invalid style" in error


class TestLoadSaveAgentData:
    """加载/保存 Agent 配置测试"""

    def test_save_persona_empty(self):
        """保存空的 persona 应返回 False"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        result = svc.save_agent_persona("abcd", "")
        assert result is False
        result = svc.save_agent_persona("abcd", "   ")
        assert result is False

    def test_save_persona_success(self):
        """保存有效的 persona 应返回 True"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        with patch.object(svc, "_write_config_db") as mock_write:
            result = svc.save_agent_persona("abcd", "new persona content")
            assert result is True
            mock_write.assert_called_once()

    def test_save_tools_success(self):
        """保存有效的工具配置应返回 (True, None)"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        config = {"enabled": ["file_read"], "disabled": []}
        with patch.object(svc, "_write_config_db") as mock_write:
            valid, error = svc.save_agent_tools("abcd", config)
            assert valid is True
            assert error is None

    def test_save_tools_invalid(self):
        """保存无效的工具配置应返回错误"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        config = {"enabled": ["nonexistent_tool"], "disabled": []}
        valid, error = svc.save_agent_tools("abcd", config)
        assert valid is False

    def test_save_config_with_style(self):
        """保存配置时带 style 字段应验证 style"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        config = {"style": "friendly", "max_tokens": 1000}
        with patch.object(svc, "load_agent_config", return_value={}):
            with patch.object(svc, "_write_config_db") as mock_write:
                valid, error = svc.save_agent_config("abcd", config)
                assert valid is True

    def test_save_config_invalid_style(self):
        """保存配置时带无效 style 应返回错误"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        config = {"style": "nonexistent"}
        valid, error = svc.save_agent_config("abcd", config)
        assert valid is False

    def test_reload_agent_config(self):
        """reload_agent_config 应返回所有配置"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        with patch.object(svc, "load_agent_persona", return_value="persona_content"):
            with patch.object(svc, "load_agent_tools", return_value={"enabled": [], "disabled": []}):
                with patch.object(svc, "load_agent_config", return_value={"style": "professional"}):
                    result = svc.reload_agent_config("abcd")
                    assert result["persona"] == "persona_content"
                    assert result["tools"] == {"enabled": [], "disabled": []}
                    assert result["style"] == "professional"


class TestGetAgentStatus:
    """get_agent_status 测试"""

    def test_get_status_with_config(self):
        """get_agent_status 应返回配置文件和 VFS 目录状态"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        agent = MagicMock()
        agent.hash = "abcd"
        agent.user_id = 42
        agent.status = "initialized"
        agent.initialized_at = MagicMock()
        agent.initialized_at.isoformat.return_value = "2024-01-01T00:00:00"

        with patch.object(svc, "_config_exists", return_value=True):
            svc._storage = MagicMock()
            result = svc.get_agent_status(agent)
            assert result["agent_hash"] == "abcd"
            assert result["status"] == "initialized"
            assert result["profile_files"]["persona"] is True
            assert result["profile_files"]["tools"] is True

    def test_get_status_missing_config(self):
        """缺少配置时 profile_files 应返回 False"""
        from services.agent_init_service import AgentInitService
        svc = AgentInitService()
        agent = MagicMock()
        agent.hash = "abcd"
        agent.user_id = 42
        agent.status = "initialized"
        agent.initialized_at = MagicMock()
        agent.initialized_at.isoformat.return_value = "2024-01-01T00:00:00"

        with patch.object(svc, "_config_exists", return_value=False):
            svc._storage = MagicMock()
            result = svc.get_agent_status(agent)
            assert result["profile_files"]["persona"] is False


class TestConfigKeyHelpers:
    """_config_key / _read_config_db / _write_config_db / _config_exists 测试"""

    def test_config_key_format(self):
        """_config_key 应生成 agents/{hash}/{name} 格式"""
        from services.agent_init_service import AgentInitService
        key = AgentInitService._config_key("abcd", "persona")
        assert key == "agents/abcd/persona"

    def test_read_config_db(self):
        """_read_config_db 应读取 AgentConfig 表"""
        from services.agent_init_service import AgentInitService
        mock_db = MagicMock()
        config_record = MagicMock()
        config_record.value = "test_value"
        mock_db.query.return_value.filter.return_value.first.return_value = config_record

        value = AgentInitService._read_config_db("abcd", "persona", db=mock_db)
        assert value == "test_value"

    def test_read_config_db_not_found(self):
        """配置不存在时应返回 None"""
        from services.agent_init_service import AgentInitService
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        value = AgentInitService._read_config_db("abcd", "nonexistent", db=mock_db)
        assert value is None

    def test_write_config_db_new(self):
        """_write_config_db 应创建新记录"""
        from services.agent_init_service import AgentInitService
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        AgentInitService._write_config_db("abcd", "persona", "new value", db=mock_db)
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()

    def test_write_config_db_update(self):
        """_write_config_db 应更新已有记录"""
        from services.agent_init_service import AgentInitService
        mock_db = MagicMock()
        existing = MagicMock()
        existing.value = "old"
        mock_db.query.return_value.filter.return_value.first.return_value = existing

        AgentInitService._write_config_db("abcd", "persona", "updated value", db=mock_db)
        assert existing.value == "updated value"
        mock_db.commit.assert_called_once()

    def test_config_exists(self):
        """_config_exists 应检查配置是否存在"""
        from services.agent_init_service import AgentInitService
        with patch.object(AgentInitService, "_read_config_db", return_value="some_value"):
            assert AgentInitService._config_exists("abcd", "persona") is True

    def test_config_not_exists(self):
        """配置不存在时 _config_exists 应返回 False"""
        from services.agent_init_service import AgentInitService
        with patch.object(AgentInitService, "_read_config_db", return_value=None):
            assert AgentInitService._config_exists("abcd", "nonexistent") is False
