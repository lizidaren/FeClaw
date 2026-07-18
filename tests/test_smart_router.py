"""
SmartRouter 专项测试

测试覆盖:
- 模型选择逻辑
- 路由配置加载
- 回退逻辑
- 无配置时的默认行为
"""

import os
import sys
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

# 确保项目根在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from services.smart_router import SmartRouter, RouteDecision, SR_TEXT_MODEL, SR_VL_MODEL


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def smart_router():
    """创建 SmartRouter 实例"""
    return SmartRouter()


# ============================================================================
# 1. RouteDecision 数据类测试
# ============================================================================

class TestRouteDecision:
    """测试 RouteDecision 数据类"""

    def test_default_decision(self):
        """默认决策所有字段为默认值"""
        decision = RouteDecision()

        assert decision.thinking is False
        assert decision.prefetch == []
        assert decision.buffer_msg is None
        assert decision.direct_reply is None
        assert decision.inject_rules == []

    def test_decision_with_values(self):
        """带值的决策"""
        decision = RouteDecision(
            thinking=True,
            prefetch=[{"tool": "web_search", "query": "天气"}],
            buffer_msg="正在思考...",
            direct_reply="你好",
            inject_rules=["规则1", "规则2"]
        )

        assert decision.thinking is True
        assert len(decision.prefetch) == 1
        assert decision.prefetch[0]["tool"] == "web_search"
        assert decision.buffer_msg == "正在思考..."
        assert decision.direct_reply == "你好"
        assert len(decision.inject_rules) == 2


# ============================================================================
# 2. _parse_decision 方法测试
# ============================================================================

class TestSmartRouterParseDecision:
    """测试 _parse_decision 方法"""

    def test_parse_full_valid_response(self, smart_router):
        """解析完整的有效响应"""
        result = {
            "thinking": True,
            "prefetch": [
                {"tool": "web_search", "query": "深圳图书馆"}
            ],
            "buffer_msg": "正在查询...",
            "direct_reply": None,
            "inject_rules": ["规则1", "规则2"]
        }

        decision = smart_router._parse_decision(result)

        assert decision.thinking is True
        assert len(decision.prefetch) == 1
        assert decision.prefetch[0]["tool"] == "web_search"
        assert decision.buffer_msg == "正在查询..."
        assert decision.inject_rules == ["规则1", "规则2"]

    def test_parse_thinking_true(self, smart_router):
        """解析 thinking=true"""
        result = {"thinking": True}
        decision = smart_router._parse_decision(result)

        assert decision.thinking is True

    def test_parse_thinking_false(self, smart_router):
        """解析 thinking=false"""
        result = {"thinking": False}
        decision = smart_router._parse_decision(result)

        assert decision.thinking is False

    def test_parse_thinking_missing(self, smart_router):
        """解析 missing thinking 字段"""
        result = {}
        decision = smart_router._parse_decision(result)

        assert decision.thinking is False

    def test_parse_prefetch_valid_tools(self, smart_router):
        """解析有效的 prefetch 工具列表"""
        result = {
            "prefetch": [
                {"tool": "web_search", "query": "关键词"},
                {"tool": "file_read", "query": "路径", "index": "textbook"}
            ]
        }

        decision = smart_router._parse_decision(result)

        assert len(decision.prefetch) == 2
        assert decision.prefetch[0]["tool"] == "web_search"
        assert decision.prefetch[1]["tool"] == "file_read"
        assert decision.prefetch[1].get("index") == "textbook"

    def test_parse_prefetch_invalid_entry_filtered(self, smart_router):
        """解析时过滤无效的 prefetch 条目"""
        result = {
            "prefetch": [
                {"tool": "web_search", "query": "有效"},
                "invalid_entry",
                {"query": "no_tool"},
                {"tool": ""},
                {},
            ]
        }

        decision = smart_router._parse_decision(result)

        assert len(decision.prefetch) == 1
        assert decision.prefetch[0]["tool"] == "web_search"

    def test_parse_prefetch_empty_list(self, smart_router):
        """解析空的 prefetch 列表"""
        result = {"prefetch": []}
        decision = smart_router._parse_decision(result)

        assert decision.prefetch == []

    def test_parse_buffer_msg_valid(self, smart_router):
        """解析有效的 buffer_msg"""
        result = {"buffer_msg": "正在思考中，请稍候..."}
        decision = smart_router._parse_decision(result)

        assert decision.buffer_msg == "正在思考中，请稍候..."

    def test_parse_buffer_msg_too_short_filtered(self, smart_router):
        """解析时过滤太短的 buffer_msg"""
        result = {"buffer_msg": "短"}
        decision = smart_router._parse_decision(result)

        assert decision.buffer_msg is None

    def test_parse_buffer_msg_truncated(self, smart_router):
        """解析时截断过长的 buffer_msg"""
        result = {"buffer_msg": "a" * 300}
        decision = smart_router._parse_decision(result)

        assert len(decision.buffer_msg) <= 200

    def test_parse_direct_reply_valid(self, smart_router):
        """解析有效的 direct_reply"""
        result = {"direct_reply": "你好，有什么可以帮助你的？"}
        decision = smart_router._parse_decision(result)

        assert decision.direct_reply == "你好，有什么可以帮助你的？"

    def test_parse_direct_reply_empty_filtered(self, smart_router):
        """解析时过滤空的 direct_reply"""
        result = {"direct_reply": "   "}
        decision = smart_router._parse_decision(result)

        assert decision.direct_reply is None

    def test_parse_inject_rules_valid(self, smart_router):
        """解析有效的 inject_rules"""
        result = {"inject_rules": ["规则1", "规则2", "规则3"]}
        decision = smart_router._parse_decision(result)

        assert decision.inject_rules == ["规则1", "规则2", "规则3"]

    def test_parse_inject_rules_truncated(self, smart_router):
        """解析时截断过长的规则"""
        result = {"inject_rules": ["a" * 100]}
        decision = smart_router._parse_decision(result)

        assert len(decision.inject_rules[0]) <= 80

    def test_parse_inject_rules_invalid_filtered(self, smart_router):
        """解析时过滤无效的 inject_rules 条目"""
        result = {
            "inject_rules": [
                "有效规则",
                "",
                "   ",
                123,
                None
            ]
        }
        decision = smart_router._parse_decision(result)

        assert decision.inject_rules == ["有效规则"]

    def test_parse_empty_dict(self, smart_router):
        """解析空字典返回默认决策"""
        result = {}
        decision = smart_router._parse_decision(result)

        assert decision.thinking is False
        assert decision.prefetch == []
        assert decision.buffer_msg is None
        assert decision.direct_reply is None
        assert decision.inject_rules == []

    def test_parse_non_dict_returns_default(self, smart_router):
        """解析非字典返回默认决策"""
        decision = smart_router._parse_decision("not a dict")
        assert decision.thinking is False

        decision = smart_router._parse_decision(None)
        assert decision.thinking is False


# ============================================================================
# 3. 模型选择逻辑测试
# ============================================================================

class TestSmartRouterModelSelection:
    """测试模型选择逻辑"""

    def test_sr_text_model_defined(self):
        """SR_TEXT_MODEL 已定义"""
        assert SR_TEXT_MODEL is not None
        assert isinstance(SR_TEXT_MODEL, str)

    def test_sr_vl_model_defined(self):
        """SR_VL_MODEL 已定义"""
        assert SR_VL_MODEL is not None
        assert isinstance(SR_VL_MODEL, str)

    def test_models_are_different(self):
        """文本模型和视觉模型不同"""
        assert SR_TEXT_MODEL != SR_VL_MODEL


# ============================================================================
# 4. 回退逻辑测试
# ============================================================================

class TestSmartRouterFallback:
    """测试回退逻辑"""

    @pytest.mark.asyncio
    async def test_route_llm_failure_returns_default(self, smart_router):
        """LLM 调用失败时返回默认决策"""
        with patch("services.llm_service.llm_service") as mock_llm:
            mock_llm.chat_json = AsyncMock(side_effect=Exception("LLM error"))

            decision = await smart_router.route("测试消息")

            assert decision.thinking is False
            assert decision.prefetch == []

    @pytest.mark.asyncio
    async def test_route_empty_message_returns_default(self, smart_router):
        """空消息返回默认决策"""
        decision = await smart_router.route("")

        assert decision.thinking is False
        assert decision.prefetch == []


# ============================================================================
# 5. 无配置时的默认行为测试
# ============================================================================

class TestSmartRouterDefaultBehavior:
    """测试默认行为"""

    def test_smart_router_can_instantiate(self):
        """SmartRouter 可以实例化"""
        router = SmartRouter()
        assert router is not None
