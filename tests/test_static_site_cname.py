"""
测试静态网站 CNAME 验证功能

测试内容：
- verify_cname() 方法
- get_cname_status() 方法
- 数据库模型新字段
- API 端点功能
"""

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, UTC, date
import dns.resolver

from services.static_site_service import StaticSiteService, StaticSite
from models.database import StaticSite as StaticSiteModel, SessionLocal


class TestCnameVerify(unittest.TestCase):
    """测试 CNAME 验证功能"""

    def setUp(self):
        """设置测试环境"""
        self.storage_mock = MagicMock()
        self.service = StaticSiteService(self.storage_mock)

    def test_verify_cname_no_custom_cname(self):
        """测试未设置 custom_cname 时返回正确错误"""
        # Mock site without custom_cname
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active",
            custom_cname=None,
            cname_verified=False,
            cname_verified_at=None
        )

        with patch.object(self.service, 'get_site', return_value=site):
            result = self.service.verify_cname(1, 2)
            self.assertFalse(result["verified"])
            self.assertEqual(result["error"], "No custom CNAME configured")

    def test_verify_cname_success(self):
        """测试 CNAME 验证成功"""
        # Mock site with custom_cname
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active",
            custom_cname="blog.example.com",
            cname_verified=False,
            cname_verified_at=None
        )

        # Mock DNS resolver
        mock_answer = MagicMock()
        mock_answer.__str__ = lambda self: "test.site.firstentrance.net."

        # Mock database session
        mock_db = MagicMock()
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.user_id = "2"
        mock_row.cname_verified = False
        mock_row.cname_verified_at = None
        mock_db.query.return_value.filter.return_value.first.return_value = mock_row

        with patch.object(self.service, 'get_site', return_value=site):
            with patch('dns.resolver.resolve', return_value=[mock_answer]):
                with patch('services.static_site_service.SessionLocal', return_value=mock_db):
                    result = self.service.verify_cname(1, 2)

                    self.assertTrue(result["verified"])
                    self.assertEqual(result["expected_cname"], "test.site.firstentrance.net")
                    self.assertEqual(result["actual_cname"], "test.site.firstentrance.net")
                    self.assertIsNone(result["error"])

    def test_verify_cname_mismatch(self):
        """测试 CNAME 不匹配"""
        # Mock site with custom_cname
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active",
            custom_cname="blog.example.com",
            cname_verified=False,
            cname_verified_at=None
        )

        # Mock DNS resolver with wrong CNAME
        mock_answer = MagicMock()
        mock_answer.__str__ = lambda self: "other.site.firstentrance.net."

        # Mock database session
        mock_db = MagicMock()
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.user_id = "2"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_row

        with patch.object(self.service, 'get_site', return_value=site):
            with patch('dns.resolver.resolve', return_value=[mock_answer]):
                with patch('services.static_site_service.SessionLocal', return_value=mock_db):
                    result = self.service.verify_cname(1, 2)

                    self.assertFalse(result["verified"])
                    self.assertEqual(result["expected_cname"], "test.site.firstentrance.net")
                    self.assertEqual(result["actual_cname"], "other.site.firstentrance.net")
                    self.assertIn("CNAME mismatch", result["error"])

    def test_verify_cname_no_answer(self):
        """测试没有 CNAME 记录"""
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active",
            custom_cname="blog.example.com",
            cname_verified=False,
            cname_verified_at=None
        )

        with patch.object(self.service, 'get_site', return_value=site):
            with patch('dns.resolver.resolve', side_effect=dns.resolver.NoAnswer()):
                result = self.service.verify_cname(1, 2)

                self.assertFalse(result["verified"])
                self.assertEqual(result["error"], "No CNAME record found")

    def test_verify_cname_nxdomain(self):
        """测试域名不存在"""
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active",
            custom_cname="nonexistent.example.com",
            cname_verified=False,
            cname_verified_at=None
        )

        with patch.object(self.service, 'get_site', return_value=site):
            with patch('dns.resolver.resolve', side_effect=dns.resolver.NXDOMAIN()):
                result = self.service.verify_cname(1, 2)

                self.assertFalse(result["verified"])
                self.assertIn("does not exist", result["error"])

    def test_verify_cname_site_not_found(self):
        """测试站点不存在"""
        with patch.object(self.service, 'get_site', return_value=None):
            with self.assertRaises(ValueError) as ctx:
                self.service.verify_cname(999, 2)
            self.assertIn("not found", str(ctx.exception))


class TestCnameStatus(unittest.TestCase):
    """测试 CNAME 状态查询"""

    def setUp(self):
        self.storage_mock = MagicMock()
        self.service = StaticSiteService(self.storage_mock)

    def test_get_cname_status_verified(self):
        """测试已验证的 CNAME 状态"""
        verified_at = datetime(2026, 5, 11, 2, 30, 0, tzinfo=UTC)
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active",
            custom_cname="blog.example.com",
            cname_verified=True,
            cname_verified_at=verified_at
        )

        with patch.object(self.service, 'get_site', return_value=site):
            result = self.service.get_cname_status(1, 2)

            self.assertEqual(result["custom_cname"], "blog.example.com")
            self.assertTrue(result["verified"])
            self.assertEqual(result["verified_at"], verified_at)
            self.assertEqual(result["expected_cname"], "test.site.firstentrance.net")

    def test_get_cname_status_unverified(self):
        """测试未验证的 CNAME 状态"""
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active",
            custom_cname="blog.example.com",
            cname_verified=False,
            cname_verified_at=None
        )

        with patch.object(self.service, 'get_site', return_value=site):
            result = self.service.get_cname_status(1, 2)

            self.assertEqual(result["custom_cname"], "blog.example.com")
            self.assertFalse(result["verified"])
            self.assertIsNone(result["verified_at"])
            self.assertEqual(result["expected_cname"], "test.site.firstentrance.net")

    def test_get_cname_status_no_custom_cname(self):
        """测试未设置 custom_cname 的状态"""
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active",
            custom_cname=None,
            cname_verified=False,
            cname_verified_at=None
        )

        with patch.object(self.service, 'get_site', return_value=site):
            result = self.service.get_cname_status(1, 2)

            self.assertIsNone(result["custom_cname"])
            self.assertFalse(result["verified"])
            self.assertIsNone(result["verified_at"])
            self.assertIsNone(result["expected_cname"])

    def test_get_cname_status_site_not_found(self):
        """测试站点不存在"""
        with patch.object(self.service, 'get_site', return_value=None):
            result = self.service.get_cname_status(999, 2)

            self.assertIsNone(result["custom_cname"])
            self.assertFalse(result["verified"])
            self.assertIsNone(result["verified_at"])
            self.assertIsNone(result["expected_cname"])


class TestStaticSiteDataclass(unittest.TestCase):
    """测试 StaticSite dataclass 新字段"""

    def test_dataclass_has_cname_verified_field(self):
        """测试 dataclass 包含 cname_verified 字段"""
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active",
            custom_cname="blog.example.com",
            cname_verified=True,
            cname_verified_at=datetime.now(UTC)
        )

        self.assertTrue(hasattr(site, 'cname_verified'))
        self.assertTrue(hasattr(site, 'cname_verified_at'))
        self.assertTrue(site.cname_verified)

    def test_dataclass_default_values(self):
        """测试 dataclass 默认值"""
        site = StaticSite(
            id=1,
            user_id=2,
            subdomain="test",
            root_path="/workspace/2/public_html/",
            status="active"
        )

        self.assertFalse(site.cname_verified)
        self.assertIsNone(site.cname_verified_at)


class TestDbToDataclass(unittest.TestCase):
    """测试 _db_to_dataclass 方法"""

    def test_db_to_dataclass_with_cname_fields(self):
        """测试数据库转换包含 CNAME 字段"""
        self.storage_mock = MagicMock()
        self.service = StaticSiteService(self.storage_mock)

        # Mock database row
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.user_id = "2"
        mock_row.subdomain = "test"
        mock_row.root_path = "/workspace/2/public_html/"
        mock_row.status = "active"
        mock_row.custom_cname = "blog.example.com"
        mock_row.cname_verified = True
        mock_row.cname_verified_at = datetime(2026, 5, 11, 2, 30, 0, tzinfo=UTC)
        mock_row.created_at = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)
        mock_row.updated_at = datetime(2026, 5, 11, 2, 30, 0, tzinfo=UTC)

        site = self.service._db_to_dataclass(mock_row)

        self.assertEqual(site.id, 1)
        self.assertEqual(site.user_id, 2)
        self.assertEqual(site.subdomain, "test")
        self.assertEqual(site.custom_cname, "blog.example.com")
        self.assertTrue(site.cname_verified)
        self.assertEqual(site.cname_verified_at, datetime(2026, 5, 11, 2, 30, 0, tzinfo=UTC))

    def test_db_to_dataclass_without_cname_verified_field(self):
        """测试数据库缺少字段时的兼容性"""
        self.storage_mock = MagicMock()
        self.service = StaticSiteService(self.storage_mock)

        # Mock database row without cname_verified field
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.user_id = "2"
        mock_row.subdomain = "test"
        mock_row.root_path = "/workspace/2/public_html/"
        mock_row.status = "active"
        mock_row.custom_cname = None
        # No cname_verified or cname_verified_at attributes
        del mock_row.cname_verified
        del mock_row.cname_verified_at
        mock_row.created_at = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)
        mock_row.updated_at = datetime(2026, 5, 11, 2, 30, 0, tzinfo=UTC)

        site = self.service._db_to_dataclass(mock_row)

        self.assertEqual(site.id, 1)
        self.assertFalse(site.cname_verified)  # Default False
        self.assertIsNone(site.cname_verified_at)  # Default None


if __name__ == "__main__":
    unittest.main()