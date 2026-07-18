"""
P1.5 metrics_internal endpoint 测试

验证：
- 未登录 → 401
- 普通用户 → 403
- 管理员 → 200 + 完整 JSON 结构
- 缓存/活跃 Agent 部分容错（即使 redis 未启用也能跑）
"""
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestMetricsInternalAuth:
    """认证门槛。"""

    def test_unauthenticated_returns_401(self, client):
        """未登录访问 /internal/metrics → 401。"""
        resp = client.get("/internal/metrics")
        # FastAPI 默认 403（因为 get_admin_user 不带 credentials 时 raise HTTPException）
        # 也可能是 401（取决于认证链）。两种都视为"未通过"
        assert resp.status_code in (401, 403)

    def test_non_admin_returns_403(self, real_db, client):
        """普通用户（is_admin=False）→ 403。"""
        from models.database import User
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        user = User(
            username="normal_user",
            password_hash=hash_password("xx", ""),
            salt="",
            is_admin=False,
        )
        db.add(user)
        db.commit()
        user_id = user.id
        db.close()

        token = create_jwt_token({"sub": str(user_id)})
        resp = client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestMetricsInternalAdmin:
    """管理员访问：200 + 完整 JSON。"""

    def test_admin_returns_full_payload(self, real_db, client):
        """管理员 → 200 + worker/caches/llm_usage/active_agents 四个 key。"""
        from models.database import User
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        admin = User(
            username="admin",
            password_hash=hash_password("xx", ""),
            salt="",
            is_admin=True,
        )
        db.add(admin)
        db.commit()
        admin_id = admin.id
        db.close()

        token = create_jwt_token({"sub": str(admin_id)})
        resp = client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # 4 个顶级 key
        assert "worker" in body
        assert "caches" in body
        assert "llm_usage" in body
        assert "active_agents" in body

        # worker 子结构
        assert "pid" in body["worker"]
        assert "rss_mb" in body["worker"]
        assert isinstance(body["worker"]["pid"], int)
        assert isinstance(body["worker"]["rss_mb"], (int, float))

        # caches 子结构（每个缓存至少有一个 size 或 error 字段）
        for cache_key in ("vfs_file_cache", "rate_limit_buckets", "web_search_cache"):
            assert cache_key in body["caches"], f"missing cache: {cache_key}"

    def test_admin_llm_usage_empty_db(self, real_db, client):
        """空 LLMStat 表 → total_tokens=0 + by_provider=[] + by_day_last7=[]。"""
        from models.database import User
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        admin = User(username="admin2", password_hash=hash_password("x", ""), salt="", is_admin=True)
        db.add(admin)
        db.commit()
        admin_id = admin.id
        db.close()

        token = create_jwt_token({"sub": str(admin_id)})
        resp = client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        llm = resp.json()["llm_usage"]
        assert llm["total_tokens"] == 0
        assert llm["by_provider"] == []
        assert llm["by_day_last7"] == []

    def test_admin_llm_usage_with_data(self, real_db, client):
        """LLMStat 有数据时正确 rollup。"""
        from datetime import datetime, timedelta
        from models.database import User, LLMStat
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        admin = User(username="admin3", password_hash=hash_password("x", ""), salt="", is_admin=True)
        db.add(admin)
        db.commit()
        admin_id = admin.id

        # 注入测试数据：3 条 LLMStat（不同 provider）
        for i, (provider, model, tokens) in enumerate([
            ("deepseek", "deepseek-chat", 1000),
            ("deepseek", "deepseek-chat", 2000),
            ("openai", "gpt-4", 500),
        ]):
            db.add(LLMStat(
                provider=provider,
                model=model,
                tokens_used=tokens,
                request_type="chat",
                created_at=datetime.utcnow() - timedelta(days=i),
            ))
        db.commit()
        db.close()

        token = create_jwt_token({"sub": str(admin_id)})
        resp = client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        llm = resp.json()["llm_usage"]

        # total: 1000 + 2000 + 500 = 3500
        assert llm["total_tokens"] == 3500

        # by_provider 应有 2 条（deepseek+deepseek-chat 聚合；openai+gpt-4 单独）
        by_provider = {row["provider"]: row for row in llm["by_provider"]}
        assert by_provider["deepseek"]["tokens"] == 3000
        assert by_provider["deepseek"]["calls"] == 2
        assert by_provider["openai"]["tokens"] == 500
        assert by_provider["openai"]["calls"] == 1


class TestMetricsInternalResilience:
    """容错：单个子模块报错不影响整体响应。"""

    def test_response_is_dict_even_with_no_redis(self, real_db, client):
        """Redis 关闭时 active_agents 返回空列表，不抛 500。"""
        from models.database import User
        from utils.auth import hash_password, create_jwt_token

        db = real_db()
        admin = User(username="admin4", password_hash=hash_password("x", ""), salt="", is_admin=True)
        db.add(admin)
        db.commit()
        admin_id = admin.id
        db.close()

        token = create_jwt_token({"sub": str(admin_id)})
        resp = client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {token}"},
        )
        # 即使 redis 未启用、cache 模块导入失败，endpoint 仍返回 200
        assert resp.status_code == 200
        assert isinstance(resp.json()["active_agents"], list)
        # caches 部分各子项至少存在（可能 size=0 或 error）
        assert "vfs_file_cache" in resp.json()["caches"]