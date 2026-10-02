"""
模型注册表 — 中央模型管理

将"模型名 → Provider + 能力"集中管理。添加新模型只需在此注册，
无需修改调用逻辑。

用法:
    info = resolve("deepseek-flash")
    info["provider"]  → "deepseek"
    info["supports_thinking"]  → True

    find_by_capability(supports_vision=True)  → "qwen3.6-35b-a3b"
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Provider 元信息（api_key 属性名 + base_url） ───

PROVIDER_META = {
    "deepseek": {
        "api_key_attr": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com"
    },
    "zhipuai": {
        "api_key_attr": "ZHIPU_API_KEY",
        "base_url": "https://open.bigmodel.cn/api/paas/v4"
    },
    "doubao": {
        "api_key_attr": "DOUBAO_API_KEY",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3"
    },
    "kimi": {
        "api_key_attr": "KIMI_API_KEY",
        "base_url": None  # 使用 settings.KIMI_BASE_URL
    },
    "injection_proxy": {
        "api_key_attr": "DEEPSEEK_API_KEY",
        "base_url": "http://127.0.0.1:58081/v1"
    },
    "qwen": {
        "api_key_attr": "QWEN_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    },
    "mimo": {
        "api_key_attr": "MIMO_API_KEY",
        "base_url": "https://api.xiaomimimo.com/v1"
    },
    "aliyun": {
        "api_key_attr": "ALIYUN_DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com"
    },
}

# ─── 模型注册表 ───

MODEL_REGISTRY = {
    # ─── DeepSeek ───
    # 主模型：DeepSeek V4.1 Flash —— 官方 GET /models 已返回 input_modalities=["text","image"]
    # ⇒ 真支持图片输入，标记 supports_vision=True（图片直通主模型，不再预识别转文字）。
    "deepseek-flash": {
        "provider": "deepseek",
        "supports_thinking": True,
        "supports_vision": True,
    },
    # back-compat 别名：线上旧名 deepseek-v4-flash 仍可用（API 静默别名到 deepseek-flash）。
    # 沿用 _alias_of 机制（与 doubao-seed-2.0-lite 一致）：漏改的引用不至于炸。
    "deepseek-v4-flash": {
        "provider": "deepseek",
        "supports_thinking": True,
        "supports_vision": True,
        "_alias_of": "deepseek-flash",
    },

    # ─── 通义千问 ───
    "qwen3.6-flash": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "qwen3.6-plus": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "qwen3.7-plus": {
        "provider": "qwen",
        "supports_thinking": True,
        "supports_vision": False,
    },
    "qwen3.7-max": {
        "provider": "qwen",
        "supports_thinking": True,
        "supports_vision": False,
    },
    "qwen3.6-35b-a3b": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": True,
    },
    "qwen3-vl-flash": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": True,
    },
    "qwen3-vl-plus": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": True,
    },
    # ─── 专用 OCR（非通用 VLM） ───
    "qwen3.5-ocr": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": True,
        "is_ocr": True,  # 专用 OCR 模型，比通用 VLM 更便宜更快
    },
    # ─── 智谱 GLM ───
    "glm-4.7": {
        "provider": "zhipuai",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "glm-4.7-flash": {
        "provider": "zhipuai",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "glm-4.6v": {
        "provider": "zhipuai",
        "supports_thinking": False,
        "supports_vision": True,
    },
    "glm-4.5-air": {
        "provider": "zhipuai",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "glm-5-turbo": {
        "provider": "zhipuai",
        "supports_thinking": True,
        "supports_vision": False,
    },
    "glm-5": {
        "provider": "zhipuai",
        "supports_thinking": True,
        "supports_vision": False,
    },
    # ─── 豆包 ───
    "doubao-seed-2-0-lite-260215": {
        "provider": "doubao",
        "supports_thinking": False,
        "supports_vision": True,
    },
    # T 阶段别名：vision.heavy.name 的稳定短名（不带日期版本号）
    "doubao-seed-2.0-lite": {
        "provider": "doubao",
        "supports_thinking": False,
        "supports_vision": True,
        "_alias_of": "doubao-seed-2-0-lite-260215",
    },
    "doubao-seed-2-1-turbo-260628": {
        "provider": "doubao",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "doubao-seed-2-1-pro-260628": {
        "provider": "doubao",
        "supports_thinking": True,
        "supports_vision": False,
    },
    "doubao-seedream-5-0-260128": {
        "provider": "doubao",
        "supports_thinking": False,
        "supports_vision": False,  # 文生图模型
    },
    # ─── Kimi ───
    "kimi-k2.5": {
        "provider": "kimi",
        "supports_thinking": True,
        "supports_vision": False,
    },
    "kimi-k2.6": {
        "provider": "kimi",
        "supports_thinking": True,
        "supports_vision": False,
    },
    # ─── 小米 MiMo ───
    "mimo-v2.5": {
        "provider": "mimo",
        "supports_thinking": True,
        "supports_vision": False,
    },
    "mimo-v2.5-pro": {
        "provider": "mimo",
        "supports_thinking": True,
        "supports_vision": False,
    },
    "mimo-v2.5-pro-ultraspeed": {
        "provider": "mimo",
        "supports_thinking": True,
        "supports_vision": False,
    },
    # ─── Embedding ───
    "text-embedding-v4": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "embedding-3": {
        "provider": "zhipuai",
        "supports_thinking": False,
        "supports_vision": False,
    },
    # ─── Rerank ───
    "qwen3-rerank": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": False,
        "rerank_url": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
    },
    # ─── ASR（语音识别） ───
    "fun-asr": {
        "provider": "aliyun",
        "supports_asr": True,
        "asr_mode": "async",
        "supports_diarization": True,
        "max_duration_seconds": 43200,  # 12h
        "asr_endpoint": "/api/v1/services/audio/asr/transcription",
    },
    "fun-asr-flash": {
        "provider": "aliyun",
        "supports_asr": True,
        "asr_mode": "sync",
        "supports_diarization": False,
        "max_duration_seconds": 300,    # 5min
        "asr_endpoint": "/api/v1/services/aigc/multimodal-generation/generation",
    },
}


def resolve(model_name: str) -> dict:
    """
    根据模型名返回 provider、能力信息以及 provider 元信息。

    Args:
        model_name: 模型名称

    Returns:
        {"provider": str, "supports_thinking": bool, "supports_vision": bool,
         "api_key_attr": str, "base_url": str|None}

    未注册的模型返回默认 provider（来自 config.py）。
    """
    info = MODEL_REGISTRY.get(model_name)
    if info:
        result = dict(info)
        provider_meta = PROVIDER_META.get(info["provider"], {})
        result["api_key_attr"] = provider_meta.get("api_key_attr")
        result["base_url"] = provider_meta.get("base_url")
        return result

    from config import settings
    logger.warning(f"Model '{model_name}' not in registry, using default provider from MAIN_TEXT_MODEL")
    main_info = MODEL_REGISTRY.get(settings.MAIN_TEXT_MODEL, {})
    provider = main_info.get("provider", "deepseek")
    provider_meta = PROVIDER_META.get(provider, {})
    return {
        "provider": provider,
        "supports_thinking": False,
        "supports_vision": False,
        "api_key_attr": provider_meta.get("api_key_attr"),
        "base_url": provider_meta.get("base_url"),
    }


def model_supports_vision(model_name: Optional[str] = None) -> bool:
    """判断模型是否支持图片输入（多模态）。

    Args:
        model_name: 模型名；None 时使用 settings.MAIN_TEXT_MODEL（主模型）。

    Returns:
        模型是否标记 supports_vision。未注册的模型按不支持处理（走预识别兜底），
        避免把文本模型误判成多模态后发图失败。
    """
    if not model_name:
        try:
            from config import settings
            model_name = settings.MAIN_TEXT_MODEL
        except Exception:
            model_name = None
    if not model_name:
        return False
    return bool(resolve(model_name).get("supports_vision", False))


def main_model_supports_vision() -> bool:
    """主模型（MAIN_TEXT_MODEL）是否支持图片输入。"""
    return model_supports_vision(None)


def resolve_rerank(rerank_model: str) -> dict:
    """
    根据 rerank 模型名返回 provider 元信息 + rerank URL。

    Args:
        rerank_model: rerank 模型名（如 "qwen3-rerank"）

    Returns:
        {"provider": str, "rerank_url": str, "api_key_attr": str}
    """
    info = resolve(rerank_model)
    rerank_url = info.get("rerank_url")
    if not rerank_url:
        logger.warning(
            f"Rerank model '{rerank_model}' has no rerank_url in registry"
        )
        return {
            "provider": "qwen",
            "rerank_url": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            "api_key_attr": "QWEN_API_KEY",
        }
    return {
        "provider": info["provider"],
        "rerank_url": rerank_url,
        "api_key_attr": info["api_key_attr"],
    }


def resolve_provider(provider_name: str) -> Optional[dict]:
    """
    根据 provider 名返回元信息（api_key_attr, base_url）。

    Returns:
        {"api_key_attr": str, "base_url": str|None} 或 None
    """
    meta = PROVIDER_META.get(provider_name)
    return dict(meta) if meta else None


def find_by_capability(*, supports_vision: Optional[bool] = None,
                       supports_thinking: Optional[bool] = None,
                       supports_asr: Optional[bool] = None,
                       supports_diarization: Optional[bool] = None) -> Optional[str]:
    """
    按能力查找第一个匹配的模型名。

    Args:
        supports_vision: 是否需要多模态能力
        supports_thinking: 是否需要深度思考能力
        supports_asr: 是否需要语音识别能力
        supports_diarization: 是否需要说话人分离能力

    Returns:
        匹配的模型名，找不到则返回 None
    """
    for name, info in MODEL_REGISTRY.items():
        if supports_vision is not None and info.get("supports_vision") != supports_vision:
            continue
        if supports_thinking is not None and info.get("supports_thinking") != supports_thinking:
            continue
        if supports_asr is not None and info.get("supports_asr") != supports_asr:
            continue
        if supports_diarization is not None and info.get("supports_diarization") != supports_diarization:
            continue
        return name
    return None


# ─── TTS Provider 元信息（api_type + api_key 属性 + base_url） ───
# api_type: "dashscope_sdk" → 用 dashscope SDK (WebSocket 流式)
#           "httpx_rest"    → 用 httpx 调 REST API

TTS_PROVIDER_META = {
    "cosyvoice": {
        "api_type": "dashscope_sdk",
        "api_key_attr": "QWEN_API_KEY",
        "voicename_cn": "阿里云 CosyVoice",
    },
    "minimax": {
        "api_type": "httpx_rest",
        "api_key_attr": "MINIMAX_API_KEY",
        "base_url": "https://api.minimaxi.com/v1/text_to_speech",
        "voicename_cn": "MiniMax 语音合成",
    },
}

# ─── TTS 模型注册表 ───
# model_id: 提供商实际使用的模型名（API 调用时传）
# voices: 音色 ID → 中文描述
# max_chars_per_segment: 单次请求最大字符数（长文本按此分段）

TTS_MODEL_REGISTRY = {
    "cosyvoice-v1": {
        "provider": "cosyvoice",
        "model_id": "cosyvoice-v1",
        "voices": {
            "longxiang": "沉稳男声",
            "longxiaoxia": "知性女声",
            "longxiaomeng": "甜美少女声",
            "longxiaowan": "温暖女声",
            "longxiaolu": "活泼女声",
            "longchen": "磁性男声",
            "longhao": "温柔男声",
            "zhitian_emo": "情感女声",
            "zhiyan_emo": "情感男声",
        },
        "max_chars_per_segment": 500,
        "supports_rate": True,
        "supports_emotion": False,
    },
    "minimax-speech-02": {
        "provider": "minimax",
        "model_id": "speech-02",
        "voices": {
            "female-shaonv": "甜美少女声",
            "female-yujie": "成熟御姐声",
            "female-tianmei": "甜美可爱声",
            "female-chengshu": "沉稳女声",
            "male-qn-qingse": "温柔青年男声",
            "male-qn-jingying": "沉稳男声",
            "male-qn-badao": "霸气男声",
            "male-qn-daxuesheng": "阳光大学生男声",
        },
        "max_chars_per_segment": 2000,
        "supports_rate": True,
    },
}


def get_active_tts_model() -> str:
    """
    从 settings 读取当前激活的 TTS 模型名。

    Returns:
        settings.TTS_MODEL 的值；若未配置或不存在则返回 "cosyvoice-v1"
    """
    try:
        from config import settings
        model = getattr(settings, "TTS_MODEL", None) or "cosyvoice-v1"
    except Exception:
        model = "cosyvoice-v1"
    return model


def resolve_tts(model_name: Optional[str] = None) -> dict:
    """
    根据 TTS 模型名返回 provider 配置 + 模型元信息 + provider 元信息。

    Args:
        model_name: TTS 模型名（默认从 settings.TTS_MODEL 读取）

    Returns:
        {
            "model_name": str,           # 注册表中的 key
            "provider": str,            # provider 名
            "api_type": str,            # "dashscope_sdk" | "httpx_rest"
            "api_key_attr": str,        # settings 上的属性名
            "base_url": str|None,       # REST 调用的 base URL
            "model_id": str,            # 调用 API 时用的 model 名
            "voices": dict,             # {voice_id: 描述}
            "max_chars_per_segment": int,
            "supports_rate": bool,
            "supports_emotion": bool,
        }

    未注册的模型回退到 cosyvoice-v1。
    """
    if not model_name:
        model_name = get_active_tts_model()

    info = TTS_MODEL_REGISTRY.get(model_name)
    if not info:
        logger.warning(
            f"TTS model '{model_name}' not in registry, falling back to 'cosyvoice-v1'"
        )
        model_name = "cosyvoice-v1"
        info = TTS_MODEL_REGISTRY[model_name]

    provider = info["provider"]
    provider_meta = TTS_PROVIDER_META.get(provider, {})

    result = {
        "model_name": model_name,
        "provider": provider,
        "api_type": provider_meta.get("api_type"),
        "api_key_attr": provider_meta.get("api_key_attr"),
        "base_url": provider_meta.get("base_url"),
        "model_id": info.get("model_id"),
        "voices": info.get("voices", {}),
        "max_chars_per_segment": info.get("max_chars_per_segment", 500),
        "supports_rate": info.get("supports_rate", False),
        "supports_emotion": info.get("supports_emotion", False),
    }
    return result


def list_tts_voices(model_name: Optional[str] = None) -> dict:
    """
    返回指定 TTS 模型可用的音色列表。

    Args:
        model_name: TTS 模型名（默认从 settings.TTS_MODEL 读取）

    Returns:
        {voice_id: 中文描述} 的 dict
    """
    return resolve_tts(model_name).get("voices", {})


def list_tts_models() -> list:
    """
    列出所有已注册的 TTS 模型名。

    Returns:
        [model_name, ...]
    """
    return list(TTS_MODEL_REGISTRY.keys())


# ════════════════════════════════════════
# Vision Model 分级配置（T 代理：拆 zentrim_pipeline 硬编码）
# ════════════════════════════════════════

@dataclass
class VisionModelConfig:
    """多模态（Vision）模型配置 — 集中管理 Speed / Heavy 两档。

    Zentrim pipeline 把视觉任务拆成两档：
      - Speed:  轻量快速多模态，用于形态判断（文档/随手拍）
      - Heavy:  重量级多模态，用于主色识别 / HTML 生成 / VLM 描述

    两者都是多模态模型（都支持图片输入），只是成本和速度不同。
    """
    name: str                # 实际模型名（注册表 key），e.g. "qwen3.6-flash"
    display_name: str        # 给人看的名称
    cost_per_call: float     # 单次调用成本（用于计费/监控）
    max_image_size_mb: int   # 单图上限
    max_tokens: int          # 单次输出 token 上限
    timeout_s: int           # HTTP 超时秒数


# 兜底默认 — 没从 env/config 读到时用这套
# 注：name 是人/代码里用的稳定标识符（不一定是 registry key — registry key 可能带日期版本号）
#     resolve() 未命中时会回退到 MAIN_TEXT_MODEL 的 provider，不阻断调用
_DEFAULT_VISION_SPEED = VisionModelConfig(
    name="qwen3.6-flash",
    display_name="Qwen3.6 Flash",
    cost_per_call=0.001,
    max_image_size_mb=20,
    max_tokens=2048,
    timeout_s=15,
)

_DEFAULT_VISION_HEAVY = VisionModelConfig(
    name="doubao-seed-2.0-lite",
    display_name="豆包 Seed 2.0 Lite",
    cost_per_call=0.01,
    max_image_size_mb=20,
    max_tokens=4096,
    timeout_s=60,
)


def _resolve_vision_config(
    configured_name: Optional[str],
    default: VisionModelConfig,
    role: str,
) -> VisionModelConfig:
    """把 settings.VISION_*_MODEL 解析成 VisionModelConfig。

    解析规则：
      1. settings 中有值 → 复用 default 的成本/超时/tokens，只换 name + display_name
         （避免每个新模型都要在 settings 里塞 6 个字段）
      2. settings 为空 → 用 default 并打 warning
      3. settings 中的名字不在 MODEL_REGISTRY → 仍接受（不强制校验，
         因为 registry 可能滞后于配置；z 阶段再统一）
    """
    if configured_name and configured_name.strip():
        # 有配置：name 用配置的，display_name 沿用 default
        if configured_name != default.name:
            logger.info(
                f"[model_registry] vision.{role} overridden: "
                f"{default.name} → {configured_name}"
            )
        from dataclasses import replace
        return replace(default, name=configured_name)

    # 没配置：兜底
    logger.warning(
        f"[model_registry] vision.{role} not configured, using fallback default: "
        f"{default.name}"
    )
    return default


def get_vision_speed() -> VisionModelConfig:
    """轻量快速多模态 — 形态判断用（文档/随手拍）"""
    try:
        from config import settings
        configured = getattr(settings, "VISION_SPEED_MODEL", None)
    except Exception:
        configured = None
    return _resolve_vision_config(configured, _DEFAULT_VISION_SPEED, "speed")


def get_vision_heavy() -> VisionModelConfig:
    """重量级多模态 — 主色识别 / HTML 生成 / VLM 描述"""
    try:
        from config import settings
        configured = getattr(settings, "VISION_HEAVY_MODEL", None)
    except Exception:
        configured = None
    return _resolve_vision_config(configured, _DEFAULT_VISION_HEAVY, "heavy")
