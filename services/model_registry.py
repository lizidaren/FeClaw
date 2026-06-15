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
