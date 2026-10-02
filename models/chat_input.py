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
    # 面向主模型的图片数据 URL（data:image/...;base64,...）。
    # 仅用于"图片直通"：主模型 supports_vision 时把图片作为 image block 直接发，
    # 不再预识别转文字。仅进模型、不入库（_save_conversation 序列化时 exclude）。
    image_data_url: Optional[str] = None


class ChatInput(BaseModel):
    """统一的聊天输入模型"""
    text: str
    attachments: List[Attachment] = []
    meta: Dict = {}
