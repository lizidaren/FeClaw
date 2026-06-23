"""
TTS Agent 工具 — 文字转语音

Agent 调用 tts 工具将文本合成为 MP3 音频文件，
保存到 VFS 后可通过分享链接（create_share_link）分享。
"""

import os
import uuid
import logging
from typing import Optional
from services.tools.base import AgentToolsServiceBase, tool
from services.tts_client import synthesize, COSYVOICE_VOICES

logger = logging.getLogger(__name__)


class TtsToolsMixin(AgentToolsServiceBase):

    @tool(
        description="""将给定文本合成为语音音频（MP3），保存到 agent 工作区。
用法: tts text=<文本> voice=longxiaoxia rate=1.0
前置条件：无。文本尽量简短（500字以内效果好），长文本会自动分段处理。

可用声音：
- longxiaoxia（默认）— 知性女声，适合播报/朗读
- longxiang — 沉稳男声，适合讲解
- longxiaowan — 温暖女声
- longxiaomeng — 甜美少女声
- longhao — 温柔男声
- longchen — 磁性男声

返回: 音频文件的 VFS 路径，可用 create_share_link 分享""",
        category="code"
    )
    async def tts(self, text: str, voice: str = "longxiaoxia", rate: float = 1.0) -> str:
        """将文字合成为语音

        Args:
            text: 要朗读的文本
            voice: 声音名称
            rate: 语速 (0.5-2.0)

        Returns:
            VFS 文件路径
        """
        if not text or not text.strip():
            return "错误：text 参数不能为空"

        # 检查 API Key
        api_key = (
            os.environ.get("DASHSCOPE_API_KEY") or
            os.environ.get("QWEN_API_KEY") or ""
        )
        if not api_key:
            return "错误：DASHSCOPE_API_KEY 未配置，请先设置环境变量"

        # 截断过长文本（API 限制~10000字）
        text = text.strip()[:8000]

        # 合成语音
        audio_bytes = await synthesize(text, voice=voice, rate=rate)
        if audio_bytes is None:
            return "TTS 合成失败，请检查日志"

        # 保存到 VFS workspace
        filename = f"audio_{uuid.uuid4().hex[:8]}.mp3"

        from services.filestorage import create_file_storage
        storage = create_file_storage()
        cos_key = f"feclaw/agents/{self.agent_hash}/workspace/{filename}"
        storage.put_object(cos_key, audio_bytes)

        save_path = f"/workspace/{filename}"
        logger.info(f"TTS saved: {save_path} ({len(audio_bytes)} bytes)")
        return f"✅ 语音已生成: {save_path}\n大小: {len(audio_bytes)} 字节\n可用 `create_share_link` 分享"


async def init_tts_tools(service) -> None:
    """初始化 TTS 工具"""
    logger.info("TTS tools initialized")
