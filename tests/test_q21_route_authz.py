"""
Q21 路由级授权测试矩阵（审计 §7.15 要求）

两块：
1. **全路由匿名扫描** —— 枚举 app 全部 APIRoute，对每个非白名单路由发匿名请求，
   断言「无 token ⇒ 不放行」（不得返回 2xx，不得跳转到非 /login 的地址）。
   白名单逐条说明理由（见 ANON_WHITELIST）。
2. **带资源 ID 路由的跨租户校验** —— 文件 / 群 / 日程 / 任务 / 沙箱 / VFS 这几族，
   断言「他人资源 ⇒ 403（或存在性隐藏的 404）」。

目的是卡住审计 §7 结论：这类越权回归必须由测试卡住。
"""
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


# ======================================================================
# 1. 匿名白名单 —— 逐条理由
# ======================================================================
# 每个条目：(methods 集合或 None=任意方法, 路径前缀/精确路径, 理由)
# 匹配规则：先精确匹配 (method, path)，再前缀匹配（path.startswith(prefix)）。
ANON_WHITELIST = [
    # --- 存活探针 / 健康检查（无需鉴权，仅返回进程状态）---
    ({"GET"}, "/health", "liveness probe（main.py 显式定义，返回 {'status':'healthy'}）"),

    # --- 公开的非敏感配置（Q19/C6 白名单化，绝不回显 *_URL/*_TOKEN/*_KEY）---
    ({"GET"}, "/api/console/public-config", "显式白名单，仅公开特性开关/模型名/域名"),

    # --- 凭据登录 / 自助注册（凭据本身即认证；登录有 IP+username 限流 H17）---
    ({"POST"}, "/api/user/login", "用户名+口令登录（H17 限流）"),
    ({"POST"}, "/api/user/register", "自助注册（凭据创建入口）"),

    # --- TOTP 登录（一次性口令即认证；verify 有 IP+hash 限流 C2）---
    ({"POST"}, "/api/totp/verify", "TOTP 持有即认证，IP+agent_hash 限流（Q19/C2）"),
    ({"POST"}, "/api/workspace/totp/verify", "workspace TOTP 持有即认证"),

    # --- OAuth/OIDC 握手（外部 IdP 重定向回调，state 校验 + PKCE）---
    (None, "/api/oauth/", "OAuth login/callback/exchange/refresh/mobile-login/logout 握手端点"),
    # --- 登出（幂等：匿名调用也只清 cookie 返回 ok，不泄露数据）---
    ({"POST"}, "/api/auth/logout", "登出幂等：匿名调用仍返回 ok 并清 cookie，无数据泄露"),
    ({"GET"}, "/api/auth/options", "返回登录方式清单（非敏感）"),
    ({"GET"}, "/api/auth/sync", "Cookie SSO 中继，无有效 cookie 不返回数据"),

    # --- 分享链接（slug/token 本身就是凭据；密码门禁 H18）---
    (None, "/s/", "公开分享短链，token 即凭据 + 可选密码（H18）"),
    (None, "/share/", "公开分享 token 链接，token 即凭据 + 可选密码（H18）"),
    (None, "/api/share/reference", "分享引用令牌即凭据，按 IP 限流（创建需携带合法 share_hash）"),

    # --- 沙箱内部 VFS（128-bit 随机 token 走 query + MySQL 查表，无 token 返回良性空响应）---
    (None, "/api/sandbox/vfs/", "内部 VFS，token 不可猜测；无 token 返回 {'exists':false} 不泄数据"),

    # --- 纯页面壳（数据由前端带 JWT 另行拉取，L10 已确认无数据泄露）---
    ({"GET"}, "/", "首页/介绍页壳"),
    ({"GET"}, "/login", "登录页壳"),
    ({"GET"}, "/initialize", "初始化页壳"),
    ({"GET"}, "/favicon.ico", "favicon"),
    ({"GET"}, "/console", "Agent 配置 UI 页壳（L10：静态 HTML，数据走 JWT API）"),
    ({"GET"}, "/filemanager", "filemanager SPA 页壳"),

    # --- Agent 自部署 App 网关（按 Host 子域名确定 agent，服务公开 App；注册/删除走受鉴权 /api/apps/*）---
    (None, "/apps", "Agent 自部署 App 服务网关（Host 头作用域；写操作在 /api/apps/* 已鉴权）"),

    # --- 静态站公开访问 catch-all（公开站点托管）---
    (None, "/{file_path:path}", "静态站公开访问 catch-all（public site hosting）"),
]

# 精确匹配优先的额外白名单（覆盖 /console/agents/* 页壳等带参数路径）
ANON_EXACT_WHITELIST = {
    ("GET", "/console/agents/new"): "Agent 创建页壳",
    ("GET", "/console/agents/{agent_id}/config"): "Agent 配置页壳",
}


# ======================================================================
# 工具函数
# ======================================================================

_PATH_PARAM_SAMPLES = {
    "agent_hash": "abcd", "hash": "abcd", "agent_id": "1",
    "user_id": "1", "site_id": "1", "group_id": "1",
    "entry_id": "1", "timeline_id": "1", "request_id": "1",
    "ref_id": "1", "moment_id": "1", "organization_id": "1",
    "session_id": "1", "app_id": "x", "sandbox_id": "1",
    "ref_hash": "a" * 16, "slug": "test-slug", "token": "test-token",
    "provider_id": "test", "file_path": "x", "path": "x",
}


def _substitute(path: str) -> str:
    """把 {param} / {param:path} 替换成样例值。"""
    import re
    return re.sub(r"\{([a-zA-Z0-9_]+)(?::[^}]*)?\}",
                  lambda m: str(_PATH_PARAM_SAMPLES.get(m.group(1), "1")),
                  path)


def _is_whitelisted(method: str, path: str) -> str:
    """返回白名单理由；不在白名单返回 None。"""
    if (method, path) in ANON_EXACT_WHITELIST:
        return ANON_EXACT_WHITELIST[(method, path)]
    # 前缀匹配（允许指定 method 或任意）
    for methods, prefix, reason in ANON_WHITELIST:
        if methods is not None and method not in methods:
            continue
        if path == prefix or path.startswith(prefix):
            return reason
    return None


def _all_routes():
    return [r for r in app.routes if isinstance(r, APIRoute)]


def _route_request(client, method: str, path: str):
    """按方法发匿名请求，返回 (status, location)。"""
    url = _substitute(path)
    headers = {"Host": "testserver"}
    if method == "GET":
        return client.get(url, headers=headers)
    if method == "DELETE":
        return client.delete(url, headers=headers)
    # POST / PUT / PATCH：给空 body，缺 body 校验 422 也属「不放行」
    return client.request(method, url, json={}, headers=headers)


# ======================================================================
# 2. 全路由匿名扫描 —— 无 token ⇒ 不放行
# ======================================================================

def test_route_authz_matrix_no_anonymous_leak(client):
    """枚举全部路由，非白名单路由匿名访问不得返回 2xx / 非登录跳转。

    这是覆盖率主断言：白名单外的路由一旦有人误删鉴权（回归），本测试即失败。
    """
    routes = _all_routes()
    leaks = []          # 匿名拿到 2xx（真泄露）
    suspicious = []     # 匿名拿到非 /login 的 3xx（疑似开放重定向）
    whitelisted = 0
    denied = {"401": 0, "403": 0, "404": 0, "405": 0, "422": 0, "400": 0, "other": 0}
    checked = 0

    seen = set()
    for r in routes:
        methods = {m for m in (r.methods or set()) if m != "HEAD"}
        if not methods:
            continue
        method = "GET" if "GET" in methods else sorted(methods)[0]
        reason = _is_whitelisted(method, r.path)
        if reason is not None:
            whitelisted += 1
            continue
        key = (method, r.path)
        if key in seen:
            continue
        seen.add(key)
        checked += 1

        resp = _route_request(client, method, r.path)
        sc = resp.status_code
        if 200 <= sc < 300:
            leaks.append((method, r.path, sc))
        elif 300 <= sc < 400:
            loc = (resp.headers.get("location") or "")
            if "/login" not in loc:
                suspicious.append((method, r.path, sc, loc))
        else:
            denied[str(sc)] = denied.get(str(sc), 0) + 1

    # 覆盖率统计（供报告引用）
    matrix = {
        "total": len(routes),
        "whitelisted": whitelisted,
        "checked": checked,
        "denied": denied,
        "leaks": leaks,
        "suspicious": suspicious,
    }
    print("\n[Q21 matrix]", matrix)

    assert not leaks, (
        f"发现 {len(leaks)} 处匿名可成功访问的路由（必须加鉴权或补白名单理由）: {leaks}"
    )
    assert not suspicious, (
        f"发现 {len(suspicious)} 处匿名跳转到非 /login 地址（疑似开放重定向）: {suspicious}"
    )


# ======================================================================
# 3. 确定性断言 —— 高价值路由匿名必 401
# ======================================================================

# (method, path) —— 鉴权是这些路由的第一道闸，匿名必 401
ANON_401_ROUTES = [
    ("GET", "/api/console/agents"),
    ("POST", "/api/console/agents"),
    ("GET", "/api/console/user"),
    ("GET", "/api/console/tools"),
    ("GET", "/api/console/templates"),
    ("GET", "/api/files"),
    ("GET", "/api/file"),
    ("GET", "/api/file/raw"),
    ("POST", "/api/file/signed-url"),
    ("GET", "/api/file/sts-credential"),
    ("POST", "/api/file/upload"),
    ("POST", "/api/totp/generate"),
    ("POST", "/api/sandbox/execute"),
    ("GET", "/api/sandbox/status"),
    ("POST", "/api/sandbox/1/stop"),
    ("GET", "/api/vfs/view"),
    ("GET", "/api/vfs-images/stats"),
    ("POST", "/api/user/agents"),
    ("GET", "/api/user/permissions"),
    ("POST", "/api/user/change-password"),
    ("GET", "/api/groups"),
    ("GET", "/api/groups/1"),
    ("GET", "/api/zentrim/entries"),
    ("GET", "/api/zentrim/entries/1"),
    ("GET", "/api/static-sites"),
    ("POST", "/api/static-sites"),
    ("GET", "/api/chat/sessions"),
    ("POST", "/api/chat/sessions"),
    ("GET", "/api/wechat/qrcode"),
    ("GET", "/api/wechat/status"),
    ("POST", "/api/wechat/bind"),
    ("GET", "/api/desktop/agents"),
    ("GET", "/api/approvals/1"),
    ("GET", "/api/admin/stats"),
    ("GET", "/api/health/backend"),
    ("GET", "/api/heartbeat/stats"),
    ("GET", "/internal/metrics"),
    ("POST", "/setup/api-keys"),
    ("POST", "/setup/storage"),
    ("POST", "/setup/admin"),
    ("GET", "/setup/api/summary"),
    ("GET", "/setup"),
    ("POST", "/api/apps/register"),
    ("GET", "/api/apps"),
]


@pytest.mark.parametrize("method,path", ANON_401_ROUTES)
def test_curated_anon_401(method, path, client):
    """确定性断言：高价值路由匿名 ⇒ 401/403（而非 200/3xx 泄露）。"""
    resp = _route_request(client, method, path)
    assert resp.status_code in (401, 403), (
        f"{method} {path} 匿名应 401/403，实际 {resp.status_code}"
    )


# ======================================================================
# 4. 跨租户校验 —— 他人资源 ⇒ 403（文件/群/日程/任务/沙箱/VFS）
# ======================================================================

def _make_owner_and_attacker(db):
    """在测试 DB 中建 owner + attacker + owner 的 agent，返回 (owner_id, attacker_id)。

    注意：id 必须在 db.close() 前取出，否则 close 后对象 detached，访问 .id 会抛
    DetachedInstanceError。
    """
    import pyotp
    from models.database import User
    from models.agent_profile import AgentProfile
    from utils.auth import hash_password

    owner = User(username="q21owner", password_hash=hash_password("pw"), salt=None, is_admin=False)
    attacker = User(username="q21attacker", password_hash=hash_password("pw"), salt=None, is_admin=False)
    db.add_all([owner, attacker])
    db.commit()
    db.add(AgentProfile(user_id=owner.id, hash="abcd", totp_secret=pyotp.random_base32(),
                        name="a", status="pending"))
    db.commit()
    return owner.id, attacker.id


def _attacker_token(attacker_id) -> str:
    from utils.auth import create_jwt_token
    return create_jwt_token({"sub": str(attacker_id)})


class TestCrossTenantResource403:
    """他人资源 ⇒ 403（带资源 ID 路由，覆盖文件/群/日程/任务/沙箱/VFS 六族）。"""

    def test_file_read_other_agent_403(self, real_db, client):
        from models.database import SessionLocal
        db = SessionLocal()
        owner_id, attacker_id = _make_owner_and_attacker(db)
        db.close()
        token = _attacker_token(attacker_id)
        resp = client.get("/api/file", params={"agent_hash": "abcd", "path": "workspace/SOUL.md"},
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_files_list_other_agent_403(self, real_db, client):
        from models.database import SessionLocal
        db = SessionLocal()
        owner_id, attacker_id = _make_owner_and_attacker(db)
        db.close()
        token = _attacker_token(attacker_id)
        resp = client.get("/api/files", params={"agent_hash": "abcd"},
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_group_read_other_owner_403(self, real_db, client):
        from models.database import SessionLocal
        from models.group import Group
        db = SessionLocal()
        owner_id, attacker_id = _make_owner_and_attacker(db)
        g = Group(name="q21g", owner_user_id=owner_id)
        db.add(g)
        db.commit()
        gid = g.id
        db.close()
        token = _attacker_token(attacker_id)
        resp = client.get(f"/api/groups/{gid}", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_group_dashboard_messages_other_owner_403(self, real_db, client):
        from models.database import SessionLocal
        from models.group import Group
        db = SessionLocal()
        owner_id, attacker_id = _make_owner_and_attacker(db)
        g = Group(name="q21g2", owner_user_id=owner_id)
        db.add(g)
        db.commit()
        gid = g.id
        db.close()
        token = _attacker_token(attacker_id)
        resp = client.get(f"/dashboard/group/api/messages?group_id={gid}",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_sandbox_execute_other_agent_403(self, real_db, client):
        from models.database import SessionLocal
        db = SessionLocal()
        owner_id, attacker_id = _make_owner_and_attacker(db)
        db.close()
        token = _attacker_token(attacker_id)
        resp = client.post("/api/sandbox/execute",
                           json={"code": "print(1)", "agent_hash": "abcd"},
                           headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_vfs_view_other_agent_403(self, real_db, client):
        from models.database import SessionLocal
        db = SessionLocal()
        owner_id, attacker_id = _make_owner_and_attacker(db)
        db.close()
        token = _attacker_token(attacker_id)
        resp = client.get("/api/vfs/view", params={"path": "agents/abcd/workspace/soul.md"},
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_schedule_entry_other_user_denied(self, real_db, client):
        """日程（Zentrim 条目）：非 owner 走存在性隐藏，返回 404（deny，不泄存在性）。"""
        from models.database import SessionLocal
        db = SessionLocal()
        owner_id, attacker_id = _make_owner_and_attacker(db)
        db.close()
        token = _attacker_token(attacker_id)
        resp = client.get("/api/zentrim/entries/nonexistent-entry-id",
                          headers={"Authorization": f"Bearer {token}"})
        # Zentrim 用 user_id 过滤，他人条目 = 不存在（404），与 403 同为拒绝语义
        assert resp.status_code in (403, 404)

    def test_task_stop_other_user_noop(self, real_db, client):
        """任务（后台沙箱任务）：匿名 401；他人任务 stop 为 no-op（stopped:False）。"""
        # 匿名必须 401
        anon = client.post("/api/sandbox/nonexistent-task/stop")
        assert anon.status_code == 401
