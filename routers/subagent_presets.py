"""
Subagent 预置角色 API 路由
提供预置角色列表和智能模型推荐接口
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any

router = APIRouter(prefix="/api/subagent", tags=["Subagent Presets"])


# ============================================================
# Response Models (保留供参考，所有端点已移除)
# ============================================================

class PresetRoleInfo(BaseModel):
    """单个预置角色的信息"""
    model_config = ConfigDict(exclude_none=True)

    name: str
    description: str
    system_prompt: Optional[str] = None


class PresetRolesResponse(BaseModel):
    """预置角色列表响应"""
    roles: Dict[str, PresetRoleInfo]
    count: int


class ModelRecommendRequest(BaseModel):
    """模型推荐请求"""
    task: str
    preset_role: Optional[str] = None
    prefer_speed: bool = False
    prefer_quality: bool = False


class ModelRecommendResponse(BaseModel):
    """模型推荐响应"""
    model: str
    reasoning_effort: Optional[str]
    reason: str
    preset_role_used: Optional[str] = None


class ModelInfo(BaseModel):
    """单个模型的信息"""
    name: str
    description: str
    speed: str
    quality: str
    supports_vision: bool


class ModelsInfoResponse(BaseModel):
    """可用模型信息响应"""
    models: Dict[str, ModelInfo]
    default_model: str
    default_reasoning_effort: Optional[str]


class RoleRecommendation(BaseModel):
    """角色对应的模型推荐"""
    primary: str
    reason: str
    reasoning_effort: Optional[str]


class KeywordCategory(BaseModel):
    """关键词分类"""
    deep_analysis_keywords: List[str]
    precision_keywords: List[str]
    quick_task_keywords: List[str]
    vision_keywords: List[str]
    code_keywords: List[str]


class FullRecommendationInfoResponse(BaseModel):
    """完整的推荐系统信息"""
    available_models: Dict[str, ModelInfo]
    role_recommendations: Dict[str, RoleRecommendation]
    keyword_categories: KeywordCategory
    defaults: Dict[str, Any]
