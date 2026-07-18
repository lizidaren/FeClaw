"""
FileStorage 抽象层测试

测试覆盖:
- FileStorage ABC 不可实例化
- CosStorage 基本操作（mock COS client）
- LocalStorage 基本操作（tempdir）
- 路径穿越防护
- create_file_storage() 工厂函数
- 边缘情况（空文件、不存在文件、特殊字符路径）
"""

import os
import sys
import pytest
import tempfile
import shutil
from unittest.mock import MagicMock, patch
from datetime import datetime

# 确保项目根在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from services.file_storage import FileStorage, create_file_storage


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_root():
    """创建临时目录作为 LocalStorage 根目录"""
    path = tempfile.mkdtemp(prefix="feclaw_test_storage_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def mock_cos_client():
    """Mock COS S3 client"""
    mock = MagicMock()
    return mock


@pytest.fixture
def cos_storage(mock_cos_client):
    """创建 CosStorage 实例（mock client）"""
    with patch("services.storage_service.CosS3Client", return_value=mock_cos_client):
        # 需要 mock settings（CosConfig 要求 region 是字符串类型）
        with patch("services.storage_service.settings") as mock_settings:
            mock_settings.TENCENT_COS_SECRET_ID = "test-id"
            mock_settings.TENCENT_COS_SECRET_KEY = "test-key"
            mock_settings.TENCENT_COS_BUCKET = "test-bucket"
            mock_settings.TENCENT_COS_REGION = "ap-guangzhou"
            from services.storage_service import CosStorage
            storage = CosStorage()
            storage.client = mock_cos_client  # 覆盖为 mock
            return storage, mock_cos_client


@pytest.fixture
def local_storage(temp_root):
    """创建 LocalStorage 实例"""
    from services.local_storage import LocalStorage
    return LocalStorage(root_dir=temp_root)


# ============================================================================
# 1. FileStorage ABC 测试
# ============================================================================

class TestFileStorageABC:
    """测试抽象基类不可直接实例化"""

    def test_cannot_instantiate_abc(self):
        """FileStorage ABC 不能直接实例化"""
        with pytest.raises(TypeError):
            FileStorage()

    def test_subclass_must_implement_all_methods(self):
        """子类必须实现全部 5 个抽象方法"""
        # 少实现一个方法
        class IncompleteStorage(FileStorage):
            def get_file_content(self, key): return None
            def put_object(self, key, bytes): pass
            def delete_file_by_key(self, key): return True
            def list_objects(self, prefix, max_keys=1000): return []

        with pytest.raises(TypeError):
            IncompleteStorage()


# ============================================================================
# 2. CosStorage 测试
# ============================================================================

class TestCosStorage:
    """测试 COS 存储后端"""

    def test_get_file_content_success(self, cos_storage):
        storage, mock = cos_storage
        mock_response = MagicMock()
        mock_response['Body'].read.side_effect = [b"hello", b" world", b""]
        mock.get_object.return_value = mock_response

        result = storage.get_file_content("test/key.txt")
        assert result == b"hello world"
        mock.get_object.assert_called_once()
        assert mock.get_object.call_args[1]["Key"] == "test/key.txt"

    def test_get_file_content_not_found(self, cos_storage):
        storage, mock = cos_storage
        mock.get_object.side_effect = Exception("Not found")

        result = storage.get_file_content("nonexistent/key.txt")
        assert result is None

    def test_put_object_success(self, cos_storage):
        storage, mock = cos_storage

        result = storage.put_object("test/key.txt", b"file content")
        assert result is None
        mock.put_object.assert_called_once()
        assert mock.put_object.call_args[1]["Key"] == "test/key.txt"

    def test_delete_file_by_key_success(self, cos_storage):
        storage, mock = cos_storage
        mock.delete_object.return_value = {}

        result = storage.delete_file_by_key("test/key.txt")
        assert result is True
        mock.delete_object.assert_called_once()
        assert mock.delete_object.call_args[1]["Key"] == "test/key.txt"

    def test_delete_file_by_key_failure(self, cos_storage):
        storage, mock = cos_storage
        mock.delete_object.side_effect = Exception("Delete failed")

        result = storage.delete_file_by_key("test/key.txt")
        assert result is False

    def test_list_objects(self, cos_storage):
        storage, mock = cos_storage
        mock.list_objects.return_value = {
            "Contents": [
                {"Key": "test/file1.txt", "Size": 100},
                {"Key": "test/file2.txt", "Size": 200},
            ],
            "IsTruncated": "false",
        }

        result = storage.list_objects("test/")
        assert result is not None
        assert len(result) == 2
        assert result[0]["Key"] == "test/file1.txt"

    def test_list_objects_with_pagination(self, cos_storage):
        storage, mock = cos_storage
        # 模拟多页数据
        mock.list_objects.side_effect = [
            {
                "Contents": [
                    {"Key": "test/file1.txt", "Size": 100},
                    {"Key": "test/file2.txt", "Size": 200},
                ],
                "IsTruncated": "true",
            },
            {
                "Contents": [
                    {"Key": "test/file3.txt", "Size": 300},
                ],
                "IsTruncated": "false",
            },
        ]

        result = storage.list_objects("test/", max_keys=1000)
        assert result is not None
        assert len(result) == 3

    def test_list_objects_failure(self, cos_storage):
        storage, mock = cos_storage
        mock.list_objects.side_effect = Exception("List failed")

        result = storage.list_objects("test/")
        assert result is None

    def test_file_exists_true(self, cos_storage):
        storage, mock = cos_storage
        mock.head_object.return_value = {
            "ContentLength": "1024",
            "ContentType": "text/plain",
        }

        result = storage.file_exists("test/key.txt")
        assert result is not None
        assert result["exists"] is True
        assert result["size"] == "1024"

    def test_file_exists_false(self, cos_storage):
        storage, mock = cos_storage
        mock.head_object.side_effect = Exception("Not found")

        result = storage.file_exists("nonexistent/key.txt")
        assert result is None


# ============================================================================
# 3. LocalStorage 测试
# ============================================================================

class TestLocalStorage:
    """测试本地文件系统存储后端"""

    def test_init_creates_root(self, temp_root):
        """初始化时自动创建根目录"""
        from services.local_storage import LocalStorage
        new_root = os.path.join(temp_root, "auto-created")
        storage = LocalStorage(root_dir=new_root)
        assert os.path.isdir(new_root)

    def test_put_and_get(self, local_storage):
        """写入后能正确读取"""
        local_storage.put_object("test/hello.txt", b"Hello, World!")
        result = local_storage.get_file_content("test/hello.txt")
        assert result == b"Hello, World!"

    def test_get_nonexistent(self, local_storage):
        """读不存在的文件返回 None"""
        result = local_storage.get_file_content("nonexistent/file.txt")
        assert result is None

    def test_put_object_returns_none(self, local_storage):
        """put_object 不返回值"""
        result = local_storage.put_object("test/returns_none.txt", b"data")
        assert result is None

    def test_delete_existing(self, local_storage):
        """删除存在的文件返回 True"""
        local_storage.put_object("test/to_delete.txt", b"delete me")
        result = local_storage.delete_file_by_key("test/to_delete.txt")
        assert result is True
        # 确认文件已删除
        assert local_storage.get_file_content("test/to_delete.txt") is None

    def test_delete_nonexistent(self, local_storage):
        """删除不存在的文件返回 False"""
        result = local_storage.delete_file_by_key("test/nonexistent.txt")
        assert result is False

    def test_list_objects(self, local_storage):
        """列出对象"""
        local_storage.put_object("test/dir/a.txt", b"aaa")
        local_storage.put_object("test/dir/b.txt", b"bbb")
        local_storage.put_object("test/other/c.txt", b"ccc")

        result = local_storage.list_objects("test/dir/")
        assert result is not None
        keys = [r["Key"] for r in result]
        assert len(keys) == 2
        assert "test/dir/a.txt" in keys
        assert "test/dir/b.txt" in keys

    def test_list_objects_with_prefix_no_slash(self, local_storage):
        """不带尾部斜杠的 prefix"""
        local_storage.put_object("test/dir/a.txt", b"aaa")
        result = local_storage.list_objects("test/dir")
        assert result is not None
        assert len(result) >= 1

    def test_list_objects_nonexistent_dir(self, local_storage):
        """不存在的目录返回空列表"""
        result = local_storage.list_objects("nonexistent/")
        assert result == []

    def test_file_exists_true(self, local_storage):
        """存在的文件返回元数据"""
        local_storage.put_object("test/exists.txt", b"hello")
        result = local_storage.file_exists("test/exists.txt")
        assert result is not None
        assert result["exists"] is True
        assert result["size"] == 5
        assert result["is_dir"] is False

    def test_file_exists_false(self, local_storage):
        """不存在的文件返回 None"""
        result = local_storage.file_exists("test/nonexistent.txt")
        assert result is None

    def test_file_exists_dir(self, local_storage):
        """目录返回 is_dir=True"""
        local_storage.put_object("test/subdir/.placeholder", b"")
        result = local_storage.file_exists("test/subdir")
        assert result is not None
        assert result["is_dir"] is True


# ============================================================================
# 4. 路径穿越防护测试
# ============================================================================

class TestLocalStoragePathTraversal:
    """测试 LocalStorage 的路径穿越防护"""

    def test_simple_traversal(self, local_storage):
        """简单路径穿越被拒绝"""
        with pytest.raises(ValueError, match="Path traversal"):
            local_storage._resolve("../../etc/passwd")

    def test_deep_traversal(self, local_storage):
        """深层路径穿越被拒绝"""
        with pytest.raises(ValueError, match="Path traversal"):
            local_storage._resolve("a/b/c/../../../../etc/shadow")

    def test_encoded_traversal(self, local_storage):
        """编码后的路径穿越"""
        with pytest.raises(ValueError, match="Path traversal"):
            local_storage._resolve("test/../../../etc/hosts")

    def test_normal_path_ok(self, local_storage):
        """正常路径不被拒绝"""
        path = local_storage._resolve("feclaw/user_1/original/file.jpg")
        assert "feclaw" in path
        assert "user_1" in path

    def test_root_level_key_ok(self, local_storage):
        """顶级 key 正常"""
        path = local_storage._resolve("config.json")
        assert path.endswith("config.json")

    def test_backslashes_windows_style(self, local_storage):
        """Windows 反斜杠被转换为正斜杠"""
        path = local_storage._resolve("test\\dir\\file.txt")
        assert "test" in path
        assert "dir" in path

    def test_empty_key(self, local_storage):
        """空 key"""
        path = local_storage._resolve("")
        assert path == os.path.realpath(local_storage.root)


# ============================================================================
# 5. 工厂函数测试
# ============================================================================

class TestCreateFileStorage:
    """测试 create_file_storage 工厂函数"""

    def test_mode_local(self):
        """local 模式返回 LocalStorage"""
        with tempfile.TemporaryDirectory() as tmp:
            with patch("config.settings.LOCAL_STORAGE_ROOT", tmp):
                storage = create_file_storage(mode="local")
                from services.local_storage import LocalStorage
                assert isinstance(storage, LocalStorage)

    def test_mode_cos_with_config(self):
        """cos 模式且有配置时返回 CosStorage"""
        with patch("config.settings.TENCENT_COS_SECRET_ID", "id"):
            with patch("config.settings.TENCENT_COS_SECRET_KEY", "key"):
                with patch("config.settings.TENCENT_COS_BUCKET", "bucket"):
                    with patch("services.storage_service.CosS3Client"):
                        storage = create_file_storage(mode="cos")
                        from services.storage_service import CosStorage
                        assert isinstance(storage, CosStorage)

    def test_mode_cos_no_config(self):
        """cos 模式无配置时抛异常"""
        with patch("config.settings.TENCENT_COS_SECRET_ID", ""):
            with patch("config.settings.TENCENT_COS_SECRET_KEY", ""):
                with patch("config.settings.TENCENT_COS_BUCKET", ""):
                    with pytest.raises(ValueError, match="COS mode requires"):
                        create_file_storage(mode="cos")

    def test_mode_auto_with_cos_config(self):
        """auto 模式有 COS 配置时返回 CosStorage"""
        with patch("config.settings.TENCENT_COS_SECRET_ID", "id"):
            with patch("config.settings.TENCENT_COS_SECRET_KEY", "key"):
                with patch("config.settings.TENCENT_COS_BUCKET", "bucket"):
                    with patch("services.storage_service.CosS3Client"):
                        storage = create_file_storage(mode="auto")
                        from services.storage_service import CosStorage
                        assert isinstance(storage, CosStorage)

    def test_mode_auto_without_cos(self):
        """auto 模式无 COS 配置时 fallback 到 LocalStorage"""
        with patch("config.settings.TENCENT_COS_SECRET_ID", ""):
            with patch("config.settings.TENCENT_COS_SECRET_KEY", ""):
                with patch("config.settings.TENCENT_COS_BUCKET", ""):
                    with tempfile.TemporaryDirectory() as tmp:
                        with patch("config.settings.LOCAL_STORAGE_ROOT", tmp):
                            storage = create_file_storage(mode="auto")
                            from services.local_storage import LocalStorage
                            assert isinstance(storage, LocalStorage)

    def test_invalid_mode(self):
        """非法 mode 抛异常"""
        with patch("config.settings.TENCENT_COS_SECRET_ID", ""):
            with patch("config.settings.TENCENT_COS_SECRET_KEY", ""):
                with patch("config.settings.TENCENT_COS_BUCKET", ""):
                    with pytest.raises(ValueError, match="Unknown storage mode"):
                        create_file_storage(mode="invalid")


# ============================================================================
# 6. 边缘情况测试
# ============================================================================

class TestEdgeCases:
    """测试边缘情况"""

    def test_empty_file_content(self, local_storage):
        """空文件写入和读取"""
        local_storage.put_object("test/empty.txt", b"")
        result = local_storage.get_file_content("test/empty.txt")
        assert result == b""

    def test_large_file(self, local_storage):
        """大文件读写"""
        large_content = b"x" * 100_000  # 100KB
        local_storage.put_object("test/large.bin", large_content)
        result = local_storage.get_file_content("test/large.bin")
        assert result == large_content

    def test_binary_content(self, local_storage):
        """二进制内容（含 null 字节）"""
        binary = bytes(range(256))
        local_storage.put_object("test/binary.bin", binary)
        result = local_storage.get_file_content("test/binary.bin")
        assert result == binary

    def test_special_chars_in_key(self, local_storage):
        """特殊字符路径"""
        key = "test/special chars/汉字/emoji_🎉/file.txt"
        content = b"special"
        local_storage.put_object(key, content)
        result = local_storage.get_file_content(key)
        assert result == content

    def test_deeply_nested_path(self, local_storage):
        """深层嵌套路径"""
        key = "a/b/c/d/e/f/g/h/i/j/k/file.txt"
        local_storage.put_object(key, b"deep")
        result = local_storage.get_file_content(key)
        assert result == b"deep"

    def test_overwrite_existing_file(self, local_storage):
        """覆盖已有文件"""
        local_storage.put_object("test/overwrite.txt", b"original")
        local_storage.put_object("test/overwrite.txt", b"updated")
        result = local_storage.get_file_content("test/overwrite.txt")
        assert result == b"updated"

    def test_list_objects_max_keys_limit(self, local_storage):
        """list_objects 的 max_keys 限制"""
        for i in range(10):
            local_storage.put_object(f"test/limit/file_{i}.txt", b"data")

        result = local_storage.list_objects("test/limit/", max_keys=3)
        assert result is not None
        assert len(result) == 3

    def test_delete_returns_false_on_nonexistent(self, local_storage):
        """删除不存在的文件返回 False（不被视为异常）"""
        assert local_storage.delete_file_by_key("never/existed.txt") is False

    def test_list_objects_format(self, local_storage):
        """list_objects 返回格式与 COS 兼容"""
        local_storage.put_object("test/format/report.pdf", b"pdf content")

        result = local_storage.list_objects("test/format/")
        assert result is not None
        entry = result[0]

        # Key 字段
        assert "Key" in entry
        assert entry["Key"] == "test/format/report.pdf"

        # Size 字段
        assert "Size" in entry
        assert isinstance(entry["Size"], int)

        # LastModified 字段
        assert "LastModified" in entry
        assert isinstance(entry["LastModified"], str)  # ISO 格式字符串


# ============================================================================
# 7. CosStorage 兼容性测试
# ============================================================================

class TestCosStorageCompatibility:
    """测试 StorageService 兼容性"""

    def test_storage_service_is_cos_storage(self):
        """StorageService 是 CosStorage 的子类"""
        from services.storage_service import StorageService, CosStorage
        assert issubclass(StorageService, CosStorage)
        assert issubclass(StorageService, FileStorage)

    def test_storage_service_can_instantiate(self):
        """StorageService 仍可实例化"""
        from services.storage_service import StorageService, CosStorage
        # 需要 mock COS 配置
        with patch("services.storage_service.settings") as mock_settings:
            mock_settings.TENCENT_COS_SECRET_ID = "id"
            mock_settings.TENCENT_COS_SECRET_KEY = "key"
            mock_settings.TENCENT_COS_BUCKET = "bucket"
            mock_settings.TENCENT_COS_REGION = "ap-guangzhou"
            with patch("services.storage_service.CosS3Client"):
                svc = StorageService()
                assert isinstance(svc, CosStorage)

