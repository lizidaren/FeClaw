"""
Subagent 预置角色系统
提供常用任务的预设 system prompt 和智能模型推荐
"""

from typing import Optional, Tuple


# ============================================================
# 向后兼容桩（原 PRESET_ROLES / MODEL_RECOMMENDATIONS 已移除）
# ============================================================

def get_preset_role_prompt(preset_role: str) -> Optional[str]:
    """[已弃用] 获取预置角色的 system prompt"""
    return None


def list_preset_roles() -> dict:
    """[已弃用] 列出所有预置角色及其描述"""
    return {}


def recommend_model_for_task(
    task: str,
    preset_role: Optional[str] = None,
    prefer_speed: bool = False,
    prefer_quality: bool = False
) -> Tuple[str, Optional[str], str]:
    """[已弃用] 根据任务内容智能推荐模型"""
    from config import settings
    return (settings.AGENT_LLM_MODEL, None, "默认模型（预置角色系统已移除）")


def get_model_recommendation_info() -> dict:
    """[已弃用] 获取模型推荐系统的配置信息"""
    return {
        "available_models": {},
        "role_recommendations": {},
        "keyword_categories": {},
        "defaults": {"primary": "", "pro": "", "reasoning_effort": None}
    }
