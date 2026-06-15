"""
静态网站服务测试

测试 StaticSiteService 的核心功能：
- 子域名验证
- COS key 生成
- 公开 URL 生成
- 日期解析
- 数据类创建
"""

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# 添加项目根目录到 Python 路径
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.static_site_service import (
    StaticSiteService,
    StaticSite,
    SiteFile
)


class TestSubdomainValidation(unittest.TestCase):
    """测试子域名验证"""

    def test_valid_simple(self):
        """有效的简单子域名"""
        service = StaticSiteService(MagicMock())
        self.assertTrue(service._validate_subdomain("lizidaren"))
        self.assertTrue(service._validate_subdomain("test"))
        self.assertTrue(service._validate_subdomain("my-site"))

    def test_valid_with_numbers(self):
        """有效的数字子域名"""
        service = StaticSiteService(MagicMock())
        self.assertTrue(service._validate_subdomain("site123"))
        self.assertTrue(service._validate_subdomain("123site"))
        self.assertTrue(service._validate_subdomain("my-site-2024"))

    def test_invalid_empty(self):
        """无效：空字符串"""
        service = StaticSiteService(MagicMock())
        self.assertFalse(service._validate_subdomain(""))

    def test_invalid_too_long(self):
        """无效：超过 63 字符"""
        service = StaticSiteService(MagicMock())
        long_name = "a" * 64
        self.assertFalse(service._validate_subdomain(long_name))

    def test_invalid_start_with_dash(self):
        """无效：以 - 开头"""
        service = StaticSiteService(MagicMock())
        self.assertFalse(service._validate_subdomain("-site"))
        self.assertFalse(service._validate_subdomain("-my-site-"))

    def test_invalid_end_with_dash(self):
        """无效：以 - 结尾"""
        service = StaticSiteService(MagicMock())
        self.assertFalse(service._validate_subdomain("site-"))
        self.assertFalse(service._validate_subdomain("my-site-"))

    def test_invalid_uppercase(self):
        """无效：包含大写字母"""
        service = StaticSiteService(MagicMock())
        self.assertFalse(service._validate_subdomain("MySite"))
        self.assertFalse(service._validate_subdomain("SITE"))

    def test_invalid_special_chars(self):
        """无效：包含特殊字符"""
        service = StaticSiteService(MagicMock())
        self.assertFalse(service._validate_subdomain("my_site"))
        self.assertFalse(service._validate_subdomain("my.site"))
        self.assertFalse(service._validate_subdomain("my@site"))


class TestCosKeyGeneration(unittest.TestCase):
    """测试 COS key 生成"""

    def test_root_key(self):
        """根目录 key"""
        service = StaticSiteService(MagicMock())
        site = StaticSite(
            id=1,
            user_id=123,
            subdomain="test",
            root_path="/workspace/123/public_html/",
            status="active"
        )
        key = service.get_cos_key(site)
        self.assertEqual(key, "firstentrance/static-sites/123/")

    def test_file_key(self):
        """文件 key"""
        service = StaticSiteService(MagicMock())
        site = StaticSite(
            id=1,
            user_id=123,
            subdomain="test",
            root_path="/workspace/123/public_html/",
            status="active"
        )
        key = service.get_cos_key(site, "index.html")
        self.assertEqual(key, "firstentrance/static-sites/123/index.html")

    def test_nested_file_key(self):
        """嵌套文件 key"""
        service = StaticSiteService(MagicMock())
        site = StaticSite(
            id=1,
            user_id=123,
            subdomain="test",
            root_path="/workspace/123/public_html/",
            status="active"
        )
        key = service.get_cos_key(site, "css/style.css")
        self.assertEqual(key, "firstentrance/static-sites/123/css/style.css")


class TestPublicUrlGeneration(unittest.TestCase):
    """测试公开 URL 生成"""

    def test_root_url(self):
        """根目录 URL"""
        service = StaticSiteService(MagicMock())
        site = StaticSite(
            id=1,
            user_id=123,
            subdomain="lizidaren",
            root_path="/workspace/123/public_html/",
            status="active"
        )
        url = service.get_public_url(site)
        self.assertEqual(url, "https://lizidaren.site.firstentrance.net/")

    def test_file_url(self):
        """文件 URL"""
        service = StaticSiteService(MagicMock())
        site = StaticSite(
            id=1,
            user_id=123,
            subdomain="lizidaren",
            root_path="/workspace/123/public_html/",
            status="active"
        )
        url = service.get_public_url(site, "index.html")
        self.assertEqual(url, "https://lizidaren.site.firstentrance.net/index.html")

    def test_nested_file_url(self):
        """嵌套文件 URL"""
        service = StaticSiteService(MagicMock())
        site = StaticSite(
            id=1,
            user_id=123,
            subdomain="lizidaren",
            root_path="/workspace/123/public_html/",
            status="active"
        )
        url = service.get_public_url(site, "images/logo.png")
        self.assertEqual(url, "https://lizidaren.site.firstentrance.net/images/logo.png")


class TestDateParsing(unittest.TestCase):
    """测试日期解析"""

    def test_parse_with_milliseconds(self):
        """带毫秒的 ISO 时间"""
        result = StaticSiteService._parse_cos_date("2026-04-26T14:30:00.123Z")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 4)
        self.assertEqual(result.day, 26)
        self.assertEqual(result.hour, 14)
        self.assertEqual(result.minute, 30)

    def test_parse_without_milliseconds(self):
        """不带毫秒的 ISO 时间"""
        result = StaticSiteService._parse_cos_date("2026-04-26T14:30:00Z")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 4)

    def test_parse_empty(self):
        """空字符串"""
        result = StaticSiteService._parse_cos_date("")
        self.assertIsNone(result)

    def test_parse_invalid(self):
        """无效格式"""
        result = StaticSiteService._parse_cos_date("not-a-date")
        self.assertIsNone(result)


class TestSiteDataclass(unittest.TestCase):
    """测试 StaticSite 数据类"""

    def test_create_site(self):
        """创建站点数据"""
        site = StaticSite(
            id=1,
            user_id=123,
            subdomain="test",
            root_path="/workspace/123/public_html/",
            status="active",
            custom_cname="custom.example.com",
            created_at=datetime(2026, 4, 26, 14, 30, 0),
            updated_at=datetime(2026, 4, 26, 15, 0, 0)
        )
        self.assertEqual(site.id, 1)
        self.assertEqual(site.user_id, 123)
        self.assertEqual(site.subdomain, "test")
        self.assertEqual(site.status, "active")
        self.assertEqual(site.custom_cname, "custom.example.com")

    def test_create_site_minimal(self):
        """创建最小站点数据"""
        site = StaticSite(
            id=1,
            user_id=123,
            subdomain="test",
            root_path="/workspace/123/public_html/",
            status="active"
        )
        self.assertIsNone(site.custom_cname)
        self.assertIsNone(site.created_at)


class TestSiteFileDataclass(unittest.TestCase):
    """测试 SiteFile 数据类"""

    def test_create_file(self):
        """创建文件数据"""
        file = SiteFile(
            path="css/style.css",
            size=1024,
            is_dir=False,
            modified_at=datetime(2026, 4, 26, 14, 30, 0)
        )
        self.assertEqual(file.path, "css/style.css")
        self.assertEqual(file.size, 1024)
        self.assertFalse(file.is_dir)

    def test_create_directory(self):
        """创建目录数据"""
        file = SiteFile(
            path="images",
            size=4096,
            is_dir=True
        )
        self.assertEqual(file.path, "images")
        self.assertTrue(file.is_dir)
        self.assertIsNone(file.modified_at)


if __name__ == "__main__":
    unittest.main()


class TestStaticSiteIntegration(unittest.TestCase):
    """集成测试：测试与数据库的交互"""

    @classmethod
    def setUpClass(cls):
        """设置测试数据库"""
        from models.database import Base, engine, SessionLocal, StaticSite as StaticSiteModel
        cls.Base = Base
        cls.engine = engine
        cls.SessionLocal = SessionLocal
        cls.StaticSiteModel = StaticSiteModel

        # 创建表（如果不存在）
        Base.metadata.create_all(bind=engine)

        # 创建 mock storage service（避免 COS 配置问题）
        cls.mock_storage = MagicMock()

    def setUp(self):
        """每个测试前清空 static_sites 表"""
        db = self.SessionLocal()
        try:
            db.query(self.StaticSiteModel).delete()
            db.commit()
        finally:
            db.close()

    def test_create_site_in_database(self):
        """测试在数据库中创建站点"""
        service = StaticSiteService(self.mock_storage)

        site = service.create_site(
            user_id=999,
            subdomain="testuser999",
            custom_cname=None
        )

        self.assertIsNotNone(site.id)
        self.assertEqual(site.user_id, 999)
        self.assertEqual(site.subdomain, "testuser999")
        self.assertEqual(site.status, "active")

    def test_list_user_sites(self):
        """测试列出用户站点"""
        service = StaticSiteService(self.mock_storage)

        # 创建两个站点
        service.create_site(user_id=888, subdomain="site1user888")
        service.create_site(user_id=888, subdomain="site2user888")
        service.create_site(user_id=777, subdomain="site1user777")

        # 列出用户 888 的站点
        sites = service.list_user_sites(888)
        self.assertEqual(len(sites), 2)

        # 列出用户 777 的站点
        sites = service.list_user_sites(777)
        self.assertEqual(len(sites), 1)

    def test_check_subdomain_available(self):
        """测试检查子域名可用性"""
        service = StaticSiteService(self.mock_storage)

        # 未使用的子域名应该可用
        self.assertTrue(service.check_subdomain_available("newsite123"))

        # 创建站点后应该不可用
        service.create_site(user_id=666, subdomain="takensite")
        self.assertFalse(service.check_subdomain_available("takensite"))

    def test_delete_site_soft_delete(self):
        """测试软删除站点"""
        service = StaticSiteService(self.mock_storage)

        # 创建站点
        site = service.create_site(user_id=555, subdomain="deletetest")

        # 删除站点
        success = service.delete_site(site.id, 555)
        self.assertTrue(success)

        # 验证站点状态变为 deleted
        db = self.SessionLocal()
        try:
            row = db.query(self.StaticSiteModel).filter(
                self.StaticSiteModel.id == site.id
            ).first()
            self.assertEqual(row.status, "deleted")
        finally:
            db.close()

        # 子域名应该再次可用
        self.assertTrue(service.check_subdomain_available("deletetest"))

    def test_update_site(self):
        """测试更新站点配置"""
        service = StaticSiteService(self.mock_storage)

        # 创建站点
        site = service.create_site(user_id=444, subdomain="updatetest")

        # 更新站点
        updated = service.update_site(
            site.id,
            444,
            status="suspended",
            custom_cname="custom.example.com"
        )

        self.assertEqual(updated.status, "suspended")
        self.assertEqual(updated.custom_cname, "custom.example.com")

    def test_get_site_by_id(self):
        """测试通过 ID 获取站点"""
        service = StaticSiteService(self.mock_storage)

        # 创建站点
        created = service.create_site(user_id=333, subdomain="gettest")

        # 获取站点
        site = service.get_site(created.id, 333)
        self.assertIsNotNone(site)
        self.assertEqual(site.subdomain, "gettest")

        # 其他用户不能获取
        site = service.get_site(created.id, 999)
        self.assertIsNone(site)
