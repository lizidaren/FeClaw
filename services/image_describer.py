"""
图片快速描述服务（Pre-LLM Image Recognition）

在所有图片到达主模型之前，先用 Qwen3.6-35b-a3b 快速生成一段话描述，
附带在给主模型的提示词中，帮助主模型判断是否需要启动更强的子 Agent。

性能（2026-05-23 实测）：
- qwen3.6-35b-a3b + thinking=disabled: ~1.4s 总耗时, ~0.9s 首token, 190+ 字
- 比 qwen3-vl-flash 快 20%，生成速度翻倍（241.8 vs 112.3 tks/s）
"""

import logging
import base64
import time
from typing import Optional

import os
import asyncio
from dotenv import load_dotenv
load_dotenv()
import httpx

logger = logging.getLogger(__name__)

# VL 模型配置 — 从 registry 动态解析（provider / base_url / api_key）
from config import settings as _img_settings
from services.model_registry import resolve as _img_resolve

_img_info = _img_resolve(_img_settings.MAIN_VISION_MODEL)
QWEN_VL_MODEL = _img_settings.MAIN_VISION_MODEL
QWEN_VL_BASE = f"{_img_info['base_url']}/chat/completions"
QWEN_VL_KEY: str = os.getenv(_img_info.get("api_key_attr", ""), "")

# 快速描述提示词：简短、客观、不联想
QUICK_DESCRIBE_PROMPT = "3句话客观描述此图：图中的内容、文字信息、布局结构。不联想不评价。"

# 三维度并行预识别提示词（3D，不含意图判断，2026-05-24 经实测优化）
# 每个维度控制在3句话/150字以内，确保总耗时接近单次调用
PROMPTS_3D = {
    "场景": "3句话客观描述此图：主体内容、场景布局、判断这是什么地方或情境。不联想不评价。",
    "文字": "逐字识别图中所有可见文字，保持原始语言和格式。不要翻译，不要改写，不要解释含义。无文字则回复'无可见文字'。",
    "风格": "3句话描述此图视觉特征：画面构成、色彩光线、整体氛围。不联想不评价。",
}

# 四维度并行预识别提示词（保留向后兼容）
PROMPTS_4D = {
    **PROMPTS_3D,
    "意图": "一句话为主模型提供参考：1)图的用途（延续话题/转换话题/无上下文待确认）；2)用户需求可能性猜测。无上下文时标为待确认，可能是图片先于文字到达。",
}

# 图片大小限制（超过此大小降级处理）
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB


async def _call_qwen_vl(b64_data: str, mime: str, timeout: float) -> Optional[str]:
    """调用 Qwen VL API（当前使用 qwen3.6-35b-a3b，MoE 3B 激活参数）"""
    content = [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_data}"}},
        {"type": "text", "text": QUICK_DESCRIBE_PROMPT},
    ]
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        response = await client.post(
            QWEN_VL_BASE,
            headers={
                "Authorization": f"Bearer {QWEN_VL_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": QWEN_VL_MODEL,
                "messages": [{"role": "user", "content": content}],
                "stream": False,
                "thinking": {"type": "disabled"},  # 显式关思考，加快速度
                "max_tokens": 512,
            },
        )
    elapsed = time.time() - t0
    if response.status_code != 200:
        logger.warning(
            f"[ImageDescriber] API error: HTTP {response.status_code}, "
            f"body={response.text[:200]}"
        )
        return None
    result = response.json()
    description = (
        result.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if description:
        logger.info(f"[ImageDescriber] Describe OK ({len(description)} chars in {elapsed:.1f}s)")
    return description or None


async def describe_image(image_data: bytes, timeout: float = 15.0) -> Optional[str]:
    """
    使用 qwen3.6-35b-a3b（MoE 3B 激活参数）快速描述图片内容。

    Args:
        image_data: 原始图片字节数据
        timeout: API 超时时间（秒）

    Returns:
        描述文本字符串，失败返回 None
    """
    if not image_data:
        logger.warning("[ImageDescriber] Empty image data")
        return None
    if len(image_data) > MAX_IMAGE_BYTES:
        logger.warning(f"[ImageDescriber] Image too large ({len(image_data)} bytes), skipping")
        return None

    try:
        b64 = base64.b64encode(image_data).decode("utf-8")
        ext = _detect_image_format(image_data)
        mime = f"image/{ext}" if ext else "image/png"

        # 首次尝试：原始格式
        result = await _call_qwen_vl(b64, mime, timeout)
        if result is not None:
            return result

        # 格式被拒，转 JPEG 重试
        try:
            from io import BytesIO
            from PIL import Image as PILImage
            img = PILImage.open(BytesIO(image_data))
            buf = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=95)
            jpeg_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            logger.info("[ImageDescriber] Retrying with JPEG conversion")
            result = await _call_qwen_vl(jpeg_b64, "image/jpeg", timeout)
            if result is not None:
                return result
        except Exception as conv_err:
            logger.warning(f"[ImageDescriber] Image conversion failed: {conv_err}")

        return None

    except httpx.TimeoutException:
        logger.warning(f"[ImageDescriber] Timeout after {timeout}s")
        return None
    except Exception as e:
        logger.error(f"[ImageDescriber] Failed: {e}", exc_info=True)
        return None


async def describe_image_from_path(image_path: str, timeout: float = 15.0) -> Optional[str]:
    """从文件路径读取图片并描述。"""
    import aiofiles
    try:
        async with aiofiles.open(image_path, "rb") as f:
            data = await f.read()
        return await describe_image(data, timeout=timeout)
    except FileNotFoundError:
        logger.warning(f"[ImageDescriber] File not found: {image_path}")
        return None
    except Exception as e:
        logger.error(f"[ImageDescriber] Read file error: {e}")
        return None


async def describe_image_4d(image_data: bytes, timeout: float = 15.0, context: Optional[str] = None) -> str:
    """
    使用 4 个并行 Qwen3.6-35b-a3b 调用，从场景、文字、风格、意图与关联性四维度分析图片。
    实测：~2s 完成 4 个维度分析，300+ 字描述。

    Args:
        image_data: 原始图片字节数据
        timeout: 每个 API 调用的超时时间（秒）
        context: 对话上下文文本（可选），用于意图维度更精准的预测

    Returns:
        拼接后的多维描述文本（空字符串表示完全失败）
    """
    if not image_data or len(image_data) > MAX_IMAGE_BYTES:
        return ""

    b64 = base64.b64encode(image_data).decode("utf-8")
    ext = _detect_image_format(image_data)
    mime = f"image/{ext}" if ext else "image/png"

    async def _call_4d(prompt: str) -> str:
        """单个维度的 API 调用"""
        content = [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                resp = await client.post(
                    QWEN_VL_BASE,
                    headers={"Authorization": f"Bearer {QWEN_VL_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": QWEN_VL_MODEL,
                        "messages": [{"role": "user", "content": content}],
                        "stream": False,
                        "thinking": {"type": "disabled"},
                        "max_tokens": 256,
                    },
                )
                if resp.status_code == 200:
                    text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    if text:
                        return text
        except Exception as e:
            logger.warning(f"[ImageDescriber] 4d parallel call failed: {e}")
        return ""

    t0 = time.time()
    # 意图提示词：有上下文则注入
    intent_prompt = PROMPTS_4D["意图"]
    if context:
        intent_prompt = f"对话上下文：{context}\n\n一句话为主模型提供参考：1)图的用途（延续话题/转换话题）；2)用户需求可能性猜测。注意：图片内容若与上下文明显无关，可能是用户想转换话题。"

    results = await asyncio.gather(
        _call_4d(PROMPTS_4D["场景"]),
        _call_4d(PROMPTS_4D["文字"]),
        _call_4d(PROMPTS_4D["风格"]),
        _call_4d(intent_prompt),
        return_exceptions=True,
    )
    elapsed = time.time() - t0

    labels = ["场景", "文字", "风格", "意图与关联"]
    parts = []
    for label, result in zip(labels, results):
        if isinstance(result, str) and result:
            parts.append(f"【{label}】{result}")

    combined = "\n".join(parts)
    logger.info(f"[PERF] image_describer: 4d_parallel ({elapsed:.1f}s, {len(combined)} chars)")
    return combined


async def describe_image_3d(image_data: bytes, timeout: float = 15.0) -> str:
    """
    使用 3 个并行 Qwen3.6-35b-a3b 调用，从场景、文字、风格三维度预识别图片。
    不含意图判断，适合与 SmartRouter 并行使用。

    Args:
        image_data: 原始图片字节数据
        timeout: 每个 API 调用的超时时间（秒）

    Returns:
        拼接后的多维描述文本（空字符串表示完全失败）
    """
    if not image_data or len(image_data) > MAX_IMAGE_BYTES:
        return ""

    b64 = base64.b64encode(image_data).decode("utf-8")
    ext = _detect_image_format(image_data)
    mime = f"image/{ext}" if ext else "image/png"

    async def _call(prompt: str) -> str:
        """单个维度的 API 调用"""
        content = [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                resp = await client.post(
                    QWEN_VL_BASE,
                    headers={"Authorization": f"Bearer {QWEN_VL_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": QWEN_VL_MODEL,
                        "messages": [{"role": "user", "content": content}],
                        "stream": False,
                        "thinking": {"type": "disabled"},
                        "max_tokens": 256,
                    },
                )
                if resp.status_code == 200:
                    text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    if text:
                        return text
        except Exception as e:
            logger.warning(f"[ImageDescriber] 3d parallel call failed: {e}")
        return ""

    t0 = time.time()
    results = await asyncio.gather(
        _call(PROMPTS_3D["场景"]),
        _call(PROMPTS_3D["文字"]),
        _call(PROMPTS_3D["风格"]),
        return_exceptions=True,
    )
    elapsed = time.time() - t0

    labels = ["场景", "文字", "风格"]
    parts = []
    for label, result in zip(labels, results):
        if isinstance(result, str) and result:
            parts.append(f"【{label}】{result}")

    combined = "\n".join(parts)
    logger.info(f"[PERF] image_describer: 3d_parallel ({elapsed:.1f}s, {len(combined)} chars)")
    return combined


async def describe_image_3d_async(img_bytes: bytes, context: Optional[str] = None) -> str:
    """
    运行 3D 预识别，返回结构化描述。
    与 SmartRouter 并行使用的便捷接口。

    Args:
        img_bytes: 原始图片字节数据
        context: 对话上下文（仅用于日志，3D 不依赖上下文做意图判断）

    Returns:
        拼接后的多维描述文本
    """
    return await describe_image_3d(img_bytes, timeout=15.0)


async def describe_image_from_path_4d(image_path: str, timeout: float = 15.0) -> str:
    """从文件路径读取图片并进行 4 维度并行预识别。"""
    import aiofiles
    try:
        async with aiofiles.open(image_path, "rb") as f:
            data = await f.read()
        return await describe_image_4d(data, timeout=timeout)
    except Exception as e:
        logger.error(f"[ImageDescriber] 4D from path failed: {e}")
        return ""


def describe_image_sync(image_data: bytes, timeout: float = 15.0) -> Optional[str]:
    """同步版本。"""
    try:
        return asyncio.run(describe_image(image_data, timeout=timeout))
    except Exception as e:
        logger.error(f"[ImageDescriber] Sync describe failed: {e}")
        return None


def _detect_image_format(data: bytes) -> str:
    """通过文件魔数检测图片格式"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] in (b"\xff\xd8",):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if data[:4] == b"\x89JP\x02":
        return "jpeg2000"
    return "png"
