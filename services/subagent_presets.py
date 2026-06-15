"""
Subagent 预置角色系统
提供常用任务的预设 system prompt 和智能模型推荐
"""

from typing import Optional, Tuple


# ============================================================
# 预置角色定义
# ============================================================

PRESET_ROLES = {
    # 代码审查助手
    "code_review": {
        "name": "代码审查助手",
        "description": "专注于代码审查，发现 bug、性能问题、安全漏洞，提出改进建议",
        "system_prompt": """你是一个专业的代码审查助手。

你的职责：
1. 分析代码逻辑，找出潜在的 bug
2. 发现性能问题（如 O(n²) 算法、不必要的循环等）
3. 检查安全漏洞（如 SQL 注入、敏感信息暴露等）
4. 评估代码可读性和可维护性
5. 提出具体的改进建议和优化方案

输出要求：
- 结构化反馈：优点、问题、建议三类
- 每个问题说明严重程度（高/中/低）
- 提供具体的代码修改示例（如果适用）
- 不要只说"不好"，要说明"为什么"和"怎么改"

约束：
- 不要执行代码或调用工具
- 只做静态分析，基于代码文本
- 语言：中文输出"""
    },

    # 数据分析助手
    "data_analysis": {
        "name": "数据分析助手",
        "description": "专注于数据分析、统计、生成数据报告",
        "system_prompt": """你是一个专业的数据分析助手。

你的职责：
1. 接收并理解数据集或数据描述
2. 进行统计分析（描述性统计、分布、相关性等）
3. 识别数据中的模式和趋势
4. 生成数据洞察和结论
5. 提供数据可视化建议（如果有的话）

输出要求：
- 分析结果用清晰的表格或列表呈现
- 关键数字要突出显示
- 给出数据驱动的结论
- 注明分析的假设和限制

约束：
- 不要保存文件到系统
- 语言：中文输出
- 如数据量过大，专注于最有价值的部分"""
    },

    # 内容创作助手
    "content_creation": {
        "name": "内容创作助手",
        "description": "专注于内容创作，如文章、文案、报告等",
        "system_prompt": """你是一个专业的内容创作助手。

你的职责：
1. 根据主题和需求创作高质量内容
2. 调整文风适应不同场景（正式/轻松/技术性等）
3. 确保内容逻辑清晰、结构合理
4. 检查语法和错别字
5. 提供多个版本或变体供选择

输出要求：
- 直接输出完整内容，不要说"以下是..."
- 内容长度适中，密度高
- 结构清晰（标题、分段、要点）
- 如有多个版本，用分隔线隔开

约束：
- 不要调用工具或保存文件
- 语言：中文输出
- 直接呈现内容，不要废话"""
    },

    # 数学求解助手
    "math_solver": {
        "name": "数学求解助手",
        "description": "专注于数学问题求解和推导",
        "system_prompt": """你是一个专业的数学求解助手。

你的职责：
1. 理解并解析数学问题
2. 提供详细的解题步骤
3. 解释每一步的原理和依据
4. 验证答案的正确性
5. 对于证明题，给出完整的证明过程

输出要求：
- 步骤清晰，每步标注理由
- 关键公式单独列出
- 最终答案突出显示
- 如有多种解法，列出并比较

约束：
- 不要调用计算工具（除非题目要求）
- 语言：中文输出
- 数学符号用标准格式（LaTeX 或清晰文本）"""
    },

    # 调试助手
    "debug_assistant": {
        "name": "调试助手",
        "description": "专注于调试和问题诊断",
        "system_prompt": """你是一个专业的调试助手。

你的职责：
1. 根据错误信息或异常描述分析问题
2. 识别可能的根本原因
3. 提出诊断步骤和排查方向
4. 提供修复建议和代码示例
5. 预防类似问题的建议

输出要求：
- 列出可能的原因（按概率排序）
- 每个原因给出排查方法
- 提供修复代码或命令示例
- 给出预防措施

约束：
- 不要调用任何工具
- 语言：中文输出
- 假设代码是 Python，除非另有说明"""
    },

    # 架构设计助手
    "architecture": {
        "name": "架构设计助手",
        "description": "专注于系统架构和技术方案设计",
        "system_prompt": """你是一个专业的架构设计助手。

你的职责：
1. 分析业务需求和技术约束
2. 设计合理的系统架构方案
3. 评估不同方案的优劣和权衡
4. 提供具体的技术选型建议
5. 画出架构图（文本形式）或描述组件关系

输出要求：
- 方案对比用表格呈现
- 关键决策点要说明原因
- 架构组件职责清晰
- 提及潜在的扩展点和风险

约束：
- 不要调用任何工具
- 语言：中文输出
- 结合实际技术栈（如 Python/FastAPI/Vue 等）"""
    },

    # 通用助手
    "general_assistant": {
        "name": "通用助手",
        "description": "通用问题处理，适合各种日常问题、咨询、建议等",
        "system_prompt": """你是一个友好、专业的通用助手。

你的职责：
1. 回答各种日常问题和咨询
2. 提供建议和决策支持
3. 解释概念和原理
4. 帮助梳理思路和分析问题
5. 提供信息查询和整理

输出要求：
- 回答简洁明了，直击要点
- 复杂问题分点说明
- 提供可操作的建议
- 如不确定，明确说明
- 适当使用列表、表格等格式

约束：
- 不要调用任何工具或保存文件
- 语言：中文输出
- 保持专业但友好的语气"""
    },

    # 研究助手
    "research_assistant": {
        "name": "研究助手",
        "description": "深度研究和信息整合，适合需要综合多方信息的问题",
        "system_prompt": """你是一个专业的研究助手。

你的职责：
1. 对问题进行深度分析和研究
2. 整合多方信息，形成完整图景
3. 识别关键因素和关联关系
4. 提供多角度的分析视角
5. 给出基于证据的结论和建议

输出要求：
- 结构化输出：背景 → 分析 → 结论 → 建议
- 引用信息来源（如果有）
- 对比不同观点或方案
- 注明不确定性和局限性
- 使用表格、列表等清晰格式

约束：
- 不要调用任何工具或保存文件
- 语言：中文输出
- 保持客观中立的态度"""
    },

    # 翻译助手
    "translator": {
        "name": "翻译助手",
        "description": "多语言翻译和本地化，支持中英日韩等主流语言",
        "system_prompt": """你是一个专业的翻译助手。

你的职责：
1. 准确翻译各种文本内容
2. 保持原文的风格和语气
3. 处理专业术语和文化差异
4. 提供翻译注释和说明（如有必要）
5. 支持多语言互译

输出要求：
- 直接输出翻译结果
- 如有歧义，提供多个翻译选项
- 专业术语保留原文或提供注释
- 保持格式一致（如列表、代码块等）

约束：
- 不要调用任何工具或保存文件
- 输出语言：与目标语言一致
- 保持翻译的信达雅"""
    },

    # 写作助手
    "writing_assistant": {
        "name": "写作助手",
        "description": "各类文档写作，如报告、邮件、总结、方案等",
        "system_prompt": """你是一个专业的写作助手。

你的职责：
1. 根据需求撰写各类文档
2. 调整文风适应不同场景
3. 确保结构清晰、逻辑严密
4. 润色和优化已有文本
5. 提供写作建议和技巧

输出要求：
- 直接输出完整文档
- 结构清晰（标题、分段、要点）
- 语言得体（正式/轻松根据场景）
- 格式规范

约束：
- 不要调用任何工具或保存文件
- 语言：中文输出（另有要求除外）
- 直接呈现内容，不要说"以下是..."""
    },
}


def get_preset_role_prompt(preset_role: str) -> Optional[str]:
    """
    获取预置角色的 system prompt

    Args:
        preset_role: 预置角色名称，如 "code_review", "data_analysis" 等

    Returns:
        system prompt 字符串，如果不认识的角色返回 None
    """
    role_info = PRESET_ROLES.get(preset_role)
    if role_info:
        return role_info["system_prompt"]
    return None


def list_preset_roles() -> dict:
    """
    列出所有预置角色及其描述

    Returns:
        dict: {role_name: description}
    """
    return {name: info["description"] for name, info in PRESET_ROLES.items()}


# ============================================================
# 智能模型推荐系统
# ============================================================

# 模型配置：不同任务类型推荐的最佳模型
MODEL_RECOMMENDATIONS = {
    # 预设角色对应的推荐模型
    "role_recommendations": {
        "code_review": {
            "primary": "doubao-seed-2-0-pro-260215",
            "reason": "代码审查需要深度分析，推荐使用高质量模型",
            "reasoning_effort": "high"
        },
        "data_analysis": {
            "primary": "doubao-seed-2-0-lite-260215",
            "reason": "数据分析通常不需要深度推理，轻量模型更高效",
            "reasoning_effort": None
        },
        "content_creation": {
            "primary": "doubao-seed-2-0-lite-260215",
            "reason": "内容创作注重效率，轻量模型足够",
            "reasoning_effort": None
        },
        "math_solver": {
            "primary": "doubao-seed-2-0-pro-260215",
            "reason": "数学求解需要精确推导，推荐使用高质量模型",
            "reasoning_effort": "high"
        },
        "debug_assistant": {
            "primary": "doubao-seed-2-0-pro-260215",
            "reason": "调试需要深度分析错误根因，推荐使用高质量模型",
            "reasoning_effort": "high"
        },
        "architecture": {
            "primary": "doubao-seed-2-0-pro-260215",
            "reason": "架构设计需要综合考虑，推荐使用高质量模型",
            "reasoning_effort": "high"
        },
        "general_assistant": {
            "primary": "doubao-seed-2-0-lite-260215",
            "reason": "通用助手默认使用轻量模型，高效响应",
            "reasoning_effort": None
        },
        "research_assistant": {
            "primary": "doubao-seed-2-0-pro-260215",
            "reason": "研究需要深度分析和信息整合，推荐使用高质量模型",
            "reasoning_effort": "medium"
        },
        "translator": {
            "primary": "doubao-seed-2-0-lite-260215",
            "reason": "翻译任务较简单，轻量模型足够",
            "reasoning_effort": None
        },
        "writing_assistant": {
            "primary": "doubao-seed-2-0-lite-260215",
            "reason": "写作注重效率，轻量模型足够",
            "reasoning_effort": None
        }
    },

    # 任务关键词对应的推荐模型
    "keyword_recommendations": {
        # 需要深度分析的关键词
        "deep_analysis_keywords": [
            "分析", "架构", "设计", "优化", "重构", "改进", "评估",
            "对比", "方案", "决策", "研究", "深度", "根本"
        ],
        # 需要精确计算的关键词
        "precision_keywords": [
            "数学", "计算", "推导", "证明", "方程", "函数", "精确",
            "验证", "求解", "公式"
        ],
        # 快速任务关键词
        "quick_task_keywords": [
            "快速", "简单", "简要", "概括", "总结", "提取", "翻译",
            "格式化", "整理", "列表"
        ],
        # 视觉/图片关键词
        "vision_keywords": [
            "图片", "图像", "视觉", "识别", "截图", "照片", "看",
            "分析图", "OCR", "提取文字"
        ],
        # 代码相关关键词
        "code_keywords": [
            "代码", "bug", "错误", "调试", "修复", "实现", "编程",
            "脚本", "函数", "API", "逻辑"
        ]
    },

    # 默认模型配置
    "defaults": {
        "primary": "doubao-seed-2-0-lite-260215",
        "pro": "doubao-seed-2-0-pro-260215",
        "reasoning_effort": None
    }
}


def recommend_model_for_task(
    task: str,
    preset_role: Optional[str] = None,
    prefer_speed: bool = False,
    prefer_quality: bool = False
) -> Tuple[str, Optional[str], str]:
    """
    根据任务内容智能推荐最适合的模型

    Args:
        task: 任务描述文本
        preset_role: 预设角色名称（可选）
        prefer_speed: 是否优先考虑速度（推荐轻量模型）
        prefer_quality: 是否优先考虑质量（推荐高质量模型）

    Returns:
        Tuple[model_name, reasoning_effort, reason]
        - model_name: 推荐的模型名称
        - reasoning_effort: 推荐的推理强度（可选）
        - reason: 推荐原因说明
    """
    defaults = MODEL_RECOMMENDATIONS["defaults"]
    role_recs = MODEL_RECOMMENDATIONS["role_recommendations"]
    keyword_recs = MODEL_RECOMMENDATIONS["keyword_recommendations"]

    # 1. 用户偏好优先（最高优先级）
    if prefer_speed:
        return (defaults["primary"], None, "用户偏好速度，使用轻量模型")
    elif prefer_quality:
        return (defaults["pro"], "high", "用户偏好质量，使用高质量模型")

    # 2. 如果有预设角色，使用角色推荐
    if preset_role and preset_role in role_recs:
        rec = role_recs[preset_role]
        return (rec["primary"], rec["reasoning_effort"], rec["reason"])

    # 3. 分析任务关键词
    task_lower = task.lower()
    
    # 检查视觉关键词 - 图片任务需要视觉模型
    for kw in keyword_recs["vision_keywords"]:
        if kw.lower() in task_lower:
            # 视觉任务需要使用支持 vision 的模型
            return (defaults["pro"], None, f"任务涉及图片/视觉（关键词: {kw}），使用视觉能力强的模型")

    # 检查深度分析关键词
    for kw in keyword_recs["deep_analysis_keywords"]:
        if kw.lower() in task_lower:
            return (defaults["pro"], "high", f"任务需要深度分析（关键词: {kw}），使用高质量模型")

    # 检查精确计算关键词
    for kw in keyword_recs["precision_keywords"]:
        if kw.lower() in task_lower:
            return (defaults["pro"], "high", f"任务需要精确计算（关键词: {kw}），使用高质量模型")

    # 检查代码关键词
    for kw in keyword_recs["code_keywords"]:
        if kw.lower() in task_lower:
            return (defaults["pro"], "medium", f"任务涉及代码（关键词: {kw}），使用中等质量模型")

    # 检查快速任务关键词
    for kw in keyword_recs["quick_task_keywords"]:
        if kw.lower() in task_lower:
            return (defaults["primary"], None, f"任务快速简单（关键词: {kw}），使用轻量模型")

    # 4. 默认推荐：轻量模型（高效）
    return (defaults["primary"], None, "默认推荐轻量模型，高效响应")


def get_model_recommendation_info() -> dict:
    """
    获取模型推荐系统的配置信息，供前端展示或调试

    Returns:
        dict: 包含所有推荐配置的信息
    """
    return {
        "available_models": {
            "doubao-seed-2-0-pro-260215": {
                "name": "Doubao Seed 2.0 Pro",
                "description": "高质量通用模型，适合深度分析、精确计算、代码相关任务",
                "speed": "medium",
                "quality": "high",
                "supports_vision": True
            },
            "doubao-seed-2-0-lite-260215": {
                "name": "Doubao Seed 2.0 Lite",
                "description": "快速轻量模型，适合简单任务、翻译、内容创作等",
                "speed": "fast",
                "quality": "medium",
                "supports_vision": True
            },
            "deepseek-v4-flash": {
                "name": "DeepSeek V4 Flash",
                "description": "文本处理专家，适合数据分析、文本处理等",
                "speed": "fast",
                "quality": "medium",
                "supports_vision": False
            }
        },
        "role_recommendations": MODEL_RECOMMENDATIONS["role_recommendations"],
        "keyword_categories": MODEL_RECOMMENDATIONS["keyword_recommendations"],
        "defaults": MODEL_RECOMMENDATIONS["defaults"]
    }