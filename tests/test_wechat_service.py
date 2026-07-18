"""
WeChat Service 专项测试

测试覆盖:
- 单例模式
- 登录状态管理
- 消息格式转换
- 轮询状态管理
- 事件回调
"""

import os
import sys
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime

# 确保项目根在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ============================================================================
# 1. 单例模式测试
# ============================================================================

class TestWeChatServiceSingleton:
    """测试单例模式"""

    def test_singleton_returns_same_instance(self):
        """多次调用返回相同实例"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service1 = WeChatService()
        service2 = WeChatService()

        assert service1 is service2

    def test_singleton_initialized_flag(self):
        """单例初始化标志正确"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        assert service._initialized is True


# ============================================================================
# 2. 登录状态管理测试
# ============================================================================

class TestWeChatServiceLoginState:
    """测试登录状态管理"""

    def test_initial_login_state(self):
        """初始登录状态正确"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        state = service.login_state

        assert state["status"] == "idle"
        assert state["qrcode_token"] is None
        assert state["qrcode_image"] is None
        assert state["bot_token"] is None

    def test_reset_login_state(self):
        """重置登录状态"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        service._login_state = {
            "qrcode_token": "test_token",
            "qrcode_image": "test_image",
            "status": "confirmed",
            "bot_token": "test_bot_token",
            "ilink_bot_id": "test_bot_id",
            "ilink_user_id": "test_user_id",
            "base_url": "test_url",
        }

        service.reset_login_state()
        state = service.login_state

        assert state["status"] == "idle"
        assert state["qrcode_token"] is None
        assert state["bot_token"] is None

    def test_login_state_returns_copy(self):
        """login_state 返回字典副本"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        state1 = service.login_state
        state1["status"] = "modified"

        state2 = service.login_state
        assert state2["status"] == "idle"


# ============================================================================
# 3. 会话管理测试
# ============================================================================

class TestWeChatServiceSession:
    """测试会话管理"""

    @pytest.mark.asyncio
    async def test_get_session_creates_session(self):
        """获取会话创建新会话"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        session = await service._get_session()

        assert session is not None
        assert service._session is session


# ============================================================================
# 4. 轮询状态测试
# ============================================================================

class TestWeChatServicePolling:
    """测试轮询状态管理"""

    def test_polling_state_initialization(self):
        """轮询状态初始为空"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        assert service._polling_running == {}
        assert service._polling_tasks == {}
        assert service._sdk_executors == {}

    def test_user_clients_initialization(self):
        """用户客户端字典初始化"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        assert service._user_clients == {}
        assert service._last_heartbeat == {}
        assert service._last_activity == {}


# ============================================================================
# 5. 上下文 Token 测试
# ============================================================================

class TestWeChatServiceContextTokens:
    """测试上下文 Token"""

    def test_context_tokens_initialization(self):
        """上下文 token 字典初始化"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        assert service._context_tokens == {}

    def test_context_tokens_set_and_get(self):
        """设置和获取上下文 token"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        service._context_tokens[("account1", "user1")] = "token123"

        assert service._context_tokens[("account1", "user1")] == "token123"


# ============================================================================
# 6. 状态缓存测试
# ============================================================================

class TestWeChatServiceStatusCache:
    """测试状态缓存"""

    def test_status_cache_initialization(self):
        """状态缓存初始化"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        assert service._status_cache == {}
        assert service._status_cache_ttl == 1.0


# ============================================================================
# 7. 事件回调测试
# ============================================================================

class TestWeChatServiceCallbacks:
    """测试事件回调"""

    def test_callbacks_initialized_to_none(self):
        """回调初始化为 None"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        assert service._on_qrcode is None
        assert service._on_scanned is None
        assert service._on_confirmed is None
        assert service._on_message is None
        assert service._on_error is None
        assert service._on_session_expired is None

    def test_callbacks_can_be_set(self):
        """回调可以设置"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        callback = MagicMock()

        service._on_message = callback

        assert service._on_message is callback


# ============================================================================
# 8. 光标管理测试
# ============================================================================

class TestWeChatServiceCursor:
    """测试消息光标"""

    def test_cursor_initialization(self):
        """光标初始为 None"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        assert service._cursor is None

    def test_cursor_can_be_set(self):
        """光标可以设置"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        service._cursor = "cursor_token_123"

        assert service._cursor == "cursor_token_123"


# ============================================================================
# 9. JSON 响应处理测试
# ============================================================================

class TestWeChatServiceJsonResponse:
    """测试 JSON 响应处理"""

    @pytest.mark.asyncio
    async def test_json_response_valid_json(self):
        """处理有效 JSON 响应"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        mock_response = AsyncMock()
        mock_response.text = AsyncMock(return_value='{"ret": 0, "data": "test"}')
        mock_response.status = 200

        result = await service._json_response(mock_response)

        assert result["ret"] == 0
        assert result["data"] == "test"

    @pytest.mark.asyncio
    async def test_json_response_invalid_json_raises(self):
        """无效 JSON 响应抛出异常"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        mock_response = AsyncMock()
        mock_response.text = AsyncMock(return_value='not valid json')
        mock_response.status = 200

        with pytest.raises(Exception, match="JSON parse error"):
            await service._json_response(mock_response)


# ============================================================================
# 10. 关闭服务测试
# ============================================================================

class TestWeChatServiceClose:
    """测试 close 方法"""

    @pytest.mark.asyncio
    async def test_close_sets_polling_false(self):
        """关闭设置轮询状态为 False"""
        from services.wechat_service import WeChatService
        WeChatService._instance = None
        service = WeChatService()

        service._polling_running[123] = True

        await service.close()

        # close 设置为 False，不立即清空
        assert service._polling_running.get(123) is False
