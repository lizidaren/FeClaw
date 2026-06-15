"""
静态网站使用统计测试

测试 StaticSiteService 的使用统计功能：
- 访问记录
- 统计查询
- 热门页面
"""

import unittest
from datetime import datetime, date, timedelta
from unittest.mock import MagicMock, patch
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.static_site_service import (
    StaticSiteService,
    StaticSite,
    SiteUsageStats,
    SiteUsageSummary,
)


class TestSiteUsageStats(unittest.TestCase):
    """测试 SiteUsageStats 数据类"""

    def test_create_usage_stats(self):
        """创建使用统计"""
        stats = SiteUsageStats(
            site_id=1,
            date=date.today(),
            visit_count=100,
            bandwidth_bytes=1024000,
            unique_ips=50,
            request_count=200
        )
        self.assertEqual(stats.site_id, 1)
        self.assertEqual(stats.visit_count, 100)
        self.assertEqual(stats.bandwidth_bytes, 1024000)
        self.assertEqual(stats.unique_ips, 50)
        self.assertEqual(stats.request_count, 200)

    def test_default_values(self):
        """默认值测试"""
        stats = SiteUsageStats(
            site_id=1,
            date=date.today()
        )
        self.assertEqual(stats.visit_count, 0)
        self.assertEqual(stats.bandwidth_bytes, 0)
        self.assertEqual(stats.unique_ips, 0)
        self.assertEqual(stats.request_count, 0)


class TestSiteUsageSummary(unittest.TestCase):
    """测试 SiteUsageSummary 数据类"""

    def test_create_summary(self):
        """创建统计汇总"""
        today = date.today()
        daily = [
            SiteUsageStats(site_id=1, date=today, visit_count=10),
            SiteUsageStats(site_id=1, date=today - timedelta(days=1), visit_count=20),
        ]
        summary = SiteUsageSummary(
            site_id=1,
            total_visits=30,
            total_bandwidth_bytes=5000,
            total_requests=50,
            daily_stats=daily
        )
        self.assertEqual(summary.site_id, 1)
        self.assertEqual(summary.total_visits, 30)
        self.assertEqual(len(summary.daily_stats), 2)


class TestRecordVisit(unittest.TestCase):
    """测试访问记录功能"""

    def setUp(self):
        """测试前准备"""
        self.mock_storage = MagicMock()
        self.service = StaticSiteService(self.mock_storage)

    @patch('services.static_site_service.SessionLocal')
    def test_record_visit_html_page(self, mock_session_local):
        """记录 HTML 页面访问"""
        # 模拟数据库会话
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db

        # 模拟查询返回 None（首次访问）
        mock_db.query.return_value.filter.return_value.first.return_value = None

        # 记录访问
        result = self.service.record_visit(
            site_id=1,
            file_path="index.html",
            client_ip="192.168.1.1",
            user_agent="Mozilla/5.0",
            referer="https://example.com",
            response_size=1024,
            response_status=200
        )

        # 验证
        self.assertTrue(result)
        mock_db.add.assert_called()
        mock_db.commit.assert_called()

    @patch('services.static_site_service.SessionLocal')
    def test_record_visit_static_asset(self, mock_session_local):
        """记录静态资源访问（不计入 visit_count）"""
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db

        # 模拟已有每日记录
        mock_usage = MagicMock()
        mock_usage.request_count = 10
        mock_usage.bandwidth_bytes = 5000
        mock_usage.visit_count = 5
        mock_db.query.return_value.filter.return_value.first.return_value = mock_usage

        # 记录静态资源访问
        result = self.service.record_visit(
            site_id=1,
            file_path="css/style.css",
            client_ip="192.168.1.1",
            response_size=2000
        )

        self.assertTrue(result)
        # 静态资源不计入 visit_count
        self.assertEqual(mock_usage.visit_count, 5)
        # 但会增加 request_count
        self.assertEqual(mock_usage.request_count, 11)

    @patch('services.static_site_service.SessionLocal')
    def test_record_visit_error_handling(self, mock_session_local):
        """访问记录错误处理"""
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_db.add.side_effect = Exception("DB Error")

        result = self.service.record_visit(
            site_id=1,
            file_path="index.html"
        )

        self.assertFalse(result)
        mock_db.rollback.assert_called()


class TestGetUsageStats(unittest.TestCase):
    """测试统计查询功能"""

    def setUp(self):
        self.mock_storage = MagicMock()
        self.service = StaticSiteService(self.mock_storage)

    @patch('services.static_site_service.SessionLocal')
    def test_get_usage_stats(self, mock_session_local):
        """获取使用统计"""
        # 模拟站点查询
        with patch.object(self.service, 'get_site') as mock_get_site:
            mock_get_site.return_value = StaticSite(
                id=1,
                user_id=123,
                subdomain="test",
                root_path="/workspace/123/public_html/",
                status="active"
            )

            # 模拟数据库会话
            mock_db = MagicMock()
            mock_session_local.return_value = mock_db

            # 模拟统计记录
            today = date.today()
            mock_usage = MagicMock()
            mock_usage.site_id = 1
            mock_usage.date = today
            mock_usage.visit_count = 100
            mock_usage.bandwidth_bytes = 10000
            mock_usage.unique_ips = 50
            mock_usage.request_count = 200

            mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [mock_usage]

            # 获取统计
            stats = self.service.get_usage_stats(1, 123, days=7)

            self.assertIsNotNone(stats)
            self.assertEqual(stats.site_id, 1)
            self.assertEqual(stats.total_visits, 100)
            self.assertEqual(len(stats.daily_stats), 1)

    def test_get_usage_stats_no_permission(self):
        """无权限访问统计"""
        with patch.object(self.service, 'get_site') as mock_get_site:
            mock_get_site.return_value = None

            stats = self.service.get_usage_stats(999, 123, days=7)

            self.assertIsNone(stats)


class TestGetPopularPages(unittest.TestCase):
    """测试热门页面功能"""

    def setUp(self):
        self.mock_storage = MagicMock()
        self.service = StaticSiteService(self.mock_storage)

    @patch('services.static_site_service.SessionLocal')
    def test_get_popular_pages(self, mock_session_local):
        """获取热门页面"""
        # 模拟站点查询
        with patch.object(self.service, 'get_site') as mock_get_site:
            mock_get_site.return_value = StaticSite(
                id=1,
                user_id=123,
                subdomain="test",
                root_path="/workspace/123/public_html/",
                status="active"
            )

            # 模拟数据库会话
            mock_db = MagicMock()
            mock_session_local.return_value = mock_db

            # 模拟查询结果
            mock_result = MagicMock()
            mock_result.file_path = "index.html"
            mock_result.visits = 100
            mock_result.bandwidth = 50000

            mock_db.query.return_value.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = [mock_result]

            # 获取热门页面
            pages = self.service.get_popular_pages(1, 123, days=7, limit=10)

            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0]["path"], "index.html")
            self.assertEqual(pages[0]["visits"], 100)

    def test_get_popular_pages_no_permission(self):
        """无权限访问热门页面"""
        with patch.object(self.service, 'get_site') as mock_get_site:
            mock_get_site.return_value = None

            pages = self.service.get_popular_pages(999, 123, days=7)

            self.assertEqual(pages, [])


class TestGetAllSitesUsage(unittest.TestCase):
    """测试所有站点统计功能"""

    def setUp(self):
        self.mock_storage = MagicMock()
        self.service = StaticSiteService(self.mock_storage)

    def test_get_all_sites_usage(self):
        """获取所有站点统计"""
        # 模拟站点列表
        mock_sites = [
            StaticSite(id=1, user_id=123, subdomain="test1", root_path="/workspace/123/public_html/", status="active"),
            StaticSite(id=2, user_id=123, subdomain="test2", root_path="/workspace/123/public_html/", status="active"),
        ]

        with patch.object(self.service, 'list_user_sites') as mock_list:
            mock_list.return_value = mock_sites

            # 模拟 get_usage_stats
            with patch.object(self.service, 'get_usage_stats') as mock_stats:
                mock_stats.return_value = SiteUsageSummary(
                    site_id=1,
                    total_visits=100,
                    total_bandwidth_bytes=10000,
                    total_requests=200,
                    daily_stats=[]
                )

                result = self.service.get_all_sites_usage(123, days=7)

                self.assertEqual(len(result), 2)


if __name__ == '__main__':
    unittest.main()
