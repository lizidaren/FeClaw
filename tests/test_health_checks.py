"""T02 — 巡检必须真的只读 + /ready 门禁（tests/test_health_checks.py）

前半部分不依赖真实服务、数据库或 MySQL：每个用例起一个本机 stub HTTP 服务，记录
**每一条**到达的请求，然后跑 `scripts/health_check.py` 子进程断言行为。
后半部分是 `/ready` 端点的真实验证（需要 fastapi/sqlalchemy，依赖缺失时 skip 并给原因）。

覆盖（对应规格 §5 T02 验收）：
  1. 默认路径不出现任何 mutation 请求（POST/PUT/PATCH/DELETE），也不 login/register；
  2. `--only 4` 也不能提前准备账号；
  3. mutation 模式必须显式非生产 base-url：缺参数/生产域名/公网地址一律拒绝且不发请求；
  4. mutation 模式必须自己注册一次性账号、自己建 Agent，不复用已有账号首个 agent，
     清理只针对本次 ledger 记录的资源；
  5. `/ready` 门禁：空响应 / 非 200 / JSON 坏 / status != ready 全部失败（部署门禁契约）；
  6. deploy.sh 同时判退出码、HTTP status 与 JSON status == "ready"；
  7. `/ready` 端点：DB 失败 / 关键表缺失 / schema_version 版本过低 ⇒ 503；
     健康路径 200 且 status=ready；探活前后库中 Agent 数量不变；`/health` 不碰 DB。

注：本文件不引入 fixture，方便在依赖尚未装齐的环境里手工跑
（`python tests/test_health_checks.py` 会执行 _manual_run）。
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
SCRIPT = REPO / "scripts" / "health_check.py"

MUTATING = ("POST", "PUT", "PATCH", "DELETE")

try:  # 依赖未装齐时仍可被手工 runner 导入
    import pytest
except ImportError:  # pragma: no cover
    pytest = None


def _skip(reason):
    if pytest is not None:
        pytest.skip(reason)
    raise unittest.SkipTest(reason)


def _load(module_path):
    try:
        return importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001
        _skip(f"{module_path} 不可导入（依赖未装齐？）：{type(exc).__name__}: {exc}")

# 用于枚举的假 openapi：故意混入 GET 与各种写方法、自毁接口、模型路径
OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "paths": {
        "/login": {"get": {}},
        "/api/health/backend": {"get": {}},
        "/api/console/agents": {"get": {}, "post": {}},
        "/api/console/agents/{agent_id}": {"get": {}, "delete": {}},
        "/api/user/agents": {"post": {}},
        "/api/user/agents/{hash}/settings": {"patch": {}},
        "/api/user/agents/{hash}/vfs/permissions": {"patch": {}},
        "/api/chat/stream": {"post": {}},
        "/api/auth/logout": {"post": {}},
    },
}


class StubSite:
    """本机 stub 服务：记录 (method, path, authorization, body)，按路由返回。"""

    def __init__(self, routes=None, default=(200, '{"status":"ok"}', "application/json")):
        self.requests = []
        self.routes = dict(routes or {})
        self.default = default
        self._server = None
        self._thread = None

    # -- context manager -------------------------------------------------
    def __enter__(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                site.requests.append({
                    "method": method,
                    "path": path,
                    "raw_path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": raw_body.decode("utf-8", "replace"),
                })
                status, payload, ctype = (
                    site.routes.get((method, path))
                    or site.routes.get((method, "*"))
                    or site.default
                )
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_HEAD(self):
                self._handle("HEAD")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_PATCH(self):
                self._handle("PATCH")

            def do_DELETE(self):
                self._handle("DELETE")

            def log_message(self, *args):  # 静音
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False

    # -- helpers ---------------------------------------------------------
    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def mutating_requests(self):
        return [r for r in self.requests if r["method"] in MUTATING]

    def paths(self):
        return [r["path"] for r in self.requests]


def _base_routes():
    return {
        ("GET", "/openapi.json"): (200, json.dumps(OPENAPI_SPEC), "application/json"),
        ("GET", "/login"): (200, "<html>login</html>", "text/html"),
    }


def _health_module():
    """按文件路径加载 scripts/health_check.py（纯 stdlib，导入很快）。"""
    global _HEALTH_MODULE
    if _HEALTH_MODULE is not None:
        return _HEALTH_MODULE
    import importlib.util
    spec = importlib.util.spec_from_file_location("feclaw_health_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _HEALTH_MODULE = module
    return module


_HEALTH_MODULE = None


def run_main(args, env=None):
    """在进程内跑 health_check.main()，返回 (returncode, 输出)。

    进程内而不是 subprocess：本机 Windows 上每次 spawn 解释器约 20s（杀软扫描），
    24 个用例会被拖到 8 分钟；CLI 入口本身另有 test_cli_entrypoint_ready_check 覆盖。
    """
    import contextlib
    import io

    hc = _health_module()
    saved_argv = sys.argv
    saved_env = dict(os.environ)
    buf = io.StringIO()
    try:
        for key in ("FECLAW_HC_URL", "FECLAW_HC_TOKEN", "FECLAW_HC_PROD_HOSTS",
                    "FECLAW_HC_MUTATION_HOSTS", "FECLAW_HC_MUTATION_USER",
                    "FECLAW_HC_MUTATION_PASS", "FECLAW_HC_AGENT_HASH"):
            os.environ.pop(key, None)
        if env:
            os.environ.update(env)
        sys.argv = ["health_check.py", *args]
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = hc.main()
    finally:
        sys.argv = saved_argv
        os.environ.clear()
        os.environ.update(saved_env)
    return rc, buf.getvalue()


def run_script(args, env=None):
    """跑 health_check 子进程（只用于证明真实 CLI 入口），返回 (rc, 输出)。"""
    full_env = dict(os.environ)
    full_env.pop("FECLAW_HC_URL", None)
    full_env.pop("FECLAW_HC_TOKEN", None)
    full_env.pop("FECLAW_HC_PROD_HOSTS", None)
    # 输出固定 UTF-8：Windows 默认 locale 会把中文/✓ 解成乱码或直接解码失败
    full_env["PYTHONIOENCODING"] = "utf-8"
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=full_env, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def test_cli_entrypoint_ready_check_contract():
    """真实 CLI（python scripts/health_check.py --ready-check）的退出码契约。"""
    with StubSite(routes={("GET", "/ready"): (200, '{"status":"ready"}', "application/json")}) as site:
        rc, out = run_script(["--ready-check", "--base-url", site.base_url])
        assert rc == 0, out
    with StubSite(routes={("GET", "/ready"): (503, '{"status":"unhealthy"}', "application/json")}) as site:
        rc, out = run_script(["--ready-check", "--base-url", site.base_url])
        assert rc == 1, out


# ─────────────────────────── 1/2. 默认只读 ───────────────────────────

def test_default_run_issues_no_mutation_and_no_login():
    with StubSite(routes=_base_routes()) as site:
        rc, out = run_main(["--base-url", site.base_url, "--only", "4", "--skip-browser"])
        assert rc == 0, out
        assert not site.mutating_requests(), f"默认路径发出写请求: {site.mutating_requests()}"
        login_like = [p for p in site.paths()
                      if p.endswith("/api/user/login") or p.endswith("/api/user/register")]
        assert login_like == [], f"默认路径动了账号: {login_like}"
        # 显式确认没有创建任何资源（创建 Agent 只能是 POST）
        assert not any(r["method"] == "POST" and r["path"].endswith("/api/user/agents")
                       for r in site.requests), site.requests


def test_only_4_does_not_prepare_account():
    with StubSite(routes=_base_routes()) as site:
        rc, out = run_main(["--base-url", site.base_url, "--only", "4", "--skip-browser"])
        assert rc == 0, out
        assert not site.mutating_requests(), site.mutating_requests()
        # 只有可达性探测的 GET /login；绝不能有 POST login/register
        posts = [r["path"] for r in site.requests if r["method"] == "POST"]
        assert posts == [], posts
        assert not any(p.endswith("/api/user/register") for p in site.paths()), site.paths()


def test_readonly_output_marks_skipped_mutations():
    with StubSite(routes=_base_routes()) as site:
        json_path = Path(tempfile.mkdtemp(prefix="hc-")) / "out.json"
        rc, out = run_main([
            "--base-url", site.base_url, "--only", "4", "--skip-browser",
            "--json", str(json_path),
        ])
        assert rc == 0, out
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        api = payload["api"]
        assert api["readonly"] is True
        assert api["mutating_skipped"] >= 1
        for row in api["rows"]:
            if row["method"] in MUTATING:
                assert row["code"] is None, f"写方法居然被发出并拿到响应: {row}"
        assert payload["meta"]["mode"] == "read-only"


# ─────────────────── 3. mutation 目标安全校验 ───────────────────

def test_mutations_require_integration_flag():
    rc, out = run_main(["--allow-mutations", "--base-url", "http://127.0.0.1:1"])
    assert rc == 2, out
    assert "--integration" in out


def test_mutations_require_explicit_base_url():
    rc, out = run_main(["--integration", "--allow-mutations"])
    assert rc == 2, out
    assert "--base-url" in out


def test_mutations_refuse_production_host_without_network():
    rc, out = run_main([
        "--integration", "--allow-mutations",
        "--base-url", "https://feclaw.lizidaren.cn",
    ])
    assert rc == 2, out
    assert "生产" in out or "production" in out.lower()


def test_mutations_refuse_public_host():
    rc, out = run_main([
        "--integration", "--allow-mutations", "--base-url", "http://8.8.8.8:8080",
    ])
    assert rc == 2, out
    assert "私网" in out or "回环" in out


# ─────────────────── 4. 一次性账号 + 清理白名单 ───────────────────

def test_mutation_mode_registers_own_account_and_cleans_only_ledger():
    routes = dict(_base_routes())
    routes[("POST", "/api/user/register")] = (
        200, json.dumps({"token": "tok-disposable", "user": {"id": 7}}), "application/json")
    routes[("POST", "/api/user/agents")] = (
        200, json.dumps({"hash": "hc000001", "id": 42}), "application/json")
    routes[("DELETE", "/api/console/agents/42")] = (200, '{"status":"ok"}', "application/json")
    with StubSite(routes=routes) as site:
        rc, out = run_main([
            "--integration", "--allow-mutations", "--only", "4", "--skip-browser",
            "--base-url", site.base_url,
        ])
        assert rc == 0, out
        assert "hc-probe-" in out, out  # 本次测试 Agent 名称

        regs = [r for r in site.requests if r["path"] == "/api/user/register"]
        assert len(regs) == 2, f"应注册两个一次性账号（主 + 自毁用），实际 {len(regs)}"

        creates = [r for r in site.requests
                   if r["method"] == "POST" and r["path"] == "/api/user/agents"]
        assert len(creates) == 1, creates
        assert creates[0]["authorization"] == "Bearer tok-disposable"

        # 不复用已有账号的首个 Agent：旧版会在建 Agent 之前 GET /api/console/agents
        # 并直接取 items[0]（别人的第一个 agent）。现在只允许建自己的。
        create_idx = site.requests.index(creates[0])
        before = site.requests[:create_idx]
        assert not any(r["method"] == "GET" and r["path"] == "/api/console/agents"
                       for r in before), before

        deletes = [r["path"] for r in site.requests if r["method"] == "DELETE"]
        assert deletes, "mutation 模式应执行删除探活/清理"
        assert "/api/console/agents/42" in deletes, deletes
        for path in deletes:
            assert path.startswith("/api/console/agents/"), deletes
        # 只允许本次记录的 42 与 bogus 探活 id，不允许别的真实 id
        assert set(deletes) <= {"/api/console/agents/42", "/api/console/agents/99999999"}, deletes
        assert "retained_accounts" in out and "无删除端点" in out, out


def test_cleanup_refuses_unrecorded_resource():
    """ledger 之外的 id：即使调用方传进 allowlist 之外的参数也必须拒绝删除。"""
    sys.path.insert(0, str(REPO))
    from scripts.health_check import ResourceLedger, cleanup_created_resources

    ledger = ResourceLedger()
    ledger.record("agent", 42)
    ledger.add_allowlist("agent:43")
    assert ledger.may_delete("agent", 42) is True
    assert ledger.may_delete("agent", 43) is True   # 显式白名单
    assert ledger.may_delete("agent", 44) is False  # 未记录 ⇒ 拒绝

    with StubSite(routes={("DELETE", "/api/console/agents/42"):
                          (200, '{"status":"ok"}', "application/json")}) as site:
        result = cleanup_created_resources(site.base_url, ledger, "tok")
        assert result["deleted_agents"] == ["42"], result
        assert result["failed"] == [], result
        deleted_paths = [r["path"] for r in site.requests if r["method"] == "DELETE"]
        assert deleted_paths == ["/api/console/agents/42"], deleted_paths


# ─────────────────── 5. /ready 门禁 ───────────────────

def _ready_rc(payload, status=200):
    with StubSite(routes={("GET", "/ready"): (status, payload, "application/json"),
                          ("GET", "/login"): (200, "ok", "text/html")}) as site:
        rc, out = run_main(["--ready-check", "--base-url", site.base_url])
        return rc, out


def test_ready_passes_when_status_ready():
    rc, out = _ready_rc('{"status":"ready","checks":{"database":{"ok":true}}}')
    assert rc == 0, out
    assert "ready" in out


def test_ready_fails_on_empty_body():
    rc, out = _ready_rc("")
    assert rc == 1, out
    assert "empty-body" in out


def test_ready_fails_on_unhealthy_status():
    rc, out = _ready_rc('{"status":"unhealthy","checks":{}}')
    assert rc == 1, out
    assert "unhealthy" in out or "status=" in out


def test_ready_fails_on_503():
    rc, out = _ready_rc('{"status":"unhealthy","checks":{"database":{"ok":false}}}', status=503)
    assert rc == 1, out
    assert "http-503" in out


def test_ready_fails_on_bad_schema_report():
    # 数据库连通但 schema/migration 未就绪 ⇒ 服务端返回 unhealthy
    rc, out = _ready_rc(json.dumps({
        "status": "unhealthy",
        "checks": {"database": {"ok": True},
                   "schema": {"ok": False, "version_state": "mismatch"}},
    }))
    assert rc == 1, out


def test_ready_fails_on_invalid_json():
    rc, out = _ready_rc("<html>502 Bad Gateway</html>")
    assert rc == 1, out
    assert "invalid-json" in out


def test_probe_ready_reports_reasons():
    sys.path.insert(0, str(REPO))
    from scripts.health_check import probe_ready

    with StubSite(routes={("GET", "/ready"): (200, '{"status":"ready"}', "application/json")}) as site:
        ok, report = probe_ready(site.base_url, timeout=5)
        assert ok is True and report["status"] == "ready", report
    with StubSite(routes={("GET", "/ready"): (200, "", "application/json")}) as site:
        ok, report = probe_ready(site.base_url, timeout=5)
        assert ok is False and report["reason"] == "empty-body", report


# ─────────────────── 6. deploy.sh 门禁契约 ───────────────────

def test_deploy_sh_gates_on_http_status_and_json_status():
    text = (REPO / "deploy.sh").read_text(encoding="utf-8")
    assert "/ready" in text, "deploy.sh 必须用 /ready 而不是只看 /health"
    # 退出码
    assert "FAIL" in text and "exit 1" in text
    # HTTP status
    assert "%{http_code}" in text, "必须显式取 HTTP status"
    assert '"200"' in text or "200" in text
    # JSON status == ready
    assert "json.load" in text or "json.loads" in text, "必须解析 JSON 而不是只 grep 文本"
    assert "ready" in text
    # 空输出必须失败
    assert "-z " in text or "-n " in text


# ─────────────── 7. /ready 端点（真实 DB 语义，依赖缺失则 skip） ───────────────

def _build_sqlite_db(tmpdir, schema_version=None, drop_tables=(), drop_columns=()):
    """建一个带全部模型表的临时 SQLite；返回 (engine, SessionLocal)。

    schema_version 用 T20a 的真实模型形状（version + step_name + applied_at），
    不再手写简化表 —— 否则会和 models.SchemaVersion 的 NOT NULL 列冲突。
    """
    _load("sqlalchemy")
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from datetime import datetime

    models_db = _load("models.database")
    # 导入所有模型，Base.metadata 才能建全表（conftest.real_db 同样做法）
    for name in ("models.agent_profile", "models.agent_buffer", "models.group",
                 "models.fehub", "models.zentrim", "models.organization"):
        _load(name)

    engine = create_engine(f"sqlite:///{tmpdir}/feclaw.db")
    models_db.Base.metadata.create_all(bind=engine)
    for table in drop_tables:
        with engine.connect() as conn:
            conn.execute(text(f"DROP TABLE {table}"))
            conn.commit()
    for table, column in drop_columns:
        with engine.connect() as conn:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
            conn.commit()
    if schema_version is not None:
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        try:
            session.add(models_db.SchemaVersion(
                version=int(schema_version),
                step_name="test-baseline",
                applied_at=datetime.utcnow(),
                note="health-check test fixture",
            ))
            session.commit()
        finally:
            session.close()
    return engine, sessionmaker(bind=engine)


def _ready_http(engine, patch_storage_ok=True):
    """用 TestClient 请求 /ready；不进入 lifespan，因此不会跑启动期 DDL/admin 重置。"""
    main = _load("main")
    testclient_mod = _load("fastapi.testclient")

    original_engine = main._ready_engine
    original_storage = main._check_storage
    main._ready_engine = lambda: engine
    if patch_storage_ok:
        main._check_storage = lambda: {"ok": True, "backend": "test"}
    try:
        client = testclient_mod.TestClient(main.app, raise_server_exceptions=False)
        return client.get("/ready")
    finally:
        main._ready_engine = original_engine
        main._check_storage = original_storage


def _agent_count(SessionLocal):
    profile_mod = _load("models.agent_profile")
    session = SessionLocal()
    try:
        return session.query(profile_mod.AgentProfile).count()
    finally:
        session.close()


def test_ready_endpoint_returns_ready_and_leaves_agent_count_unchanged():
    tmpdir = tempfile.mkdtemp(prefix="hc-ready-")
    engine, SessionLocal = _build_sqlite_db(tmpdir)

    profile_mod = _load("models.agent_profile")
    session = SessionLocal()
    for i in range(2):
        session.add(profile_mod.AgentProfile(
            user_id=1, hash=f"hc00000{i}", totp_secret="JBSWY3DPEHPK3PXP",
            name=f"agent-{i}", status="initialized",
        ))
    session.commit()
    session.close()

    before = _agent_count(SessionLocal)
    response = _ready_http(engine)
    after = _agent_count(SessionLocal)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"]["database"]["ok"] is True
    schema = payload["checks"]["schema"]
    assert schema["ok"] is True
    # create_all 建出 schema_version 表但没有迁移记录 ⇒ 如实报 unrecorded，
    # 结构检查仍生效（不把“没记录”伪装成“版本正常”，也不因此拒绝就绪）
    assert schema["version_state"] in ("unrecorded", "present", "absent")
    assert schema.get("critical_columns_missing") == {}
    # 探活不得改变库中 Agent 数量
    assert before == after == 2


def test_ready_endpoint_503_when_database_unreachable():
    sqlalchemy = _load("sqlalchemy")
    # 指向不存在的目录 ⇒ 连接必然失败（确定性，且不需要真 MySQL）
    bad_engine = sqlalchemy.create_engine(
        f"sqlite:///{tempfile.mkdtemp(prefix='hc-bad-')}/missing_dir/db.sqlite3"
    )
    response = _ready_http(bad_engine)
    assert response.status_code == 503, response.text
    payload = response.json()
    assert payload["status"] == "unhealthy"
    assert payload["checks"]["database"]["ok"] is False
    # 只暴露异常类名：不回显 DSN / 口令 / 堆栈
    error = payload["checks"]["database"]["error"]
    assert isinstance(error, str) and error and "sqlite" not in error.lower()
    body = response.text.lower()
    for secret_hint in ("password", "mysql+pymysql", "sqlite:/"):
        assert secret_hint not in body, f"/ready 泄露了内部信息: {secret_hint}"


def test_ready_endpoint_503_when_required_table_missing():
    tmpdir = tempfile.mkdtemp(prefix="hc-missing-")
    engine, _ = _build_sqlite_db(tmpdir, drop_tables=("agent_config",))
    response = _ready_http(engine)
    assert response.status_code == 503, response.text
    payload = response.json()
    assert payload["status"] == "unhealthy"
    assert "agent_config" in payload["checks"]["schema"]["missing_tables"]


def test_ready_endpoint_200_when_schema_version_table_absent():
    """T20 尚未落地的旧库：schema_version 表不存在 ⇒ 可选检查（结构仍判定）。"""
    tmpdir = tempfile.mkdtemp(prefix="hc-nosv-")
    engine, _ = _build_sqlite_db(tmpdir, drop_tables=("schema_version",))
    response = _ready_http(engine)
    assert response.status_code == 200, response.text
    payload = response.json()
    schema = payload["checks"]["schema"]
    assert schema["ok"] is True
    assert schema["version_state"] == "absent"
    assert "version_note" in schema


def test_ready_endpoint_503_when_critical_column_missing():
    """T20a CRITICAL_COLUMNS 缺列 ⇒ 相关接口必 500，/ready 必须发现。"""
    tmpdir = tempfile.mkdtemp(prefix="hc-col-")
    try:
        engine, _ = _build_sqlite_db(tmpdir, drop_columns=(("users", "is_active"),))
    except Exception as exc:  # noqa: BLE001 —— 老 SQLite 不支持 DROP COLUMN
        _skip(f"SQLite 不支持 DROP COLUMN：{type(exc).__name__}: {exc}")
    response = _ready_http(engine)
    assert response.status_code == 503, response.text
    payload = response.json()
    schema = payload["checks"]["schema"]
    assert schema["ok"] is False
    assert schema["critical_columns_missing"].get("users") == ["is_active"]


def test_ready_endpoint_503_when_schema_version_too_old():
    tmpdir = tempfile.mkdtemp(prefix="hc-ver-")
    engine, _ = _build_sqlite_db(tmpdir, schema_version=0)
    response = _ready_http(engine)
    assert response.status_code == 503, response.text
    payload = response.json()
    schema = payload["checks"]["schema"]
    assert schema["version_state"] == "present"
    assert schema["version"] == 0
    assert schema["ok"] is False


def test_ready_endpoint_200_when_schema_version_satisfies_minimum():
    init_meta = _load("services.schema_version")
    tmpdir = tempfile.mkdtemp(prefix="hc-ver-ok-")
    engine, _ = _build_sqlite_db(tmpdir, schema_version=init_meta.SCHEMA_VERSION)
    response = _ready_http(engine)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"]["schema"]["version_state"] == "present"


def test_ready_endpoint_503_when_storage_unavailable():
    tmpdir = tempfile.mkdtemp(prefix="hc-storage-")
    engine, _ = _build_sqlite_db(tmpdir)

    main = _load("main")
    testclient_mod = _load("fastapi.testclient")
    original_engine = main._ready_engine
    original_storage = main._check_storage
    main._ready_engine = lambda: engine
    main._check_storage = lambda: {"ok": False, "backend": "local", "writable": False}
    try:
        client = testclient_mod.TestClient(main.app, raise_server_exceptions=False)
        response = client.get("/ready")
    finally:
        main._ready_engine = original_engine
        main._check_storage = original_storage

    assert response.status_code == 503, response.text
    payload = response.json()
    assert payload["checks"]["storage"]["ok"] is False


def test_health_is_lightweight_and_never_collects_readiness():
    main = _load("main")
    testclient_mod = _load("fastapi.testclient")

    def _boom():
        raise AssertionError("/health 不应触发 readiness 采集（DB/存储）")

    original = main.collect_readiness
    main.collect_readiness = _boom
    try:
        client = testclient_mod.TestClient(main.app, raise_server_exceptions=False)
        response = client.get("/health")
    finally:
        main.collect_readiness = original

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "healthy"}


MANUAL_TESTS = [
    test_default_run_issues_no_mutation_and_no_login,
    test_only_4_does_not_prepare_account,
    test_readonly_output_marks_skipped_mutations,
    test_mutations_require_integration_flag,
    test_mutations_require_explicit_base_url,
    test_mutations_refuse_production_host_without_network,
    test_mutations_refuse_public_host,
    test_mutation_mode_registers_own_account_and_cleans_only_ledger,
    test_cleanup_refuses_unrecorded_resource,
    test_ready_passes_when_status_ready,
    test_ready_fails_on_empty_body,
    test_ready_fails_on_unhealthy_status,
    test_ready_fails_on_503,
    test_ready_fails_on_bad_schema_report,
    test_ready_fails_on_invalid_json,
    test_probe_ready_reports_reasons,
    test_deploy_sh_gates_on_http_status_and_json_status,
    test_ready_endpoint_returns_ready_and_leaves_agent_count_unchanged,
    test_ready_endpoint_503_when_database_unreachable,
    test_ready_endpoint_503_when_required_table_missing,
    test_ready_endpoint_200_when_schema_version_table_absent,
    test_ready_endpoint_503_when_critical_column_missing,
    test_ready_endpoint_503_when_schema_version_too_old,
    test_ready_endpoint_200_when_schema_version_satisfies_minimum,
    test_ready_endpoint_503_when_storage_unavailable,
    test_health_is_lightweight_and_never_collects_readiness,
]


def _manual_run():
    """无 pytest 时的兜底执行器（依赖装齐后仍用 pytest 跑）。"""
    passed = failed = skipped = 0
    for fn in MANUAL_TESTS:
        try:
            fn()
        except unittest.SkipTest as exc:
            skipped += 1
            print(f"SKIP {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"PASS {fn.__name__}")
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_manual_run())
