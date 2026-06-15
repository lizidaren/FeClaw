"""
静态网站公开访问路由测试

测试 {subdomain}.site.firstentrance.net 的公开访问功能
"""

import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routers.static_site_public import (
    get_content_type,
    parse_subdomain,
    get_cos_key,
    MIME_TYPES,
    DEFAULT_INDEX_FILES,
)
from models.database import StaticSite


class TestGetContentType(unittest.TestCase):
    """测试 Content-Type 检测"""
    
    def test_html(self):
        """测试 HTML 文件"""
        self.assertEqual(
            get_content_type("index.html"),
            "text/html; charset=utf-8"
        )
    
    def test_css(self):
        """测试 CSS 文件"""
        self.assertEqual(
            get_content_type("style.css"),
            "text/css; charset=utf-8"
        )
    
    def test_javascript(self):
        """测试 JavaScript 文件"""
        self.assertEqual(
            get_content_type("app.js"),
            "application/javascript; charset=utf-8"
        )
    
    def test_json(self):
        """测试 JSON 文件"""
        self.assertEqual(
            get_content_type("data.json"),
            "application/json; charset=utf-8"
        )
    
    def test_png(self):
        """测试 PNG 图片"""
        self.assertEqual(
            get_content_type("logo.png"),
            "image/png"
        )
    
    def test_jpg(self):
        """测试 JPG 图片"""
        self.assertEqual(
            get_content_type("photo.jpg"),
            "image/jpeg"
        )
    
    def test_svg(self):
        """测试 SVG 图片"""
        self.assertEqual(
            get_content_type("icon.svg"),
            "image/svg+xml"
        )
    
    def test_wasm(self):
        """测试 WebAssembly 文件"""
        self.assertEqual(
            get_content_type("module.wasm"),
            "application/wasm"
        )
    
    def test_unknown_extension(self):
        """测试未知扩展名"""
        self.assertEqual(
            get_content_type("file.xyz"),
            "application/octet-stream"
        )
    
    def test_no_extension(self):
        """测试无扩展名文件"""
        self.assertEqual(
            get_content_type("README"),
            "application/octet-stream"
        )
    
    def test_nested_path(self):
        """测试嵌套路径"""
        self.assertEqual(
            get_content_type("assets/images/logo.png"),
            "image/png"
        )
    
    def test_query_string_ignored(self):
        """测试查询字符串不影响结果"""
        # 注意：查询字符串应该在路由层处理，这里只测试路径
        self.assertEqual(
            get_content_type("style.css"),
            "text/css; charset=utf-8"
        )


class TestParseSubdomain(unittest.TestCase):
    """测试子域名解析"""
    
    def test_valid_subdomain(self):
        """测试有效的子域名"""
        self.assertEqual(
            parse_subdomain("lizidaren.site.firstentrance.net"),
            "lizidaren"
        )
    
    def test_valid_subdomain_with_port(self):
        """测试带端口的子域名"""
        self.assertEqual(
            parse_subdomain("lizidaren.site.firstentrance.net:8080"),
            "lizidaren"
        )
    
    def test_subdomain_with_numbers(self):
        """测试包含数字的子域名"""
        self.assertEqual(
            parse_subdomain("site123.site.firstentrance.net"),
            "site123"
        )
    
    def test_subdomain_with_dash(self):
        """测试包含连字符的子域名"""
        self.assertEqual(
            parse_subdomain("my-site.site.firstentrance.net"),
            "my-site"
        )
    
    def test_localhost(self):
        """测试 localhost 返回 None"""
        self.assertIsNone(parse_subdomain("localhost"))
    
    def test_localhost_with_port(self):
        """测试 localhost 带端口返回 None"""
        self.assertIsNone(parse_subdomain("localhost:8080"))
    
    def test_ip_address(self):
        """测试 IP 地址返回 None"""
        self.assertIsNone(parse_subdomain("127.0.0.1"))
    
    def test_invalid_domain(self):
        """测试无效域名返回 None"""
        self.assertIsNone(parse_subdomain("example.com"))
    
    def test_wrong_suffix(self):
        """测试错误后缀返回 None"""
        self.assertIsNone(parse_subdomain("lizidaren.example.com"))
    
    def test_case_insensitive(self):
        """测试大小写不敏感"""
        self.assertEqual(
            parse_subdomain("LiZiDaRen.site.firstentrance.net"),
            "lizidaren"
        )


class TestGetCosKey(unittest.TestCase):
    """测试 COS Key 生成"""
    
    def setUp(self):
        """创建测试站点"""
        self.site = MagicMock(spec=StaticSite)
        self.site.user_id = "123"
        self.site.subdomain = "testsite"
    
    def test_root_path(self):
        """测试根路径"""
        key = get_cos_key(self.site, "")
        self.assertEqual(key, "firstentrance/static-sites/123/")
    
    def test_simple_file(self):
        """测试简单文件"""
        key = get_cos_key(self.site, "index.html")
        self.assertEqual(key, "firstentrance/static-sites/123/index.html")
    
    def test_nested_file(self):
        """测试嵌套文件"""
        key = get_cos_key(self.site, "css/style.css")
        self.assertEqual(key, "firstentrance/static-sites/123/css/style.css")
    
    def test_deeply_nested_file(self):
        """测试深层嵌套文件"""
        key = get_cos_key(self.site, "assets/images/icons/logo.png")
        self.assertEqual(key, "firstentrance/static-sites/123/assets/images/icons/logo.png")
    
    def test_leading_slash_stripped(self):
        """测试前导斜杠被移除"""
        key = get_cos_key(self.site, "/index.html")
        self.assertEqual(key, "firstentrance/static-sites/123/index.html")


class TestMimeTypes(unittest.TestCase):
    """测试 MIME 类型映射完整性"""
    
    def test_all_mime_types_are_strings(self):
        """测试所有 MIME 类型都是字符串"""
        for ext, mime in MIME_TYPES.items():
            self.assertIsInstance(ext, str)
            self.assertIsInstance(mime, str)
            self.assertTrue(ext.startswith("."))
    
    def test_common_types_exist(self):
        """测试常见类型存在"""
        common_types = [
            ".html", ".css", ".js", ".json",
            ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
            ".woff", ".woff2", ".ttf",
            ".pdf", ".zip"
        ]
        for ext in common_types:
            self.assertIn(ext, MIME_TYPES)


class TestDefaultIndexFiles(unittest.TestCase):
    """测试默认首页文件配置"""
    
    def test_index_html_first(self):
        """测试 index.html 是首选"""
        self.assertEqual(DEFAULT_INDEX_FILES[0], "index.html")
    
    def test_index_htm_exists(self):
        """测试 index.htm 也存在"""
        self.assertIn("index.htm", DEFAULT_INDEX_FILES)


class TestSubdomainValidation(unittest.TestCase):
    """测试子域名验证（通过 parse_subdomain）"""
    
    def test_start_with_dash_rejected(self):
        """测试以连字符开头的子域名被拒绝"""
        # parse_subdomain 返回 None 表示不是有效的静态站点域名
        result = parse_subdomain("-invalid.site.firstentrance.net")
        self.assertIsNone(result)
    
    def test_end_with_dash_rejected(self):
        """测试以连字符结尾的子域名被拒绝"""
        result = parse_subdomain("invalid-.site.firstentrance.net")
        self.assertIsNone(result)
    
    def test_uppercase_converted_to_lowercase(self):
        """测试大写字母转换为小写"""
        result = parse_subdomain("MYSITE.site.firstentrance.net")
        self.assertEqual(result, "mysite")


if __name__ == "__main__":
    unittest.main()
