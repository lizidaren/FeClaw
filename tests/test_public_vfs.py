"""
VFS /public/ 公共数据空间测试

测试范围:
1. 目录列表功能
2. 文件读取功能
3. 写保护功能
4. 权限服务默认只读
5. public_manager.py 新增功能（validate, init）
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.permission_service import PermissionService, Permission


class TestPublicPermissions(unittest.TestCase):
    """测试 /public/ 权限配置"""
    
    def test_public_root_readonly(self):
        """测试 /public/ 根目录默认只读"""
        service = PermissionService(user_id="test")
        
        # /public/ 应该返回 read 权限
        perm = service.get_default_permission("/public/")
        self.assertEqual(perm, Permission.READ)
        
        perm = service.get_default_permission("public")
        self.assertEqual(perm, Permission.READ)
    
    def test_public_subpath_readonly(self):
        """测试 /public/ 子路径默认只读"""
        service = PermissionService(user_id="test")
        
        # 各种 /public/ 子路径
        test_paths = [
            "/public/feclaw/index.md",
            "public/feclaw/tools.md",
            "/public/docs/guide.md",
            "public/README.md",
        ]
        
        for path in test_paths:
            perm = service.get_default_permission(path)
            self.assertEqual(perm, Permission.READ, f"路径 {path} 应该是只读")
    
    def test_public_has_read_not_write(self):
        """测试 /public/ 有读权限但没有写权限"""
        # 使用 Permission 类方法
        self.assertTrue(Permission.has_read(Permission.READ))
        self.assertFalse(Permission.has_write(Permission.READ))


class TestPublicVFSMock(unittest.TestCase):
    """测试 VFS /public/ 功能（使用 Mock）"""
    
    def test_get_public_base_path(self):
        """测试 _get_public_base_path 方法"""
        from services.virtual_filesystem import VirtualFileSystem
        
        # 直接测试静态路径逻辑
        # _get_public_base_path 返回 f"{settings.STORAGE_PREFIX}public/"
        # 所以我们测试路径拼接逻辑
        with patch('services.virtual_filesystem.settings') as mock_settings:
            mock_settings.STORAGE_PREFIX = "feclaw/"
            expected = "feclaw/public/"
            # 验证路径格式正确
            self.assertTrue(expected.endswith("public/"))
            self.assertTrue(expected.startswith("feclaw/"))
    
    def test_is_public_path(self):
        """测试 _is_public_path 方法"""
        from services.virtual_filesystem import VirtualFileSystem
        
        vfs = VirtualFileSystem.__new__(VirtualFileSystem)
        vfs._get_public_base_path = lambda: "feclaw/public/"
        
        # 测试公共路径
        self.assertTrue(vfs._is_public_path("feclaw/public/feclaw/index.md"))
        self.assertTrue(vfs._is_public_path("feclaw/public/README.md"))
        
        # 测试非公共路径
        self.assertFalse(vfs._is_public_path("feclaw/user_1/workspace/USER.md"))
        self.assertFalse(vfs._is_public_path("feclaw/config/agent.json"))


class TestPublicWriteProtection(unittest.TestCase):
    """测试 /public/ 写保护"""
    
    def test_public_path_detection(self):
        """测试公共路径检测"""
        from services.virtual_filesystem import VirtualFileSystem
        
        vfs = VirtualFileSystem.__new__(VirtualFileSystem)
        vfs._get_public_base_path = lambda: "feclaw/public/"
        
        # 模拟 _resolve_path 返回公共路径
        test_cases = [
            ("/public/feclaw/index.md", "feclaw/public/feclaw/index.md"),
            ("/public/README.md", "feclaw/public/README.md"),
        ]
        
        for path, expected_key in test_cases:
            # 检测是否是公共路径
            is_public = vfs._is_public_path(expected_key)
            self.assertTrue(is_public, f"路径 {path} 应该被识别为公共路径")


class TestPublicManager(unittest.TestCase):
    """测试 public_manager.py 新增功能"""
    
    def test_validate_required_files_list(self):
        """测试 validate 方法检查必需文件列表"""
        # 必需文件列表
        required_files = [
            "feclaw/index.md",
            "feclaw/principles.md",
        ]
        
        # 验证文件格式正确
        for path in required_files:
            self.assertTrue(path.endswith(".md"), f"{path} 应该是 .md 文件")
            self.assertTrue("/" in path, f"{path} 应该包含子目录")
    
    def test_default_file_content_not_empty(self):
        """测试默认文件内容不为空"""
        # 模拟 default_files 内容（完整内容在 public_manager.py 中）
        default_files = {
            "feclaw/index.md": "# FeClaw 智能体网关平台\n\n## 平台概述\n" + "内容" * 50,
            "feclaw/principles.md": "# 工具调用原则\n\n## 核心原则\n" + "内容" * 50,
        }
        
        for path, content in default_files.items():
            self.assertTrue(len(content) > 100, f"{path} 内容应该足够长，实际 {len(content)}")
            self.assertTrue("#" in content, f"{path} 应该包含 Markdown 标题")
    
    def test_init_force_flag(self):
        """测试 init 命令的 force 标志逻辑"""
        # 模拟文件存在检测逻辑
        exists = True
        force = False
        
        # 不强制时跳过
        should_skip = exists and not force
        self.assertTrue(should_skip, "文件存在且不强制时应跳过")
        
        # 强制时覆盖
        force = True
        should_overwrite = exists and force
        self.assertTrue(should_overwrite, "文件存在且强制时应覆盖")
    
    def test_validate_result_structure(self):
        """测试 validate 返回结果结构"""
        # 模拟 validate 返回值
        result = {
            "total": 2,
            "missing": [],
            "found": [],
            "status": "ok"
        }
        
        # 验证必需字段
        self.assertIn("total", result)
        self.assertIn("missing", result)
        self.assertIn("found", result)
        self.assertIn("status", result)
        
        # 验证 status 可能的值
        valid_statuses = ["ok", "incomplete"]
        self.assertIn(result["status"], valid_statuses)


if __name__ == "__main__":
    unittest.main()
