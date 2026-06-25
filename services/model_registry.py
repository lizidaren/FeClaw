"""
模型注册表 — 中央模型管理

将"模型名 → Provider + 能力"集中管理。添加新模型只需在此注册，
无需修改调用逻辑。

用法:
    info = resolve("deepseek-v4-flash")
    info["provider"]  → "deepseek"
    info["supports_thinking"]  → True

    find_by_capability(supports_vision=True)  → "qwen3.6-35b-a3b"
"""

import logging
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
    "qwen": {
        "api_key_attr": "QWEN_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    },
    "mimo": {
        "api_key_attr": "MIMO_API_KEY",
        "base_url": "https://api.xiaomimimo.com/v1"
    },
}

# ─── 模型注册表 ───

MODEL_REGISTRY = {
    # ─── DeepSeek ───
    "deepseek-v4-flash": {
        "provider": "deepseek",
        "supports_thinking": True,
        "supports_vision": False,
    },
    "deepseek-chat": {  # 兼容期别名，无深度思考
        "provider": "deepseek",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "deepseek-reasoner": {  # 兼容期别名，深度思考模式
        "provider": "deepseek",
        "supports_thinking": True,
        "supports_vision": False,
    },
    # ─── 通义千问 ───
    "qwen3.6-flash": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "qwen3.6-35b-a3b": {
        "provider": "qwen",
        "supports_thinking": False,
        "supports_vision": True,
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
    # ─── 豆包 ───
    "doubao-seed-2-0-lite-260215": {
        "provider": "doubao",
        "supports_thinking": False,
        "supports_vision": True,
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
    # ─── 小米 MiMo ───
    "mimo-v2-flash": {
        "provider": "mimo",
        "supports_thinking": False,
        "supports_vision": False,
    },
    "mimo-v2-pro": {
        "provider": "mimo",
        "supports_thinking": True,
        "supports_vision": False,
    },
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
                       supports_thinking: Optional[bool] = None) -> Optional[str]:
    """
    按能力查找第一个匹配的模型名。

    Args:
        supports_vision: 是否需要多模态能力
        supports_thinking: 是否需要深度思考能力

    Returns:
        匹配的模型名，找不到则返回 None
    """
    for name, info in MODEL_REGISTRY.items():
        if supports_vision is not None and info["supports_vision"] != supports_vision:
            continue
        if supports_thinking is not None and info["supports_thinking"] != supports_thinking:
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
    "cosyvoice-v3.5-plus": {
        "provider": "cosyvoice",
        "model_id": "cosyvoice-v3.5-plus",
        "voices": {
            "longyingxun": "年轻青涩男声",
            "longyingmu": "优雅知性女声",
            "longhuhu": "天真烂漫女童",
            "longanpei": "青少年教师女",
            "longpaopao": "飞天泡泡音",
            "longshanshan": "戏剧化童声",
            "longniuniu": "阳光男童声",
            "longwangwang": "台湾少年音",
        },
        "max_chars_per_segment": 500,
        "supports_rate": True,
        "supports_emotion": True,
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
