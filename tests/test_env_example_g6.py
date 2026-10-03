"""
FIX-F G6 —— `.env` 重复键 ⇒ OAuth endpoint 漂移（模板侧锁死）

审计（AUDIT-R1 §G6）：`.env` 里 `OAUTH_TOKEN_URL` / `OAUTH_USERINFO_URL` 各出现
两次（先 8081 后 58081），pydantic-settings 取后值 ⇒ 生效 58081，与 Platform 实际
监听端口（8081）漂移；一旦启用 OAuth 令牌交换会打到错误端口。

修复：`.env.example` 在 OAuth 段补上这两个端点键（单值 + 重复键告警）。本测试锁定
「模板不允许重复键」这一性质，防止模板再次漂移。（生产/本地 `.env` 不在此列 ——
gitignored，按纪律不动。）
"""
import os
import re


def _env_keys(path):
    keys = {}
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            # 去掉前导注释符（模板里的键多为注释掉的示例），再取 KEY= 左侧
            stripped = line.lstrip("#").strip()
            m = re.match(r"^([A-Z][A-Z0-9_]*)\s*=", stripped)
            if m:
                key = m.group(1)
                keys[key] = keys.get(key, 0) + 1
    return keys


def test_env_example_has_no_duplicate_keys():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    keys = _env_keys(os.path.join(root, ".env.example"))
    dups = {k: v for k, v in keys.items() if v > 1}
    assert not dups, f".env.example 存在重复键（pydantic-settings 取最后一次会漂移）: {dups}"


def test_env_example_documents_oauth_endpoints():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".env.example")) as f:
        content = f.read()
    assert "OAUTH_TOKEN_URL" in content
    assert "OAUTH_USERINFO_URL" in content
    # 告警必须明示「重复键 ⇒ 取最后一次」
    assert "重复键" in content or "最后一次" in content
