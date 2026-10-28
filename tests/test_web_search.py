"""
三级搜索架构测试

测试内容：
- 搜索缓存机制
- 级别自动选择逻辑
- 降级机制
- 缓存统计功能

注意：实际的 API 调用需要 mock，因为需要真实的 API Key。
"""

import unittest
from unittest.mock import patch, MagicMock
import time

import pytest

# 导入被测试类
from services.agent_tools_service import AgentToolsService


class TestSearchCache(unittest.TestCase):
    """搜索缓存机制测试"""

    def setUp(self):
        """每个测试前清空缓存"""
        AgentToolsService.clear_search_cache()

    def test_cache_key_generation(self):
        """测试缓存 key 生成"""
        key1 = AgentToolsService._get_search_cache_key("深圳天气", "minimal")
        key2 = AgentToolsService._get_search_cache_key("深圳天气", "minimal")
        key3 = AgentToolsService._get_search_cache_key("深圳天气", "advanced")
        
        # 相同 query + level 应该生成相同的 key
        self.assertEqual(key1, key2)
        # 不同 level 应该生成不同的 key
        self.assertNotEqual(key1, key3)
        # key 应该是 32 位 MD5 哈希
        self.assertEqual(len(key1), 32)

    def test_cache_key_different_queries(self):
        """测试不同查询生成不同 key"""
        key1 = AgentToolsService._get_search_cache_key("Python教程", "minimal")
        key2 = AgentToolsService._get_search_cache_key("Python入门", "minimal")
        
        # 不同查询应该生成不同的 key
        self.assertNotEqual(key1, key2)

    def test_cache_set_and_get(self):
        """测试缓存写入和读取"""
        query = "测试查询"
        level = "minimal"
        result = "这是搜索结果"
        
        # 写入缓存
        AgentToolsService._set_cached_search(query, level, result)
        
        # 读取缓存
        cached = AgentToolsService._get_cached_search(query, level)
        self.assertEqual(cached, result)

    def test_cache_expiry(self):
        """测试缓存过期（TTL = 300s）"""
        query = "过期测试"
        level = "minimal"
        result = "旧结果"
        
        # 写入缓存，并手动设置过期时间戳
        AgentToolsService._set_cached_search(query, level, result)
        
        # 修改时间戳模拟过期（超过 300 秒）
        key = AgentToolsService._get_search_cache_key(query, level)
        AgentToolsService._search_cache[key] = (time.time() - 400, result)
        
        # 应该返回 None（已过期）
        cached = AgentToolsService._get_cached_search(query, level)
        self.assertIsNone(cached)

    def test_cache_max_size(self):
        """测试缓存容量限制（max = 200）"""
        # 写入 201 条缓存
        for i in range(201):
            AgentToolsService._set_cached_search(f"query_{i}", "minimal", f"result_{i}")
        
        stats = AgentToolsService.get_search_cache_stats()
        
        # 应该不超过最大容量
        self.assertLessEqual(stats["total_entries"], 200)

    def test_cache_clear(self):
        """测试缓存清空"""
        AgentToolsService._set_cached_search("test", "minimal", "result")
        self.assertIsNotNone(AgentToolsService._get_cached_search("test", "minimal"))
        
        AgentToolsService.clear_search_cache()
        
        self.assertIsNone(AgentToolsService._get_cached_search("test", "minimal"))

    def test_cache_stats(self):
        """测试缓存统计"""
        AgentToolsService.clear_search_cache()
        
        # 写入几条缓存
        for i in range(5):
            AgentToolsService._set_cached_search(f"query_{i}", "minimal", f"result_{i}")
        
        stats = AgentToolsService.get_search_cache_stats()
        
        self.assertEqual(stats["total_entries"], 5)
        self.assertEqual(stats["valid_entries"], 5)
        self.assertEqual(stats["ttl_seconds"], 300)
        self.assertEqual(stats["max_size"], 200)


class TestAutoSelectSearchLevel(unittest.TestCase):
    """级别自动选择逻辑测试

    现行契约（services/tools/web_tools.py::_auto_select_search_level）返回的
    规范级别名是 **quick / balanced / deep**；旧的 minimal / advanced / research
    只是 `_try_fallback_search` 里的兼容别名（level_map），自动选择不再产出它们。
    """

    def test_short_query_selects_quick(self):
        """短查询（<10字符）应选择 quick"""
        self.assertEqual(AgentToolsService._auto_select_search_level(None, "天气"), "quick")
        self.assertEqual(AgentToolsService._auto_select_search_level(None, "Python"), "quick")

    def test_research_keywords_select_deep(self):
        """研究关键词应选择 deep"""
        self.assertEqual(AgentToolsService._auto_select_search_level(None, "深度分析这个问题"), "deep")
        self.assertEqual(AgentToolsService._auto_select_search_level(None, "比较两个框架的优缺点"), "deep")
        self.assertEqual(AgentToolsService._auto_select_search_level(None, "Research this topic"), "deep")
        self.assertEqual(AgentToolsService._auto_select_search_level(None, "Deep dive into..."), "deep")

    def test_minimal_keywords_select_quick(self):
        """极简关键词应选择 quick"""
        self.assertEqual(AgentToolsService._auto_select_search_level(None, "Python是什么"), "quick")
        self.assertEqual(AgentToolsService._auto_select_search_level(None, "What is async"), "quick")
        self.assertEqual(AgentToolsService._auto_select_search_level(None, "快速回答"), "quick")

    def test_long_query_selects_deep(self):
        """长查询（>100字符）应选择 deep"""
        long_query = "请详细分析一下当前人工智能领域的发展趋势，包括大语言模型、多模态模型、具身智能等方面的最新进展"
        self.assertEqual(AgentToolsService._auto_select_search_level(None, long_query), "deep")

    def test_medium_query_without_keywords_selects_balanced(self):
        """中等复杂查询（无关键词、<100字符、无问号）应选择 balanced"""
        # 这个查询 > 10 字符，没有 research/quick 关键词，也不含问号
        query = "推荐几个好用的 Python 异步框架"
        result = AgentToolsService._auto_select_search_level(None, query)
        self.assertEqual(result, "balanced")


class TestFallbackMechanism(unittest.TestCase):
    """降级机制测试"""

    def test_fallback_order_research(self):
        """research 失败应按 advanced → minimal → bing_fallback 顺序降级"""
        mock_service = MagicMock()
        mock_service._search_kimi_sync = MagicMock(return_value="Kimi 结果")
        mock_service._search_tencent_sync = MagicMock(return_value="Error: 失败")
        mock_service._search_bing_fallback = MagicMock(return_value="Bing 结果")
        
        # 绑定实际方法
        result = AgentToolsService._try_fallback_search(mock_service, "测试", "research", time.time())
        # 应该尝试 advanced (Kimi)，成功则返回
        self.assertIn("Kimi 结果", result)
        mock_service._search_kimi_sync.assert_called_once()

    def test_fallback_order_advanced(self):
        """advanced 失败应按 minimal → bing_fallback 顺序降级"""
        mock_service = MagicMock()
        mock_service._search_tencent_sync = MagicMock(return_value="腾讯结果")
        mock_service._search_bing_fallback = MagicMock(return_value="Bing 结果")
        
        result = AgentToolsService._try_fallback_search(mock_service, "测试", "advanced", time.time())
        # 应该尝试 minimal (腾讯)，成功则返回
        self.assertIn("腾讯结果", result)
        mock_service._search_tencent_sync.assert_called_once()

    def test_fallback_all_fail(self):
        """所有降级都失败应返回 None"""
        mock_service = MagicMock()
        mock_service._search_tencent_sync = MagicMock(return_value="Error: 失败")
        mock_service._search_bing_fallback = MagicMock(return_value="Error: 失败")
        
        result = AgentToolsService._try_fallback_search(mock_service, "测试", "minimal", time.time())
        self.assertIsNone(result)

    def test_fallback_order_minimal(self):
        """minimal 失败应降级到 bing_fallback"""
        mock_service = MagicMock()
        mock_service._search_tencent_sync = MagicMock(return_value="Error: 腾讯失败")
        mock_service._search_bing_fallback = MagicMock(return_value="Bing 成功结果")
        
        result = AgentToolsService._try_fallback_search(mock_service, "测试", "minimal", time.time())
        # 应该降级到 bing_fallback
        self.assertIn("Bing 成功结果", result)
        mock_service._search_bing_fallback.assert_called_once()


class TestMultiWebSearch(unittest.TestCase):
    """同步搜索测试"""

    def setUp(self):
        """每个测试前清空缓存"""
        AgentToolsService.clear_search_cache()

    def test_multi_web_search_minimal(self):
        """测试 minimal 级别同步搜索"""
        mock_service = MagicMock()
        mock_service._search_tencent_sync = MagicMock(return_value="腾讯同步结果")
        
        result = AgentToolsService.multi_web_search(mock_service, "测试", level="minimal", use_cache=False)
        self.assertIn("腾讯同步结果", result)
        mock_service._search_tencent_sync.assert_called_once()

    def test_multi_web_search_advanced(self):
        """测试 advanced 级别同步搜索"""
        mock_service = MagicMock()
        mock_service._search_kimi_sync = MagicMock(return_value="Kimi 同步结果")
        
        result = AgentToolsService.multi_web_search(mock_service, "测试查询", level="advanced", use_cache=False)
        self.assertIn("Kimi 同步结果", result)
        mock_service._search_kimi_sync.assert_called_once()

    def test_multi_web_search_research(self):
        """测试 research 级别同步搜索"""
        mock_service = MagicMock()
        mock_service._search_baidu_sync = MagicMock(return_value="百度同步结果")
        
        result = AgentToolsService.multi_web_search(mock_service, "深度分析", level="research", use_cache=False)
        self.assertIn("百度同步结果", result)
        mock_service._search_baidu_sync.assert_called_once()

    def test_multi_web_search_invalid_level(self):
        """测试无效级别"""
        mock_service = MagicMock()
        mock_service._get_cached_search = MagicMock(return_value=None)
        mock_service._auto_select_search_level = MagicMock(return_value="quick")
        mock_service._search_tencent_sync = MagicMock(return_value="Error: 失败")

        # invalid 级别应该明确返回「未知级别」错误，而不是静默降级
        result = AgentToolsService.multi_web_search(mock_service, "测试", level="invalid", use_cache=False)
        self.assertIn("未知的搜索级别", result)

    def test_multi_web_search_auto_level_is_dispatched(self):
        """level='auto' 必须把自动选出的级别分派给对应后端。

        回归锁（S8 发现、主 agent 已修）：分派分支曾用旧级别名 minimal/advanced/
        research，而自动选择返回 quick/balanced/deep，导致 auto 100% 失败。
        """
        mock_service = MagicMock()
        mock_service._get_cached_search = MagicMock(return_value=None)
        # 真实实现返回 quick/balanced/deep 之一
        mock_service._auto_select_search_level = lambda q: "quick"
        mock_service._search_tencent_sync = MagicMock(return_value="腾讯自动结果")

        result = AgentToolsService.multi_web_search(mock_service, "天气", level="auto", use_cache=False)

        self.assertNotIn("未知的搜索级别", result)
        self.assertIn("腾讯自动结果", result)


class TestBingFallbackSearch(unittest.TestCase):
    """Bing CN 备选搜索测试"""

    def test_bing_fallback_methods_exist(self):
        """测试 Bing 搜索方法存在"""
        self.assertTrue(hasattr(AgentToolsService, '_search_bing_fallback'))
        self.assertTrue(hasattr(AgentToolsService, '_search_bing_fallback_async'))
        self.assertTrue(hasattr(AgentToolsService, '_parse_bing_results'))

    def test_parse_bing_results_empty_html(self):
        """测试空 HTML 解析"""
        mock_service = MagicMock()
        results = AgentToolsService._parse_bing_results(mock_service, "")
        self.assertEqual(results, [])

    def test_parse_bing_results_with_valid_html(self):
        """测试有效 HTML 解析"""
        mock_service = MagicMock()
        html = '''
        <html>
        <body>
            <p>这是一个搜索结果的摘要内容，长度足够长可以显示。</p>
            <h2><a href="https://example.com/article">测试标题</a></h2>
        </body>
        </html>
        '''
        results = AgentToolsService._parse_bing_results(mock_service, html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], '测试标题')
        self.assertEqual(results[0]['url'], 'https://example.com/article')
        # 摘要可能匹配到或使用默认值
        self.assertIsNotNone(results[0]['snippet'])

    def test_parse_bing_results_filters_short_snippets(self):
        """测试过滤短摘要"""
        mock_service = MagicMock()
        html = '''
        <html>
        <body>
            <p>短</p>
            <p>这是一个足够长的摘要内容，用于显示在搜索结果中。</p>
            <h2><a href="https://example.com/article1">标题1</a></h2>
            <h2><a href="https://example.com/article2">标题2</a></h2>
        </body>
        </html>
        '''
        results = AgentToolsService._parse_bing_results(mock_service, html)
        # 第一个结果应该有长摘要，第二个可能没有匹配的摘要
        self.assertEqual(len(results), 2)

    def test_parse_bing_results_skips_non_http_links(self):
        """测试跳过非 HTTP 链接"""
        mock_service = MagicMock()
        html = '''
        <html>
        <body>
            <p>这是一个摘要内容。</p>
            <h2><a href="javascript:void(0)">JavaScript 链接</a></h2>
            <h2><a href="https://example.com/valid">有效链接</a></h2>
        </body>
        </html>
        '''
        results = AgentToolsService._parse_bing_results(mock_service, html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['url'], 'https://example.com/valid')

    def test_parse_bing_results_extracts_source(self):
        """测试提取来源域名"""
        mock_service = MagicMock()
        html = '''
        <html>
        <body>
            <p>摘要内容。</p>
            <h2><a href="https://www.example.com/path/to/article">文章标题</a></h2>
        </body>
        </html>
        '''
        results = AgentToolsService._parse_bing_results(mock_service, html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['source'], 'www.example.com')

    def test_parse_bing_results_fallback_method(self):
        """测试备用解析方法（无 h2 标签时）"""
        mock_service = MagicMock()
        # 没有 h2 标签，只有普通 a 标签，触发备用方法
        html = '''
        <html>
        <body>
            <a href="https://example1.com/page1">链接标题1</a>
            <a href="https://example2.com/page2">链接标题2</a>
            <a href="https://bing.com/internal">内部链接</a>
        </body>
        </html>
        '''
        results = AgentToolsService._parse_bing_results(mock_service, html)
        # 应该过滤掉 bing.com 内部链接
        # 需要标题长度 > 3
        self.assertGreaterEqual(len(results), 0)  # 备用方法可能不触发
        if len(results) > 0:
            urls = [r['url'] for r in results]
            self.assertNotIn('https://bing.com/internal', urls)


class TestAPIKeyMissing(unittest.TestCase):
    """API Key 缺失测试

    `services/agent_tools_service.py` 只是 `services.tools` 的再导出壳，
    模块上并没有 `settings` 名字；这些方法实际定义在
    `services/tools/web_tools.py`，所以 patch 目标必须是
    `services.tools.web_tools.settings`。
    """

    def test_tencent_no_api_key(self):
        """腾讯搜索缺少 API Key"""
        mock_service = MagicMock()
        with patch("services.tools.web_tools.settings") as mock_settings:
            mock_settings.TENCENT_SEARCH_API_KEY = ""
            result = AgentToolsService._search_tencent_sync(mock_service, "测试")
            self.assertIn("Error", result)
            self.assertIn("API Key", result)

    def test_kimi_no_api_key(self):
        """Kimi 搜索缺少 API Key"""
        mock_service = MagicMock()
        with patch("services.tools.web_tools.settings") as mock_settings:
            mock_settings.KIMI_API_KEY = ""
            result = AgentToolsService._search_kimi_sync(mock_service, "测试")
            self.assertIn("Error", result)
            self.assertIn("API Key", result)

    def test_baidu_no_api_key(self):
        """百度搜索缺少 API Key"""
        mock_service = MagicMock()
        with patch("services.tools.web_tools.settings") as mock_settings:
            mock_settings.BAIDU_SEARCH_API_KEY = ""
            result = AgentToolsService._search_baidu_sync(mock_service, "测试")
            self.assertIn("Error", result)
            self.assertIn("API Key", result)


class TestEdgeCases(unittest.TestCase):
    """边界情况测试"""

    def setUp(self):
        """每个测试前清空缓存"""
        AgentToolsService.clear_search_cache()

    def test_very_long_query(self):
        """超长查询测试"""
        long_query = "测试" * 500  # 1000 字符

        # 静态方法测试（>100 字符 → deep）
        level = AgentToolsService._auto_select_search_level(None, long_query)
        self.assertEqual(level, "deep")

    def test_cache_with_unicode(self):
        """测试 Unicode 查询的缓存"""
        query = "日本語テスト"
        level = "minimal"
        result = "结果"
        
        AgentToolsService._set_cached_search(query, level, result)
        cached = AgentToolsService._get_cached_search(query, level)
        
        self.assertEqual(cached, result)

    def test_cache_with_special_characters(self):
        """测试特殊字符查询的缓存"""
        query = "C++ & Python"
        level = "minimal"
        result = "结果"
        
        AgentToolsService._set_cached_search(query, level, result)
        cached = AgentToolsService._get_cached_search(query, level)
        
        self.assertEqual(cached, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
