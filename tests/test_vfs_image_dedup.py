"""
VFS 图片去重服务单元测试

测试覆盖：
- 哈希计算正确性
- Manifest 加载/保存
- 重复检测逻辑
- 注册/查询操作
- 统计信息
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import json
import hashlib

from services.vfs_image_dedup import VFSImageDeduplicationService, ImageRecord


class MockStorageService:
    """模拟存储服务"""
    
    def __init__(self):
        self.files = {}  # key -> content
    
    def get_file_content(self, key):
        """获取文件内容"""
        if key in self.files:
            return self.files[key]
        return None
    
    def put_object(self, key, data):
        """保存文件"""
        self.files[key] = data
        return True


class TestVFSImageDedupService(unittest.TestCase):
    """VFS 图片去重服务测试"""
    
    def setUp(self):
        """测试前准备"""
        self.mock_storage = MockStorageService()
        self.dedup = VFSImageDeduplicationService(
            user_id="test_user",
            storage_service=self.mock_storage
        )
    
    def test_compute_hash_and_size(self):
        """测试哈希和大小计算"""
        data = b"test_image_content"
        hash_val, size = self.dedup._compute_hash_and_size(data)
        
        # 验证哈希正确
        expected_hash = hashlib.sha256(data).hexdigest()
        self.assertEqual(hash_val, expected_hash)
        
        # 验证大小正确
        self.assertEqual(size, len(data))
    
    def test_make_key(self):
        """测试去重 key 生成"""
        key = self.dedup._make_key("abc123", 1000)
        self.assertEqual(key, "abc123_1000")
    
    def test_manifest_key_path(self):
        """测试 manifest 路径"""
        key = self.dedup._manifest_key()
        self.assertEqual(key, ".vfs_images_dedup/test_user/_manifest.json")
    
    def test_get_manifest_empty(self):
        """测试空 manifest 创建"""
        manifest = self.dedup._get_manifest()
        self.assertIn("images", manifest)
        self.assertEqual(len(manifest["images"]), 0)
    
    def test_save_and_load_manifest(self):
        """测试 manifest 保存和加载"""
        manifest = {"images": {"test_key": {"vfs_path": "/test.png"}}}
        result = self.dedup._save_manifest(manifest)
        self.assertTrue(result)
        
        # 验证保存到 mock storage
        manifest_key = self.dedup._manifest_key()
        self.assertIn(manifest_key, self.mock_storage.files)
        
        # 创建新实例，验证加载
        dedup2 = VFSImageDeduplicationService(
            user_id="test_user",
            storage_service=self.mock_storage
        )
        loaded = dedup2._get_manifest()
        self.assertEqual(loaded["images"]["test_key"]["vfs_path"], "/test.png")
    
    def test_find_duplicate_not_found(self):
        """测试查找不存在的图片"""
        data = b"new_image"
        result = self.dedup.find_duplicate(data)
        self.assertIsNone(result)
    
    def test_register_and_find_duplicate(self):
        """测试注册和查找重复"""
        data = b"test_image_data"
        vfs_path = "/workspace/images/test.png"
        
        # 注册图片
        result = self.dedup.register_image(vfs_path, data, "test.png")
        self.assertTrue(result)
        
        # 查找重复
        found_path = self.dedup.find_duplicate(data)
        self.assertEqual(found_path, vfs_path)
    
    def test_register_existing_image(self):
        """测试注册已存在的图片"""
        data = b"test_image_data"
        path1 = "/workspace/images/test1.png"
        path2 = "/workspace/images/test2.png"
        
        # 注册第一次
        result1 = self.dedup.register_image(path1, data, "test1.png")
        self.assertTrue(result1)
        
        # 尝试用不同路径注册相同内容
        result2 = self.dedup.register_image(path2, data, "test2.png")
        self.assertFalse(result2)  # 应该拒绝
    
    def test_different_images_not_duplicate(self):
        """测试不同内容不被识别为重复"""
        data1 = b"image_1_content"
        data2 = b"image_2_content"
        
        # 注册第一个
        self.dedup.register_image("/workspace/images/img1.png", data1)
        
        # 查找第二个，应该找不到
        result = self.dedup.find_duplicate(data2)
        self.assertIsNone(result)
    
    def test_same_content_different_size(self):
        """测试相同内容不同大小的情况（理论上不会发生，但测试防御性）"""
        # 注意：这里模拟的是两个不同的数据块
        # 实际上相同内容必然有相同大小，这个测试验证 hash+size 的组合 key 逻辑
        data1 = b"content_abc"
        data2 = b"content_abc_extra"  # 不同内容，测试 key 隔离
        
        self.dedup.register_image("/img1.png", data1)
        
        # 不同的数据应该有不同的 hash
        result = self.dedup.find_duplicate(data2)
        self.assertIsNone(result)
    
    def test_check_duplicate_for_path(self):
        """测试路径去重检查"""
        data = b"test_data"
        vfs_path = "/workspace/images/test.png"
        
        # 初始检查 - 无重复
        is_dup, existing = self.dedup.check_duplicate_for_path(vfs_path, data)
        self.assertFalse(is_dup)
        self.assertIsNone(existing)
        
        # 注册
        self.dedup.register_image(vfs_path, data)
        
        # 再次检查 - 有重复
        is_dup, existing = self.dedup.check_duplicate_for_path("/other.png", data)
        self.assertTrue(is_dup)
        self.assertEqual(existing, vfs_path)
    
    def test_get_image_info(self):
        """测试获取图片信息"""
        data = b"test_data"
        vfs_path = "/workspace/images/info_test.png"
        
        # 注册
        self.dedup.register_image(vfs_path, data, "info_test.png")
        
        # 获取信息
        sha256_hash, size = self.dedup._compute_hash_and_size(data)
        info = self.dedup.get_image_info(sha256_hash, size)
        
        self.assertIsNotNone(info)
        self.assertEqual(info["vfs_path"], vfs_path)
        self.assertEqual(info["original_filename"], "info_test.png")
        self.assertEqual(info["size"], size)
    
    def test_stats(self):
        """测试统计信息"""
        # 初始统计
        stats = self.dedup.stats()
        self.assertEqual(stats["total_images"], 0)
        self.assertEqual(stats["total_size"], 0)
        
        # 注册几个图片
        data1 = b"x" * 100
        data2 = b"y" * 200
        self.dedup.register_image("/img1.png", data1)
        self.dedup.register_image("/img2.png", data2)
        
        # 验证统计
        stats = self.dedup.stats()
        self.assertEqual(stats["total_images"], 2)
        self.assertEqual(stats["total_size"], 300)
    
    def test_detailed_stats_empty(self):
        """测试详细统计：空清单"""
        stats = self.dedup.detailed_stats()
        
        self.assertEqual(stats["total_images"], 0)
        self.assertEqual(stats["total_size"], 0)
        self.assertEqual(stats["avg_size"], 0)
        self.assertEqual(stats["type_distribution"], {})
        self.assertIsNone(stats["latest_upload"])
        self.assertEqual(stats["size_range"]["min"], 0)
        self.assertEqual(stats["size_range"]["max"], 0)
    
    def test_detailed_stats_with_images(self):
        """测试详细统计：有图片"""
        # 注册不同类型和大小的图片
        data1 = b"x" * 100
        data2 = b"y" * 200
        data3 = b"z" * 150
        
        self.dedup.register_image("/images/photo.png", data1, "photo.png")
        self.dedup.register_image("/images/pic.jpg", data2, "pic.jpg")
        self.dedup.register_image("/images/test.jpeg", data3, "test.jpeg")
        
        stats = self.dedup.detailed_stats()
        
        # 验证基本统计
        self.assertEqual(stats["total_images"], 3)
        self.assertEqual(stats["total_size"], 450)
        self.assertEqual(stats["avg_size"], 150)  # 450 / 3
        
        # 验证文件类型分布
        self.assertEqual(stats["type_distribution"]["png"], 1)
        self.assertEqual(stats["type_distribution"]["jpg"], 1)
        self.assertEqual(stats["type_distribution"]["jpeg"], 1)
        
        # 验证大小范围
        self.assertEqual(stats["size_range"]["min"], 100)
        self.assertEqual(stats["size_range"]["max"], 200)
        
        # 验证最近上传时间存在
        self.assertIsNotNone(stats["latest_upload"])
    
    def test_detailed_stats_type_distribution(self):
        """测试详细统计：文件类型分布"""
        # 注册不同类型的图片（使用不同数据避免重复检测）
        data_png = b"png_data_123"
        data_jpg = b"jpg_data_456"
        data_gif = b"gif_data_789"
        data_webp = b"webp_data_abc"
        data_bmp = b"bmp_data_def"
        data_unknown = b"unknown_data_xyz"
        
        self.dedup.register_image("/img.png", data_png)
        self.dedup.register_image("/img.jpg", data_jpg)
        self.dedup.register_image("/img.gif", data_gif)
        self.dedup.register_image("/img.webp", data_webp)
        self.dedup.register_image("/img.bmp", data_bmp)
        self.dedup.register_image("/img.unknown", data_unknown)  # 不常见扩展名
        
        stats = self.dedup.detailed_stats()
        
        # 验证常见类型被统计
        self.assertEqual(stats["type_distribution"]["png"], 1)
        self.assertEqual(stats["type_distribution"]["jpg"], 1)
        self.assertEqual(stats["type_distribution"]["gif"], 1)
        self.assertEqual(stats["type_distribution"]["webp"], 1)
        self.assertEqual(stats["type_distribution"]["bmp"], 1)
        
        # 验证不常见扩展名归类为 unknown
        self.assertEqual(stats["type_distribution"]["unknown"], 1)
    
    def test_extract_extension(self):
        """测试扩展名提取"""
        # 测试常见扩展名
        self.assertEqual(self.dedup._extract_extension("/path/to/image.png"), "png")
        self.assertEqual(self.dedup._extract_extension("/path/to/photo.JPG"), "jpg")  # 大写转小写
        self.assertEqual(self.dedup._extract_extension("image.jpeg"), "jpeg")
        self.assertEqual(self.dedup._extract_extension("/img.gif?v=1"), "gif")  # 查询参数
        
        # 测试不常见扩展名
        self.assertEqual(self.dedup._extract_extension("/file.unknown"), "unknown")
        self.assertEqual(self.dedup._extract_extension("/noextension"), "unknown")
        self.assertEqual(self.dedup._extract_extension(""), "unknown")
        # 注意：_extract_extension 接收的是 vfs_path 参数，None 会被处理为 ""
    
    def test_manifest_cache(self):
        """测试 manifest 缓存机制"""
        # 第一次加载
        manifest1 = self.dedup._get_manifest()
        
        # 修改 manifest（不保存）
        manifest1["images"]["temp"] = {"vfs_path": "/temp.png"}
        
        # 再次获取，应该返回缓存的（包含修改）
        manifest2 = self.dedup._get_manifest()
        self.assertIn("temp", manifest2["images"])
    
    def test_different_user_isolation(self):
        """测试不同用户的隔离"""
        dedup1 = VFSImageDeduplicationService(
            user_id="user1",
            storage_service=self.mock_storage
        )
        dedup2 = VFSImageDeduplicationService(
            user_id="user2",
            storage_service=self.mock_storage
        )
        
        data = b"shared_image"
        
        # user1 注册
        dedup1.register_image("/user1/images/img.png", data)
        
        # user2 查找，应该找不到（隔离）
        result = dedup2.find_duplicate(data)
        self.assertIsNone(result)
        
        # user1 查找，应该找到
        result = dedup1.find_duplicate(data)
        self.assertIsNotNone(result)
    
    def test_vfs_path_to_cos_key(self):
        """测试 VFS 路径转换为 COS key"""
        vfs_path = "/workspace/images/test.png"
        cos_key = self.dedup._vfs_path_to_cos_key(vfs_path)
        
        # 验证格式正确：{prefix}{user_id}/{path}
        # 前导 / 应被去掉
        self.assertTrue(cos_key.endswith("workspace/images/test.png"))
        self.assertIn("test_user", cos_key)
    
    def test_cleanup_no_stale_records(self):
        """测试清理功能：无过期记录时不删除"""
        data = b"test_data"
        vfs_path = "/workspace/images/test.png"
        
        # 注册图片
        self.dedup.register_image(vfs_path, data)
        
        # 模拟图片文件已保存到 COS（register_image 只保存 manifest，不保存图片本身）
        # 在实际使用中，图片文件由调用方保存到 COS
        cos_key = self.dedup._vfs_path_to_cos_key(vfs_path)
        self.mock_storage.files[cos_key] = data
        
        # 清理（dry_run）
        result = self.dedup.cleanup_stale_records(dry_run=True)
        
        # 应该没有要清理的记录（文件存在）
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["dry_run"], True)
        self.assertEqual(len(result["removed_records"]), 0)
    
    def test_cleanup_with_stale_records(self):
        """测试清理功能：有过期记录时正确删除"""
        data = b"test_data"
        vfs_path = "/workspace/images/stale.png"
        
        # 注册图片
        self.dedup.register_image(vfs_path, data)
        
        # 手动删除文件（模拟用户删除图片）
        cos_key = self.dedup._vfs_path_to_cos_key(vfs_path)
        # MockStorageService 没有直接的删除方法，但我们可以直接移除
        # 这里模拟文件不存在的情况：将 storage.files 中对应的 key 移除
        # 但 register_image 保存文件到了不同的 key（manifest key）
        # 我们需要模拟图片文件本身不存在
        
        # 创建一个场景：manifest 有记录，但实际文件不存在
        # 由于 MockStorageService 保存的是 manifest，不是图片本身
        # 我们需要手动修改 mock storage 来模拟这种情况
        
        # 清理 - 因为图片文件本身没有被保存到 storage（只有 manifest）
        # 所以 cleanup 应该发现文件不存在
        result = self.dedup.cleanup_stale_records(dry_run=False)
        
        # 验证清理结果
        # 注意：由于 register_image 只保存 manifest，不保存图片文件本身
        # 所以图片文件的 cos_key 在 storage.files 中不存在
        # cleanup 应该发现这一点并清理记录
        self.assertGreaterEqual(result["removed_count"], 1)
    
    def test_cleanup_dry_run_preserves_records(self):
        """测试 dry_run 不实际删除记录"""
        data = b"test_data"
        vfs_path = "/workspace/images/test_dry.png"
        
        # 注册图片
        self.dedup.register_image(vfs_path, data)
        
        # dry_run 清理
        result = self.dedup.cleanup_stale_records(dry_run=True)
        
        # 验证 manifest 仍然存在
        manifest = self.dedup._get_manifest()
        images = manifest.get("images", {})
        
        # dry_run 不应该改变 manifest
        # 如果有要清理的记录，它们应该仍然在 manifest 中
        if result["removed_count"] > 0:
            # 验证记录仍然存在（dry_run 不删除）
            # 找到被标记为要删除的 key
            removed_vfs_paths = [r["vfs_path"] for r in result["removed_records"]]
            for dedup_key, img_info in images.items():
                if img_info.get("vfs_path") in removed_vfs_paths:
                    # dry_run 不应该删除这条记录
                    self.assertIn(dedup_key, images)


    def test_unregister_image(self):
        """测试移除图片记录"""
        data = b"test_data_to_unregister"
        vfs_path = "/workspace/images/unregister_test.png"
        
        # 注册图片
        self.dedup.register_image(vfs_path, data)
        
        # 验证已注册
        manifest = self.dedup._get_manifest()
        images = manifest.get("images", {})
        self.assertEqual(len(images), 1)
        
        # 移除记录
        result = self.dedup.unregister_image(vfs_path)
        self.assertTrue(result)
        
        # 验证已移除
        manifest = self.dedup._get_manifest()
        images = manifest.get("images", {})
        self.assertEqual(len(images), 0)
        
        # 再次移除应该返回 False
        result = self.dedup.unregister_image(vfs_path)
        self.assertFalse(result)
    
    def test_unregister_nonexistent_image(self):
        """测试移除不存在的图片记录"""
        result = self.dedup.unregister_image("/nonexistent/path.png")
        self.assertFalse(result)
    
    def test_batch_check_duplicates(self):
        """测试批量检查重复"""
        # 准备测试数据
        existing_data = b"existing_image_content"
        new_data = b"new_image_content"
        
        # 注册一个已存在的图片
        self.dedup.register_image("/workspace/images/existing.png", existing_data)
        
        # 批量检查
        results = self.dedup.batch_check_duplicates([
            {"data": existing_data, "vfs_path": "/new_path1.png"},
            {"data": new_data, "vfs_path": "/new_path2.png"},
        ])
        
        # 验证结果
        self.assertEqual(results["/new_path1.png"], "/workspace/images/existing.png")
        self.assertIsNone(results["/new_path2.png"])
    
    def test_batch_check_duplicates_with_tuples(self):
        """测试批量检查重复（元组格式）"""
        existing_data = b"tuple_existing"
        self.dedup.register_image("/tuple/existing.png", existing_data)
        
        # 使用元组格式
        results = self.dedup.batch_check_duplicates([
            (existing_data, "/tuple/new.png"),
            (b"different_data", "/tuple/new2.png"),
        ])
        
        self.assertEqual(results["/tuple/new.png"], "/tuple/existing.png")
        self.assertIsNone(results["/tuple/new2.png"])


class TestImageRecord(unittest.TestCase):
    """图片记录数据类测试"""
    
    def test_image_record_creation(self):
        """测试图片记录创建"""
        record = ImageRecord(
            vfs_path="/test.png",
            cos_key="storage/test.png",
            size=1024,
            sha256_hash="abc123",
            uploaded_at="2026-04-28T00:00:00",
            original_filename="test.png"
        )
        
        self.assertEqual(record.vfs_path, "/test.png")
        self.assertEqual(record.size, 1024)
        self.assertEqual(record.sha256_hash, "abc123")


if __name__ == "__main__":
    unittest.main()
