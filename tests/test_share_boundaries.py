r"""
T17a「分享边界」验收测试（规格 §5 T17 / §9 A20）

覆盖：
1. 跨用户/跨 agent 路径拒绝；旧 `feclaw/vfs{path}`、`feclaw/user_workspaces/2/...`
   全局回退被删除（fake storage 断言**只**请求 mapping 推导出的 key）。
2. mapping 缺 owner/agent 身份、身份格式非法、owner 与 agent 冲突 → fail closed。
3. 主动内容（html/svg/js/jsxgraph/ggb…）：未配置独立分享域时 attachment +
   nosniff + application/octet-stream；配置 `SHARE_ACTIVE_ORIGIN` 时只 302。
4. 过期 / 撤销后 HTTP 与 tool 都拒绝。
5. 密码：POST unlock 换短时 HMAC cookie；错误密码/伪造 cookie/跨 slug cookie 拒绝；
   `?password=` 仅兼容并标记 deprecated。
6. 引用令牌：路径只能从父 share mapping 推导，客户端 `vfs_path` 被忽略；
   父 share 过期/删除/需要密码时引用也拒绝。
7. 新分享默认 24h、只读；`expires_hours=0` 需显式配置才永久；已有 mapping 过期时间不改。

运行环境（规格 §9.1）：本仓库 `.venv`（Python 3.12）+ `requirements*.txt`。
依赖未安装时本文件整体 skip，并在报告中注明“未跑”，不伪造通过。

    .venv\Scripts\python.exe -m pytest tests/test_share_boundaries.py -q
"""

import datetime
import hashlib
import hmac
import os
import sys
import time

import pytest

# 确保项目根在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

pytest.importorskip(
    "fastapi",
    reason="T17a 分享边界测试需要 fastapi/httpx（repo .venv 未安装依赖时整体 skip）",
)
pytest.importorskip("httpx", reason="TestClient 需要 httpx")
pytest.importorskip("sqlalchemy", reason="临时 SQLite 会话需要 sqlalchemy")

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402
from sqlalchemy import create_engine                               # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402

try:
    from config import settings                                        # noqa: E402
    from models.database import Base, get_db, ShareMapping, ShareReference  # noqa: E402
    from models.agent_profile import AgentProfile                      # noqa: E402
    from services import share_service as share_svc                    # noqa: E402
    from routers import share as share_router                          # noqa: E402
    from routers import share_reference as share_reference_router      # noqa: E402
except ImportError as _import_error:  # pragma: no cover - 环境不完整时显式 skip
    pytest.skip(
        f"T17a 分享边界测试运行环境不完整（{_import_error}）；"
        "需先按 requirements*.txt 安装依赖后重跑",
        allow_module_level=True,
    )

AGENT_HASH = "abcd1234"
OWNER_ID = 7
OTHER_USER_ID = 8
UNLOCK_COOKIE = share_svc.unlock_cookie_name()

_MISSING = object()


# ── 基础设施 ────────────────────────────────────────────────────────────────

class FakeStorage:
    """最小 fake storage：记录被请求的 key，便于断言“没有全局回退”。"""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.requested = []

    def get_file_content(self, key):
        self.requested.append(key)
        return self.objects.get(key)

    def file_exists(self, key):
        self.requested.append(key)
        return {"size": len(self.objects[key])} if key in self.objects else None


def _agent_key(rel, agent_hash=AGENT_HASH, prefix="feclaw/"):
    return f"{prefix}agents/{agent_hash}/workspace/{rel}"


def _user_key(rel, user_id=2, prefix="feclaw/"):
    return f"{prefix}user_workspaces/{user_id}/workspace/{rel}"


@pytest.fixture()
def session_factory(tmp_path):
    """临时 SQLite 会话（真实 SQL，非 mock）。"""
    # 先把所有 model 模块 import 一遍，让 Base.metadata 里的 FK 目标表齐全
    # （只 import models.database 会有 organizations 等跨模块 FK 解析失败）
    import importlib
    for _model_name in (
        "database", "agent_profile", "agent_buffer", "chat", "chat_input",
        "fehub", "group", "kaoxiang_models", "organization", "zentrim",
    ):
        try:
            importlib.import_module(f"models.{_model_name}")
        except ImportError:  # 可选模型缺失不阻塞分享边界测试
            continue

    db_path = tmp_path / "share_boundaries.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def fake_storage(monkeypatch):
    storage = FakeStorage()
    monkeypatch.setattr("services.file_storage.create_file_storage",
                        lambda mode="auto": storage)
    return storage


@pytest.fixture()
def client(session_factory):
    app = FastAPI()
    app.include_router(share_router.router)
    app.include_router(share_reference_router.router)

    def _override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_share_rate_limits():
    """每个用例独立的限流状态（避免用例之间互相触发 429）。

    T18a 起 unlock 限流走统一的 `services.rate_limiter.share_unlock_limiter`
    （内存/Redis 可切），不再有 `share_router._unlock_attempts` 这个本地桶。
    """
    from services.rate_limiter import share_unlock_limiter

    share_unlock_limiter.clear()
    share_reference_router._rate_limit.clear()
    yield
    share_unlock_limiter.clear()
    share_reference_router._rate_limit.clear()


@pytest.fixture()
def set_setting():
    """临时改 settings 覆盖项（T17a 不改 config.py，故用 getattr + 覆盖）。

    pydantic BaseSettings 实例拒绝 setattr 未知字段，这里直接写实例 `__dict__`
    （pydantic v2 的字段就存在那里），测试结束后精确还原。
    """
    applied = []

    def _set(name, value):
        store = settings.__dict__
        applied.append((name, store[name] if name in store else _MISSING))
        object.__setattr__(settings, name, value)

    yield _set

    for name, old in reversed(applied):
        if old is _MISSING:
            settings.__dict__.pop(name, None)
        else:
            object.__setattr__(settings, name, old)


# ── 数据构造 ────────────────────────────────────────────────────────────────

def add_agent(db, agent_hash=AGENT_HASH, user_id=OWNER_ID):
    existing = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
    if existing is None:
        db.add(AgentProfile(hash=agent_hash, user_id=user_id, totp_secret="JBSWY3DPEHPK3PXP"))
        db.commit()
    return agent_hash


def add_mapping(db, *, vfs_path="/workspace/doc/a.md", agent_hash=AGENT_HASH,
                user_id=str(OWNER_ID), slug=None, password=None,
                expires_at=None, share_hash=None):
    mapping = ShareMapping(
        user_id=user_id,
        agent_hash=agent_hash,
        vfs_path=vfs_path,
        share_hash=share_hash or os.urandom(8).hex(),
        slug=slug or f"slug-{os.urandom(5).hex()}",
        mode="share",
        password=password,
        created_at=datetime.datetime.utcnow(),
        expires_at=expires_at,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


def token_for(mapping, ttl=3600):
    return share_svc._encode_share_token(mapping.share_hash, int(time.time()) + ttl)


def expired_dt():
    return datetime.datetime.utcnow() - datetime.timedelta(minutes=5)


# ── 1. 解析边界：只认 mapping 身份推导的路径 ─────────────────────────────────

class TestShareResolutionBoundaries:
    def test_cross_user_path_rejected(self, client, db, fake_storage):
        """跨用户路径（旧 user_workspaces/2 兜底 / 全局 feclaw/vfs）不再可达。"""
        add_agent(db)
        mapping = add_mapping(db, vfs_path="/workspace/secret.md")
        # 攻击者把同一文件放进别的用户空间与全局 vfs 前缀
        fake_storage.objects[_user_key("secret.md", user_id=2)] = b"other-user-secret"
        fake_storage.objects[f"feclaw/vfs{mapping.vfs_path}"] = b"global-secret"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 404
        # 只允许请求 mapping 身份推导出的 agent 工作区 key
        assert fake_storage.requested == [_agent_key("secret.md")]

    def test_mapping_without_identity_fails_closed(self, client, db, fake_storage):
        """mapping 缺 owner/agent 身份 → 404，不猜、不回退到全局路径。"""
        mapping = add_mapping(db, vfs_path="/workspace/a.md", agent_hash="", user_id="")
        fake_storage.objects[_user_key("a.md", user_id=2)] = b"leak"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 404
        assert fake_storage.requested == []

    def test_mapping_path_scope_conflict_rejected(self, client, db, fake_storage):
        """mapping 路径试图跨出工作区（agents/<other>）→ 403。"""
        add_agent(db)
        mapping = add_mapping(db, vfs_path="agents/other999/workspace/x.md")
        fake_storage.objects["feclaw/agents/other999/workspace/x.md"] = b"forged"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 403
        assert fake_storage.requested == []

    def test_owner_conflict_rejected(self, client, db, fake_storage):
        """mapping 的 user_id 与 Agent 实际 owner 冲突 → 403。"""
        add_agent(db, user_id=OWNER_ID)
        mapping = add_mapping(db, user_id=str(OTHER_USER_ID))
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 403
        assert fake_storage.requested == []

    def test_agent_missing_rejected(self, client, db, fake_storage):
        """agent 已被删除（无法确认 owner）→ 404。"""
        mapping = add_mapping(db, agent_hash="deadbeef")
        fake_storage.objects[_agent_key("doc/a.md", agent_hash="deadbeef")] = b"secret"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 404
        assert fake_storage.requested == []

    def test_valid_mapping_serves_from_agent_workspace(self, client, db, fake_storage):
        """正常映射：只读一个 key，返回内容。"""
        add_agent(db)
        mapping = add_mapping(db)
        fake_storage.objects[_agent_key("doc/a.md")] = b"# hello\n"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 200
        assert fake_storage.requested == [_agent_key("doc/a.md")]
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_user_workspace_mapping_without_agent(self, client, db, fake_storage):
        """老 mapping 只有 owner 身份时，只走该 owner 自己的 user_workspaces。"""
        mapping = add_mapping(db, agent_hash="", user_id="42", vfs_path="/workspace/u.md")
        fake_storage.objects[f"feclaw/user_workspaces/42/workspace/u.md"] = b"ok"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 200
        assert fake_storage.requested == ["feclaw/user_workspaces/42/workspace/u.md"]


# ── 2. 主动内容隔离 ────────────────────────────────────────────────────────

class TestShareActiveContent:
    @pytest.mark.parametrize("filename", [
        "evil.html", "evil.htm", "evil.xhtml", "evil.svg",
        "evil.js", "geom.jsxgraph", "figure.ggb", "g.2dggb", "g.3dggb",
    ])
    def test_active_content_is_attachment(self, client, db, fake_storage, filename):
        """未配置独立分享域：主动内容一律 attachment + nosniff + octet-stream。"""
        add_agent(db)
        mapping = add_mapping(db, vfs_path=f"/workspace/{filename}")
        fake_storage.objects[_agent_key(filename)] = b"<script>alert(document.cookie)</script>"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.headers["content-disposition"].startswith("attachment;")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "text/html" not in resp.headers["content-type"]
        assert resp.content == b"<script>alert(document.cookie)</script>"

    def test_active_content_redirects_to_isolated_origin(self, client, db, fake_storage, set_setting):
        """配置独立分享域：只 302 过去，不在主应用源内联执行。"""
        set_setting("SHARE_ACTIVE_ORIGIN", "https://share.example.test")
        add_agent(db)
        mapping = add_mapping(db, vfs_path="/workspace/evil.html")
        fake_storage.objects[_agent_key("evil.html")] = b"<script>alert(1)</script>"

        resp = client.get(f"/s/{mapping.slug}", follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["location"] == f"https://share.example.test/s/{mapping.slug}"
        assert "text/html" not in resp.headers.get("content-type", "")

    def test_token_route_active_content_redirect(self, client, db, fake_storage, set_setting):
        set_setting("SHARE_ACTIVE_ORIGIN", "https://share.example.test")
        add_agent(db)
        mapping = add_mapping(db, vfs_path="/workspace/evil.svg")
        fake_storage.objects[_agent_key("evil.svg")] = b"<svg onload=alert(1)>"
        token = token_for(mapping)

        resp = client.get(f"/share/{token}", follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["location"] == f"https://share.example.test/share/{token}"

    def test_isolated_origin_serves_inline_instead_of_loop(
            self, client, db, fake_storage, set_setting):
        """已在独立分享域上时内联渲染，不能再 302 到自己（避免重定向自环）。"""
        set_setting("SHARE_ACTIVE_ORIGIN", "https://share.example.test")
        add_agent(db)
        mapping = add_mapping(db, vfs_path="/workspace/g.2dggb")
        fake_storage.objects[_agent_key("g.2dggb")] = b"A=(1,2)\n"

        resp = client.get(
            f"/s/{mapping.slug}",
            headers={"host": "share.example.test"},
            follow_redirects=False,
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert b"geogebra.org" in resp.content
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_markdown_share_page_still_rendered(self, client, db, fake_storage):
        """普通 Markdown 仍渲染（渲染安全由 T16/S2 负责），但带 nosniff。"""
        add_agent(db)
        mapping = add_mapping(db, vfs_path="/workspace/note.md")
        fake_storage.objects[_agent_key("note.md")] = b"# title\n\nbody\n"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["x-content-type-options"] == "nosniff"


class TestShareMarkdownPageSafety:
    """T16/S2 交接：分享页必须走唯一安全渲染入口，不能出现裸 marked.parse。"""

    def test_page_uses_safe_renderer_only(self):
        html = share_router._markdown_share_page("# hi\n\n```mermaid\ngraph TD;\n```", "note.md", "h")

        assert "marked.parse(" not in html
        assert "FeClawSafeRender.renderMarkdownSafe" in html
        assert "FeClawSafeRender.renderText" in html

    def test_script_load_order_marked_dompurify_safe_render(self):
        html = share_router._markdown_share_page("# hi", "note.md", "h")

        i_marked = html.index("/static/marked.min.js")
        i_purify = html.index("/static/vendor/dompurify.min.js")
        i_safe = html.index("/static/js/safe-render.js")
        assert i_marked < i_purify < i_safe

    def test_fallback_never_falls_back_to_unescaped_markdown(self):
        html = share_router._markdown_share_page("# hi", "note.md", "h")

        # 依赖缺失分支必须是转义纯文本（esc(RAW)），不得再调用 marked
        assert "SAFE = esc(RAW)" in html
        assert "marked.parse" not in html

    def test_served_markdown_page_loads_safe_renderer(self, client, db, fake_storage):
        add_agent(db)
        mapping = add_mapping(db, vfs_path="/workspace/page.md")
        fake_storage.objects[_agent_key("page.md")] = b"# title\n"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 200
        assert "/static/vendor/dompurify.min.js" in resp.text
        assert "/static/js/safe-render.js" in resp.text
        assert "marked.parse(" not in resp.text

    def test_image_inline_with_safe_type(self, client, db, fake_storage):
        add_agent(db)
        mapping = add_mapping(db, vfs_path="/workspace/pic.png")
        fake_storage.objects[_agent_key("pic.png")] = b"\x89PNG\r\n\x1a\n"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["content-disposition"].startswith("inline;")
        assert resp.headers["x-content-type-options"] == "nosniff"


# ── 3. 过期与撤销 ───────────────────────────────────────────────────────────

class TestShareExpiryAndRevocation:
    def test_expired_share_returns_410(self, client, db, fake_storage):
        add_agent(db)
        mapping = add_mapping(db, expires_at=expired_dt())
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"

        assert client.get(f"/s/{mapping.slug}").status_code == 410
        assert fake_storage.requested == []

    def test_expired_token_share_returns_410(self, client, db, fake_storage):
        add_agent(db)
        mapping = add_mapping(db, expires_at=expired_dt())
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"
        token = token_for(mapping)

        assert client.get(f"/share/{token}").status_code == 410

    def test_deleted_mapping_is_revoked(self, client, db, fake_storage):
        """当前模型没有 revoked 列，撤销的可见表现是行被删除 → 404。"""
        add_agent(db)
        mapping = add_mapping(db)
        slug = mapping.slug
        ip_token = token_for(mapping)
        db.delete(mapping)
        db.commit()
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"

        assert client.get(f"/s/{slug}").status_code == 404
        assert client.get(f"/share/{ip_token}").status_code == 404

    def test_revoked_at_is_honored_when_present(self):
        """前向兼容：模型一旦加上 revoked_at 列，本逻辑立即生效（410）。"""
        mapping = ShareMapping(
            user_id=str(OWNER_ID), agent_hash=AGENT_HASH, vfs_path="/workspace/a.md",
            share_hash="a" * 16, slug="s", mode="share",
        )
        mapping.revoked_at = datetime.datetime.utcnow()

        assert share_svc.is_mapping_revoked(mapping) is True
        with pytest.raises(share_svc.ShareBoundaryError) as exc:
            share_svc.assert_mapping_live(mapping)
        assert exc.value.status_code == 410

    def test_expired_share_does_not_serve_content(self, client, db, fake_storage):
        add_agent(db)
        mapping = add_mapping(db, expires_at=expired_dt(), vfs_path="/workspace/b.md")
        fake_storage.objects[_agent_key("b.md")] = b"secret"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 410
        assert b"secret" not in resp.content


# ── 4. 密码：POST unlock + 短时 cookie，query 仅兼容 ─────────────────────────

class TestShareUnlock:
    def _mapping(self, db, vfs_path="/workspace/doc/a.md"):
        add_agent(db)
        return add_mapping(db, vfs_path=vfs_path,
                           password=share_svc._hash_share_password("hunter2"))

    def test_password_required_without_cookie(self, client, db, fake_storage):
        mapping = self._mapping(db)
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"

        resp = client.get(f"/s/{mapping.slug}")

        assert resp.status_code == 401
        assert "需要密码" in resp.text
        assert f'action="/s/{mapping.slug}/unlock"' in resp.text
        # 密码不能出现在链接里
        assert "password=" not in resp.text
        assert fake_storage.requested == []

    def test_unlock_with_wrong_password_rejected(self, client, db, fake_storage):
        mapping = self._mapping(db)

        resp = client.post(f"/s/{mapping.slug}/unlock", json={"password": "wrong"})

        assert resp.status_code == 401
        assert UNLOCK_COOKIE not in resp.cookies

    def test_unlock_with_correct_password_sets_cookie_and_serves(self, client, db, fake_storage):
        mapping = self._mapping(db, vfs_path="/workspace/doc/a.txt")
        fake_storage.objects[_agent_key("doc/a.txt")] = b"secret"

        unlock = client.post(f"/s/{mapping.slug}/unlock", json={"password": "hunter2"})

        assert unlock.status_code == 200
        assert unlock.json()["ok"] is True
        raw_cookie = unlock.headers["set-cookie"]
        assert UNLOCK_COOKIE in raw_cookie
        assert "HttpOnly" in raw_cookie
        assert "Max-Age=600" in raw_cookie
        # 短时：约 10 分钟
        grant = share_svc.decode_unlock_cookie(unlock.cookies[UNLOCK_COOKIE])
        assert grant and grant["expires_at"] - int(time.time()) <= 600

        served = client.get(f"/s/{mapping.slug}")
        assert served.status_code == 200
        assert served.content == b"secret"

    def test_form_post_unlock_redirects(self, client, db, fake_storage):
        """无 JS 的密码页：form POST → 303 回分享页（同样下发 cookie）。"""
        mapping = self._mapping(db)
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"

        resp = client.post(
            f"/s/{mapping.slug}/unlock",
            data={"password": "hunter2"},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/s/{mapping.slug}"
        assert UNLOCK_COOKIE in resp.cookies

    def test_forged_cookie_rejected(self, client, db, fake_storage):
        mapping = self._mapping(db)
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"

        client.cookies.set(UNLOCK_COOKIE, "v1.forged.signature")

        assert client.get(f"/s/{mapping.slug}").status_code == 401
        assert fake_storage.requested == []

    def test_cookie_bound_to_slug(self, client, db, fake_storage):
        """cookie 只解开签发它的那个分享，换 slug 无效。"""
        mapping = self._mapping(db)
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"
        other = share_svc.encode_unlock_cookie("another-slug", mapping.share_hash)[0]

        client.cookies.set(UNLOCK_COOKIE, other)

        assert client.get(f"/s/{mapping.slug}").status_code == 401

    def test_expired_unlock_cookie_rejected(self, client, db, fake_storage):
        mapping = self._mapping(db)
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"

        # 用同一密钥手工造一个已过期的 cookie
        payload = f"v1|{mapping.slug}|{mapping.share_hash}|{int(time.time()) - 5}"
        sig = hmac.new(share_svc._unlock_secret(), payload.encode(), hashlib.sha256).hexdigest()
        import base64
        value = base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")
        client.cookies.set(UNLOCK_COOKIE, value)

        assert client.get(f"/s/{mapping.slug}").status_code == 401

    def test_query_password_compat_marked_deprecated(self, client, db, fake_storage):
        """`?password=` 仍可用，但必须标记 deprecated（响应头）。"""
        mapping = self._mapping(db)
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"

        resp = client.get(f"/s/{mapping.slug}", params={"password": "hunter2"})

        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "true"
        assert "password in query is deprecated" in resp.headers.get("warning", "")

    def test_token_route_unlock(self, client, db, fake_storage):
        mapping = self._mapping(db)
        fake_storage.objects[_agent_key("doc/a.md")] = b"secret"
        token = token_for(mapping)

        assert client.get(f"/share/{token}").status_code == 401

        unlock = client.post(f"/share/{token}/unlock", json={"password": "hunter2"})
        assert unlock.status_code == 200
        assert client.get(f"/share/{token}").status_code == 200

    def test_unlock_bruteforce_is_rate_limited(self, client, db, fake_storage):
        mapping = self._mapping(db)
        statuses = [
            client.post(f"/s/{mapping.slug}/unlock", json={"password": "wrong"}).status_code
            for _ in range(12)
        ]
        assert 429 in statuses

    def test_unlock_fails_closed_when_limiter_unavailable(self, client, db, fake_storage,
                                                         monkeypatch):
        """T18a：限流后端（Redis）不可用且 fail-mode=closed 时拒绝解锁（503），
        不退回“无限制猜密码”。"""
        import services.rate_limiter as rl

        mapping = self._mapping(db)

        class _Down:
            def is_limited(self, key):  # noqa: ARG002
                raise rl.RateLimitUnavailable("redis down")

        monkeypatch.setattr(rl, "share_unlock_limiter", _Down())

        resp = client.post(f"/s/{mapping.slug}/unlock", json={"password": "wrong"})

        assert resp.status_code == 503

    def test_unlock_on_expired_share_rejected(self, client, db):
        add_agent(db)
        mapping = add_mapping(db, password=share_svc._hash_share_password("hunter2"),
                              expires_at=expired_dt())

        assert client.post(f"/s/{mapping.slug}/unlock",
                           json={"password": "hunter2"}).status_code == 410


# ── 5. 新分享默认 24h、只读、永久需显式配置 ─────────────────────────────────

class TestShareCreationDefaults:
    def _create(self, db, **kwargs):
        add_agent(db)
        return share_svc.create_share_link(db=db, **kwargs)

    def test_default_is_24h_read_only(self, db, fake_storage):
        fake_storage.objects[_agent_key("out/a.md")] = b"x"
        result = self._create(
            db, vfs_path="/workspace/out/a.md", user_id=str(OWNER_ID), agent_hash=AGENT_HASH,
        )

        assert result["expires_hours"] == 24
        assert result["expiry_clamped"] is False
        assert result["mode"] == "share"
        assert result["permanent"] is False
        mapping = db.query(ShareMapping).filter(ShareMapping.slug == result["slug"]).first()
        delta = mapping.expires_at - datetime.datetime.utcnow()
        assert datetime.timedelta(hours=23) < delta <= datetime.timedelta(hours=24, minutes=1)

    def test_permanent_clamped_without_config(self, db, fake_storage):
        fake_storage.objects[_agent_key("out/a.md")] = b"x"
        result = self._create(
            db, vfs_path="/workspace/out/a.md", user_id=str(OWNER_ID),
            agent_hash=AGENT_HASH, expires_hours=0,
        )

        assert result["expires_hours"] == 24
        assert result["expiry_clamped"] is True
        assert result["permanent"] is False

    def test_permanent_allowed_when_configured(self, db, fake_storage, set_setting):
        set_setting("SHARE_ALLOW_PERMANENT", True)
        fake_storage.objects[_agent_key("out/a.md")] = b"x"
        result = self._create(
            db, vfs_path="/workspace/out/a.md", user_id=str(OWNER_ID),
            agent_hash=AGENT_HASH, expires_hours=0,
        )

        assert result["expires_hours"] == 0
        assert result["permanent"] is True
        mapping = db.query(ShareMapping).order_by(ShareMapping.id.desc()).first()
        assert mapping.expires_at > datetime.datetime.utcnow() + datetime.timedelta(days=3650)

    def test_existing_mapping_expiry_not_modified(self, db, fake_storage):
        """创建新分享不能改动已有 mapping 的过期时间。"""
        fake_storage.objects[_agent_key("out/a.md")] = b"x"
        first = self._create(
            db, vfs_path="/workspace/out/a.md", user_id=str(OWNER_ID), agent_hash=AGENT_HASH,
        )
        mapping = db.query(ShareMapping).order_by(ShareMapping.id.asc()).first()
        first_expiry = mapping.expires_at
        before = db.query(ShareMapping).count()

        second = self._create(
            db, vfs_path="/workspace/out/a.md", user_id=str(OWNER_ID),
            agent_hash=AGENT_HASH, expires_hours=168,
        )

        assert db.query(ShareMapping).count() == before + 1
        db.refresh(mapping)
        assert mapping.expires_at == first_expiry
        assert second["expires_hours"] == 168
        assert first["slug"] != second["slug"]

    def test_identity_mismatch_refused(self, db, fake_storage):
        fake_storage.objects[_agent_key("out/a.md")] = b"x"
        result = self._create(
            db, vfs_path="/workspace/out/a.md", user_id=str(OTHER_USER_ID),
            agent_hash=AGENT_HASH,
        )

        assert result["_error"] == "identity_mismatch"
        assert db.query(ShareMapping).count() == 0

    def test_missing_file_refused(self, db, fake_storage):
        result = self._create(
            db, vfs_path="/workspace/out/missing.md", user_id=str(OWNER_ID),
            agent_hash=AGENT_HASH,
        )

        assert result["_error"] == "not_found"
        assert db.query(ShareMapping).count() == 0

    def test_creation_rejects_escape_path(self, db, fake_storage):
        result = self._create(
            db, vfs_path="/workspace/../etc/passwd", user_id=str(OWNER_ID),
            agent_hash=AGENT_HASH,
        )

        assert result["_error"] == "invalid_path"
        assert db.query(ShareMapping).count() == 0


# ── 6. 引用令牌：路径由 mapping 推导，父分享过期/撤销即拒 ───────────────────

class TestShareReferenceBoundaries:
    def _live_ref_setup(self, db, fake_storage, password=None):
        add_agent(db)
        mapping = add_mapping(db, vfs_path="/workspace/doc/original.md", password=password)
        fake_storage.objects[_agent_key("doc/original.md")] = b"# original\n"
        return mapping

    def test_client_vfs_path_is_ignored(self, client, db, fake_storage):
        mapping = self._live_ref_setup(db, fake_storage)

        resp = client.post("/api/share/reference", json={
            "share_hash": mapping.share_hash,
            "vfs_path": "/workspace/doc/other.md",   # 伪造
            "selected_text": "hello",
        })

        assert resp.status_code == 200
        ref_hash = resp.json()["ref_hash"]
        ref = db.query(ShareReference).filter(ShareReference.ref_hash == ref_hash).first()
        assert ref.vfs_path == mapping.vfs_path

        got = client.get(f"/api/share/reference/{ref_hash}")
        assert got.status_code == 200
        assert got.json()["vfs_path"] == mapping.vfs_path

    def test_reference_rejects_expired_parent(self, client, db, fake_storage):
        add_agent(db)
        mapping = add_mapping(db, expires_at=expired_dt())
        # 直接塞一条历史引用（父 share 过期）
        db.add(ShareReference(
            ref_hash="abcd1234", share_hash=mapping.share_hash, agent_hash=AGENT_HASH,
            vfs_path=mapping.vfs_path, selected_text="old",
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30),
        ))
        db.commit()

        assert client.get("/api/share/reference/abcd1234").status_code == 410
        assert client.post("/api/share/reference", json={
            "share_hash": mapping.share_hash, "selected_text": "x",
        }).status_code == 410

    def test_reference_rejects_deleted_parent(self, client, db, fake_storage):
        mapping = self._live_ref_setup(db, fake_storage)
        resp = client.post("/api/share/reference", json={
            "share_hash": mapping.share_hash, "selected_text": "hello",
        })
        ref_hash = resp.json()["ref_hash"]

        db.delete(mapping)
        db.commit()

        assert client.get(f"/api/share/reference/{ref_hash}").status_code == 404

    def test_reference_requires_password_proof(self, client, db, fake_storage):
        mapping = self._live_ref_setup(
            db, fake_storage, password=share_svc._hash_share_password("hunter2")
        )

        assert client.post("/api/share/reference", json={
            "share_hash": mapping.share_hash, "selected_text": "x",
        }).status_code == 401

        unlock = client.post(f"/s/{mapping.slug}/unlock", json={"password": "hunter2"})
        assert unlock.status_code == 200

        resp = client.post("/api/share/reference", json={
            "share_hash": mapping.share_hash, "selected_text": "x",
        })
        assert resp.status_code == 200

    def test_reference_path_taken_from_mapping_not_stored_row(self, client, db, fake_storage):
        """历史/被篡改的 ShareReference.vfs_path 不会泄漏，仍以 mapping 为准。"""
        mapping = self._live_ref_setup(db, fake_storage)
        db.add(ShareReference(
            ref_hash="tamper01", share_hash=mapping.share_hash, agent_hash=AGENT_HASH,
            vfs_path="/workspace/doc/stolen.md", selected_text="tampered",
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30),
        ))
        db.commit()

        resp = client.get("/api/share/reference/tamper01")

        assert resp.status_code == 200
        assert resp.json()["vfs_path"] == mapping.vfs_path

    def test_tool_resolve_reference_fails_closed(self, client, db, fake_storage, monkeypatch):
        """Agent 工具 resolve_share_reference 与 HTTP 同逻辑：父 share 过期即失败。"""
        add_agent(db)
        mapping = add_mapping(db, expires_at=expired_dt())
        db.add(ShareReference(
            ref_hash="expired1", share_hash=mapping.share_hash, agent_hash=AGENT_HASH,
            vfs_path=mapping.vfs_path, selected_text="old",
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30),
        ))
        db.commit()
        monkeypatch.setattr("models.database.SessionLocal", lambda: db)

        try:
            from services.tools.share_tools import ShareToolsMixin
        except ImportError as exc:  # 依赖缺失时显式 skip，不伪造通过
            pytest.skip(f"share_tools 依赖不可用: {exc}")

        class _Service(ShareToolsMixin):
            user_id = str(OWNER_ID)
            agent_hash = AGENT_HASH

        service = object.__new__(_Service)
        service.user_id = str(OWNER_ID)
        service.agent_hash = AGENT_HASH

        out = service.resolve_share_reference("expired1")
        assert out.startswith("错误：")
        assert "过期" in out


# ── 7. 纯函数单测（不依赖 HTTP，便于快速定位边界回归） ─────────────────────

class TestShareBoundaryUnits:
    @pytest.mark.parametrize("path,status", [
        ("/workspace/../etc/passwd", 403),
        ("a\x00b.md", 400),
        ("C:/windows/x.md", 400),
        ("a\\b.md", 400),
        ("", 404),
    ])
    def test_normalize_rejects(self, path, status):
        with pytest.raises(share_svc.ShareBoundaryError) as exc:
            share_svc.normalize_vfs_path(path)
        assert exc.value.status_code == status

    def test_normalize_strips_leading_slash(self):
        assert share_svc.normalize_vfs_path("/workspace/a/b.md") == "workspace/a/b.md"

    @pytest.mark.parametrize("path", [
        "user_workspaces/2/workspace/x.md",
        "agents/other/workspace/x.md",
        "public/x.md",
        "config/x.md",
        "agent/persona.md",
    ])
    def test_workspace_relpath_rejects_scope_roots(self, path):
        with pytest.raises(share_svc.ShareBoundaryError) as exc:
            share_svc.mapping_workspace_relpath(path)
        assert exc.value.status_code == 403

    def test_workspace_relpath_allows_workspace_subdirs(self):
        assert share_svc.mapping_workspace_relpath("/workspace/agents/x.md") == "agents/x.md"

    def test_active_extension_classification(self):
        for name in ("a.html", "a.htm", "a.xhtml", "a.svg", "a.js",
                     "a.jsxgraph", "a.ggb", "a.2dggb"):
            assert share_svc.is_active_content(name), name
        for name in ("a.md", "a.png", "a.txt", "a.pdf", "a.json"):
            assert not share_svc.is_active_content(name), name

    def test_default_expiry_and_permanent_gate(self, set_setting):
        assert share_svc.normalize_expiry_hours(None)[0] == 24
        assert share_svc.normalize_expiry_hours(0) == (24, True)
        set_setting("SHARE_ALLOW_PERMANENT", True)
        assert share_svc.normalize_expiry_hours(0) == (0, False)


# ── chat_service 引用注入（跨模块集成：HTTP 边界 × 会话消息构建）────────────────

class TestChatReferenceInjection:
    """`chat_service._resolve_references` 必须复用分享边界，不得绕过父分享校验。

    旧实现 `from models.database import get_session, ShareReference` 实际是
    ImportError（get_session 是本模块的私有上下文管理器），被宽 except 吞掉后
    引用注入整体失效；若只把 import 修好而不管边界，已撤销/加密码分享的内容
    会继续注入模型上下文。这里把两个方向都卡住。
    """

    def _inject(self, monkeypatch, session_factory, text):
        import asyncio
        from contextlib import contextmanager

        from services import chat_service

        @contextmanager
        def _session():
            s = session_factory()
            try:
                yield s
            finally:
                s.close()

        # 直接替换模块级 `get_session`（而不是 SessionLocal）：既保证注入读的是本
        # 用例的 SQLite，也不受其他测试用例对 `services.chat_service.get_session`
        # 的 patch / 泄漏影响。
        monkeypatch.setattr(chat_service, "get_session", _session)
        # _resolve_references 的 body 不读 self，直接以 None 作为实例调用即可
        return asyncio.run(chat_service.ChatService._resolve_references(None, text))

    def _add_ref(self, db, mapping, ref_hash="ref00001", text="选中的原文", days=30):
        db.add(ShareReference(
            ref_hash=ref_hash, share_hash=mapping.share_hash, agent_hash=AGENT_HASH,
            vfs_path=mapping.vfs_path, selected_text=text,
            context_before="前文", context_after="后文",
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=days),
        ))
        db.commit()
        return ref_hash

    def test_live_reference_is_injected(self, session_factory, db, monkeypatch):
        add_agent(db)
        mapping = add_mapping(db)
        ref_hash = self._add_ref(db, mapping)

        out = self._inject(monkeypatch, session_factory, f"看看 [reference:{ref_hash}]")

        assert "选中的原文" in out
        assert "前文" in out and "后文" in out

    def test_expired_parent_blocks_injection(self, session_factory, db, monkeypatch):
        add_agent(db)
        mapping = add_mapping(db, expires_at=expired_dt())
        ref_hash = self._add_ref(db, mapping)

        out = self._inject(monkeypatch, session_factory, f"[reference:{ref_hash}]")

        assert "选中的原文" not in out

    def test_deleted_parent_blocks_injection(self, session_factory, db, monkeypatch):
        add_agent(db)
        mapping = add_mapping(db)
        ref_hash = self._add_ref(db, mapping)

        db.delete(mapping)
        db.commit()

        out = self._inject(monkeypatch, session_factory, f"[reference:{ref_hash}]")

        assert "选中的原文" not in out

    def test_password_protected_parent_blocks_injection(self, session_factory, db, monkeypatch):
        add_agent(db)
        mapping = add_mapping(db, password=share_svc._hash_share_password("hunter2"))
        ref_hash = self._add_ref(db, mapping)

        out = self._inject(monkeypatch, session_factory, f"[reference:{ref_hash}]")

        assert "选中的原文" not in out

    def test_expired_reference_blocks_injection(self, session_factory, db, monkeypatch):
        add_agent(db)
        mapping = add_mapping(db)
        ref_hash = self._add_ref(db, mapping, days=-1)

        out = self._inject(monkeypatch, session_factory, f"[reference:{ref_hash}]")

        assert "选中的原文" not in out

    def test_unknown_reference_leaves_message_untouched(self, session_factory, db, monkeypatch):
        add_agent(db)
        text = "没有这条引用 [reference:deadbeef]"

        out = self._inject(monkeypatch, session_factory, text)

        assert out == text

    def test_long_reference_text_is_truncated(self, session_factory, db, monkeypatch):
        add_agent(db)
        mapping = add_mapping(db)
        ref_hash = self._add_ref(db, mapping, text="x" * 5000)

        out = self._inject(monkeypatch, session_factory, f"[reference:{ref_hash}]")

        # 正文与「上下文」两处都会注入，但每处都不得超过 2000 字符
        assert "x" * 2000 in out
        assert "x" * 2001 not in out
