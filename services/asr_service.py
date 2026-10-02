"""
ASR（语音识别）服务 — 统一抽象层

通过 Model Registry 路由到不同 provider，输出归一化的 TranscriptionResult。
换 provider 只需在 model_registry.py 加一条、本文件加一个适配函数，
调用方（zentrim_pipeline）无需改动。

提供商适配函数签名:
    async def _transcribe_<provider>(audio_url: str, model_info: dict) -> TranscriptionResult
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config import settings
from models.database import SessionLocal
from services.model_registry import resolve, find_by_capability

logger = logging.getLogger(__name__)

# ── 归一化数据结构 ──────────────────────────────────────────────────

@dataclass
class TranscriptionWord:
    begin_ms: int
    end_ms: int
    text: str
    punctuation: str = ""

@dataclass
class TranscriptionSegment:
    speaker_id: int
    begin_ms: int
    end_ms: int
    text: str
    words: list[TranscriptionWord] = field(default_factory=list)

@dataclass
class TranscriptionResult:
    model: str
    provider: str
    diarization_enabled: bool
    full_text: str
    segments: list[TranscriptionSegment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "provider": self.provider,
            "diarization_enabled": self.diarization_enabled,
            "status": "completed",
            "full_text": self.full_text,
            "segments": [
                {
                    "speaker_id": seg.speaker_id,
                    "begin_ms": seg.begin_ms,
                    "end_ms": seg.end_ms,
                    "text": seg.text,
                    "words": [
                        {"begin_ms": w.begin_ms, "end_ms": w.end_ms,
                         "text": w.text, "punctuation": w.punctuation}
                        for w in seg.words
                    ],
                }
                for seg in self.segments
            ],
        }


# ── 统一入口 ───────────────────────────────────────────────────────

async def transcribe(audio_url: str) -> Optional[TranscriptionResult]:
    """
    对音频文件进行语音识别，返回归一化结果。

    Args:
        audio_url: 音频文件的公网可访问 URL（COS 预签名 URL）

    Returns:
        TranscriptionResult，失败返回 None
    """
    # 优先找支持说话人分离的模型
    model_name = find_by_capability(
        supports_asr=True, supports_diarization=True
    )
    if not model_name:
        logger.warning("[ASR] 无支持说话人分离的 ASR 模型，回退到无分离")
        model_name = find_by_capability(supports_asr=True)
    if not model_name:
        logger.error("[ASR] 无可用 ASR 模型")
        return None

    info = resolve(model_name)
    provider = info.get("provider", "")

    logger.info(
        f"[ASR] transcribe start: model={model_name} provider={provider}"
    )

    try:
        if provider == "aliyun":
            return await _transcribe_aliyun(audio_url, info)
        else:
            logger.error(f"[ASR] 未知 provider: {provider}")
            return None
    except Exception:
        logger.exception(f"[ASR] transcribe failed: model={model_name}")
        return None


# ── 阿里云 FunASR 实现 ─────────────────────────────────────────────

async def _transcribe_aliyun(audio_url: str, info: dict) -> TranscriptionResult:
    """阿里云 DashScope FunASR 录音文件识别（异步）"""

    api_key = getattr(settings, info["api_key_attr"], "") or ""
    if not api_key:
        raise ValueError(f"Missing API key: {info['api_key_attr']}")

    base_url = info["base_url"]
    endpoint = info["asr_endpoint"]
    model_name = info.get("name", "fun-asr")
    diarization = info.get("supports_diarization", True)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. 提交任务
        submit_body = {
            "model": "fun-asr",
            "input": {"file_urls": [audio_url]},
            "parameters": {
                "diarization_enabled": diarization,
            },
        }
        submit_url = f"{base_url}{endpoint}"
        resp = await client.post(submit_url, json=submit_body, headers=headers)
        if resp.status_code != 200:
            logger.error(f"[ASR] submit failed: {resp.status_code} {resp.text}")
            raise RuntimeError(f"ASR submit failed: {resp.status_code}")

        data = resp.json()
        task_id = data.get("output", {}).get("task_id")
        if not task_id:
            logger.error(f"[ASR] submit response missing task_id: {data}")
            raise RuntimeError("ASR submit: no task_id")

        logger.info(f"[ASR] task submitted: task_id={task_id}")

        # 2. 轮询任务状态
        poll_url = f"{base_url}/api/v1/tasks/{task_id}"
        max_wait = 600  # 最多等 10 分钟
        interval = 2    # 每 2 秒轮询一次
        waited = 0

        while waited < max_wait:
            await asyncio.sleep(interval)
            waited += interval

            resp = await client.get(poll_url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"[ASR] poll failed: {resp.status_code}")
                continue

            task_data = resp.json()
            output = task_data.get("output", {})
            status = output.get("task_status", "")

            if status == "SUCCEEDED":
                break
            elif status == "FAILED":
                logger.error(f"[ASR] task failed: {output}")
                raise RuntimeError(f"ASR task FAILED: {output}")

        if status != "SUCCEEDED":
            raise RuntimeError(f"ASR task timed out after {max_wait}s: status={status}")

        # 3. 下载结果 JSON
        results = output.get("results", [])
        if not results:
            raise RuntimeError("ASR task SUCCEEDED but no results")

        transcription_url = results[0].get("transcription_url")
        if not transcription_url:
            raise RuntimeError("ASR result missing transcription_url")

        resp = await client.get(transcription_url)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to download transcription: {resp.status_code}")

        raw = resp.json()

    # 4. 归一化
    return _normalize_aliyun(raw, model_name, diarization)


def _normalize_aliyun(
    raw: dict, model: str, diarization_enabled: bool
) -> TranscriptionResult:
    """将阿里云 FunASR 原始返回 JSON 归一化为 TranscriptionResult"""

    transcripts = raw.get("transcripts", [])
    segments: list[TranscriptionSegment] = []
    all_texts: list[str] = []

    for transcript in transcripts:
        for sent in transcript.get("sentences", []):
            speaker_id = sent.get("speaker_id", 0)
            text = sent.get("text", "").strip()
            if not text:
                continue

            words = [
                TranscriptionWord(
                    begin_ms=w.get("begin_time", 0),
                    end_ms=w.get("end_time", 0),
                    text=w.get("text", ""),
                    punctuation=w.get("punctuation", ""),
                )
                for w in sent.get("words", [])
            ]

            segments.append(TranscriptionSegment(
                speaker_id=speaker_id,
                begin_ms=sent.get("begin_time", 0),
                end_ms=sent.get("end_time", 0),
                text=text,
                words=words,
            ))
            all_texts.append(text)

    return TranscriptionResult(
        model=model,
        provider="aliyun",
        diarization_enabled=diarization_enabled,
        full_text="\n".join(all_texts),
        segments=segments,
    )
