"""
Subagent 预置角色 API 路由
提供预置角色列表和智能模型推荐接口
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any
from services.subagent_presets import (
    PRESET_ROLES,
    get_preset_role_prompt,
    list_preset_roles,
    recommend_model_for_task,
    get_model_recommendation_info
)

router = APIRouter(prefix="/api/subagent", tags=["Subagent Presets"])


# ============================================================
# Response Models
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


# ============================================================
# API Endpoints
# ============================================================

@router.get("/roles", response_model=PresetRolesResponse, response_model_exclude_none=True)
async def get_preset_roles(
    include_prompt: bool = Query(False, description="是否包含 system prompt")
):
    """
    获取所有预置角色列表
    
    - **include_prompt**: 是否返回每个角色的完整 system prompt（默认不返回，只返回名称和描述）
    """
    roles_dict = list_preset_roles()
    
    roles = {}
    for role_name, description in roles_dict.items():
        role_info = PresetRoleInfo(
            name=PRESET_ROLES[role_name]["name"],
            description=description
        )
        if include_prompt:
            role_info.system_prompt = get_preset_role_prompt(role_name)
        roles[role_name] = role_info
    
    return PresetRolesResponse(roles=roles, count=len(roles))


@router.get("/roles/{role_name}", response_model=PresetRoleInfo)
async def get_single_role(role_name: str):
    """
    获取单个预置角色的详细信息
    
    - **role_name**: 角色名称（如 code_review, data_analysis 等）
    """
    if role_name not in PRESET_ROLES:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Role '{role_name}' not found")
    
    role_data = PRESET_ROLES[role_name]
    return PresetRoleInfo(
        name=role_data["name"],
        description=role_data["description"],
        system_prompt=role_data["system_prompt"]
    )


@router.post("/recommend", response_model=ModelRecommendResponse)
async def recommend_model(request: ModelRecommendRequest):
    """
    根据任务内容智能推荐最适合的模型
    
    - **task**: 任务描述文本
    - **preset_role**: 预置角色名称（可选）
    - **prefer_speed**: 是否优先考虑速度（推荐轻量模型）
    - **prefer_quality**: 是否优先考虑质量（推荐高质量模型）
    """
    model, reasoning_effort, reason = recommend_model_for_task(
        task=request.task,
        preset_role=request.preset_role,
        prefer_speed=request.prefer_speed,
        prefer_quality=request.prefer_quality
    )
    
    return ModelRecommendResponse(
        model=model,
        reasoning_effort=reasoning_effort,
        reason=reason,
        preset_role_used=request.preset_role if request.preset_role else None
    )


@router.get("/recommend", response_model=ModelRecommendResponse)
async def recommend_model_get(
    task: str = Query(..., description="任务描述文本"),
    preset_role: Optional[str] = Query(None, description="预置角色名称"),
    prefer_speed: bool = Query(False, description="优先速度"),
    prefer_quality: bool = Query(False, description="优先质量")
):
    """
    根据任务内容智能推荐最适合的模型（GET 版本）
    
    - **task**: 任务描述文本（必需）
    - **preset_role**: 预置角色名称（可选）
    - **prefer_speed**: 是否优先考虑速度
    - **prefer_quality**: 是否优先考虑质量
    """
    model, reasoning_effort, reason = recommend_model_for_task(
        task=task,
        preset_role=preset_role,
        prefer_speed=prefer_speed,
        prefer_quality=prefer_quality
    )
    
    return ModelRecommendResponse(
        model=model,
        reasoning_effort=reasoning_effort,
        reason=reason,
        preset_role_used=preset_role if preset_role else None
    )


@router.get("/models", response_model=ModelsInfoResponse)
async def get_available_models():
    """
    获取所有可用的模型信息
    
    返回模型列表、速度/质量评级、是否支持视觉等信息
    """
    info = get_model_recommendation_info()
    
    models = {}
    for model_name, model_data in info["available_models"].items():
        models[model_name] = ModelInfo(
            name=model_data["name"],
            description=model_data["description"],
            speed=model_data["speed"],
            quality=model_data["quality"],
            supports_vision=model_data["supports_vision"]
        )
    
    return ModelsInfoResponse(
        models=models,
        default_model=info["defaults"]["primary"],
        default_reasoning_effort=info["defaults"]["reasoning_effort"]
    )


@router.get("/info", response_model=FullRecommendationInfoResponse)
async def get_full_recommendation_info():
    """
    获取完整的模型推荐系统配置信息
    
    包含：所有模型信息、角色推荐配置、关键词分类、默认设置
    """
    info = get_model_recommendation_info()
    
    models = {}
    for model_name, model_data in info["available_models"].items():
        models[model_name] = ModelInfo(
            name=model_data["name"],
            description=model_data["description"],
            speed=model_data["speed"],
            quality=model_data["quality"],
            supports_vision=model_data["supports_vision"]
        )
    
    role_recs = {}
    for role_name, rec_data in info["role_recommendations"].items():
        role_recs[role_name] = RoleRecommendation(
            primary=rec_data["primary"],
            reason=rec_data["reason"],
            reasoning_effort=rec_data["reasoning_effort"]
        )
    
    keyword_cats = KeywordCategory(**info["keyword_categories"])
    
    return FullRecommendationInfoResponse(
        available_models=models,
        role_recommendations=role_recs,
        keyword_categories=keyword_cats,
        defaults=info["defaults"]
    )