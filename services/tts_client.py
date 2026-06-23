"""
TTS Client — 语音合成服务

封装 MiniMax TTS API，生成 MP3 音频。
Agent 通过 tts 工具调用，结果存 VFS / COS。
"""

import os
import uuid
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

# MiniMax TTS API
TTS_API_URL = "https://api.minimaxi.com/v1/text_to_speech"

# MiniMax 声音列表（speech-01 模型）
AVAILABLE_VOICES = {
    # 中文女声
    "female-shaonv": "female-shaonv",        # 少女 — 甜美少女声
    "female-yujie": "female-yujie",          # 御姐 — 成熟知性女声
    "female-tianmei": "female-tianmei",       # 甜美 — 甜美可爱声
    "female-chengshu": "female-chengshu",    # 成熟 — 沉稳女声
    # 中文男声
    "male-qn-qingse": "male-qn-qingse",      # 青涩 — 温柔青年男声
    "male-qn-jingying": "male-qn-jingying",  # 精英 — 沉稳男声
    "male-qn-badao": "male-qn-badao",        # 霸道 — 霸气男声
    "male-qn-daxuesheng": "male-qn-daxuesheng",  # 大学生 — 阳光男声
}


def get_api_key() -> str:
    """获取 MiniMax API Key"""
    key = os.environ.get("MINIMAX_API_KEY") or ""
    if not key:
        logger.error("MINIMAX_API_KEY 未设置")
    return key


async def synthesize(
    text: str,
    voice: str = "female-zh-yue",
    rate: float = 1.0,
    format: str = "mp3",
) -> Optional[bytes]:
    """调用 MiniMax TTS API 合成语音

    Args:
        text: 要合成的文本
        voice: 声音名称
        rate: 语速 (0.5-2.0)
        format: 输出格式 (mp3/wav/pcm)

    Returns:
        bytes: 音频数据，失败返回 None
    """
    api_key = get_api_key()
    if not api_key:
        return None

    if voice not in AVAILABLE_VOICES:
        logger.warning(f"未知声音 '{voice}'，使用默认")
        voice = "female-zh-yue"

    payload = {
        "model": "speech-01",
        "text": text,
        "voice_id": voice,
        "speed": rate,
        "output_format": format,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                TTS_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 200:
                logger.info(f"TTS OK: {len(resp.content)} bytes, voice={voice}")
                return resp.content
            else:
                logger.error(f"TTS failed: {resp.status_code} {resp.text[:300]}")
                return None
    except Exception as e:
        logger.error(f"TTS request error: {e}")
        return None


async def synthesize_script(
    segments: list[dict],
    save_path: str,
    voice_a: str = "longxiaoxia",
    voice_b: str = "longxiang",
) -> bool:
    """合成多段对话脚本（双播客模式）并保存到 VFS

    每段格式: {"speaker": "A", "text": "..."}

    Args:
        segments: 对话段列表
        save_path: 保存路径（VFS 绝对路径）
        voice_a: speaker A 的声音
        voice_b: speaker B 的声音

    Returns:
        bool: 是否成功
    """
    # 需要 pydub + ffmpeg
    try:
        from pydub import AudioSegment
    except ImportError:
        logger.error("pydub 未安装，无法合成多段音频")
        return False

    mixed = AudioSegment.silent(duration=0)

    for seg in segments:
        speaker = seg.get("speaker", "A")
        text = seg.get("text", "")
        if not text.strip():
            continue

        voice = voice_a if speaker == "A" else voice_b
        audio_bytes = await synthesize(text, voice=voice)
        if audio_bytes is None:
            continue

        # 临时文件 → AudioSegment
        tmp_path = f"/tmp/tts_seg_{uuid.uuid4().hex}.mp3"
        with open(tmp_path, "wb") as f:
            f.write(audio_bytes)

        segment_audio = AudioSegment.from_mp3(tmp_path)
        os.remove(tmp_path)

        # 追加 + 0.5秒停顿
        mixed += segment_audio
        mixed += AudioSegment.silent(duration=500)

    # 导出到临时路径
    tmp_out = f"/tmp/tts_{uuid.uuid4().hex}.mp3"
    mixed.export(tmp_out, format="mp3")

    # 通过 VFS 上传到 COS
    from services.vfs.virtual_filesystem import VirtualFileSystem
    from services.filestorage import create_file_storage

    storage = create_file_storage()
    with open(tmp_out, "rb") as f:
        storage.put_object(save_path, f.read())
    os.remove(tmp_out)

    logger.info(f"Script TTS saved to {save_path}")
    return True
