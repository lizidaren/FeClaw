"""
FeClaw ChatInput 数据模型
统一所有渠道的用户输入格式
"""

from typing import Optional, List, Dict
from pydantic import BaseModel


class Attachment(BaseModel):
    """附件模型"""
    type: str  # "image" | "file" | "voice" | "video"
    url: str  # VFS 路径
    mime_type: Optional[str] = None
    description: Optional[str] = None  # 4D 预识别回填


class ChatInput(BaseModel):
    """统一的聊天输入模型"""
    text: str
    attachments: List[Attachment] = []
    meta: Dict = {}
