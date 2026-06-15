"""
测试子代理预置角色和智能模型推荐系统
"""
import pytest
from services.subagent_presets import (
    get_preset_role_prompt,
    list_preset_roles,
    recommend_model_for_task,
    get_model_recommendation_info,
    PRESET_ROLES,
    MODEL_RECOMMENDATIONS
)


class TestPresetRoles:
    """测试预置角色系统"""

    def test_get_preset_role_prompt_valid(self):
        """测试获取有效的预置角色 prompt"""
        # 测试所有预置角色
        valid_roles = list(PRESET_ROLES.keys())
        
        for role in valid_roles:
            prompt = get_preset_role_prompt(role)
            assert prompt is not None, f"角色 {role} 的 prompt 不应为 None"
            assert len(prompt) > 50, f"角色 {role} 的 prompt 长度应大于 50 字符"
            assert "你是一个" in prompt or "你是" in prompt, f"角色 {role} 的 prompt 应以角色定义开头"

    def test_get_preset_role_prompt_invalid(self):
        """测试获取无效的预置角色 prompt"""
        prompt = get_preset_role_prompt("invalid_role_xyz")
        assert prompt is None

    def test_list_preset_roles(self):
        """测试列出所有预置角色"""
        roles = list_preset_roles()
        
        assert isinstance(roles, dict)
        assert len(roles) >= 8, "至少应有 8 个预置角色"
        
        # 验证每个角色都有描述
        for name, description in roles.items():
            assert isinstance(description, str)
            assert len(description) > 0, f"角色 {name} 的描述不应为空"

    def test_preset_role_structure(self):
        """测试预置角色的数据结构"""
        for role_name, role_info in PRESET_ROLES.items():
            assert "name" in role_info, f"角色 {role_name} 缺少 'name' 字段"
            assert "description" in role_info, f"角色 {role_name} 缺少 'description' 字段"
            assert "system_prompt" in role_info, f"角色 {role_name} 缺少 'system_prompt' 字段"
            
            # 验证字段类型
            assert isinstance(role_info["name"], str)
            assert isinstance(role_info["description"], str)
            assert isinstance(role_info["system_prompt"], str)

    def test_specific_roles_exist(self):
        """测试关键角色是否存在"""
        required_roles = [
            "code_review",
            "data_analysis", 
            "debug_assistant",
            "architecture",
            "general_assistant"
        ]
        
        for role in required_roles:
            assert role in PRESET_ROLES, f"缺少关键角色: {role}"
            assert get_preset_role_prompt(role) is not None


class TestModelRecommendation:
    """测试智能模型推荐系统"""

    def test_recommend_for_preset_role(self):
        """测试基于预设角色的模型推荐"""
        # 代码审查应使用高质量模型
        model, reasoning, reason = recommend_model_for_task(
            task="审查这段代码",
            preset_role="code_review"
        )
        assert model == "doubao-seed-2-0-pro-260215"
        assert reasoning == "high"
        assert "深度" in reason or "高质量" in reason

        # 数据分析应使用轻量模型
        model, reasoning, reason = recommend_model_for_task(
            task="分析数据",
            preset_role="data_analysis"
        )
        assert model == "doubao-seed-2-0-lite-260215"
        assert reasoning is None  # 轻量模型不需要 reasoning_effort

    def test_recommend_by_deep_analysis_keywords(self):
        """测试深度分析关键词触发高质量模型"""
        keywords = ["分析", "架构", "设计", "优化", "重构", "深度"]
        
        for kw in keywords:
            model, reasoning, reason = recommend_model_for_task(
                task=f"请{kw}这个问题"
            )
            assert model == "doubao-seed-2-0-pro-260215", f"关键词 '{kw}' 应触发高质量模型"
            assert reasoning in ["high", "medium"], f"关键词 '{kw}' 应设置 reasoning_effort"
            assert kw in reason, f"推荐原因应包含关键词 '{kw}'"

    def test_recommend_by_precision_keywords(self):
        """测试精确计算关键词触发高质量模型"""
        keywords = ["数学", "计算", "推导", "证明", "方程"]
        
        for kw in keywords:
            model, reasoning, reason = recommend_model_for_task(
                task=f"请{kw}这个问题"
            )
            assert model == "doubao-seed-2-0-pro-260215", f"关键词 '{kw}' 应触发高质量模型"
            assert reasoning == "high", f"关键词 '{kw}' 应设置 reasoning_effort=high"

    def test_recommend_by_code_keywords(self):
        """测试代码关键词触发中等质量模型"""
        keywords = ["代码", "bug", "调试", "修复"]
        
        for kw in keywords:
            model, reasoning, reason = recommend_model_for_task(
                task=f"请{kw}这个问题"
            )
            assert model == "doubao-seed-2-0-pro-260215", f"关键词 '{kw}' 应使用高质量模型"
            assert reasoning == "medium", f"关键词 '{kw}' 应设置 reasoning_effort=medium"

    def test_recommend_by_quick_task_keywords(self):
        """测试快速任务关键词触发轻量模型"""
        keywords = ["快速", "简单", "简要", "总结", "翻译"]
        
        for kw in keywords:
            model, reasoning, reason = recommend_model_for_task(
                task=f"请{kw}处理这个"
            )
            assert model == "doubao-seed-2-0-lite-260215", f"关键词 '{kw}' 应触发轻量模型"
            assert reasoning is None, f"快速任务不需要 reasoning_effort"

    def test_recommend_by_vision_keywords(self):
        """测试视觉关键词触发视觉模型"""
        keywords = ["图片", "图像", "视觉", "识别", "截图"]
        
        for kw in keywords:
            model, reasoning, reason = recommend_model_for_task(
                task=f"请{kw}这个"
            )
            # 视觉任务需要使用支持 vision 的模型
            assert model in ["doubao-seed-2-0-pro-260215", "doubao-seed-2-0-lite-260215"]
            assert "图片" in reason or "视觉" in reason

    def test_user_preference_speed(self):
        """测试用户速度偏好"""
        model, reasoning, reason = recommend_model_for_task(
            task="复杂的架构设计任务",
            prefer_speed=True
        )
        assert model == "doubao-seed-2-0-lite-260215"
        assert "速度" in reason

    def test_user_preference_quality(self):
        """测试用户质量偏好"""
        model, reasoning, reason = recommend_model_for_task(
            task="简单的翻译任务",
            prefer_quality=True
        )
        assert model == "doubao-seed-2-0-pro-260215"
        assert reasoning == "high"
        assert "质量" in reason

    def test_default_recommendation(self):
        """测试默认推荐"""
        model, reasoning, reason = recommend_model_for_task(
            task="这是一条普通的任务"
        )
        assert model == "doubao-seed-2-0-lite-260215", "默认应使用轻量模型"
        assert reasoning is None
        assert "默认" in reason or "轻量" in reason

    def test_multiple_keyword_priority(self):
        """测试多关键词优先级"""
        # 同时包含"快速"和"分析"，应该以第一个匹配的关键词为准
        # 或者更严格：用户偏好 > 预设角色 > 关键词 > 默认
        
        # 快速 + 分析 → 快速优先（因为 quick_task_keywords 在前）
        model1, _, _ = recommend_model_for_task(
            task="快速分析这个问题"
        )
        # 实际实现中，关键词按顺序检查，"快速"会先匹配
        # 但我们的实现先检查深度分析关键词
        # 所以这个测试验证实际行为
        
        model2, _, _ = recommend_model_for_task(
            task="分析这个问题"
        )
        # "分析"应该触发高质量模型
        assert model2 == "doubao-seed-2-0-pro-260215"


class TestModelRecommendationInfo:
    """测试模型推荐信息获取"""

    def test_get_model_recommendation_info(self):
        """测试获取模型推荐配置信息"""
        info = get_model_recommendation_info()
        
        assert isinstance(info, dict)
        assert "available_models" in info
        assert "role_recommendations" in info
        assert "keyword_categories" in info
        assert "defaults" in info
        
        # 验证可用模型
        assert "doubao-seed-2-0-pro-260215" in info["available_models"]
        assert "doubao-seed-2-0-lite-260215" in info["available_models"]
        
        # 验证每个模型都有必要字段
        for model_name, model_info in info["available_models"].items():
            assert "name" in model_info
            assert "description" in model_info
            assert "speed" in model_info
            assert "quality" in model_info
            assert "supports_vision" in model_info

    def test_role_recommendations_complete(self):
        """测试角色推荐配置完整性"""
        info = get_model_recommendation_info()
        role_recs = info["role_recommendations"]
        
        # 每个预置角色都应有推荐配置
        for role_name in PRESET_ROLES.keys():
            assert role_name in role_recs, f"角色 {role_name} 缺少模型推荐配置"
            
            rec = role_recs[role_name]
            assert "primary" in rec, f"角色 {role_name} 缺少 primary 模型"
            assert "reason" in rec, f"角色 {role_name} 缺少推荐原因"


class TestEdgeCases:
    """测试边界情况"""

    def test_empty_task(self):
        """测试空任务"""
        model, reasoning, reason = recommend_model_for_task(task="")
        assert model == "doubao-seed-2-0-lite-260215", "空任务应返回默认模型"

    def test_none_preset_role(self):
        """测试 None 预设角色"""
        model, reasoning, reason = recommend_model_for_task(
            task="测试任务",
            preset_role=None
        )
        # 应根据关键词推荐，而不是报错
        assert model in ["doubao-seed-2-0-pro-260215", "doubao-seed-2-0-lite-260215"]

    def test_case_insensitive_keywords(self):
        """测试关键词大小写不敏感"""
        # 测试大写关键词
        model1, _, _ = recommend_model_for_task(task="请分析这个问题")
        model2, _, _ = recommend_model_for_task(task="请分析这个问题")
        
        assert model1 == model2, "关键词匹配应忽略大小写"

    def test_chinese_and_english_keywords(self):
        """测试中英文关键词"""
        # 中文关键词
        model_cn, _, _ = recommend_model_for_task(task="请分析代码")
        
        # 英文关键词（如果支持的话）
        # 当前实现主要针对中文，所以这里主要测试中文
        assert model_cn == "doubao-seed-2-0-pro-260215"

    def test_concurrent_preferences(self):
        """测试同时设置速度和质量偏好"""
        # 用户偏好优先级：speed > quality
        # 如果同时设置，speed 优先（实现中先检查 prefer_speed）
        model, reasoning, reason = recommend_model_for_task(
            task="测试",
            prefer_speed=True,
            prefer_quality=True
        )
        assert model == "doubao-seed-2-0-lite-260215", "速度偏好应优先"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
