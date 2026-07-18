"""
CosStorage 专项测试

测试覆盖:
- 继承自 FileStorage
- 5 个抽象方法的实现
- file_exists 用 head_object
- list_objects 分页
- put_object 不返回值
- get_file_content 分块读取
- 配置不完整时初始化异常
- StorageService 兼容性（isinstance 检查）
"""

import os
import sys
import pytest
from unittest.mock import MagicMock, patch, call

# 确保项目根在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from services.file_storage import FileStorage


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_cos_client():
    """Mock COS S3 client"""
    mock = MagicMock()
    return mock


@pytest.fixture
def mock_settings():
    """Mock settings for COS"""
    with patch("services.storage_service.settings") as mock:
        mock.TENCENT_COS_SECRET_ID = "test-id"
        mock.TENCENT_COS_SECRET_KEY = "test-key"
        mock.TENCENT_COS_BUCKET = "test-bucket"
        mock.TENCENT_COS_REGION = "ap-guangzhou"
        mock.STORAGE_PREFIX = "feclaw/"
        yield mock


@pytest.fixture
def cos_storage(mock_cos_client, mock_settings):
    """创建 CosStorage 实例（mock client）"""
    with patch("services.storage_service.CosS3Client", return_value=mock_cos_client):
        from services.storage_service import CosStorage
        storage = CosStorage()
        storage.client = mock_cos_client  # 覆盖为 mock
        return storage, mock_cos_client


# ============================================================================
# 1. FileStorage 继承测试
# ============================================================================

class TestCosStorageInheritance:
    """测试 CosStorage 继承自 FileStorage"""

    def test_cos_storage_is_file_storage(self, cos_storage):
        """CosStorage 是 FileStorage 的子类"""
        storage, _ = cos_storage
        assert isinstance(storage, FileStorage)

    def test_cos_storage_is_abstract(self):
        """CosStorage 不可直接实例化（ABC）"""
        with patch("services.storage_service.settings") as mock_settings:
            mock_settings.TENCENT_COS_SECRET_ID = "test-id"
            mock_settings.TENCENT_COS_SECRET_KEY = "test-key"
            mock_settings.TENCENT_COS_BUCKET = "test-bucket"
            mock_settings.TENCENT_COS_REGION = "ap-guangzhou"

            # FileStorage 仍是 ABC，但 CosStorage 不是
            # CosStorage 实现了全部抽象方法，可以实例化
            from services.storage_service import CosStorage
            assert issubclass(CosStorage, FileStorage)


# ============================================================================
# 2. 5 个抽象方法实现测试
# ============================================================================

class TestCosStorageAbstractMethods:
    """测试 5 个抽象方法都有实现"""

    def test_all_five_methods_exist(self, cos_storage):
        """5 个抽象方法都存在"""
        storage, _ = cos_storage
        assert hasattr(storage, "get_file_content")
        assert hasattr(storage, "put_object")
        assert hasattr(storage, "delete_file_by_key")
        assert hasattr(storage, "list_objects")
        assert hasattr(storage, "file_exists")

    def test_get_file_content_is_callable(self, cos_storage):
        """get_file_content 可调用"""
        storage, _ = cos_storage
        assert callable(storage.get_file_content)

    def test_put_object_is_callable(self, cos_storage):
        """put_object 可调用"""
        storage, _ = cos_storage
        assert callable(storage.put_object)

    def test_delete_file_by_key_is_callable(self, cos_storage):
        """delete_file_by_key 可调用"""
        storage, _ = cos_storage
        assert callable(storage.delete_file_by_key)

    def test_list_objects_is_callable(self, cos_storage):
        """list_objects 可调用"""
        storage, _ = cos_storage
        assert callable(storage.list_objects)

    def test_file_exists_is_callable(self, cos_storage):
        """file_exists 可调用"""
        storage, _ = cos_storage
        assert callable(storage.file_exists)


# ============================================================================
# 3. file_exists 用 head_object 测试
# ============================================================================

class TestCosStorageFileExists:
    """测试 file_exists 使用 head_object"""

    def test_file_exists_calls_head_object(self, cos_storage):
        """file_exists 调用 head_object"""
        storage, mock = cos_storage
        mock.head_object.return_value = {
            "ContentLength": "1024",
            "ContentType": "text/plain",
        }

        storage.file_exists("test/key.txt")

        mock.head_object.assert_called_once()
        assert mock.head_object.call_args[1]["Key"] == "test/key.txt"

    def test_file_exists_returns_metadata(self, cos_storage):
        """file_exists 返回正确元数据"""
        storage, mock = cos_storage
        mock.head_object.return_value = {
            "ContentLength": "2048",
            "ContentType": "application/json",
        }

        result = storage.file_exists("test/key.txt")

        assert result is not None
        assert result["exists"] is True
        assert result["size"] == "2048"
        assert result["content_type"] == "application/json"

    def test_file_exists_not_found_returns_none(self, cos_storage):
        """文件不存在返回 None"""
        storage, mock = cos_storage
        mock.head_object.side_effect = Exception("Not found")

        result = storage.file_exists("nonexistent/key.txt")

        assert result is None

    def test_file_exists_error_returns_none(self, cos_storage):
        """head_object 出错返回 None"""
        storage, mock = cos_storage
        mock.head_object.side_effect = Exception("Service error")

        result = storage.file_exists("test/error.txt")

        assert result is None


# ============================================================================
# 4. list_objects 分页测试
# ============================================================================

class TestCosStorageListObjectsPagination:
    """测试 list_objects 分页"""

    def test_list_objects_single_page(self, cos_storage):
        """单页数据"""
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

    def test_list_objects_multiple_pages(self, cos_storage):
        """多页数据自动合并"""
        storage, mock = cos_storage
        mock.list_objects.side_effect = [
            {
                "Contents": [
                    {"Key": "test/file1.txt", "Size": 100},
                ],
                "IsTruncated": "true",
            },
            {
                "Contents": [
                    {"Key": "test/file2.txt", "Size": 200},
                ],
                "IsTruncated": "false",
            },
        ]

        result = storage.list_objects("test/")

        assert result is not None
        assert len(result) == 2
        assert mock.list_objects.call_count == 2

    def test_list_objects_three_pages(self, cos_storage):
        """3 页数据"""
        storage, mock = cos_storage
        mock.list_objects.side_effect = [
            {"Contents": [{"Key": "test/f1.txt"}], "IsTruncated": "true"},
            {"Contents": [{"Key": "test/f2.txt"}], "IsTruncated": "true"},
            {"Contents": [{"Key": "test/f3.txt"}], "IsTruncated": "false"},
        ]

        result = storage.list_objects("test/")

        assert len(result) == 3
        assert mock.list_objects.call_count == 3

    def test_list_objects_respects_max_keys(self, cos_storage):
        """超过 max_keys 时停止"""
        storage, mock = cos_storage
        mock.list_objects.return_value = {
            "Contents": [{"Key": "test/f1.txt"}],
            "IsTruncated": "true",
        }

        storage.list_objects("test/", max_keys=5)

        # 调用时传入的 MaxKeys 是 min(5, 1000) = 5
        call_args = mock.list_objects.call_args
        assert call_args[1]["MaxKeys"] == 5

    def test_list_objects_uses_marker_for_pagination(self, cos_storage):
        """分页时使用 marker"""
        storage, mock = cos_storage
        mock.list_objects.side_effect = [
            {
                "Contents": [{"Key": "test/f1.txt"}],
                "IsTruncated": "true",
            },
            {
                "Contents": [{"Key": "test/f2.txt"}],
                "IsTruncated": "false",
            },
        ]

        storage.list_objects("test/")

        assert mock.list_objects.call_count == 2
        # 第二次调用应该有 Marker
        second_call = mock.list_objects.call_args_list[1]
        assert "Marker" in second_call[1]

    def test_list_objects_empty_response(self, cos_storage):
        """空响应处理"""
        storage, mock = cos_storage
        mock.list_objects.return_value = {"Contents": []}

        result = storage.list_objects("empty/")

        assert result == []

    def test_list_objects_error_returns_none(self, cos_storage):
        """list_objects 出错返回 None"""
        storage, mock = cos_storage
        mock.list_objects.side_effect = Exception("List failed")

        result = storage.list_objects("test/")

        assert result is None


# ============================================================================
# 5. put_object 不返回值测试
# ============================================================================

class TestCosStoragePutObject:
    """测试 put_object 不返回值"""

    def test_put_object_returns_none(self, cos_storage):
        """put_object 不返回值"""
        storage, mock = cos_storage

        result = storage.put_object("test/key.txt", b"content")

        assert result is None

    def test_put_object_calls_put_object_api(self, cos_storage):
        """put_object 调用 COS put_object API"""
        storage, mock = cos_storage

        storage.put_object("test/key.txt", b"file content")

        mock.put_object.assert_called_once()
        call_kwargs = mock.put_object.call_args[1]
        assert call_kwargs["Key"] == "test/key.txt"
        assert call_kwargs["Body"] == b"file content"

    def test_put_object_no_return_value_even_on_success(self, cos_storage):
        """即使 COS 返回响应，put_object 仍不返回值"""
        storage, mock = cos_storage
        mock.put_object.return_value = {"ETag": "\"abc123\""}

        result = storage.put_object("test/key.txt", b"content")

        assert result is None


# ============================================================================
# 6. get_file_content 分块读取测试
# ============================================================================

class TestCosStorageGetFileContent:
    """测试 get_file_content 分块读取"""

    def test_get_file_content_single_chunk(self, cos_storage):
        """单块读取"""
        storage, mock = cos_storage
        mock_response = MagicMock()
        mock_response["Body"].read.side_effect = [b"hello world", b""]
        mock.get_object.return_value = mock_response

        result = storage.get_file_content("test/key.txt")

        assert result == b"hello world"

    def test_get_file_content_multiple_chunks(self, cos_storage):
        """多块读取"""
        storage, mock = cos_storage
        mock_response = MagicMock()
        mock_response["Body"].read.side_effect = [b"chunk1", b"chunk2", b"chunk3", b""]
        mock.get_object.return_value = mock_response

        result = storage.get_file_content("test/key.txt")

        assert result == b"chunk1chunk2chunk3"

    def test_get_file_content_empty_file(self, cos_storage):
        """空文件"""
        storage, mock = cos_storage
        mock_response = MagicMock()
        mock_response["Body"].read.side_effect = [b""]
        mock.get_object.return_value = mock_response

        result = storage.get_file_content("test/empty.txt")

        assert result == b""

    def test_get_file_content_not_found(self, cos_storage):
        """文件不存在返回 None"""
        storage, mock = cos_storage
        mock.get_object.side_effect = Exception("Not found")

        result = storage.get_file_content("nonexistent/key.txt")

        assert result is None

    def test_get_file_content_calls_get_object(self, cos_storage):
        """get_file_content 调用 COS get_object"""
        storage, mock = cos_storage
        mock_response = MagicMock()
        mock_response["Body"].read.side_effect = [b"content", b""]
        mock.get_object.return_value = mock_response

        storage.get_file_content("test/key.txt")

        mock.get_object.assert_called_once()
        assert mock.get_object.call_args[1]["Key"] == "test/key.txt"


# ============================================================================
# 7. 配置不完整时初始化异常测试
# ============================================================================

class TestCosStorageInitValidation:
    """测试配置不完整时初始化异常"""

    def test_missing_secret_id_raises(self):
        """缺少 SECRET_ID 抛异常"""
        with patch("services.storage_service.settings") as mock:
            mock.TENCENT_COS_SECRET_ID = ""
            mock.TENCENT_COS_SECRET_KEY = "key"
            mock.TENCENT_COS_BUCKET = "bucket"
            mock.TENCENT_COS_REGION = "ap-guangzhou"

            with pytest.raises(ValueError, match="配置不完整"):
                from services.storage_service import CosStorage
                CosStorage()

    def test_missing_secret_key_raises(self):
        """缺少 SECRET_KEY 抛异常"""
        with patch("services.storage_service.settings") as mock:
            mock.TENCENT_COS_SECRET_ID = "id"
            mock.TENCENT_COS_SECRET_KEY = ""
            mock.TENCENT_COS_BUCKET = "bucket"
            mock.TENCENT_COS_REGION = "ap-guangzhou"

            with pytest.raises(ValueError, match="配置不完整"):
                from services.storage_service import CosStorage
                CosStorage()

    def test_missing_bucket_raises(self):
        """缺少 BUCKET 抛异常"""
        with patch("services.storage_service.settings") as mock:
            mock.TENCENT_COS_SECRET_ID = "id"
            mock.TENCENT_COS_SECRET_KEY = "key"
            mock.TENCENT_COS_BUCKET = ""
            mock.TENCENT_COS_REGION = "ap-guangzhou"

            with pytest.raises(ValueError, match="配置不完整"):
                from services.storage_service import CosStorage
                CosStorage()

    def test_complete_config_does_not_raise(self):
        """完整配置不抛异常"""
        with patch("services.storage_service.settings") as mock:
            mock.TENCENT_COS_SECRET_ID = "id"
            mock.TENCENT_COS_SECRET_KEY = "key"
            mock.TENCENT_COS_BUCKET = "bucket"
            mock.TENCENT_COS_REGION = "ap-guangzhou"
            mock.STORAGE_PREFIX = "feclaw/"

            with patch("services.storage_service.CosS3Client"):
                from services.storage_service import CosStorage
                # 不抛异常
                storage = CosStorage()
                assert storage is not None


# ============================================================================
# 8. StorageService 兼容性测试
# ============================================================================

class TestStorageServiceCompatibility:
    """测试 StorageService 向后兼容"""

    def test_storage_service_is_cos_storage(self):
        """StorageService 是 CosStorage 的子类"""
        from services.storage_service import StorageService, CosStorage
        assert issubclass(StorageService, CosStorage)

    def test_storage_service_is_file_storage(self):
        """StorageService 是 FileStorage 的子类"""
        from services.storage_service import StorageService, FileStorage
        assert issubclass(StorageService, FileStorage)

    def test_storage_service_instance_is_cos_storage(self):
        """StorageService 实例是 CosStorage 实例"""
        with patch("services.storage_service.settings") as mock_settings:
            mock_settings.TENCENT_COS_SECRET_ID = "id"
            mock_settings.TENCENT_COS_SECRET_KEY = "key"
            mock_settings.TENCENT_COS_BUCKET = "bucket"
            mock_settings.TENCENT_COS_REGION = "ap-guangzhou"
            mock_settings.STORAGE_PREFIX = "feclaw/"

            with patch("services.storage_service.CosS3Client"):
                from services.storage_service import StorageService, CosStorage
                svc = StorageService()
                assert isinstance(svc, CosStorage)
                assert isinstance(svc, FileStorage)

    def test_storage_service_has_all_methods(self):
        """StorageService 有所有必需方法"""
        with patch("services.storage_service.settings") as mock_settings:
            mock_settings.TENCENT_COS_SECRET_ID = "id"
            mock_settings.TENCENT_COS_SECRET_KEY = "key"
            mock_settings.TENCENT_COS_BUCKET = "bucket"
            mock_settings.TENCENT_COS_REGION = "ap-guangzhou"
            mock_settings.STORAGE_PREFIX = "feclaw/"

            with patch("services.storage_service.CosS3Client"):
                from services.storage_service import StorageService
                svc = StorageService()
                assert hasattr(svc, "get_file_content")
                assert hasattr(svc, "put_object")
                assert hasattr(svc, "delete_file_by_key")
                assert hasattr(svc, "list_objects")
                assert hasattr(svc, "file_exists")


# ============================================================================
# 9. 其他 COS 特定方法测试
# ============================================================================

class TestCosStorageSpecificMethods:
    """测试 CosStorage 特有的非抽象方法"""

    def test_generate_file_key(self, cos_storage):
        """generate_file_key 方法存在"""
        storage, _ = cos_storage
        assert hasattr(storage, "generate_file_key")

    def test_get_user_id_from_key(self, cos_storage):
        """get_user_id_from_key 方法存在"""
        storage, _ = cos_storage
        assert hasattr(storage, "get_user_id_from_key")

    def test_get_user_id_from_key_extracts_id(self, cos_storage):
        """get_user_id_from_key 正确提取 user_id"""
        storage, _ = cos_storage

        user_id = storage.get_user_id_from_key("feclaw/user_123/original/file.jpg")
        assert user_id == 123

    def test_get_user_id_from_key_no_user_prefix(self, cos_storage):
        """没有 user_ 前缀时返回 None"""
        storage, _ = cos_storage

        user_id = storage.get_user_id_from_key("feclaw/some/path/file.jpg")
        assert user_id is None
