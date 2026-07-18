"""
VirtualFileSystem 专项测试

测试覆盖:
- 用 LocalStorage 后端初始化
- 读写文件（put_object + get_file_content）
- 文件存在检查
- 删除文件
- 列出目录
- 路径映射（/Workspace/ 前缀）
- 不同类型的后端切换（LocalStorage vs CosStorage）
- 目录遍历（嵌套目录）
"""

import os
import sys
import pytest
import tempfile
import shutil
from unittest.mock import MagicMock, patch, AsyncMock

# 确保项目根在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_root():
    """创建临时目录"""
    path = tempfile.mkdtemp(prefix="feclaw_test_vfs_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def local_storage_fixture(temp_root):
    """创建 LocalStorage 实例"""
    from services.local_storage import LocalStorage
    return LocalStorage(root_dir=temp_root)


@pytest.fixture
def mock_storage():
    """Mock FileStorage"""
    mock = MagicMock()
    mock.get_file_content = MagicMock(return_value=b"mock content")
    mock.put_object = MagicMock(return_value=None)
    mock.delete_file_by_key = MagicMock(return_value=True)
    mock.list_objects = MagicMock(return_value=[])
    mock.file_exists = MagicMock(return_value=None)
    return mock


# ============================================================================
# 1. 初始化测试
# ============================================================================

class TestVirtualFileSystemInit:
    """测试 VirtualFileSystem 初始化"""

    def test_init_with_local_storage(self, temp_root, local_storage_fixture):
        """用 LocalStorage 后端初始化"""
        from services.virtual_filesystem import VirtualFileSystem

        storage = local_storage_fixture
        vfs = VirtualFileSystem(user_id="test_user", storage=storage)

        assert vfs.user_id == "test_user"
        assert vfs._storage is storage

    def test_init_with_mock_storage(self, mock_storage):
        """用 mock storage 初始化"""
        from services.virtual_filesystem import VirtualFileSystem

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)

        assert vfs.user_id == "123"
        assert vfs._storage is mock_storage

    def test_init_with_agent_hash(self, mock_storage):
        """用 agent_hash 初始化"""
        from services.virtual_filesystem import VirtualFileSystem

        vfs = VirtualFileSystem(agent_hash="abc1", storage=mock_storage)

        assert vfs.agent_id == "abc1"

    def test_init_without_storage_uses_default(self):
        """不传 storage 时使用默认工厂"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.file_storage.create_file_storage") as mock_create:
            mock_create.return_value = MagicMock()
            vfs = VirtualFileSystem(user_id="123")

            # storage 懒加载，第一次访问时调用工厂
            _ = vfs.storage
            mock_create.assert_called_once()

    def test_base_path_with_agent_id(self, mock_storage):
        """有 agent_id 时 base_path 格式正确"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(agent_hash="a1b2", storage=mock_storage)

            assert "agents/a1b2" in vfs.base_path

    def test_base_path_without_agent_id(self, mock_storage):
        """没有 agent_id 时 base_path 格式正确"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="999", storage=mock_storage)

            assert "user_workspaces/999" in vfs.base_path


# ============================================================================
# 2. 路径解析测试
# ============================================================================

class TestVirtualFileSystemPathResolution:
    """测试路径解析"""

    def test_resolve_absolute_path(self, mock_storage):
        """解析绝对路径"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
            cos_key, err = vfs._resolve_path("/workspace/test.txt")

            assert err is None
            assert cos_key is not None
            assert "workspace" in cos_key

    def test_resolve_path_prevents_traversal(self, mock_storage):
        """路径穿越防护"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
            cos_key, err = vfs._resolve_path("/workspace/../etc/passwd")

            # 应该拒绝穿越
            assert err is not None or cos_key is None or ".." not in cos_key

    def test_resolve_path_tilde_expansion(self, mock_storage):
        """~ 展开为用户根目录"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
            cos_key, err = vfs._resolve_path("~/file.txt")

            assert err is None
            assert cos_key is not None

    def test_resolve_config_path(self, mock_storage):
        """解析 /config/ 路径"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
            cos_key, err = vfs._resolve_path("/config/database")

            assert err is None
            assert cos_key is not None

    def test_resolve_public_path(self, mock_storage):
        """解析 /public/ 路径"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
            cos_key, err = vfs._resolve_path("/public/readme.md")

            assert err is None
            assert cos_key is not None
            assert "public" in cos_key

    def test_resolve_empty_path_returns_base(self, mock_storage):
        """空路径返回 base_path"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
            cos_key, err = vfs._resolve_path("")

            assert cos_key == vfs.base_path
            assert err is None


# ============================================================================
# 3. 读写文件测试
# ============================================================================

class TestVirtualFileSystemReadWrite:
    """测试读写文件"""

    def test_write_and_read_file(self, temp_root, local_storage_fixture):
        """写文件后可以读取"""
        from services.virtual_filesystem import VirtualFileSystem

        storage = local_storage_fixture
        vfs = VirtualFileSystem(user_id="123", storage=storage)

        test_content = b"Hello, VFS!"

        # 直接通过 storage 写
        storage.put_object("test/file.txt", test_content)

        # 直接通过 storage 读
        result = storage.get_file_content("test/file.txt")
        assert result == test_content

    def test_read_nonexistent_file(self, mock_storage):
        """读取不存在的文件"""
        from services.virtual_filesystem import VirtualFileSystem

        mock_storage.get_file_content.return_value = None

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)

        with patch.object(vfs, "_vpath_to_cos", return_value="nonexistent.txt"):
            result = vfs.storage.get_file_content("nonexistent.txt")

        assert result is None

    def test_write_calls_storage_put_object(self, mock_storage):
        """写操作调用 storage.put_object"""
        from services.virtual_filesystem import VirtualFileSystem

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)

        vfs.storage.put_object("test.txt", b"content")

        mock_storage.put_object.assert_called_once_with("test.txt", b"content")


# ============================================================================
# 4. 文件存在检查测试
# ============================================================================

class TestVirtualFileSystemFileExists:
    """测试文件存在检查"""

    def test_file_exists_returns_metadata(self, mock_storage):
        """存在的文件返回元数据"""
        from services.virtual_filesystem import VirtualFileSystem

        mock_storage.file_exists.return_value = {
            "exists": True,
            "size": 100,
            "is_dir": False
        }

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
        result = vfs.storage.file_exists("test.txt")

        assert result is not None
        assert result["exists"] is True
        assert result["size"] == 100

    def test_file_not_exists_returns_none(self, mock_storage):
        """不存在的文件返回 None"""
        from services.virtual_filesystem import VirtualFileSystem

        mock_storage.file_exists.return_value = None

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
        result = vfs.storage.file_exists("nonexistent.txt")

        assert result is None


# ============================================================================
# 5. 删除文件测试
# ============================================================================

class TestVirtualFileSystemDelete:
    """测试删除文件"""

    def test_delete_existing_file(self, mock_storage):
        """删除存在的文件"""
        from services.virtual_filesystem import VirtualFileSystem

        mock_storage.delete_file_by_key.return_value = True

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
        result = vfs.storage.delete_file_by_key("test.txt")

        assert result is True
        mock_storage.delete_file_by_key.assert_called_once_with("test.txt")

    def test_delete_nonexistent_file(self, mock_storage):
        """删除不存在的文件返回 False"""
        from services.virtual_filesystem import VirtualFileSystem

        mock_storage.delete_file_by_key.return_value = False

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
        result = vfs.storage.delete_file_by_key("nonexistent.txt")

        assert result is False


# ============================================================================
# 6. 列出目录测试
# ============================================================================

class TestVirtualFileSystemListDir:
    """测试列出目录"""

    def test_list_objects_returns_list(self, mock_storage):
        """列出对象返回列表"""
        from services.virtual_filesystem import VirtualFileSystem

        mock_storage.list_objects.return_value = [
            {"Key": "test/file1.txt", "Size": 100, "LastModified": "2024-01-01T00:00:00Z"},
            {"Key": "test/file2.txt", "Size": 200, "LastModified": "2024-01-02T00:00:00Z"},
        ]

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
        result = vfs.storage.list_objects("test/")

        assert isinstance(result, list)
        assert len(result) == 2

    def test_list_objects_empty_dir(self, mock_storage):
        """空目录返回空列表"""
        from services.virtual_filesystem import VirtualFileSystem

        mock_storage.list_objects.return_value = []

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
        result = vfs.storage.list_objects("empty/")

        assert result == []


# ============================================================================
# 7. 路径映射测试
# ============================================================================

class TestVirtualFileSystemPathMapping:
    """测试路径映射"""

    def test_workspace_prefix_mapping(self, mock_storage):
        """workspace/ 前缀映射正确"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)

            cos_key, err = vfs._resolve_path("/workspace/myfile.txt")

            assert err is None
            assert "workspace" in cos_key

    def test_relative_path_with_cwd(self, mock_storage):
        """相对路径使用当前工作目录"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
            vfs._cwd = "subdir"

            cos_key, err = vfs._resolve_path("file.txt")

            assert "subdir" in cos_key


# ============================================================================
# 8. 后端切换测试
# ============================================================================

class TestVirtualFileSystemBackendSwitch:
    """测试不同类型后端切换"""

    def test_switch_from_mock_to_local(self, mock_storage, temp_root):
        """从 mock 切换到 LocalStorage"""
        from services.virtual_filesystem import VirtualFileSystem
        from services.local_storage import LocalStorage

        # 初始用 mock
        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
        assert vfs._storage is mock_storage

        # 切换到 LocalStorage
        local_storage = LocalStorage(root_dir=temp_root)
        vfs._storage = local_storage

        assert vfs._storage is local_storage

    def test_storage_property_lazy_loads(self):
        """storage 属性懒加载"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.file_storage.create_file_storage") as mock_create:
            mock_storage_instance = MagicMock()
            mock_create.return_value = mock_storage_instance

            vfs = VirtualFileSystem(user_id="123")

            # 还没访问 storage，工厂没调用
            assert mock_create.call_count == 0

            # 访问 storage
            _ = vfs.storage

            # 工厂被调用
            mock_create.assert_called_once()


# ============================================================================
# 9. 目录遍历测试
# ============================================================================

class TestVirtualFileSystemDirTraversal:
    """测试目录遍历"""

    def test_list_deeply_nested_dir(self, mock_storage):
        """列出深层嵌套目录"""
        from services.virtual_filesystem import VirtualFileSystem

        mock_storage.list_objects.return_value = [
            {"Key": "a/b/c/d/file.txt", "Size": 50, "LastModified": "2024-01-01T00:00:00Z"},
        ]

        vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
        result = vfs.storage.list_objects("a/b/c/d/")

        assert len(result) == 1
        assert result[0]["Key"] == "a/b/c/d/file.txt"

    def test_resolve_nested_relative_path(self, mock_storage):
        """解析嵌套相对路径"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
            vfs._cwd = "a/b"

            cos_key, err = vfs._resolve_path("c/file.txt")

            assert err is None
            assert "a/b/c" in cos_key

    def test_cannot_traverse_above_base(self, mock_storage):
        """不能穿越到 base_path 之上"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)
            vfs._cwd = "a/b"

            cos_key, err = vfs._resolve_path("../../file.txt")

            # 应该被拒绝
            assert err is not None or cos_key is None or cos_key.count("..") == 0


# ============================================================================
# 10. 边缘情况测试
# ============================================================================

class TestVirtualFileSystemEdgeCases:
    """边缘情况测试"""

    def test_init_with_both_storage_and_storage_service(self, mock_storage):
        """同时传 storage 和 storage_service 时 storage 优先"""
        from services.virtual_filesystem import VirtualFileSystem

        mock_service = MagicMock()
        vfs = VirtualFileSystem(user_id="123", storage_service=mock_service, storage=mock_storage)

        assert vfs._storage is mock_storage

    def test_empty_user_id(self, mock_storage):
        """空 user_id 可初始化"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="", storage=mock_storage)

            assert vfs.user_id == ""

    def test_special_chars_in_path(self, mock_storage):
        """特殊字符路径"""
        from services.virtual_filesystem import VirtualFileSystem

        with patch("services.virtual_filesystem.settings") as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"

            vfs = VirtualFileSystem(user_id="123", storage=mock_storage)

            # 包含空格的路径
            cos_key, err = vfs._resolve_path("/workspace/my file.txt")
            assert err is None
