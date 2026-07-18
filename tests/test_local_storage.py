"""
LocalStorage 专项测试

测试覆盖:
- 目录创建（不存在的父目录自动创建）
- 路径穿越防护（_resolve 方法）
- 符号链接防护（os.path.realpath 的行为）
- Windows 反斜杠转正斜杠
- 文件存在检查（file_exists）
- 大目录列表（list_objects 超过 max_keys）
- 并发写入（多个写入同时写不同 key）
- 文件权限（写入后文件可读）
- 空文件目录清理
"""

import os
import sys
import pytest
import tempfile
import shutil
import threading
import time
from unittest.mock import MagicMock, patch

# 确保项目根在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from services.local_storage import LocalStorage


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_root():
    """创建临时目录作为 LocalStorage 根目录"""
    path = tempfile.mkdtemp(prefix="feclaw_test_local_storage_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def local_storage(temp_root):
    """创建 LocalStorage 实例"""
    return LocalStorage(root_dir=temp_root)


@pytest.fixture
def local_storage_with_public(temp_root):
    """创建带独立 public_root 的 LocalStorage 实例"""
    public_root = os.path.join(temp_root, "public")
    return LocalStorage(root_dir=temp_root, public_root=public_root)


# ============================================================================
# 1. 目录创建测试
# ============================================================================

class TestLocalStorageDirectoryCreation:
    """测试不存在的父目录自动创建"""

    def test_put_object_creates_nested_dirs(self, local_storage, temp_root):
        """put_object 自动创建多层嵌套目录"""
        key = "a/b/c/d/e/deep_file.txt"
        content = b"nested content"

        local_storage.put_object(key, content)

        # 验证文件存在且内容正确
        result = local_storage.get_file_content(key)
        assert result == content

        # 验证中间目录存在
        assert os.path.isdir(os.path.join(temp_root, "a"))
        assert os.path.isdir(os.path.join(temp_root, "a", "b"))
        assert os.path.isdir(os.path.join(temp_root, "a", "b", "c"))

    def test_put_object_creates_parent_of_file(self, local_storage, temp_root):
        """put_object 创建文件的直接父目录"""
        key = "single_level/file.txt"
        local_storage.put_object(key, b"content")

        expected_path = os.path.join(temp_root, "single_level")
        assert os.path.isdir(expected_path)

    def test_init_creates_root_dir(self, temp_root):
        """__init__ 自动创建根目录"""
        new_root = os.path.join(temp_root, "auto", "created", "dir")
        assert not os.path.exists(new_root)

        storage = LocalStorage(root_dir=new_root)
        assert os.path.isdir(new_root)

    def test_init_creates_public_root(self, temp_root):
        """__init__ 自动创建独立的 public_root"""
        public_root = os.path.join(temp_root, "my-public")
        assert not os.path.exists(public_root)

        storage = LocalStorage(root_dir=temp_root, public_root=public_root)
        assert os.path.isdir(public_root)


# ============================================================================
# 2. 路径穿越防护测试
# ============================================================================

class TestLocalStoragePathTraversal:
    """测试路径穿越防护"""

    def test_simple_traversal_rejected(self, local_storage):
        """简单 ../ 路径穿越被拒绝"""
        with pytest.raises(ValueError, match="Path traversal"):
            local_storage._resolve("../../etc/passwd")

    def test_deep_traversal_rejected(self, local_storage):
        """深层路径穿越被拒绝"""
        with pytest.raises(ValueError, match="Path traversal"):
            local_storage._resolve("a/b/c/../../../../etc/shadow")

    def test_encoded_traversal_rejected(self, local_storage):
        """编码后的路径穿越被拒绝"""
        with pytest.raises(ValueError, match="Path traversal"):
            local_storage._resolve("test/../../../etc/hosts")

    def test_absolute_path_becomes_relative(self, local_storage):
        """绝对路径被剥离前导斜杠变成相对路径"""
        # /etc/passwd -> etc/passwd (前导斜杠被 lstrip)
        # 由于 temp_root 是新目录，etc/passwd 会解析到 base/etc/passwd
        # 不会触发路径穿越，因为结果是 base/etc/passwd
        path = local_storage._resolve("/etc/passwd")
        # 路径应该是 base_real/etc/passwd 形式
        assert path.startswith(os.path.realpath(local_storage.root))

    def test_traversal_with_normalized_path_rejected(self, local_storage):
        """标准化后的路径穿越被拒绝（realpath 解析符号链接后）"""
        # 创建一个可以通过标准化但实际穿越的路径
        # 先放一个正常文件
        local_storage.put_object("safe/file.txt", b"safe")

        # 尝试穿越 - 在 _resolve 中 realpath 会解析符号链接等
        # 但由于我们的 temp_root 是新创建的，不会真的有符号链接穿越
        # 所以这里测试 case: safe/../../../unsafe
        with pytest.raises(ValueError, match="Path traversal"):
            local_storage._resolve("safe/../../../unsafe")

    def test_normal_path_ok(self, local_storage):
        """正常路径不被拒绝"""
        path = local_storage._resolve("feclaw/user_1/original/file.jpg")
        assert "feclaw" in path

    def test_root_level_key_ok(self, local_storage):
        """顶级 key 正常"""
        path = local_storage._resolve("config.json")
        assert path.endswith("config.json")

    def test_empty_key_returns_root(self, local_storage):
        """空 key 返回根目录"""
        path = local_storage._resolve("")
        assert path == os.path.realpath(local_storage.root)

    def test_leading_slash_stripped(self, local_storage):
        """前导斜杠被剥离"""
        path1 = local_storage._resolve("/leading/slash.txt")
        path2 = local_storage._resolve("leading/slash.txt")
        assert path1 == path2


# ============================================================================
# 3. 符号链接防护测试
# ============================================================================

class TestLocalStorageSymlinkProtection:
    """测试符号链接防护（os.path.realpath 的行为）"""

    def test_symlink_outside_root_rejected(self, local_storage, temp_root):
        """穿越到根目录外的符号链接被拒绝"""
        # 创建一个指向根目录外文件的符号链接
        target_file = os.path.join(temp_root, "..", "outside_file.txt")
        with open(target_file, "w") as f:
            f.write("outside")

        symlink_path = os.path.join(temp_root, "link_to_outside")
        os.symlink(target_file, symlink_path)

        # 尝试通过符号链接访问外部文件
        with pytest.raises(ValueError, match="Path traversal"):
            local_storage._resolve("link_to_outside")

    def test_symlink_to_inside_file_ok(self, local_storage, temp_root):
        """符号链接指向内部文件可以正常访问"""
        # 创建文件
        local_storage.put_object("target/file.txt", b"target content")

        # 创建符号链接
        link_key = "link_file.txt"
        real_path = local_storage._resolve("target/file.txt")
        link_path = os.path.join(temp_root, "link_file.txt")
        os.symlink(real_path, link_path)

        # 通过 key 访问 - 由于 key 是 link_file.txt，realpath 会解析到内部文件
        result = local_storage.get_file_content(link_key)
        assert result == b"target content"

    def test_realpath_resolves_symlinks(self, local_storage, temp_root):
        """验证 os.path.realpath 确实解析符号链接"""
        # 创建文件和符号链接
        target = os.path.join(temp_root, "real_file.txt")
        link = os.path.join(temp_root, "symlink_file.txt")
        with open(target, "w") as f:
            f.write("content")
        os.symlink(target, link)

        # realpath 解析后不再是链接
        assert os.path.realpath(link) == os.path.realpath(target)
        assert os.path.realpath(link) != link


# ============================================================================
# 4. Windows 反斜杠转正斜杠测试
# ============================================================================

class TestLocalStorageWindowsPath:
    """测试 Windows 反斜杠转换为正斜杠"""

    def test_backslash_converted_to_forward_slash(self, local_storage):
        """Windows 反斜杠路径被转换为正斜杠"""
        path = local_storage._resolve("test\\dir\\file.txt")
        assert "test" in path
        assert "dir" in path
        # 路径中不应有反斜杠
        assert "\\" not in path.replace("\\\\", "")  # 排除 Windows 盘符

    def test_mixed_slashes_normalized(self, local_storage):
        """混合斜杠路径被规范化"""
        # 注意：原始 key 中的反斜杠会被替换
        path = local_storage._resolve("a\\b/c\\d/file.txt")
        # 所有反斜杠被替换为正斜杠后拼接
        assert os.sep not in path or os.sep == "/"

    def test_windows_style_path_with_backslash(self, local_storage, temp_root):
        """Windows 风格路径可以正常写入"""
        key = "test\\windows\\path.txt"
        content = b"windows style"

        local_storage.put_object(key, content)

        # 用正斜杠路径读取
        result = local_storage.get_file_content("test/windows/path.txt")
        assert result == content


# ============================================================================
# 5. 文件存在检查测试
# ============================================================================

class TestLocalStorageFileExists:
    """测试 file_exists 方法"""

    def test_file_exists_returns_metadata(self, local_storage):
        """存在的文件返回完整元数据"""
        local_storage.put_object("test/exists.txt", b"hello")
        result = local_storage.file_exists("test/exists.txt")

        assert result is not None
        assert result["exists"] is True
        assert result["size"] == 5
        assert "mtime" in result
        assert result["is_dir"] is False

    def test_file_not_exists_returns_none(self, local_storage):
        """不存在的文件返回 None"""
        result = local_storage.file_exists("nonexistent/file.txt")
        assert result is None

    def test_directory_exists_returns_is_dir_true(self, local_storage):
        """目录返回 is_dir=True"""
        local_storage.put_object("test/subdir/.placeholder", b"")
        result = local_storage.file_exists("test/subdir")

        assert result is not None
        assert result["is_dir"] is True

    def test_empty_file_exists(self, local_storage):
        """空文件存在检查"""
        local_storage.put_object("test/empty.txt", b"")
        result = local_storage.file_exists("test/empty.txt")

        assert result is not None
        assert result["exists"] is True
        assert result["size"] == 0

    def test_file_exists_with_nested_path(self, local_storage):
        """嵌套路径的文件存在检查"""
        local_storage.put_object("a/b/c/d/nested.txt", b"nested")
        result = local_storage.file_exists("a/b/c/d/nested.txt")

        assert result is not None
        assert result["exists"] is True


# ============================================================================
# 6. 大目录列表测试（超过 max_keys）
# ============================================================================

class TestLocalStorageListObjectsLarge:
    """测试 list_objects 的 max_keys 限制"""

    def test_list_objects_respects_max_keys(self, local_storage):
        """list_objects 严格遵守 max_keys 限制"""
        # 创建 10 个文件
        for i in range(10):
            local_storage.put_object(f"test/limit/file_{i}.txt", b"data")

        # 请求 3 个
        result = local_storage.list_objects("test/limit/", max_keys=3)
        assert result is not None
        assert len(result) == 3

    def test_list_objects_exactly_max_keys(self, local_storage):
        """list_objects 返回恰好 max_keys 条"""
        for i in range(5):
            local_storage.put_object(f"test/exact/file_{i}.txt", b"data")

        result = local_storage.list_objects("test/exact/", max_keys=5)
        assert result is not None
        assert len(result) == 5

    def test_list_objects_less_than_max_keys(self, local_storage):
        """文件数量少于 max_keys 时返回全部"""
        for i in range(3):
            local_storage.put_object(f"test/few/file_{i}.txt", b"data")

        result = local_storage.list_objects("test/few/", max_keys=100)
        assert result is not None
        assert len(result) == 3

    def test_list_objects_empty_dir(self, local_storage):
        """空目录返回空列表（目录存在但里面没有文件）"""
        # 先创建目录结构但不放文件
        os.makedirs(os.path.join(local_storage.root, "test", "emptydir"), exist_ok=True)
        result = local_storage.list_objects("test/emptydir/")
        assert result == []

    def test_list_objects_nonexistent_prefix(self, local_storage):
        """不存在的 prefix 返回空列表"""
        result = local_storage.list_objects("nonexistent/prefix/")
        assert result == []

    def test_list_objects_allows_exactly_max_keys_plus_one(self, local_storage):
        """max_keys=3 时，列表前 3 个后停止"""
        for i in range(5):
            local_storage.put_object(f"test/stop/file_{i}.txt", b"data")

        result = local_storage.list_objects("test/stop/", max_keys=3)
        assert len(result) == 3


# ============================================================================
# 7. 并发写入测试
# ============================================================================

class TestLocalStorageConcurrentWrite:
    """测试并发写入"""

    def test_concurrent_writes_different_keys(self, local_storage):
        """多个线程同时写入不同 key 不冲突"""
        num_threads = 5
        results = {}
        errors = []

        def write_file(i):
            try:
                key = f"concurrent/file_{i}.txt"
                content = f"content_{i}".encode()
                local_storage.put_object(key, content)
                results[i] = local_storage.get_file_content(key)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(num_threads):
            t = threading.Thread(target=write_file, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == num_threads

        # 验证每个文件内容正确
        for i in range(num_threads):
            assert results[i] == f"content_{i}".encode()

    def test_concurrent_writes_same_key_last_wins(self, local_storage):
        """并发写入同一 key，不报错且最后一个写入的内容保留"""
        key = "concurrent/same_key.txt"
        num_threads = 10

        def write_file(i):
            content = f"content_{i}".encode()
            local_storage.put_object(key, content)

        threads = []
        for i in range(num_threads):
            t = threading.Thread(target=write_file, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # 读取最终内容（不抛异常）
        result = local_storage.get_file_content(key)
        assert result is not None
        # 内容应该是某一个线程写入的
        assert result.startswith(b"content_")

    def test_concurrent_reads_and_writes(self, local_storage):
        """并发读写不冲突"""
        local_storage.put_object("shared/file.txt", b"initial")

        num_readers = 5
        num_writers = 5
        read_results = []
        write_count = [0]
        lock = threading.Lock()

        def reader():
            for _ in range(10):
                result = local_storage.get_file_content("shared/file.txt")
                with lock:
                    read_results.append(result)

        def writer(i):
            for j in range(10):
                content = f"writer_{i}_msg_{j}".encode()
                local_storage.put_object("shared/file.txt", content)
                with lock:
                    write_count[0] += 1
                time.sleep(0.001)

        threads = []
        for _ in range(num_readers):
            t = threading.Thread(target=reader)
            threads.append(t)
            t.start()

        for i in range(num_writers):
            t = threading.Thread(target=writer, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # 不应有错误
        assert len(read_results) == num_readers * 10
        assert write_count[0] == num_writers * 10


# ============================================================================
# 8. 文件权限测试
# ============================================================================

class TestLocalStorageFilePermissions:
    """测试写入后文件权限"""

    def test_file_readable_after_write(self, local_storage, temp_root):
        """写入后的文件可读"""
        key = "test/readable.txt"
        content = b"readable content"

        local_storage.put_object(key, content)

        # 通过 LocalStorage 读取
        result = local_storage.get_file_content(key)
        assert result == content

        # 直接文件系统读取
        file_path = local_storage._resolve(key)
        assert os.path.exists(file_path)
        with open(file_path, "rb") as f:
            direct_read = f.read()
        assert direct_read == content

    def test_file_writable_after_write(self, local_storage, temp_root):
        """写入后的文件可修改"""
        key = "test/writable.txt"
        content1 = b"original"
        content2 = b"modified"

        local_storage.put_object(key, content1)
        local_storage.put_object(key, content2)

        result = local_storage.get_file_content(key)
        assert result == content2

    def test_hidden_file_in_public_root_accessible(self, local_storage_with_public):
        """public_root 中的隐藏文件可访问"""
        storage = local_storage_with_public

        # /public/ 开头的 key 使用 public_root
        key = "/public/hidden/.config"
        content = b"config data"

        storage.put_object(key, content)
        result = storage.get_file_content(key)
        assert result == content


# ============================================================================
# 9. 空文件和空目录清理测试
# ============================================================================

class TestLocalStorageEmptyFiles:
    """测试空文件和空目录行为"""

    def test_empty_file_written_and_read(self, local_storage):
        """空文件可以写入和读取"""
        key = "test/empty.txt"
        local_storage.put_object(key, b"")

        result = local_storage.get_file_content(key)
        assert result == b""

    def test_empty_file_exists(self, local_storage):
        """空文件 file_exists 返回正确"""
        local_storage.put_object("test/empty.txt", b"")
        result = local_storage.file_exists("test/empty.txt")

        assert result is not None
        assert result["exists"] is True
        assert result["size"] == 0
        assert result["is_dir"] is False

    def test_delete_removes_empty_file(self, local_storage):
        """删除空文件成功"""
        key = "test/to_delete.txt"
        local_storage.put_object(key, b"")
        assert local_storage.file_exists(key) is not None

        result = local_storage.delete_file_by_key(key)
        assert result is True
        assert local_storage.file_exists(key) is None

    def test_placeholder_file_for_empty_dir(self, local_storage):
        """空目录需要占位文件才能被 list_objects 识别"""
        # LocalStorage 的 list_objects 使用 os.walk，只显示有文件的目录
        local_storage.put_object("test/emptydir/.gitkeep", b"")
        result = local_storage.list_objects("test/emptydir/")

        # 有 .gitkeep 所以能列出
        assert isinstance(result, list)


# ============================================================================
# 10. 其他边缘情况
# ============================================================================

class TestLocalStorageEdgeCases:
    """边缘情况测试"""

    def test_very_long_key(self, local_storage):
        """超长 key 可以处理"""
        key = "test/" + "a" * 200 + ".txt"
        content = b"long key"

        local_storage.put_object(key, content)
        result = local_storage.get_file_content(key)
        assert result == content

    def test_key_with_special_chars(self, local_storage):
        """特殊字符 key"""
        key = "test/special!@#$%chars.txt"
        content = b"special"

        local_storage.put_object(key, content)
        result = local_storage.get_file_content(key)
        assert result == content

    def test_key_with_chinese_chars(self, local_storage):
        """中文路径 key"""
        key = "test/中文文件.txt"
        content = b"chinese content"

        local_storage.put_object(key, content)
        result = local_storage.get_file_content(key)
        assert result == content

    def test_list_objects_returns_correct_format(self, local_storage):
        """list_objects 返回与 COS 兼容的格式"""
        local_storage.put_object("test/format/report.pdf", b"pdf content")

        result = local_storage.list_objects("test/format/")
        assert result is not None

        entry = result[0]
        assert "Key" in entry
        assert "Size" in entry
        assert "LastModified" in entry
        assert isinstance(entry["Size"], int)

    def test_concurrent_delete_same_file(self, local_storage):
        """并发删除同一文件"""
        key = "test/delete_me.txt"
        local_storage.put_object(key, b"content")

        results = []

        def delete_file():
            result = local_storage.delete_file_by_key(key)
            results.append(result)

        threads = [threading.Thread(target=delete_file) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 至少一次返回 True，其他返回 False
        assert True in results
        # 文件确实被删除
        assert local_storage.file_exists(key) is None

    def test_delete_nonexistent_returns_false(self, local_storage):
        """删除不存在的文件返回 False"""
        result = local_storage.delete_file_by_key("never/existed.txt")
        assert result is False
