"""
共享工具 — 消息格式化

将消息列表格式化为人类可读的对话文本，供所有服务共享使用。
"""

import json
from typing import Any, Dict, List


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _format_content(content: Any) -> str:
    """把 text/image/tool 等多模态 block 转为可读文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _json_text(content)

    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(_json_text(item))
            continue

        block_type = str(item.get("type") or "").lower()
        if block_type == "text":
            parts.append(str(item.get("text") or item.get("content") or ""))
        elif block_type in {"image", "image_url", "photo"}:
            label = item.get("alt") or item.get("name") or item.get("description")
            if not label:
                image_ref = item.get("url") or item.get("image_url")
                if isinstance(image_ref, dict):
                    image_ref = image_ref.get("url")
                # 避免把巨大的 base64 data URI 写进 memory prompt。
                if image_ref and not str(image_ref).startswith("data:"):
                    label = image_ref
            parts.append(f"[图片]{(' ' + str(label)) if label else ''}")
        elif block_type in {"tool", "tool_use", "tool_call"}:
            tool_name = item.get("tool_name") or item.get("name") or "未知工具"
            tool_content = item.get("content")
            if tool_content in (None, ""):
                tool_content = item.get("input", item.get("args", ""))
            parts.append(f"[工具调用 {tool_name}]: {_json_text(tool_content)}")
        elif block_type in {"tool_result", "tool_response"}:
            tool_name = item.get("tool_name") or item.get("name") or "未知工具"
            tool_content = _format_content(item.get("content", ""))
            if len(tool_content) > 500:
                tool_content = tool_content[:500] + "...(截断)"
            parts.append(f"[工具结果 {tool_name}]: {tool_content}")
        else:
            fallback = item.get("text", item.get("content"))
            parts.append(_json_text(fallback if fallback is not None else item))
    return " ".join(part for part in parts if part)


def _append_structured_metadata(content: str, msg: Dict[str, Any]) -> str:
    parts = [content] if content else []
    for image in msg.get("images") or []:
        if isinstance(image, dict):
            label = image.get("description") or image.get("name") or image.get("url")
            if label and str(label).startswith("data:"):
                label = None
        else:
            label = str(image)
        parts.append(f"[图片]{(' ' + str(label)) if label else ''}")
    for file_info in msg.get("files") or []:
        if isinstance(file_info, dict):
            label = file_info.get("name") or file_info.get("path") or "文件"
        else:
            label = str(file_info)
        parts.append(f"[文件] {label}")
    for tool_call in msg.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            parts.append(f"[工具调用]: {_json_text(tool_call)}")
            continue
        tool_name = tool_call.get("name") or tool_call.get("tool_name") or "未知工具"
        args = tool_call.get("args", tool_call.get("tool_args", ""))
        parts.append(f"[工具调用 {tool_name}]: {_json_text(args)}")
        if tool_call.get("result") is not None:
            result = _json_text(tool_call["result"])
            if len(result) > 500:
                result = result[:500] + "...(截断)"
            parts.append(f"[工具结果 {tool_name}]: {result}")
    return " ".join(parts)


def format_conversation(messages: List[Dict], user_label="用户", assistant_label="AI"):
    """将消息列表格式化为可读对话文本，并保留多模态与工具上下文。"""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = _append_structured_metadata(
            _format_content(msg.get("content", "")),
            msg,
        )
        if role == "user":
            lines.append(f"{user_label}: {content}")
        elif role == "assistant":
            if len(content) > 2000:
                content = content[:2000] + "...(截断)"
            lines.append(f"{assistant_label}: {content}")
        elif role == "tool":
            if len(content) > 500:
                content = content[:500] + "...(截断)"
            tool_name = msg.get("tool_name") or msg.get("name")
            label = f"工具结果 {tool_name}" if tool_name else "工具结果"
            lines.append(f"[{label}]: {content}")
        else:
            lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)


def format_for_memory(messages: List[Dict], user_label="用户", assistant_label="AI"):
    """语义明确的 memory 调用别名，保持旧 ``format_conversation`` API。"""
    return format_conversation(messages, user_label=user_label, assistant_label=assistant_label)
