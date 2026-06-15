"""
Subagent 预置角色 API 测试
"""

import unittest
from fastapi.testclient import TestClient
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app


class TestSubagentPresetsAPI(unittest.TestCase):
    """Subagent 预置角色 API 测试"""

    def setUp(self):
        self.client = TestClient(app)

    def test_get_preset_roles_list(self):
        """测试获取预置角色列表"""
        response = self.client.get("/api/subagent/roles")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("roles", data)
        self.assertIn("count", data)
        self.assertGreater(data["count"], 0)
        
        # 检查角色结构
        for role_name, role_info in data["roles"].items():
            self.assertIn("name", role_info)
            self.assertIn("description", role_info)
            # 默认不包含 system_prompt
            self.assertNotIn("system_prompt", role_info)

    def test_get_preset_roles_with_prompt(self):
        """测试获取预置角色列表（包含 system prompt）"""
        response = self.client.get("/api/subagent/roles?include_prompt=true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # 检查角色结构（包含 system_prompt）
        for role_name, role_info in data["roles"].items():
            self.assertIn("name", role_info)
            self.assertIn("description", role_info)
            self.assertIn("system_prompt", role_info)
            self.assertIsNotNone(role_info["system_prompt"])
            self.assertGreater(len(role_info["system_prompt"]), 50)

    def test_get_single_role(self):
        """测试获取单个预置角色"""
        response = self.client.get("/api/subagent/roles/code_review")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["name"], "代码审查助手")
        self.assertIn("description", data)
        self.assertIn("system_prompt", data)
        self.assertGreater(len(data["system_prompt"]), 100)

    def test_get_single_role_not_found(self):
        """测试获取不存在的预置角色"""
        response = self.client.get("/api/subagent/roles/nonexistent_role")
        self.assertEqual(response.status_code, 404)

    def test_recommend_model_post(self):
        """测试模型推荐（POST）"""
        response = self.client.post(
            "/api/subagent/recommend",
            json={
                "task": "分析这段代码的安全性并提出改进建议",
                "preset_role": "code_review",
                "prefer_speed": False,
                "prefer_quality": False
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertIn("model", data)
        self.assertIn("reasoning_effort", data)
        self.assertIn("reason", data)
        self.assertIn("preset_role_used", data)
        self.assertEqual(data["preset_role_used"], "code_review")

    def test_recommend_model_get(self):
        """测试模型推荐（GET）"""
        response = self.client.get(
            "/api/subagent/recommend",
            params={
                "task": "快速翻译这段英文",
                "prefer_speed": True
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertIn("model", data)
        self.assertIn("reason", data)
        # prefer_speed 应该返回轻量模型
        self.assertIn(data["model"], ["doubao-seed-2-0-lite-260215", "doubao-seed-2-0-lite"])

    def test_recommend_model_by_keyword(self):
        """测试基于关键词的模型推荐"""
        # 深度分析关键词
        response = self.client.get(
            "/api/subagent/recommend",
            params={"task": "分析系统架构设计方案的优劣"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("model", data)
        self.assertIn("high", data["reasoning_effort"] or "")
        
        # 快速任务关键词
        response = self.client.get(
            "/api/subagent/recommend",
            params={"task": "快速总结这篇文章的核心观点"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("model", data)

    def test_recommend_model_with_vision_keyword(self):
        """测试视觉关键词的模型推荐"""
        response = self.client.get(
            "/api/subagent/recommend",
            params={"task": "识别这张图片中的文字内容"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("model", data)
        self.assertIn("图片", data["reason"])

    def test_get_available_models(self):
        """测试获取可用模型信息"""
        response = self.client.get("/api/subagent/models")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertIn("models", data)
        self.assertIn("default_model", data)
        self.assertGreater(len(data["models"]), 0)
        
        # 检查模型结构
        for model_name, model_info in data["models"].items():
            self.assertIn("name", model_info)
            self.assertIn("description", model_info)
            self.assertIn("speed", model_info)
            self.assertIn("quality", model_info)
            self.assertIn("supports_vision", model_info)

    def test_get_full_recommendation_info(self):
        """测试获取完整推荐系统信息"""
        response = self.client.get("/api/subagent/info")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertIn("available_models", data)
        self.assertIn("role_recommendations", data)
        self.assertIn("keyword_categories", data)
        self.assertIn("defaults", data)
        
        # 检查关键词分类
        keyword_cats = data["keyword_categories"]
        self.assertIn("deep_analysis_keywords", keyword_cats)
        self.assertIn("precision_keywords", keyword_cats)
        self.assertIn("quick_task_keywords", keyword_cats)
        self.assertIn("vision_keywords", keyword_cats)
        self.assertIn("code_keywords", keyword_cats)
        
        # 检查角色推荐
        role_recs = data["role_recommendations"]
        self.assertIn("code_review", role_recs)
        self.assertIn("primary", role_recs["code_review"])
        self.assertIn("reason", role_recs["code_review"])

    def test_role_recommendations_count(self):
        """测试预置角色数量"""
        response = self.client.get("/api/subagent/roles")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # 应至少有 10 个预置角色
        self.assertGreaterEqual(data["count"], 10)
        
        # 检查关键角色存在
        roles = data["roles"]
        self.assertIn("code_review", roles)
        self.assertIn("data_analysis", roles)
        self.assertIn("general_assistant", roles)
        self.assertIn("research_assistant", roles)

    def test_empty_task_recommendation(self):
        """测试空任务描述的模型推荐"""
        response = self.client.get(
            "/api/subagent/recommend",
            params={"task": ""}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # 空任务应返回默认推荐
        self.assertIn("model", data)
        self.assertIn("默认", data["reason"])


if __name__ == "__main__":
    unittest.main()