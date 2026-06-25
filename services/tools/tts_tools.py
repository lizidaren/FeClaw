"""
TTS Agent 工具 — 文字转语音

Agent 调用 tts 工具将文本合成为 MP3 音频文件，
保存到 VFS 后可通过分享链接（create_share_link）分享。

TTS 提供商和声音列表通过 model_registry 动态读取，
通过 settings.TTS_MODEL 切换（TTS_MODEL_REGISTRY 中注册的 key）。
"""

import os
import uuid
import logging
from typing import Optional
from services.tools.base import AgentToolsServiceBase, tool
from services.tts_client import synthesize

logger = logging.getLogger(__name__)


def _get_voice_description() -> str:
    """
    从 model_registry 动态生成工具描述中的声音列表。
    当 TTS_MODEL 切换时，Agent 看到的可用声音会自动更新。
    """
    try:
        from services.model_registry import resolve_tts
        cfg = resolve_tts()
        voices = cfg.get("voices") or {}
        provider_cn = (
            (cfg.get("voicename_cn") if "voicename_cn" in cfg else None)
            or cfg.get("provider")
        )
        model_name = cfg.get("model_name", "")
    except Exception as e:
        logger.warning(f"读取 TTS voice 列表失败: {e}")
        voices = {}
        provider_cn = "TTS"
        model_name = ""

    if not voices:
        return "可用声音：使用 provider 默认音色"

    lines = [f"可用声音（{provider_cn}，model={model_name}）："]
    for voice_id, desc in voices.items():
        lines.append(f"- {voice_id} — {desc}")
    return "\n".join(lines)


class TtsToolsMixin(AgentToolsServiceBase):

    @tool(
        description="""将给定文本合成为语音音频（MP3），保存到 agent 工作区。
用法: tts text=<文本> voice=<voice_id> rate=1.0
前置条件：无。文本尽量简短（500字以内效果好），长文本会自动分段处理。

{voices}

返回: 音频文件的 VFS 路径，可用 create_share_link 分享""",
        category="code"
    )
    async def tts(self, text: str, voice: str = "longxiaoxia", rate: float = 1.0) -> str:
        """将文字合成为语音

        Args:
            text: 要朗读的文本
            voice: 声音名称（见工具描述中的可用声音列表，来源 model_registry）
            rate: 语速 (0.5-2.0)

        Returns:
            VFS 文件路径
        """
        if not text or not text.strip():
            return "错误：text 参数不能为空"

        # 检查 API Key（从 registry 拿 api_key_attr）
        try:
            from services.model_registry import resolve_tts
            from config import settings
            cfg = resolve_tts()
            api_key_attr = cfg.get("api_key_attr") or "QWEN_API_KEY"
            api_key = getattr(settings, api_key_attr, "") or os.environ.get(api_key_attr, "")
        except Exception:
            api_key = os.environ.get("QWEN_API_KEY", "")
            api_key_attr = "QWEN_API_KEY"
        if not api_key:
            return f"错误：{api_key_attr} 未配置，请先在 .env 中设置"

        # 截断过长文本（API 限制~10000字）
        text = text.strip()[:8000]

        # 合成语音
        audio_bytes = await synthesize(text, voice=voice, rate=rate)
        if audio_bytes is None:
            return "TTS 合成失败，请检查日志"

        # 保存到 VFS workspace
        filename = f"audio_{uuid.uuid4().hex[:8]}.mp3"

        from services.file_storage import create_file_storage
        storage = create_file_storage()
        cos_key = f"feclaw/agents/{self.agent_hash}/workspace/{filename}"
        storage.put_object(cos_key, audio_bytes)

        save_path = f"/workspace/{filename}"
        logger.info(f"TTS saved: {save_path} ({len(audio_bytes)} bytes)")
        return f"✅ 语音已生成: {save_path}\n大小: {len(audio_bytes)} 字节\n可用 `create_share_link` 分享"


# ─── 模块导入时：把动态 voice 描述注入到 tts 工具的 TOOL_REGISTRY ───
# 装饰器已运行（上方），TOOL_REGISTRY["tts"] 包含占位符 {voices} 的 description。
# 在此把占位符替换为从 model_registry 读到的真实声音列表。
try:
    from services.tool_registry import TOOL_REGISTRY
    _voice_desc = _get_voice_description()
    if "tts" in TOOL_REGISTRY and "{voices}" in TOOL_REGISTRY["tts"].get("description", ""):
        new_desc = TOOL_REGISTRY["tts"]["description"].replace("{voices}", _voice_desc)
        TOOL_REGISTRY["tts"]["description"] = new_desc
        TOOL_REGISTRY["tts"]["schema"]["function"]["description"] = new_desc
        logger.info(
            f"TTS tool description injected with {_voice_desc.count(chr(10))} voice entries"
        )
except Exception as e:
    logger.warning(f"TTS voice 描述注入失败（不影响功能）: {e}")
