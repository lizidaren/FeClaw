"""
FIX-F G7 —— 会话 cookie `secure` 按「配置 / 是否 HTTPS」决定

审计（AUDIT-R1 §G7）：`.env` 里 `COOKIE_SECURE=false` 显式覆盖了自动探测，
生产 HTTPS 域名（feclaw.lizidaren.cn）下 `feclaw_jwt` 仍以 `Secure=False` 种下，
会话令牌可经明文 HTTP 传输（降级/MITM 截获）。

修复：HTTPS 请求（或 X-Forwarded-Proto: https）**永远**种 Secure cookie，
`COOKIE_SECURE=false` 只对非 HTTPS 请求生效（本地 HTTP 调试仍能拿到非 Secure cookie）。
"""
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def _seed_user(real_db, name):
    from models.database import User
    from utils.auth import hash_password

    db = real_db()
    db.add(User(username=name, password_hash=hash_password("pw123456"), salt=None, is_admin=False))
    db.commit()
    db.close()


def _login(client, name, **headers):
    return client.post(
        "/api/user/login",
        json={"username": name, "password": "pw123456"},
        headers=headers,
    )


class TestCookieSecureG7:
    def test_https_secure_even_when_cookie_secure_false(self, real_db, client, monkeypatch):
        from config import settings

        monkeypatch.setattr(settings, "COOKIE_SECURE", False)
        _seed_user(real_db, "g7https")
        resp = _login(client, "g7https", **{"x-forwarded-proto": "https"})
        assert resp.status_code == 200, resp.text
        assert "Secure" in resp.headers.get("set-cookie", "")

    def test_http_not_secure_when_cookie_secure_false(self, real_db, client, monkeypatch):
        from config import settings

        monkeypatch.setattr(settings, "COOKIE_SECURE", False)
        _seed_user(real_db, "g7http")
        resp = _login(client, "g7http")
        assert resp.status_code == 200, resp.text
        assert "Secure" not in resp.headers.get("set-cookie", "")

    def test_http_secure_when_cookie_secure_true(self, real_db, client, monkeypatch):
        from config import settings

        monkeypatch.setattr(settings, "COOKIE_SECURE", True)
        _seed_user(real_db, "g7force")
        resp = _login(client, "g7force")
        assert resp.status_code == 200, resp.text
        assert "Secure" in resp.headers.get("set-cookie", "")
