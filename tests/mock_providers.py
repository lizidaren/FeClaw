"""
Mock Provider 工具，用于 LLM 服务测试

提供：
- MockLLMProvider: 实现 LLMProvider ABC，可控制返回内容和 usage
"""

from typing import AsyncGenerator, Dict, List, Optional, Any
from unittest.mock import MagicMock


class MockLLMProvider:
    """模拟 LLMProvider，不继承 ABC 以便省去 abstractmethod 检查。

    Attributes:
        last_usage: 最后一次调用的 usage 数据
        chat_responses: chat() 调用时 yield 的内容列表
        last_messages: 最近一次收到的 messages 参数
    """

    def __init__(self, chat_responses: Optional[List[str]] = None):
        self.last_usage: Optional[Dict] = None
        self.chat_responses = chat_responses or ["mock response"]
        self.last_messages: Optional[List[Dict]] = None

    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        response_format: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False,
        usage_holder: Optional[List[Optional[Dict[str, Any]]]] = None,
    ) -> AsyncGenerator[str, None]:
        """Mock chat 实现，yield 预设内容"""
        self.last_messages = messages
        for chunk in self.chat_responses:
            yield chunk


class MockLLMProviderWithUsage(MockLLMProvider):
    """模拟带 usage 的 LLMProvider。

    调用后自动设置 self.last_usage。
    """

    def __init__(
        self,
        chat_responses: Optional[List[str]] = None,
        usage: Optional[Dict[str, int]] = None,
    ):
        super().__init__(chat_responses)
        self._usage = usage or {
            "prompt_tokens": 50,
            "completion_tokens": 30,
            "total_tokens": 80,
        }

    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        response_format: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False,
        usage_holder: Optional[List[Optional[Dict[str, Any]]]] = None,
    ) -> AsyncGenerator[str, None]:
        """Mock chat 实现，结束后自动设置 last_usage + 写入 usage_holder（P0.5）"""
        self.last_messages = messages
        for chunk in self.chat_responses:
            yield chunk
        self.last_usage = self._usage
        if usage_holder is not None:
            usage_holder[0] = self._usage
