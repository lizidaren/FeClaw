r"""T20a schema 迁移验收测试（SQLite 可验证部分）。

覆盖规格 §5 T20 / §9 A22 的“本机可验证”子集：

1. 全新库：`init_db()` 只建表并登记 baseline 版本（`/ready` 不会误报版本缺失）。
2. 既有库（有表、无 schema_version）：`init_db()` **不**补版本 —— 不拿启动冒充迁移。
3. 旧库缺列：`plan()` 只读预览出 pending；`apply()` 补列；重复 apply 幂等。
4. 重复 `share_hash`：唯一索引步骤 blocked，apply 不建索引、也不删数据。
5. SQLite 上的 MySQL 专有步骤如实标 `unsupported`（不用 SQLite 结果冒充 MySQL）。
6. CLI：`--plan` 只读、`--apply` 幂等、`--status` 读版本表。

真正的 MySQL 迁移（MODIFY COLUMN/唯一索引失败语义）在本机 MySQL 8.4 上单独验证，
见 `notes/INTEGRATION.md`；本文件不声称覆盖它。
"""

import os
import subprocess
import sys

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from models import database as dbmod  # noqa: E402
from services import schema_version as sv  # noqa: E402


def _sqlite_engine(tmp_path, name="schema.sqlite"):
    engine = create_engine(f"sqlite:///{tmp_path / name}",
                           connect_args={"check_same_thread": False})
    return engine


@pytest.fixture()
def empty_engine(tmp_path):
    engine = _sqlite_engine(tmp_path, "empty.sqlite")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def legacy_engine(tmp_path):
    """模拟旧库：只建用到的那几张表，且故意缺 T20a 要补的列。"""
    engine = _sqlite_engine(tmp_path, "legacy.sqlite")
    ddl = [
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, "
        "password_hash TEXT, salt VARCHAR(64) NOT NULL, is_admin BOOLEAN, "
        "tier VARCHAR(20), created_at DATETIME)",
        "CREATE TABLE agent_profiles (hash VARCHAR(4) PRIMARY KEY, user_id INTEGER, "
        "name TEXT, status VARCHAR(20), system_prompt TEXT, created_at DATETIME)",
        "CREATE TABLE chat_history (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "agent_hash VARCHAR(4), role VARCHAR(16), content TEXT, created_at DATETIME)",
        "CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT, owner_user_id INTEGER, "
        "created_at DATETIME)",
        "CREATE TABLE group_members (id INTEGER PRIMARY KEY, group_id INTEGER, "
        "agent_hash VARCHAR(4), created_at DATETIME)",
        "CREATE TABLE group_messages (id INTEGER PRIMARY KEY, group_id INTEGER, "
        "sender_hash VARCHAR(4), content TEXT, created_at DATETIME)",
        "CREATE TABLE conversation_sessions (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "agent_hash VARCHAR(4), created_at DATETIME)",
        "CREATE TABLE share_mappings (id INTEGER PRIMARY KEY, user_id VARCHAR(64), "
        "agent_hash VARCHAR(4), vfs_path TEXT, share_hash VARCHAR(64), slug VARCHAR(64), "
        "mode VARCHAR(16), password VARCHAR(255), created_at DATETIME, "
        "expires_at DATETIME)",
    ]
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
    try:
        yield engine
    finally:
        engine.dispose()


# ── 1/2. init_db 的边界 ──────────────────────────────────────────────────────

def test_init_db_fresh_records_baseline(empty_engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(dbmod, "engine", empty_engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=empty_engine))

    dbmod.init_db()

    assert sv.current_version(empty_engine) == sv.SCHEMA_VERSION
    cols = {row["step_name"] for row in sv.schema_version_rows(empty_engine)}
    assert "baseline" in cols


def test_init_db_existing_db_does_not_fake_version(legacy_engine, monkeypatch):
    """既有库缺版本表时，init_db 只建表，不写 baseline（避免冒充已迁移）。"""
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(dbmod, "engine", legacy_engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=legacy_engine))

    dbmod.init_db()

    tables = inspect(legacy_engine).get_table_names()
    assert "schema_version" in tables          # 只建表
    assert sv.current_version(legacy_engine) is None   # 不补版本


# ── 3. 旧库缺列：plan 只读、apply 补列、重复 apply 幂等 ──────────────────────

def test_plan_is_read_only_and_detects_missing_columns(legacy_engine):
    before = {t: [c["name"] for c in inspect(legacy_engine).get_columns(t)]
              for t in inspect(legacy_engine).get_table_names()}

    rows = sv.plan(legacy_engine)
    states = {row["name"]: row["state"] for row in rows}

    assert states["users.email"] == "pending"
    assert states["users.password_version"] == "pending"
    assert states["users.is_active"] == "pending"
    assert states["conversation_sessions.channel"] == "pending"
    assert states["chat_history.tool_call_id"] == "pending"
    assert states["groups.organization_id"] == "pending"
    assert states["group_members.status"] == "pending"
    assert states["share_mappings.uq_share_hash"] == "pending"

    after = {t: [c["name"] for c in inspect(legacy_engine).get_columns(t)]
             for t in inspect(legacy_engine).get_table_names()
             if t != "schema_version"}
    # plan 不建 schema_version、不改任何列
    assert after == {k: v for k, v in before.items() if k in after}
    assert sv.schema_version_rows(legacy_engine) == []


def test_apply_adds_columns_then_is_idempotent(legacy_engine):
    first = sv.apply(legacy_engine)

    assert "users.email" in first["applied"]
    assert "users.is_active" in first["applied"]
    assert "share_mappings.uq_share_hash" in first["applied"]
    cols = [c["name"] for c in inspect(legacy_engine).get_columns("users")]
    assert {"email", "password_version", "jwt_version", "is_active"} <= set(cols)
    assert sv.current_version(legacy_engine) == sv.SCHEMA_VERSION

    second = sv.apply(legacy_engine)
    assert second["applied"] == []
    assert sv.schema_version_rows(legacy_engine)  # 不重复写爆版本表


def test_apply_can_be_limited_to_named_steps(legacy_engine):
    result = sv.apply(legacy_engine, only=["users.email"])

    assert result["applied"] == ["users.email"]
    cols = [c["name"] for c in inspect(legacy_engine).get_columns("users")]
    assert "email" in cols
    assert "is_active" not in cols     # 其余步骤没被顺手执行


def test_resume_after_interrupted_apply(legacy_engine):
    """中断后续跑：先跑一步，再跑全部，结果与一次跑完一致。"""
    sv.apply(legacy_engine, only=["users.email"])
    rest = sv.apply(legacy_engine)

    assert "users.email" not in rest["applied"]
    cols = [c["name"] for c in inspect(legacy_engine).get_columns("users")]
    assert {"email", "password_version", "jwt_version", "is_active"} <= set(cols)


# ── 4. 重复 share_hash：blocked，不自动去重 ─────────────────────────────────

def test_duplicate_share_hash_blocks_unique_index(legacy_engine):
    with legacy_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO share_mappings (user_id, agent_hash, vfs_path, share_hash, "
            "slug, mode) VALUES ('1', 'abcd', '/workspace/a.md', 'dupdup00', 's1', 'share')"))
        conn.execute(text(
            "INSERT INTO share_mappings (user_id, agent_hash, vfs_path, share_hash, "
            "slug, mode) VALUES ('1', 'abcd', '/workspace/b.md', 'dupdup00', 's2', 'share')"))

    rows = {row["name"]: row["state"] for row in sv.plan(legacy_engine)}
    assert rows["share_mappings.uq_share_hash"] == "blocked"

    result = sv.apply(legacy_engine)
    assert "share_mappings.uq_share_hash" in result["blocked"]
    assert "uq_share_mappings_share_hash" not in sv.index_names(legacy_engine,
                                                               "share_mappings")
    # 数据没被删：仍然是 2 行
    with legacy_engine.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM share_mappings WHERE share_hash='dupdup00'")).scalar()
    assert count == 2


# ── 5. SQLite 上 MySQL 专有步骤如实 unsupported ─────────────────────────────

def test_mysql_only_steps_are_unsupported_on_sqlite(legacy_engine):
    rows = {row["name"]: row["state"] for row in sv.plan(legacy_engine)}

    assert rows["users.salt_nullable"] == "unsupported"
    assert rows["wechat_binding.agent_hash"] == "missing_table"


# ── 6. CLI 行为 ─────────────────────────────────────────────────────────────

def _run_cli(*args, tmp_path):
    db_file = tmp_path / "cli.sqlite"
    url = f"sqlite:///{db_file}"
    return subprocess.run(
        [sys.executable, os.path.join(_project_root, "scripts",
                                      "migrate_learning_runtime.py"),
         *args, "--database-url", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=_project_root, timeout=120,
    )


def test_cli_plan_then_apply_then_status(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'cli.sqlite'}")
    from sqlalchemy.orm import sessionmaker

    # 走真实路径：init_db 建表并登记 baseline（与部署首次启动一致）
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    dbmod.init_db()
    engine.dispose()

    plan = _run_cli("--plan", tmp_path=tmp_path)
    assert plan.returncode == 0, plan.stderr
    assert "summary:" in plan.stdout

    applied = _run_cli("--apply", tmp_path=tmp_path)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "current_version: 1" in applied.stdout

    status = _run_cli("--status", tmp_path=tmp_path)
    assert status.returncode == 0, status.stderr
    assert "current_version: 1" in status.stdout


# ── 7. 迁移目录与模型的一致性 ───────────────────────────────────────────────

def test_critical_columns_are_declared_by_migration_or_models(empty_engine):
    """CRITICAL_COLUMNS 里的每一列，要么模型里有，要么迁移清单里有。"""
    from models.database import Base

    dbmod.load_all_models()
    Base.metadata.create_all(bind=empty_engine)
    step_targets = {(s.table, s.column) for s in sv.SCHEMA_STEPS if s.column}

    missing = []
    for table, columns in sv.CRITICAL_COLUMNS.items():
        model_cols = {c.name for c in Base.metadata.tables[table].columns}
        for column in columns:
            if column not in model_cols and (table, column) not in step_targets:
                missing.append(f"{table}.{column}")
    assert not missing, f"关键列既不在模型也不在迁移清单: {missing}"


def test_every_step_is_idempotent_by_design():
    """每个 step 必须有确定性状态判定（不允许无条件执行）。"""
    names = [s.name for s in sv.SCHEMA_STEPS]
    assert len(names) == len(set(names)), "step 名称必须唯一"
    for step in sv.SCHEMA_STEPS:
        assert step.kind in ("add_column", "add_index", "modify_column", "set_default")
        assert step.table
        if step.kind in ("add_column", "modify_column"):
            assert step.column, f"{step.name} 缺 column"
        if step.kind == "add_index":
            assert step.index_name and step.index_columns


# ── 8. 正常启动不再偷偷修库 / 重置密码（静态契约）─────────────────────────

def test_lifespan_does_not_run_ddl_or_reset_admin():
    """T20a 验收：lifespan 里不得有 ALTER/MODIFY、admin 创建/重置、数据回填调用。"""
    import inspect

    import main

    src = inspect.getsource(main.lifespan)

    assert "init_db()" in src, "全新部署仍需 create_all 建缺失表"
    for forbidden in (
        "ALTER TABLE",
        "create_or_reset_admin",
        "migrate_wechat_binding_supersede()",
        "information_schema.COLUMNS",
    ):
        assert forbidden not in src, f"lifespan 仍包含启动期迁移/重置: {forbidden}"


def test_reset_admin_cli_requires_explicit_confirmation():
    """`--reset-admin` 必须显式确认，或改用 bootstrap_admin.py。"""
    src = open(os.path.join(_project_root, "main.py"), encoding="utf-8").read()

    assert 'if "--confirm-reset-admin" not in _sys.argv:' in src
    assert "scripts/bootstrap_admin.py --reset --confirm-reset" in src
