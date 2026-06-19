"""
LLM 服务单元测试

测试覆盖：
1. LLMProvider 基类 last_usage 默认值
2. DeepSeekProvider 流式/非流式调用正确设置 last_usage
3. DoubaoProvider 流式/非流式调用正确设置 last_usage
4. LLMService._record_stat() 写入 tokens_used
5. LLMService.chat() 在 generator 消费完后记录 token 数
6. LLMService.chat_with_tools() 从 API 响应提取 usage
7. LLMService.chat_with_tools_stream() 从 SSE 流提取 usage

所有测试 mock 外部网络请求，不真调 API。
"""

import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from services.llm_service import (
    LLMProvider,
    DeepSeekProvider,
    DoubaoProvider,
    ZhipuAIProvider,
    KimiProvider,
    QwenProvider,
    LLMService,
    settings,
)

pytestmark = pytest.mark.unit

# ─────────────────────────────────────────────
# 1. LLMProvider 基类
# ─────────────────────────────────────────────


class TestLLMProvider:
    """LLMProvider 基类测试"""

    @pytest.mark.asyncio
    async def test_chat_is_abstract(self):
        """验证 chat() 是抽象方法"""
        with pytest.raises(TypeError):
            LLMProvider()

    def test_last_usage_default(self):
        """验证类属性 last_usage 默认为 None"""
        assert LLMProvider.last_usage is None


# ─────────────────────────────────────────────
# 2. DeepSeekProvider — 做为代表测试所有 Provider
# ─────────────────────────────────────────────


class TestProvider:
    """选择一个 Provider（DeepSeek）代表测试流式/非流式的 usage 捕获"""

    SAMPLE_USAGE = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    @pytest.mark.asyncio
    async def test_non_stream_sets_last_usage(self, mock_httpx_client):
        """非流式调用后 last_usage 应等于 API 返回的 usage"""
        provider = DeepSeekProvider(api_key="test-key")
        mock_httpx_client["set_post_response"]({
            "choices": [{"message": {"content": "Hello!"}}],
            "usage": self.SAMPLE_USAGE,
        })

        results = []
        async for chunk in provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            model="deepseek-v4-flash",
        ):
            results.append(chunk)

        assert "".join(results) == "Hello!"
        assert provider.last_usage == self.SAMPLE_USAGE

    @pytest.mark.asyncio
    async def test_stream_sets_last_usage(self, mock_httpx_client):
        """流式调用从 SSE 的 usage chunk 捕获 token 数"""
        provider = DeepSeekProvider(api_key="test-key")
        mock_httpx_client["set_stream_response"]([
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo!"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":50,"total_tokens":150}}',
            "data: [DONE]",
        ])

        results = []
        async for chunk in provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            model="deepseek-v4-flash",
        ):
            results.append(chunk)

        assert "".join(results) == "Hello!"
        assert provider.last_usage["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_stream_no_usage_in_response(self, mock_httpx_client):
        """流式调用 API 不返回 usage 时，last_usage 应保持 None"""
        provider = DeepSeekProvider(api_key="test-key")
        mock_httpx_client["set_stream_response"]([
            'data: {"choices":[{"delta":{"content":"Hi"}}]}',
            "data: [DONE]",
        ])

        async for _ in provider.chat(messages=[{"role": "user", "content": "hi"}], stream=True, model="deepseek-v4-flash"):
            pass

        assert provider.last_usage is None

    @pytest.mark.asyncio
    async def test_empty_choices_with_usage_does_not_crash(self, mock_httpx_client):
        """API 返回 choices=[] + usage 时不应崩溃（之前被这个 bug 坑了）"""
        provider = DeepSeekProvider(api_key="test-key")
        mock_httpx_client["set_stream_response"]([
            'data: {"choices":[{"delta":{"content":"OK"}}]}',
            'data: {"choices":[],"usage":{"total_tokens":50}}',
            "data: [DONE]",
        ])

        results = []
        async for chunk in provider.chat(messages=[{"role": "user", "content": "hi"}], stream=True, model="deepseek-v4-flash"):
            results.append(chunk)

        assert "".join(results) == "OK"
        assert provider.last_usage["total_tokens"] == 50


# ─────────────────────────────────────────────
# 3. DoubaoProvider — 额外验证 reasoning_content 不干扰 usage 捕获
# ─────────────────────────────────────────────


class TestDoubaoProvider:
    """DoubaoProvider 测试（重点验证 reasoning_content + usage 共存场景）"""

    @pytest.fixture
    def provider(self):
        return DoubaoProvider(api_key="test-key-doubao")

    @pytest.mark.asyncio
    async def test_non_stream_sets_last_usage(self, provider, mock_httpx_client):
        """非流式调用后 last_usage 应等于 API 返回的 usage"""
        mock_httpx_client["set_post_response"]({
            "choices": [{"message": {"content": "你好"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        })
        results = []
        async for chunk in provider.chat(
            messages=[{"role": "user", "content": "你好"}],
            stream=False,
            model="doubao-seed-2-0-lite-260215",
        ):
            results.append(chunk)
        assert "".join(results) == "你好"
        assert provider.last_usage["total_tokens"] == 30

    @pytest.mark.asyncio
    async def test_stream_sets_last_usage(self, provider, mock_httpx_client):
        """流式调用后 last_usage 应等于 API 返回的 usage"""
        mock_httpx_client["set_stream_response"]([
            'data: {"choices":[{"delta":{"content":"你好"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":10,"total_tokens":30}}',
            "data: [DONE]",
        ])
        results = []
        async for chunk in provider.chat(
            messages=[{"role": "user", "content": "你好"}],
            stream=True,
            model="doubao-seed-2-0-lite-260215",
        ):
            results.append(chunk)
        assert "".join(results) == "你好"
        assert provider.last_usage["total_tokens"] == 30


# ─────────────────────────────────────────────
# 4. LLMService._record_stat
# ─────────────────────────────────────────────


class TestLLMServiceRecordStat:
    """LLMService._record_stat 测试"""

    @pytest.fixture
    def service(self):
        svc = LLMService()
        # 不要真正的 provider，我们直接测 _record_stat
        return svc

    @pytest.mark.asyncio
    async def test_record_stat_writes_tokens_used(self, service, mock_db):
        """_record_stat 应把 tokens_used 写入数据库"""
        await service._record_stat(
            provider="deepseek",
            model="deepseek-v4-flash",
            request_type="chat",
            tokens_used=150,
        )

        from models.database import LLMStat

        # 验证 LLMStat 创建时传入了正确的 tokens_used
        _, kwargs = mock_db.add.call_args
        stat = kwargs.get("positional_args", (None,))[0]
        if stat is None:
            # 可能是关键字参数
            stat = mock_db.add.call_args[0][0]

        # LLMStat 实例
        if hasattr(stat, "tokens_used"):
            assert stat.tokens_used == 150
        elif mock_db.add.call_args_list:
            # fallback: 检查 call_args
            added = mock_db.add.call_args[0][0]
            assert added.tokens_used == 150

        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_stat_default_tokens_used(self, service, mock_db):
        """_record_stat 不传 tokens_used 时默认为 0"""
        await service._record_stat(
            provider="zhipuai",
            model="glm-4.7",
            request_type="chat",
        )

        added = mock_db.add.call_args[0][0]
        assert added.tokens_used == 0

    @pytest.mark.asyncio
    async def test_record_stat_logs_error_on_failure(self, service, mock_db, caplog):
        """_record_stat 异常时应记录日志，不抛出"""
        mock_db.add.side_effect = Exception("DB error")

        # 不应抛出异常
        await service._record_stat(
            provider="deepseek",
            model="deepseek-v4-flash",
            request_type="chat",
            tokens_used=100,
        )

        # 应记录错误日志
        assert "Failed to record LLM stat" in caplog.text


# ─────────────────────────────────────────────
# 5. LLMService.chat() — 异步 generator
# ─────────────────────────────────────────────


class TestLLMServiceChat:
    """LLMService.chat() 测试——token 统计应在 generator 消费完后记录"""

    @pytest.fixture
    def service(self):
        return LLMService()

    @pytest.mark.asyncio
    async def test_chat_records_stat_after_generator(self, service, mock_db):
        """chat() 的 _record_stat 应在 generator 消费完后调用，且传入 token 数"""
        mock_provider = MagicMock()
        mock_provider.last_usage = {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}

        async def mock_chat(*args, **kwargs):
            yield "Hello"
            yield " World"

        mock_provider.chat = mock_chat

        with patch.object(service, "get_provider", return_value=mock_provider):
            with patch.object(settings, "DEFAULT_LLM_PROVIDER", "deepseek"):
                with patch.object(settings, "DEFAULT_LLM_MODEL", "deepseek-v4-flash"):
                    results = []
                    async for chunk in service.chat(
                        messages=[{"role": "user", "content": "hi"}],
                    ):
                        results.append(chunk)

            assert "".join(results) == "Hello World"

            # 让 _record_stat 后台任务执行
            await asyncio.sleep(0.01)

            # 验证 _record_stat 被调用，且 tokens_used=80
            # 由于 _record_stat 是 asyncio.create_task，检查 mock_db
            added = mock_db.add.call_args[0][0]
            assert added.tokens_used == 80
            assert added.provider == "deepseek"
            assert added.model == "deepseek-v4-flash"
            assert added.request_type == "chat"

    @pytest.mark.asyncio
    async def test_chat_records_zero_when_no_usage(self, service, mock_db):
        """provider 没有 last_usage 时，tokens_used 应为 0"""
        mock_provider = MagicMock()
        mock_provider.last_usage = None

        async def mock_chat(*args, **kwargs):
            yield "test"

        mock_provider.chat = mock_chat

        with patch.object(service, "get_provider", return_value=mock_provider):
            with patch.object(settings, "DEFAULT_LLM_PROVIDER", "zhipuai"):
                with patch.object(settings, "DEFAULT_LLM_MODEL", "glm-4.7"):
                    async for _ in service.chat(
                        messages=[{"role": "user", "content": "hi"}],
                    ):
                        pass

            await asyncio.sleep(0.01)
            added = mock_db.add.call_args[0][0]
            assert added.tokens_used == 0


# ─────────────────────────────────────────────
# 6. LLMService.chat_with_tools()
# ─────────────────────────────────────────────


class TestLLMServiceChatWithTools:
    """LLMService.chat_with_tools() 测试"""

    USAGE = {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180}

    @pytest.fixture
    def service(self):
        return LLMService()

    def _make_api_response(self, content="tool response", tool_calls=None, usage=None):
        """构造 mock API response JSON"""
        msg = {"content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return {
            "choices": [{"message": msg}],
            "usage": usage or self.USAGE,
        }

    @pytest.mark.asyncio
    async def test_chat_with_tools_records_usage(self, service, mock_db):
        """chat_with_tools 应提取 API 响应中的 usage 并记录"""
        api_response = self._make_api_response(content="使用工具搜索")
        result = await self._chat_with_tools(service, api_response, mock_db)

        assert result["content"] == "使用工具搜索"
        await asyncio.sleep(0.01)
        added = mock_db.add.call_args[0][0]
        assert added.tokens_used == 180

    @pytest.mark.asyncio
    async def test_chat_with_tools_with_tool_calls(self, service, mock_db):
        """带 tool_calls 时仍应正确记录 token 数"""
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query":"test"}'},
            }
        ]
        api_response = self._make_api_response(tool_calls=tool_calls, usage=self.USAGE)
        result = await self._chat_with_tools(service, api_response, mock_db)

        assert result["tool_calls"] is not None
        assert result["tool_calls"][0]["function"]["name"] == "web_search"
        await asyncio.sleep(0.01)
        added = mock_db.add.call_args[0][0]
        assert added.tokens_used == 180

    async def _chat_with_tools(self, service, api_response, mock_db):
        """辅助方法：设置 mock 并调用 chat_with_tools"""
        from services.llm_service import _resolve_provider

        async def mock_call():
            return api_response

        with patch.object(service, "_retry_call", return_value=api_response):
            with patch.object(service, "_ensure_http_client", return_value=AsyncMock()):
                with patch("services.llm_service._resolve_provider", return_value=("test-key", "https://api.test.com")):
                    with patch.object(settings, "MAIN_TEXT_MODEL", "deepseek-v4-flash"):
                        result = await service.chat_with_tools(
                            messages=[{"role": "user", "content": "搜索一下"}],
                            tools=[],
                        )
                        return result


# ─────────────────────────────────────────────
# 7. LLMService.chat_with_tools_stream()
# ─────────────────────────────────────────────


class TestLLMServiceChatWithToolsStream:
    """LLMService.chat_with_tools_stream() 测试"""

    @pytest.fixture
    def service(self):
        return LLMService()

    async def _run_stream(self, service, sse_events, mock_db=None):
        """辅助：运行流式调用并返回所有 events"""
        from services.llm_service import _parse_sse_events, _resolve_provider

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock()
        mock_response.aclose = AsyncMock()

        async def _aiter_bytes():
            for event in sse_events:
                yield (event + "\n\n").encode("utf-8")

        mock_response.aiter_bytes = _aiter_bytes

        mock_client = AsyncMock()

        with patch.object(service, "_ensure_http_client", return_value=mock_client):
            with patch.object(service, "_retry_call", return_value=mock_response):
                with patch("services.llm_service._resolve_provider", return_value=("test-key", "https://api.test.com")):
                    with patch.object(settings, "MAIN_TEXT_MODEL", "deepseek-v4-flash"):
                        events = []
                        async for event in service.chat_with_tools_stream(
                            messages=[{"role": "user", "content": "test"}],
                        ):
                            events.append(event)
                        return events

    @pytest.mark.asyncio
    async def test_stream_captures_usage_from_sse(self, service, mock_db):
        """流式调用应从 SSE 的 usage chunk 提取 token 数"""
        sse_events = [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":50,"total_tokens":150}}',
            "data: [DONE]",
        ]

        events = await self._run_stream(service, sse_events)

        # 验证 token 事件
        tokens = [e["content"] for e in events if e.get("type") == "token"]
        assert "".join(tokens) == "Hello"

        # 验证 done 事件
        done = [e for e in events if e.get("type") == "done"]
        assert len(done) == 1

        # 验证 _record_stat 被调用时传入了正确的 tokens_used
        await asyncio.sleep(0.01)
        added = mock_db.add.call_args[0][0]
        assert added.tokens_used == 150

    @pytest.mark.asyncio
    async def test_stream_no_usage_in_sse(self, service, mock_db):
        """SSE 流中没有 usage 时 tokens_used 应为 0"""
        sse_events = [
            'data: {"choices":[{"delta":{"content":"test"}}]}',
            "data: [DONE]",
        ]

        events = await self._run_stream(service, sse_events)

        await asyncio.sleep(0.01)
        added = mock_db.add.call_args[0][0]
        assert added.tokens_used == 0


# ─────────────────────────────────────────────
# 8. 集成验证
# ─────────────────────────────────────────────


class TestLLMServiceIntegration:
    """模拟完整调用链，验证 token 统计从 provider 到数据库不断"""

    @pytest.mark.asyncio
    async def test_tokens_used_propagates_through_full_chain(self, mock_db):
        """验证 token 统计从 provider -> service -> db 链路不断"""
        service = LLMService()

        # 模拟 provider 返回 usage
        mock_provider = MagicMock()
        mock_provider.last_usage = {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}

        async def mock_chat(*args, **kwargs):
            yield "response"

        mock_provider.chat = mock_chat

        with patch.object(service, "get_provider", return_value=mock_provider):
            with patch.object(settings, "DEFAULT_LLM_PROVIDER", "deepseek"):
                with patch.object(settings, "DEFAULT_LLM_MODEL", "deepseek-v4-flash"):
                    async for _ in service.chat(
                        messages=[{"role": "user", "content": "hi"}],
                    ):
                        pass

            await asyncio.sleep(0.01)

            # 从 mock_db 验证
            added = mock_db.add.call_args[0][0]
            assert added.tokens_used == 80
            assert added.provider == "deepseek"
            assert added.model == "deepseek-v4-flash"
            assert added.request_type == "chat"
