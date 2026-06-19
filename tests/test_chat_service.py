"""
聊天服务单元测试

测试覆盖：
1. ChatService 初始化与属性懒加载
2. _check_agent_status 状态检查
3. _build_messages 消息构建（含多模态）
4. _inject_corrections 修正注入
5. build_system_prompt 系统提示词构建
6. chat() 核心流程（pre_process_hook / pending / 正常）
7. _save_conversation 对话保存
8. _stream_ai_response 流式响应处理

所有测试 mock 外部依赖，不真调 LLM/COS/DB。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime

pytestmark = pytest.mark.unit


def _make_fixture(agent_status="initialized", user_id=42):
    """创建 ChatService 测试 fixture 的通用工厂"""
    patcher = patch("services.chat_service.get_session")
    mock_get_session = patcher.start()
    mock_session = MagicMock()
    mock_get_session.return_value.__enter__.return_value = mock_session
    agent = MagicMock()
    agent.user_id = user_id
    agent.hash = "abcd"
    agent.status = agent_status
    agent.system_prompt = None
    mock_session.query.return_value.filter.return_value.first.return_value = agent
    return patcher, mock_session, agent


class TestChatServiceInit:
    """ChatService 初始化测试"""

    def test_init_sets_agent_hash(self):
        """初始化应设置 agent_hash 和 channel"""
        patcher = patch("services.chat_service.get_session")
        mock_get_session = patcher.start()
        mock_session = MagicMock()
        mock_get_session.return_value.__enter__.return_value = mock_session
        agent = MagicMock()
        agent.user_id = 42
        agent.status = "initialized"
        agent.system_prompt = None
        mock_session.query.return_value.filter.return_value.first.return_value = agent
        try:
            from services.chat_service import ChatService
            svc = ChatService(agent_hash="abcd", channel="api")
            assert svc.agent_hash == "abcd"
            assert svc.channel == "api"
        finally:
            patcher.stop()

    def test_user_id_lazy_loaded(self):
        """user_id 应为懒加载，首次访问后缓存"""
        patcher, mock_session, agent = _make_fixture()
        try:
            from services.chat_service import ChatService
            svc = ChatService(agent_hash="abcd", channel="api")
            assert svc.user_id == "42"
            assert svc.user_id == "42"
        finally:
            patcher.stop()


class TestCheckAgentStatus:
    """_check_agent_status 测试"""

    def test_check_agent_status_initialized(self):
        """Agent 状态为 initialized 时应返回 initialized"""
        patcher, mock_session, agent = _make_fixture(agent_status="initialized")
        try:
            from services.chat_service import ChatService
            svc = ChatService(agent_hash="abcd", channel="api")
            assert svc._check_agent_status() == "initialized"
        finally:
            patcher.stop()

    def test_check_agent_status_pending(self):
        """Agent 状态为 pending 时应返回 pending"""
        patcher, mock_session, agent = _make_fixture(agent_status="pending")
        try:
            from services.chat_service import ChatService
            svc = ChatService(agent_hash="abcd", channel="api")
            assert svc._check_agent_status() == "pending"
        finally:
            patcher.stop()

    def test_check_agent_status_unknown(self):
        """Agent 不存在时应返回 unknown"""
        patcher = patch("services.chat_service.get_session")
        mock_get_session = patcher.start()
        mock_session = MagicMock()
        mock_get_session.return_value.__enter__.return_value = mock_session
        # first call (user_id during __init__) returns agent, second returns None
        agent = MagicMock()
        agent.user_id = 42
        mock_session.query.return_value.filter.return_value.first.side_effect = [agent, None]
        try:
            from services.chat_service import ChatService
            svc = ChatService(agent_hash="abcd", channel="api")
            assert svc._check_agent_status() == "unknown"
        finally:
            patcher.stop()


class TestBuildMessages:
    """_build_messages 测试"""

    def _make_service(self):
        patcher, mock_session, agent = _make_fixture()
        from services.chat_service import ChatService
        svc = ChatService(agent_hash="abcd", channel="api")
        svc.context.history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        return patcher, svc

    def test_build_messages_text_only(self):
        """纯文本消息应包含 system + history + user"""
        patcher, svc = self._make_service()
        try:
            msgs = svc._build_messages("system prompt", "user text")
            assert len(msgs) == 4
            assert msgs[0]["role"] == "system"
            assert msgs[0]["content"] == "system prompt"
            assert msgs[1]["role"] == "user"
            assert msgs[1]["content"] == "hi"
            assert msgs[2]["role"] == "assistant"
            assert msgs[3]["role"] == "user"
            assert msgs[3]["content"] == "user text"
        finally:
            patcher.stop()

    def test_build_messages_with_image(self):
        """含图片的消息应构造多模态 content 数组"""
        patcher, svc = self._make_service()
        try:
            msgs = svc._build_messages("system prompt", "user text", image_url="http://img.url")
            assert len(msgs) == 4
            user_msg = msgs[3]
            assert user_msg["role"] == "user"
            assert isinstance(user_msg["content"], list)
            assert user_msg["content"][0]["type"] == "text"
            assert user_msg["content"][1]["type"] == "image_url"
        finally:
            patcher.stop()

    def test_build_messages_injects_corrections(self):
        """pending_correction 应注入到最新 user message"""
        patcher, svc = self._make_service()
        try:
            svc._pending_correction = {
                "short_desc": "修正：不要用 tool A",
                "topic": "tool a",
                "msg_count": 5,
                "consumed": False,
            }
            msgs = svc._build_messages("system prompt", "关于 tool a 的问题")
            last_user = [m for m in msgs if m["role"] == "user"][-1]
            assert "修正：不要用 tool A" in last_user["content"]
            assert svc._pending_correction["consumed"] is True
        finally:
            patcher.stop()


class TestInjectCorrections:
    """_inject_corrections 测试"""

    def _make_service(self):
        patcher, mock_session, agent = _make_fixture()
        from services.chat_service import ChatService
        svc = ChatService(agent_hash="abcd", channel="api")
        return patcher, svc

    def test_no_pending_correction(self):
        """无 pending_correction 时不应修改消息"""
        patcher, svc = self._make_service()
        try:
            msgs = [{"role": "user", "content": "hello"}]
            result = svc._inject_corrections(msgs)
            assert result == msgs
        finally:
            patcher.stop()

    def test_correction_injected_to_user_msg(self):
        """pending_correction 应注入到匹配的 user 消息"""
        patcher, svc = self._make_service()
        try:
            svc._pending_correction = {
                "short_desc": "test fix",
                "topic": "tool",
                "msg_count": 3,
                "consumed": False,
            }
            msgs = [
                {"role": "assistant", "content": "I'll use the tool"},
                {"role": "user", "content": "use the tool now"},
            ]
            result = svc._inject_corrections(msgs)
            assert "test fix" in result[1]["content"]
        finally:
            patcher.stop()

    def test_short_topic_guard(self):
        """长度 <=2 的 topic 不应触发匹配"""
        patcher, svc = self._make_service()
        try:
            svc._pending_correction = {
                "short_desc": "fix",
                "topic": "ab",
                "msg_count": 3,
                "consumed": False,
            }
            msgs = [{"role": "user", "content": "test"}]
            result = svc._inject_corrections(msgs)
            assert result[0]["content"] == "test"
        finally:
            patcher.stop()


class TestBuildSystemPrompt:
    """build_system_prompt 测试"""

    @pytest.mark.asyncio
    async def test_uses_custom_system_prompt(self):
        """AgentProfile 有 system_prompt 时应优先使用"""
        patcher = patch("services.chat_service.get_session")
        mock_get_session = patcher.start()
        mock_session = MagicMock()
        mock_get_session.return_value.__enter__.return_value = mock_session
        agent = MagicMock()
        agent.user_id = 42
        agent.status = "initialized"
        agent.system_prompt = "自定义系统提示词"
        mock_session.query.return_value.filter.return_value.first.return_value = agent
        try:
            from services.chat_service import ChatService
            svc = ChatService(agent_hash="abcd", channel="api")
            prompt = await svc.build_system_prompt()
            assert "自定义系统提示词" in prompt
        finally:
            patcher.stop()

    @pytest.mark.asyncio
    async def test_vfs_fallback_when_no_custom_prompt(self):
        """无 custom system_prompt 时应从 VFS 读取"""
        patcher = patch("services.chat_service.get_session")
        mock_get_session = patcher.start()
        mock_session = MagicMock()
        mock_get_session.return_value.__enter__.return_value = mock_session
        agent = MagicMock()
        agent.user_id = 42
        agent.status = "initialized"
        agent.system_prompt = None
        mock_session.query.return_value.filter.return_value.first.return_value = agent
        try:
            from services.chat_service import ChatService
            svc = ChatService(agent_hash="abcd", channel="api")
            with patch.object(svc, "_read_vfs_files_async", return_value={}):
                prompt = await svc.build_system_prompt()
                assert "【当前时间（BJT）】" in prompt
        finally:
            patcher.stop()


class TestChat:
    """chat() 核心流程测试"""

    @pytest.fixture
    def mock_deps(self):
        agent = MagicMock()
        agent.user_id = 42
        agent.status = "initialized"
        agent.system_prompt = None
        return agent

    def _make_svc(self, mock_deps, pre_process_hook=None, channel="api"):
        patcher = patch("services.chat_service.get_session")
        mock_get_session = patcher.start()
        mock_session = MagicMock()
        mock_get_session.return_value.__enter__.return_value = mock_session
        mock_session.query.return_value.filter.return_value.first.return_value = mock_deps
        from services.chat_service import ChatService
        svc = ChatService(agent_hash="abcd", channel=channel, pre_process_hook=pre_process_hook)
        return patcher, svc

    @pytest.mark.asyncio
    async def test_pre_process_hook_returns_empty(self, mock_deps):
        """前置钩子返回空字符串时应静默终止"""
        async def hook(channel, meta, text):
            return ""
        patcher, svc = self._make_svc(mock_deps, pre_process_hook=hook)
        try:
            events = []
            async for evt in svc.chat(input=__import__("models.chat_input", fromlist=["ChatInput"]).ChatInput(text="hello")):
                events.append(evt)
            assert len(events) == 0
        finally:
            patcher.stop()

    @pytest.mark.asyncio
    async def test_pre_process_hook_returns_reply(self, mock_deps):
        """前置钩子返回非空字符串时应直接回复"""
        async def hook(channel, meta, text):
            return "hook reply"
        patcher, svc = self._make_svc(mock_deps, pre_process_hook=hook)
        try:
            events = []
            async for evt in svc.chat(input=__import__("models.chat_input", fromlist=["ChatInput"]).ChatInput(text="hello")):
                events.append(evt)
            assert len(events) == 2
            assert events[0].type.__class__.__name__ == "ChatEventType"
            assert events[0].content == "hook reply"
            assert events[1].type.__class__.__name__ == "ChatEventType"
        finally:
            patcher.stop()

    @pytest.mark.asyncio
    async def test_pending_agent_returns_pending_prompt(self, mock_deps):
        """Agent 状态为 pending 时应返回 pending 提示"""
        mock_deps.status = "pending"
        # Need user_id to succeed during __init__ - mock_deps has user_id=42
        patcher, svc = self._make_svc(mock_deps)
        try:
            events = []
            async for evt in svc.chat(input=__import__("models.chat_input", fromlist=["ChatInput"]).ChatInput(text="hello")):
                events.append(evt)
            from models.chat import ChatEventType
            text_events = [e for e in events if e.type == ChatEventType.TEXT and e.content and "Agent 尚未初始化" in e.content]
            assert len(text_events) == 1
        finally:
            patcher.stop()

    @pytest.mark.asyncio
    async def test_normal_chat_flow(self, mock_deps):
        """正常聊天流程应产生 TEXT + DONE 事件"""
        patcher, svc = self._make_svc(mock_deps)
        try:
            with patch.object(svc, "build_system_prompt", return_value="system prompt"):
                with patch.object(svc, "_load_history", return_value=None):
                    with patch.object(svc, "_stream_ai_response") as mock_stream:
                        async def _mock_gen(messages):
                            from models.chat import ChatEvent, ChatEventType
                            yield ChatEvent(type=ChatEventType.TEXT, content="hi")
                            yield ChatEvent(type=ChatEventType.TEXT, content=" there")
                        mock_stream.return_value = _mock_gen(None)
                        with patch.object(svc, "_save_conversation", return_value=None):
                            with patch("services.chat_service.PointService") as mock_ps:
                                mock_ps.try_deduct.return_value = True

                                from models.chat_input import ChatInput
                                events = []
                                async for evt in svc.chat(input=ChatInput(text="hello")):
                                    events.append(evt)

                                text_content = "".join(
                                    e.content for e in events
                                    if hasattr(e, 'content') and e.content
                                )
                                # Should contain "hi there" in TEXT events
                                assert "hi" in text_content or "there" in text_content
        finally:
            patcher.stop()


class TestSaveConversation:
    """_save_conversation 测试"""

    def test_save_conversation_adds_to_history(self):
        """保存对话后 history 应新增两条记录"""
        patcher, mock_session, agent = _make_fixture()
        try:
            from services.chat_service import ChatService
            svc = ChatService(agent_hash="abcd", channel="api")
            svc._save_conversation("user msg", "assistant response")
            assert len(svc.context.history) >= 2
            assert svc.context.history[-2]["role"] == "user"
            assert svc.context.history[-1]["role"] == "assistant"
        finally:
            patcher.stop()

    def test_save_conversation_db_write(self):
        """保存对话应调用 db.add 和 db.commit"""
        patcher, mock_session, agent = _make_fixture()
        try:
            from services.chat_service import ChatService
            svc = ChatService(agent_hash="abcd", channel="api")
            with patch("services.chat_service.get_session") as mock_gs:
                mock_sess = MagicMock()
                mock_gs.return_value.__enter__.return_value = mock_sess
                svc._save_conversation("user msg", "assistant response")
                assert mock_sess.add.call_count >= 2
                assert mock_sess.commit.called
        finally:
            patcher.stop()


class TestStreamAiResponse:
    """_stream_ai_response 测试"""

    def _make_service(self):
        patcher, mock_session, agent = _make_fixture()
        from services.chat_service import ChatService
        svc = ChatService(agent_hash="abcd", channel="api")
        return patcher, svc

    @pytest.mark.asyncio
    async def test_stream_token_events(self):
        """流式响应应 yield TEXT 事件"""
        patcher, svc = self._make_service()
        try:
            from services.tools.base import Step
            async def mock_chat(messages):
                yield Step(step_type="token", content="Hello")
                yield Step(step_type="token", content=" World")

            with patch.object(svc.executor, "chat_with_tools", mock_chat):
                events = []
                async for evt in svc._stream_ai_response([{"role": "user", "content": "hi"}]):
                    events.append(evt)
                from models.chat import ChatEventType
                text = "".join(e.content for e in events if e.type == ChatEventType.TEXT)
                assert text == "Hello World"
        finally:
            patcher.stop()

    @pytest.mark.asyncio
    async def test_stream_tool_call_events(self):
        """流式响应应 yield TOOL_CALL 和 TOOL_RESULT 事件"""
        patcher, svc = self._make_service()
        try:
            from services.tools.base import Step
            async def mock_chat(messages):
                yield Step(step_type="tool_call", content="🔧 exec", tool_name="web_search", tool_args={"query": "test"})
                yield Step(step_type="tool_result", content="✅ done", tool_name="web_search", tool_result="result data")

            with patch.object(svc.executor, "chat_with_tools", mock_chat):
                events = []
                async for evt in svc._stream_ai_response([{"role": "user", "content": "hi"}]):
                    events.append(evt)
                from models.chat import ChatEventType
                tool_calls = [e for e in events if e.type == ChatEventType.TOOL_CALL]
                tool_results = [e for e in events if e.type == ChatEventType.TOOL_RESULT]
                assert len(tool_calls) == 1
                assert tool_calls[0].tool_name == "web_search"
                assert len(tool_results) == 1
                assert tool_results[0].tool_result == "result data"
        finally:
            patcher.stop()

    @pytest.mark.asyncio
    async def test_stream_error_handling(self):
        """流式响应异常应 yield ERROR 事件"""
        patcher, svc = self._make_service()
        try:
            async def mock_chat(messages):
                """async generator that immediately raises"""
                raise RuntimeError("LLM error")
                yield  # unreachable, makes it an async generator function

            with patch.object(svc.executor, "chat_with_tools", mock_chat):
                events = []
                async for evt in svc._stream_ai_response([{"role": "user", "content": "hi"}]):
                    events.append(evt)
                from models.chat import ChatEventType
                errors = [e for e in events if e.type == ChatEventType.ERROR]
                assert len(errors) == 1
                assert "LLM error" in errors[0].error_message
        finally:
            patcher.stop()
