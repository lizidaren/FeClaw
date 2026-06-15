"""
MessageCompactor 端到端功能测试

测试覆盖：
1. MessageCompactor.compact() 方法
2. pre_compact_memory_save() 是否正确执行预压缩保存
3. _skip_compact 标志是否防止递归
4. Agent 是否正确响应预压缩保存请求
5. 消息压缩效果验证
6. L2-L4 压缩管道功能测试
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime

from services.message_compactor import MessageCompactor

# 使用 pytest-anyio 支持异步测试
pytestmark = pytest.mark.anyio


class MockAgent:
    """
    模拟 Agent 实例，用于测试 pre_compact_memory_save()
    """
    def __init__(self):
        self._skip_compact = False
        self.responses = []
        
    async def chat_with_tools(self, messages):
        """
        模拟 chat_with_tools 方法
        - 返回预设的响应
        - 检查是否设置了 _skip_compact 标志
        """
        # 检查 _skip_compact 标志
        if not self._skip_compact:
            raise AssertionError("_skip_compact should be True during pre_compact_memory_save")
        
        # 返回预设响应
        for response in self.responses:
            yield MockChunk(response)


class MockChunk:
    """模拟响应块"""
    def __init__(self, content):
        self.content = content
        self.step_type = "token"


class TestMessageCompactorBasics:
    """MessageCompactor 基础功能测试"""
    
    def test_init_default_params(self):
        """测试默认初始化参数"""
        compactor = MessageCompactor()
        assert compactor.max_recent == 20
        assert compactor.max_tokens == 80000
        assert compactor.compression_ratio == 0.3
        assert compactor.summary_provider == "deepseek"
        assert compactor.summary_model == "deepseek-v4-flash"
    
    def test_init_custom_params(self):
        """测试自定义初始化参数"""
        compactor = MessageCompactor(
            max_recent=30,
            max_tokens=100000,
            compression_ratio=0.2,
            summary_provider="kimi",
            summary_model="moonshot-v1-8k"
        )
        assert compactor.max_recent == 30
        assert compactor.max_tokens == 100000
        assert compactor.compression_ratio == 0.2
        assert compactor.summary_provider == "kimi"
    
    def test_estimate_tokens_empty(self):
        """测试空消息列表的 token 估算"""
        compactor = MessageCompactor()
        assert compactor._estimate_tokens([]) == 0
    
    def test_estimate_tokens_text(self):
        """测试纯文本消息的 token 估算"""
        compactor = MessageCompactor()
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"}
        ]
        tokens = compactor._estimate_tokens(messages)
        # 英文约 4 chars/token
        assert tokens > 0
        assert tokens < 20  # 简单估算
    
    def test_estimate_tokens_chinese(self):
        """测试中文消息的 token 估算"""
        compactor = MessageCompactor()
        messages = [
            {"role": "user", "content": "你好世界"},
            {"role": "assistant", "content": "欢迎"}
        ]
        tokens = compactor._estimate_tokens(messages)
        # 中文约 2 chars/token
        assert tokens > 0
    
    def test_estimate_tokens_multimodal(self):
        """测试多模态消息的 token 估算（包含图片）"""
        compactor = MessageCompactor()
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image", "url": "http://example.com/image.png"}
            ]}
        ]
        tokens = compactor._estimate_tokens(messages)
        # 图片估算为 1000 tokens
        assert tokens >= 1000


class TestCompactBasicFlow:
    """compact() 方法基本流程测试"""
    
    @pytest.mark.asyncio
    async def test_compact_empty_messages(self):
        """测试空消息列表不压缩"""
        compactor = MessageCompactor()
        result = await compactor.compact([])
        assert result == []
    
    @pytest.mark.asyncio
    async def test_compact_short_messages(self):
        """测试短消息列表不压缩（低于阈值）"""
        compactor = MessageCompactor()
        # 10 条短消息，token 数远低于 80k
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"}
        ] * 5
        result = await compactor.compact(messages)
        # 不应该压缩，直接返回
        assert len(result) == len(messages)
    
    @pytest.mark.asyncio
    async def test_compact_preserves_system_messages(self):
        """测试 system 消息始终保留在最前面"""
        compactor = MessageCompactor()
        # 构建大量消息触发压缩（mock estimate_tokens）
        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
        ]
        # 添加大量历史消息（正确方式：逐条添加）
        for i in range(100):
            messages.append({"role": "user", "content": "Question " + str(i)})
            messages.append({"role": "assistant", "content": "Answer " + str(i)})
        
        # Mock _estimate_tokens 返回高值触发压缩
        with patch.object(compactor, '_estimate_tokens', return_value=90000):
            # Mock _summarize_group_async 避免真实 LLM 调用
            with patch.object(compactor, '_summarize_group_async', 
                              return_value={"role": "system", "content": "【历史摘要】Test summary"}):
                # Mock pre_compact_memory_save 返回成功
                with patch.object(compactor, 'pre_compact_memory_save',
                                  return_value={"saved": True, "summary": "OK"}):
                    result = await compactor.compact(messages)
        
        # system 消息应该在最前面
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "You are a helpful assistant"


class TestPreCompactMemorySave:
    """pre_compact_memory_save() 方法测试"""
    
    @pytest.mark.asyncio
    async def test_pre_compact_no_agent(self):
        """测试无 agent 时跳过预保存"""
        compactor = MessageCompactor()
        result = await compactor.pre_compact_memory_save([], agent=None)
        assert result["saved"] == False
        assert result["summary"] == "no_agent"
    
    @pytest.mark.asyncio
    async def test_pre_compact_sets_skip_flag(self):
        """测试 pre_compact 设置 _skip_compact 标志防止递归"""
        compactor = MessageCompactor()
        agent = MockAgent()
        agent.responses = ["已保存"]
        
        result = await compactor.pre_compact_memory_save([], agent=agent)
        
        # 标志应该在调用后恢复为 False
        assert agent._skip_compact == False
        # 应该检测到保存成功
        assert result["saved"] == True
    
    @pytest.mark.asyncio
    async def test_pre_compact_detects_save_via_text(self):
        """测试通过文本确认检测保存（基于 '已保存'）"""
        compactor = MessageCompactor()
        agent = MockAgent()
        agent.responses = ["已保存关键上下文到工作目录"]
        
        result = await compactor.pre_compact_memory_save([], agent=agent)
        
        assert result["saved"] == True
        assert "已保存" in result["summary"]
    
    @pytest.mark.asyncio
    async def test_pre_compact_detects_text_confirmation(self):
        """测试检测文本确认（'已保存'）"""
        compactor = MessageCompactor()
        agent = MockAgent()
        agent.responses = ["已保存重要信息到 memory/"]
        
        result = await compactor.pre_compact_memory_save([], agent=agent)
        
        assert result["saved"] == True
    
    @pytest.mark.asyncio
    async def test_pre_compact_detects_ok_confirmation(self):
        """测试检测旧版 OK 确认"""
        compactor = MessageCompactor()
        agent = MockAgent()
        agent.responses = ["OK"]
        
        result = await compactor.pre_compact_memory_save([], agent=agent)
        
        assert result["saved"] == True
    
    @pytest.mark.asyncio
    async def test_pre_compact_no_confirmation(self):
        """测试无确认时返回 False"""
        compactor = MessageCompactor()
        agent = MockAgent()
        agent.responses = ["我不明白你的请求"]
        
        result = await compactor.pre_compact_memory_save([], agent=agent)
        
        assert result["saved"] == False


class TestSummaryGeneration:
    """摘要生成测试"""
    
    def test_build_summary_prompt_format(self):
        """测试摘要提示词格式"""
        compactor = MessageCompactor()
        group = [
            {"role": "user", "content": "帮我分析这段代码"},
            {"role": "assistant", "content": "好的，我来分析..."},
            {"role": "user", "content": "这里有个 bug"},
        ]
        
        prompt = compactor._build_summary_prompt(group)
        
        # 检查提示词包含关键要求
        assert "用户意图和目标" in prompt
        assert "已完成的工作" in prompt
        assert "关键工具调用结果" in prompt
        assert "当前状态" in prompt
        assert "重要决策或结论" in prompt
        assert "300 字以内" in prompt
    
    def test_build_summary_prompt_truncates_long_content(self):
        """测试摘要提示词截断过长内容"""
        compactor = MessageCompactor()
        group = [
            {"role": "user", "content": "A" * 1000}  # 超过 800 字符
        ]
        
        prompt = compactor._build_summary_prompt(group)
        
        # 内容应该被截断
        assert "已截断" in prompt
        assert len(prompt) < 2000  # 整个提示词不应该太长
    
    def test_build_summary_prompt_includes_tool_info(self):
        """测试摘要提示词包含工具信息"""
        compactor = MessageCompactor()
        group = [
            {"role": "assistant", "content": "调用工具", "name": "file_read"}
        ]
        
        prompt = compactor._build_summary_prompt(group)
        
        assert "tool:file_read" in prompt


class TestIntegration:
    """集成测试"""
    
    @pytest.mark.asyncio
    async def test_full_compact_flow(self):
        """测试完整压缩流程"""
        compactor = MessageCompactor(max_recent=5)

        # 构建大量消息
        messages = [
            {"role": "system", "content": "System prompt"},
        ]
        # 添加 50 条历史消息
        for i in range(50):
            messages.append({"role": "user", "content": f"Question {i}"})
            messages.append({"role": "assistant", "content": f"Answer {i}"})

        # Mock estimate_tokens 返回高值（确保触发压缩）
        # Mock pre_compact 返回成功
        # Mock LLM 摘要
        with patch.object(compactor, '_estimate_tokens', return_value=90000):
            with patch.object(compactor, 'pre_compact_memory_save',
                              return_value={"saved": False, "summary": "no_agent"}):
                with patch.object(compactor, '_summarize_group_async',
                                  return_value={"role": "system", "content": "【历史摘要】Summary"}):
                    result = await compactor.compact(messages, agent=None)

        # 验证结果
        # 应该有：system + 压缩说明 + 摘要 + 最近消息
        assert len(result) < len(messages)  # 应该压缩了
        assert result[0]["role"] == "system"  # system 消息在最前
        # 检查是否有压缩说明或历史摘要或对话摘要
        has_compression_marker = any(
            "【上下文压缩】" in m.get("content", "") or
            "【历史摘要】" in m.get("content", "") or
            "【对话摘要】" in m.get("content", "")
            for m in result
        )
        assert has_compression_marker
    
    @pytest.mark.asyncio
    async def test_compact_with_agent_integration(self):
        """测试 compact 与 Agent 集成"""
        compactor = MessageCompactor(max_recent=10)
        agent = MockAgent()
        agent.responses = ["已保存"]
        
        messages = [
            {"role": "system", "content": "System"},
        ]
        for i in range(30):
            messages.append({"role": "user", "content": f"Q{i}"})
            messages.append({"role": "assistant", "content": f"A{i}"})
        
        # Mock estimate_tokens
        with patch.object(compactor, '_estimate_tokens', return_value=90000):
            # Mock LLM 摘要
            with patch.object(compactor, '_summarize_group_async',
                              return_value={"role": "system", "content": "【历史摘要】"}):
                result = await compactor.compact(messages, agent=agent)
        
        # agent._skip_compact 应该恢复为 False
        assert agent._skip_compact == False


# ========== L2-L4 压缩管道测试 ==========

class TestL2HistoryShear:
    """L2 历史剪切功能测试"""

    def test_l2_shear_empty_messages(self):
        """测试空消息列表"""
        compactor = MessageCompactor()
        result = compactor._l2_shear([])
        assert result == []

    def test_l2_shear_removes_duplicate_system_wrappers(self):
        """测试删除重复的 system 包装器"""
        compactor = MessageCompactor()
        messages = [
            {"role": "system", "content": "可用工具: file_read, file_write"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "system", "content": "可用工具: file_read, file_write"},  # 重复
            {"role": "user", "content": "Next question"},
        ]

        result = compactor._l2_shear(messages)

        # 应该删除重复的 system 包装器
        assert len(result) == 4
        assert result[0]["content"] == "可用工具: file_read, file_write"
        # 重复的应该被删除

    def test_l2_shear_removes_duplicate_compact_markers(self):
        """测试删除重复的压缩标记"""
        compactor = MessageCompactor()
        messages = [
            {"role": "system", "content": "【上下文压缩】摘要1"},
            {"role": "system", "content": "【上下文压缩】摘要1"},  # 重复
            {"role": "user", "content": "Question"},
        ]

        result = compactor._l2_shear(messages)

        assert len(result) == 2

    def test_l2_shear_aggressive_mode_removes_timestamp(self):
        """测试激进模式删除 timestamp"""
        compactor = MessageCompactor(shear_aggressive=True)
        messages = [
            {"role": "user", "content": "Hello", "timestamp": "2025-01-01T00:00:00"},
        ]

        result = compactor._l2_shear(messages)

        # 激进模式应该删除 timestamp
        assert "timestamp" not in result[0]

    def test_l2_shear_normal_mode_keeps_timestamp(self):
        """测试正常模式保留 timestamp"""
        compactor = MessageCompactor(shear_aggressive=False)
        messages = [
            {"role": "user", "content": "Hello", "timestamp": "2025-01-01T00:00:00"},
        ]

        result = compactor._l2_shear(messages)

        # 正常模式应该保留 timestamp
        assert "timestamp" in result[0]

    def test_l2_shear_removes_processed_tool_call_ids(self):
        """测试删除已处理的 tool_call_id"""
        compactor = MessageCompactor()
        messages = [
            {"role": "tool", "content": "Result", "tool_call_id": "call_123"},
            {"role": "tool", "content": "Result", "tool_call_id": "call_123"},  # 重复
        ]

        result = compactor._l2_shear(messages)

        # 应该删除重复的 tool_call_id
        assert len(result) == 1


class TestL3MicroCompact:
    """L3 微压缩功能测试"""

    def test_l3_micro_compact_empty_messages(self):
        """测试空消息列表"""
        compactor = MessageCompactor()
        result = compactor._l3_micro_compact([])
        assert result == []

    def test_l3_micro_compact_below_threshold(self):
        """测试消息数低于冷阈值时不压缩"""
        compactor = MessageCompactor(cold_threshold=50, hot_window=20)
        # 30 条消息，低于冷阈值
        messages = [{"role": "user", "content": f"Question {i}"} for i in range(30)]

        result = compactor._l3_micro_compact(messages)

        # 应该不压缩，直接返回
        assert len(result) == len(messages)

    def test_l3_micro_compact_above_threshold(self):
        """测试消息数超过冷阈值时进行双路径压缩"""
        compactor = MessageCompactor(cold_threshold=30, hot_window=10)
        # 60 条消息，超过冷阈值
        messages = [
            {"role": "system", "content": "System"},
        ]
        for i in range(60):
            # 使用长内容验证截断
            messages.append({"role": "user", "content": f"Question {i}: " + "x" * 300})

        result = compactor._l3_micro_compact(messages)

        # L3 只截断内容，不减少消息数量
        assert len(result) == len(messages)
        # system 消息应该保留
        assert result[0]["role"] == "system"
        # 冷路径消息内容应该被截断
        cold_msg = result[1]  # 第一条 user 消息（冷路径）
        assert len(cold_msg.get("content", "")) <= 200

    def test_l3_micro_compact_preserves_hot_window(self):
        """测试热路径窗口消息保留更多细节"""
        compactor = MessageCompactor(cold_threshold=30, hot_window=10)
        # 60 条消息
        messages = []
        for i in range(60):
            # 创建长内容
            messages.append({"role": "user", "content": f"Question {i}: " + "x" * 500})

        result = compactor._l3_micro_compact(messages)

        # 热路径消息（最近 10 条）应该保留更多内容
        # 冷路径消息应该被截断
        cold_msgs = [m for m in result[:-10] if m.get("role") == "user"]
        hot_msgs = result[-10:]

        # 冷路径消息应该被压缩（长度较短）
        for msg in cold_msgs:
            assert len(msg.get("content", "")) <= 200

    def test_l3_compress_cold_path_user_intent(self):
        """测试冷路径用户意图提取"""
        compactor = MessageCompactor()
        # 需要超过 200 字符才能触发截断
        content = "这是一个很长的用户消息，包含很多详细的内容和描述，超过了限制长度..." + "x" * 200
        result = compactor._extract_user_intent(content)

        # 应该截断到 200 字符以内
        assert len(result) <= 200
        assert "...[意图摘要]" in result

    def test_l3_compress_cold_path_tool_result(self):
        """测试冷路径工具结果提取"""
        compactor = MessageCompactor()
        content = "这是一个很长的工具结果输出，包含很多详细数据..."
        result = compactor._extract_tool_result(content)

        # 应该截断到 300 字符以内
        assert len(result) <= 300


class TestL4ContextCrash:
    """L4 上下文崩溃功能测试"""

    def test_l4_context_crash_empty_messages(self):
        """测试空消息列表"""
        compactor = MessageCompactor()
        result = compactor._l4_context_crash([])
        assert result == []

    def test_l4_context_crash_below_threshold(self):
        """测试消息数太少时不崩溃"""
        compactor = MessageCompactor()
        messages = [{"role": "user", "content": f"Q{i}"} for i in range(5)]

        result = compactor._l4_context_crash(messages)

        # 应该不崩溃，直接返回
        assert len(result) == len(messages)

    def test_l4_context_crash_groups_messages(self):
        """测试消息分组"""
        compactor = MessageCompactor()
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {"role": "tool", "content": "Result", "name": "file_read"},
        ]

        groups = compactor._group_messages_for_crash(messages)

        # 应该按 user 分组
        assert len(groups) == 2
        assert groups[0][0]["role"] == "user"
        assert groups[1][0]["role"] == "user"

    def test_l4_context_crash_crashes_groups(self):
        """测试崩溃组生成摘要"""
        compactor = MessageCompactor()
        messages = [
            {"role": "system", "content": "System"},
        ]
        # 15 条消息，超过阈值
        for i in range(15):
            messages.append({"role": "user", "content": f"Question {i}"})
            messages.append({"role": "assistant", "content": f"Answer {i}"})

        result = compactor._l4_context_crash(messages)

        # 应该进行崩溃压缩
        assert len(result) < len(messages)
        # system 消息应该保留
        assert result[0]["role"] == "system"

    def test_l4_crash_group_to_summary_format(self):
        """测试崩溃组摘要格式"""
        compactor = MessageCompactor()
        group = [
            {"role": "user", "content": "请帮我分析代码"},
            {"role": "tool", "content": "File content...", "name": "file_read"},
            {"role": "assistant", "content": "分析完成"},
        ]

        summary = compactor._crash_group_to_summary(group)

        # 应该生成摘要
        assert summary["role"] == "system"
        assert "【对话摘要】" in summary["content"]
        assert "用户" in summary["content"]
        assert "工具" in summary["content"]


class TestL2L4Integration:
    """L2-L4 压缩管道集成测试"""

    @pytest.mark.asyncio
    async def test_full_l2_l4_pipeline(self):
        """测试完整的 L2-L4 压缩管道"""
        compactor = MessageCompactor(
            cold_threshold=30,
            hot_window=10,
            max_recent=5
        )

        # 构建大量消息
        messages = [{"role": "system", "content": "System"}]
        for i in range(100):
            messages.append({"role": "user", "content": f"Question {i}: " + "x" * 200})
            messages.append({"role": "assistant", "content": f"Answer {i}: " + "y" * 200})

        # Mock estimate_tokens 返回高值触发压缩，并在 L2-L4 后仍然高值以触发 LLM 摘要
        with patch.object(compactor, '_estimate_tokens', side_effect=[90000, 90000, 90000, 90000, 90000]):
            with patch.object(compactor, 'pre_compact_memory_save',
                              return_value={"saved": False, "summary": "no_agent"}):
                with patch.object(compactor, '_summarize_group_async',
                                  return_value={"role": "system", "content": "【历史摘要】"}):
                    result = await compactor.compact(messages)

        # 应该压缩
        assert len(result) < len(messages)
        # 检查是否有压缩标记（压缩说明或历史摘要）
        compression_info = [m for m in result if
            "【上下文压缩】" in m.get("content", "") or "【历史摘要】" in m.get("content", "") or "【对话摘要】" in m.get("content", "")]
        assert len(compression_info) > 0

    @pytest.mark.asyncio
    async def test_l2_l4_preserves_system_messages(self):
        """测试 L2-L4 保留 system 消息"""
        compactor = MessageCompactor()
        messages = [
            {"role": "system", "content": "Important system prompt"},
        ]
        for i in range(20):
            messages.append({"role": "user", "content": f"Q{i}"})

        # Mock 以触发压缩
        with patch.object(compactor, '_estimate_tokens', return_value=90000):
            with patch.object(compactor, 'pre_compact_memory_save',
                              return_value={"saved": False, "summary": "no_agent"}):
                with patch.object(compactor, '_summarize_group_async',
                                  return_value={"role": "system", "content": "【历史摘要】"}):
                    result = await compactor.compact(messages)

        # system 消息应该在最前面
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "Important system prompt"

    @pytest.mark.asyncio
    async def test_compact_skip_if_tokens_sufficient(self):
        """测试 token 足够时跳过压缩"""
        compactor = MessageCompactor()
        messages = [{"role": "user", "content": "Hello"}]

        # Mock estimate_tokens 返回低值
        with patch.object(compactor, '_estimate_tokens', return_value=50000):
            result = await compactor.compact(messages)

        # 应该不压缩，直接返回
        assert result == messages

    @pytest.mark.asyncio
    async def test_compact_skip_llm_after_l2_l4(self):
        """测试 L2-L4 压缩后 tokens 足够时跳过 LLM 摘要"""
        compactor = MessageCompactor()

        messages = [{"role": "system", "content": "System"}]
        for i in range(60):
            messages.append({"role": "user", "content": f"Q{i}"})

        # Mock estimate_tokens 第一次返回高值，L2-L4 后返回低值
        call_count = 0
        def mock_estimate(msgs):
            call_count += 1
            if call_count <= 1:
                return 90000  # 触发压缩
            return 50000  # L2-L4 后足够

        with patch.object(compactor, '_estimate_tokens', side_effect=[90000, 50000, 50000, 50000]):
            with patch.object(compactor, 'pre_compact_memory_save',
                              return_value={"saved": False, "summary": "no_agent"}):
                result = await compactor.compact(messages)

        # 应该跳过 LLM 摘要
        llm_summaries = [m for m in result if "【历史摘要】" in m.get("content", "")]
        assert len(llm_summaries) == 0  # 没有 LLM 摘要


# 运行测试的命令：
# pytest tests/test_message_compactor.py -v