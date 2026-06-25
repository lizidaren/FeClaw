"""
TTS Client — 语音合成服务（多提供商路由）

通过 ModelRegistry（services/model_registry.py）进行提供商路由：
- cosyvoice (api_type=dashscope_sdk): 阿里云 CosyVoice，dashscope SDK WebSocket
- minimax  (api_type=httpx_rest):    MiniMax 语音合成，REST API

Agent 通过 tts 工具调用，结果存 VFS / COS。
"""

import os
import re
import uuid
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _resolve_api_key(api_key_attr: str) -> str:
    """从 settings 读取 API key（api_key_attr 是 settings 上的属性名）。"""
    try:
        from config import settings
        key = getattr(settings, api_key_attr, "") or ""
        if key:
            return key
    except Exception:
        pass
    # 回退到环境变量
    key = os.environ.get(api_key_attr) or ""
    return key


def _split_text(text: str, max_chars: int) -> list:
    """
    通用长文本分段：按句末标点切分，每段 ≤ max_chars 字。
    用于多提供商（段落拼接的逻辑不变）。
    """
    segments = []
    current = ""
    for part in re.split(r'(?<=[。！？\n\r])', text):
        part = part.strip()
        if not part:
            continue
        if len(current) + len(part) > max_chars:
            if current:
                segments.append(current)
            current = part
        else:
            current += part
    if current:
        segments.append(current)
    return segments or [text]


def _combine_audio_chunks(audio_chunks: list) -> Optional[bytes]:
    """
    通用多段音频拼接：单段直接返回；多段用 pydub 拼接（段间 300ms 静音）。
    """
    if not audio_chunks:
        return None
    if len(audio_chunks) == 1:
        return audio_chunks[0]

    try:
        from pydub import AudioSegment
    except ImportError:
        logger.warning("pydub 未安装，只返回第一段音频")
        return audio_chunks[0]

    combined = AudioSegment.empty()
    for chunk_data in audio_chunks:
        tmp_path = f"/tmp/tts_chunk_{uuid.uuid4().hex}.mp3"
        try:
            with open(tmp_path, "wb") as f:
                f.write(chunk_data)
            combined += AudioSegment.from_mp3(tmp_path)
            combined += AudioSegment.silent(duration=300)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    tmp_out = f"/tmp/tts_{uuid.uuid4().hex}.mp3"
    combined.export(tmp_out, format="mp3")
    with open(tmp_out, "rb") as f:
        result = f.read()
    os.remove(tmp_out)
    return result


# ─── Provider-specific 合成逻辑 ───

async def _synthesize_cosyvoice(
    segments: list,
    voice: str,
    rate: float,
    api_key: str,
    model_id: str,
) -> list:
    """CosyVoice — dashscope SDK WebSocket 流式合成。"""
    try:
        import dashscope
        from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
    except ImportError:
        logger.error("dashscope SDK 未安装，无法使用 CosyVoice TTS")
        return []

    dashscope.api_key = api_key
    audio_chunks = []

    for i, seg in enumerate(segments):
        try:
            synthesizer = SpeechSynthesizer(
                model=model_id,
                voice=voice,
                format=AudioFormat.MP3_24000HZ_MONO_256KBPS,
                speech_rate=rate,
            )
            audio = synthesizer.call(seg)
            if audio and len(audio) > 100:
                audio_chunks.append(audio)
                logger.info(f"TTS chunk {i+1}/{len(segments)}: {len(audio)} bytes")
            else:
                logger.warning(
                    f"TTS chunk {i+1} returned empty "
                    f"({len(audio) if audio else 0} bytes)"
                )
        except Exception as e:
            logger.error(f"TTS chunk {i+1} failed: {e}")
            if not audio_chunks:
                return []
    return audio_chunks


async def _synthesize_rest(
    segments: list,
    voice: str,
    rate: float,
    api_key: str,
    model_id: str,
    base_url: str,
) -> list:
    """通用 REST API 合成（MiniMax 等）。"""
    import httpx

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    audio_chunks = []

    for i, seg in enumerate(segments):
        payload = {
            "model": model_id,
            "text": seg,
            "voice_setting": {
                "voice_id": voice,
                "speed": rate,
            },
            "audio_setting": {
                "format": "mp3",
                "sample_rate": 24000,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(base_url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.error(
                    f"TTS REST chunk {i+1} HTTP {resp.status_code}: {resp.text[:200]}"
                )
                if not audio_chunks:
                    return []
                continue

            # 响应可能是 JSON (内嵌 base64) 或原始二进制；MiniMax 是 JSON
            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                import base64
                data = resp.json()
                audio_b64 = (
                    data.get("data", {}).get("audio")
                    or data.get("audio")
                    or ""
                )
                if not audio_b64:
                    logger.error(
                        f"TTS REST chunk {i+1} no audio in response: "
                        f"{str(data)[:200]}"
                    )
                    continue
                audio = base64.b64decode(audio_b64)
            else:
                audio = resp.content

            if audio and len(audio) > 100:
                audio_chunks.append(audio)
                logger.info(f"TTS chunk {i+1}/{len(segments)}: {len(audio)} bytes")
            else:
                logger.warning(
                    f"TTS chunk {i+1} returned empty "
                    f"({len(audio) if audio else 0} bytes)"
                )
        except Exception as e:
            logger.error(f"TTS REST chunk {i+1} failed: {e}")
            if not audio_chunks:
                return []
    return audio_chunks


async def synthesize(
    text: str,
    voice: str = "longxiang",
    rate: float = 1.0,
) -> Optional[bytes]:
    """调用 TTS 合成语音（多提供商路由）

    Args:
        text: 要合成的文本（自动分段，无长度限制）
        voice: 声音名称（见 model_registry.TTS_MODEL_REGISTRY[*].voices）
        rate: 语速 (0.5-2.0)

    Returns:
        bytes: MP3 音频数据，失败返回 None
    """
    from services.model_registry import resolve_tts

    text = text.strip()
    if not text:
        return None

    cfg = resolve_tts()
    api_key = _resolve_api_key(cfg["api_key_attr"])
    if not api_key:
        logger.error(f"{cfg['api_key_attr']} 未设置（.env 或环境变量）")
        return None

    # 验证 voice 是否在当前模型支持列表中
    voices = cfg.get("voices") or {}
    if voices and voice not in voices:
        fallback_voice = next(iter(voices.keys()), voice)
        logger.warning(
            f"未知声音 '{voice}'（model={cfg['model_name']}），使用默认 '{fallback_voice}'"
        )
        voice = fallback_voice

    max_chars = cfg.get("max_chars_per_segment", 500)
    segments = _split_text(text, max_chars)
    logger.info(
        f"TTS request: model={cfg['model_name']} provider={cfg['provider']} "
        f"api_type={cfg['api_type']} voice={voice} rate={rate} "
        f"text_len={len(text)} segments={len(segments)}"
    )

    api_type = cfg.get("api_type")
    model_id = cfg.get("model_id") or cfg["model_name"]
    base_url = cfg.get("base_url")

    if api_type == "dashscope_sdk":
        audio_chunks = await _synthesize_cosyvoice(
            segments, voice, rate, api_key, model_id
        )
    elif api_type == "httpx_rest":
        if not base_url:
            logger.error(
                f"TTS provider '{cfg['provider']}' 缺少 base_url"
            )
            return None
        audio_chunks = await _synthesize_rest(
            segments, voice, rate, api_key, model_id, base_url
        )
    else:
        logger.error(f"未知的 TTS api_type: {api_type}")
        return None

    return _combine_audio_chunks(audio_chunks)
