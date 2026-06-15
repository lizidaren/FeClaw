"""
VFS 图片去重 API 路由测试

测试覆盖：
- 统计 API（基本统计、详细统计）
- 清理 API（dry_run、实际删除）
- 清单管理 API（列表、删除记录）
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import json

from fastapi.testclient import TestClient

# 导入主应用和服务
from main import app
from services.vfs_image_dedup import VFSImageDeduplicationService
from routers.vfs_image_dedup import get_image_dedup_service


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


class TestVFSImageDedupAPI(unittest.TestCase):
    """VFS 图片去重 API 测试"""
    
    def setUp(self):
        """测试前准备"""
        self.mock_storage = MockStorageService()
        self.dedup_service = VFSImageDeduplicationService(
            user_id="1",
            storage_service=self.mock_storage
        )
        
        # 使用 FastAPI 的 dependency_overrides 覆盖依赖
        # 直接返回已经设置好的 dedup_service
        app.dependency_overrides[get_image_dedup_service] = lambda: self.dedup_service
        
        self.client = TestClient(app)
    
    def tearDown(self):
        """测试后清理"""
        # 清除依赖覆盖
        app.dependency_overrides.clear()
    
    def test_get_stats_empty(self):
        """测试获取统计：空清单"""
        response = self.client.get("/api/vfs-images/stats")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["total_images"], 0)
        self.assertEqual(data["total_size"], 0)
        self.assertIn("manifest_key", data)
    
    def test_get_stats_with_images(self):
        """测试获取统计：有图片"""
        # 创建 dedup 服务并注册图片
        dedup = VFSImageDeduplicationService(user_id="1", storage_service=self.mock_storage)
        data1 = b"x" * 100
        data2 = b"y" * 200
        dedup.register_image("/img1.png", data1)
        dedup.register_image("/img2.jpg", data2)
        
        response = self.client.get("/api/vfs-images/stats")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["total_images"], 2)
        self.assertEqual(data["total_size"], 300)
    
    def test_get_detailed_stats_empty(self):
        """测试获取详细统计：空清单"""
        response = self.client.get("/api/vfs-images/stats/detailed")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["total_images"], 0)
        self.assertEqual(data["total_size"], 0)
        self.assertEqual(data["avg_size"], 0)
        self.assertEqual(data["type_distribution"], {})
        self.assertIsNone(data["latest_upload"])
        self.assertEqual(data["size_range"]["min"], 0)
        self.assertEqual(data["size_range"]["max"], 0)
    
    def test_get_detailed_stats_with_images(self):
        """测试获取详细统计：有图片"""
        # 创建 dedup 服务并注册不同类型的图片
        dedup = VFSImageDeduplicationService(user_id="1", storage_service=self.mock_storage)
        data_png = b"png_data_123"
        data_jpg = b"jpg_data_456"
        data_gif = b"gif_data_789"
        
        dedup.register_image("/images/photo.png", data_png)
        dedup.register_image("/images/pic.jpg", data_jpg)
        dedup.register_image("/images/anim.gif", data_gif)
        
        response = self.client.get("/api/vfs-images/stats/detailed")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # 验证基本统计
        self.assertEqual(data["total_images"], 3)
        
        # 验证类型分布
        self.assertEqual(data["type_distribution"]["png"], 1)
        self.assertEqual(data["type_distribution"]["jpg"], 1)
        self.assertEqual(data["type_distribution"]["gif"], 1)
        
        # 验证大小范围
        self.assertGreater(data["size_range"]["max"], 0)
        
        # 验证最近上传时间
        self.assertIsNotNone(data["latest_upload"])
    
    def test_cleanup_dry_run(self):
        """测试清理 API：dry_run 模式"""
        response = self.client.post(
            "/api/vfs-images/cleanup",
            json={"dry_run": True}
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["dry_run"], True)
        self.assertIn("removed_count", data)
        self.assertIn("remaining_count", data)
        self.assertIn("removed_records", data)
    
    def test_cleanup_actual_delete(self):
        """测试清理 API：实际删除"""
        # 创建 dedup 服务并注册图片（但不保存文件本身到 COS）
        dedup = VFSImageDeduplicationService(user_id="1", storage_service=self.mock_storage)
        data = b"test_data_for_cleanup"
        dedup.register_image("/workspace/images/cleanup_test.png", data)
        
        # 执行清理（因为图片文件本身不在 COS，应该会被清理）
        response = self.client.post(
            "/api/vfs-images/cleanup",
            json={"dry_run": False}
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["dry_run"], False)
        # 应该至少清理了一条记录（因为文件不在 COS）
        self.assertGreaterEqual(data["removed_count"], 1)
    
    def test_get_manifest_list_empty(self):
        """测试获取清单列表：空清单"""
        response = self.client.get("/api/vfs-images/list")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["total_images"], 0)
        self.assertEqual(len(data["images"]), 0)
    
    def test_get_manifest_list_with_images(self):
        """测试获取清单列表：有图片"""
        # 创建 dedup 服务并注册图片
        dedup = VFSImageDeduplicationService(user_id="1", storage_service=self.mock_storage)
        data1 = b"image_data_1"
        data2 = b"image_data_2"
        
        dedup.register_image("/images/list1.png", data1, "list1.png")
        dedup.register_image("/images/list2.jpg", data2, "list2.jpg")
        
        response = self.client.get("/api/vfs-images/list")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["total_images"], 2)
        self.assertEqual(len(data["images"]), 2)
        
        # 验证图片项格式
        for img in data["images"]:
            self.assertIn("vfs_path", img)
            self.assertIn("size", img)
            self.assertIn("sha256_hash", img)
            self.assertIn("uploaded_at", img)
    
    def test_get_manifest_list_pagination(self):
        """测试获取清单列表：分页"""
        # 创建 dedup 服务并注册多个图片
        dedup = VFSImageDeduplicationService(user_id="1", storage_service=self.mock_storage)
        
        for i in range(5):
            data = f"image_data_{i}".encode()
            dedup.register_image(f"/images/img{i}.png", data)
        
        # 测试 limit
        response = self.client.get("/api/vfs-images/list?limit=2")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_images"], 5)
        self.assertEqual(len(data["images"]), 2)
        
        # 测试 offset
        response = self.client.get("/api/vfs-images/list?limit=2&offset=2")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_images"], 5)
        self.assertEqual(len(data["images"]), 2)
    
    def test_delete_image_record_success(self):
        """测试删除记录：成功"""
        # 创建 dedup 服务并注册图片
        dedup = VFSImageDeduplicationService(user_id="1", storage_service=self.mock_storage)
        data = b"delete_test_data"
        vfs_path = "/workspace/images/delete_test.png"
        dedup.register_image(vfs_path, data)
        
        # 删除记录
        response = self.client.delete(
            "/api/vfs-images/record",
            params={"vfs_path": vfs_path}
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["success"], True)
        self.assertIn("已移除记录", data["message"])
        
        # 验证记录已删除
        stats = self.client.get("/api/vfs-images/stats").json()
        self.assertEqual(stats["total_images"], 0)
    
    def test_delete_image_record_not_found(self):
        """测试删除记录：不存在"""
        response = self.client.delete(
            "/api/vfs-images/record",
            params={"vfs_path": "/nonexistent/path.png"}
        )
        
        self.assertEqual(response.status_code, 404)
        data = response.json()
        self.assertIn("detail", data)
        self.assertIn("未找到记录", data["detail"])


if __name__ == "__main__":
    unittest.main()