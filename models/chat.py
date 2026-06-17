"""
聊天事件模型 - 渠道无关的事件定义
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum
import json


class ChatEventType(Enum):
    """聊天事件类型"""
    TEXT = "text"               # 文本内容（流式片段）
    TOOL_CALL = "tool_call"     # 工具调用开始
    TOOL_RESULT = "tool_result" # 工具执行结果
    PRE_TOOL = "pre_tool"       # 工具调用前的思考
    KEEPALIVE = "keepalive"     # 工具执行中的心跳
    ERROR = "error"             # 错误
    DONE = "done"               # 对话结束
    HISTORY_LOADED = "history_loaded"  # 历史消息加载完成
    PIPELINE = "pipeline"       # 流水线状态更新（SmartRouter/预取等）
    REASONING = "reasoning"     # 深度思考推理过程
    SEARCH_PROGRESS = "search_progress"  # 搜索结果的流式内容


@dataclass
class ChatEvent:
    """聊天事件"""
    type: ChatEventType
    content: str = ""
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    tool_result: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {"type": self.type.value}
        if self.content:
            result["content"] = self.content
        if self.tool_name:
            result["tool_name"] = self.tool_name
        if self.tool_args:
            result["tool_args"] = self.tool_args
        if self.tool_result:
            result["tool_result"] = self.tool_result
        if self.error_message:
            result["error_message"] = self.error_message
        if self.metadata:
            result["metadata"] = self.metadata
        return result
    
    def to_json(self) -> str:
        """转换为 JSON"""
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class ChatContext:
    """聊天上下文"""
    user_id: str
    channel: str
    session_id: Optional[str] = None
    history: List[Dict[str, str]] = field(default_factory=list)
    current_session_messages: List[Dict[str, str]] = field(default_factory=list)
    
    def add_message(self, role: str, content: str) -> None:
        """添加消息到当前会话"""
        self.current_session_messages.append({"role": role, "content": content})
