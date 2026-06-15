"""
共享工具 — 消息格式化

将消息列表格式化为人类可读的对话文本，供所有服务共享使用。
"""

from typing import List, Dict


def format_conversation(messages: List[Dict], user_label="用户", assistant_label="AI"):
    """将消息列表格式化为可读对话文本"""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if not isinstance(content, str):
            if isinstance(content, list):
                text_parts = [
                    item.get("text", "") for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                content = " ".join(text_parts)
            else:
                content = str(content)
        if role == "user":
            lines.append(f"{user_label}: {content}")
        elif role == "assistant":
            if len(content) > 1000:
                content = content[:1000] + "...(截断)"
            lines.append(f"{assistant_label}: {content}")
        elif role == "tool":
            preview = content[:200] + "..." if len(content) > 200 else content
            lines.append(f"[工具结果]: {preview}")
        else:
            lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)
