"""Schema 版本与增量迁移目录（T20a）。

设计约束（规格 §5 T20 / §9 A22）：

* **正常启动不做 DDL、不重置密码**。启动期只允许 `Base.metadata.create_all()`
  建出缺失的表（新部署），既有表一律靠本模块登记的显式迁移步骤升级。
* **单一权威**：`SCHEMA_STEPS` 是“旧库需要补什么”的唯一清单；`/ready` 读同一份
  `CRITICAL_COLUMNS` 判定 schema 是否可用，不再各自维护一张表。
* **只加不删**：所有步骤都是 expand（加列/放宽/加索引），没有 DROP/收缩；
  唯一索引遇到重复数据只报 blocked，不自动去重（去重策略要人工批准）。
* **诚实**：SQLite 不支持 `MODIFY COLUMN` 这类 MySQL 专有步骤，本模块把它们标成
  `unsupported` 并如实报告，不用 SQLite 结果冒充 MySQL 通过；MySQL DDL 可能隐式
  commit，`apply()` 不宣传“整体可回滚事务”。
* **标识符一律加引号**：MySQL 8 里 `groups` 是保留字，裸写 `ALTER TABLE groups ...`
  会直接语法错误（历史的启动期迁移正是这样被 try/except 静默跳过的）。

命令行入口见 `scripts/migrate_learning_runtime.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# 当前代码期望的 schema 版本。迁移脚本每加一批 expand 步骤时 +1。
SCHEMA_VERSION = 1

# 版本表 /ready 也会读（main.py: READY_SCHEMA_VERSION_TABLE）。
SCHEMA_VERSION_TABLE = "schema_version"

# 关键列：旧库缺任何一列，相关接口就会 500，因此 /ready 必须能发现。
CRITICAL_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "users": ("email", "password_version", "tier", "jwt_version", "is_active"),
    "agent_profiles": ("sr_enabled", "agent_mode"),
    "groups": ("organization_id",),
    "group_members": ("job_description", "status", "allow_dm"),
    "group_messages": ("sender_hash",),
    "conversation_sessions": ("channel",),
    "chat_history": ("tool_call_id", "tool_name", "tool_args"),
    "share_mappings": ("share_hash",),
}


def _q(name: str) -> str:
    """标识符引用（反引号在 MySQL 与 SQLite 上都被接受）。"""
    return "`" + str(name).replace("`", "``") + "`"


@dataclass(frozen=True)
class Step:
    """一条增量迁移步骤（幂等：先 inspect，缺什么补什么）。"""

    name: str
    table: str
    kind: str                    # add_column | add_index | modify_column | set_default
    column: str = ""
    ddl: str = ""                # 真正执行的 DDL
    sqlite_compatible: bool = True
    index_name: str = ""
    index_columns: Tuple[str, ...] = ()
    unique: bool = False
    mysql_only: bool = False
    note: str = ""
    extra_ddl: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        target = f"{self.table}.{self.column}" if self.column else self.table
        return f"{self.name} ({target})"


def _add_column(name: str, table: str, column: str, type_sql: str,
                note: str = "") -> Step:
    return Step(name=name, table=table, kind="add_column", column=column,
                ddl=f"ALTER TABLE {_q(table)} ADD COLUMN {_q(column)} {type_sql}",
                note=note)


def _modify(name: str, table: str, column: str, definition: str,
            note: str = "") -> Step:
    """MySQL 专有的列定义调整（SQLite 上标 unsupported）。"""
    return Step(name=name, table=table, kind="modify_column", column=column,
                ddl=f"ALTER TABLE {_q(table)} MODIFY COLUMN {_q(column)} {definition}",
                sqlite_compatible=False, mysql_only=True, note=note)


# ── 迁移清单 ────────────────────────────────────────────────────────────────
# 与改造前 main.py lifespan 里的临时 ALTER 逐条对应（历史来源见各 step 注释），
# 外加 T20a 为后续任务预置的 expand 列（users.is_active）。
SCHEMA_STEPS: Tuple[Step, ...] = (
    # Q7/DEPLOY-GATE：旧库升级时 create_all 不会给已存在的表加列
    _add_column("users.email", "users", "email", "VARCHAR(128)", "旧库缺少 email 列"),
    _add_column("agent_profiles.sr_enabled", "agent_profiles", "sr_enabled",
                "BOOLEAN DEFAULT 0"),
    _add_column("agent_profiles.agent_mode", "agent_profiles", "agent_mode",
                "VARCHAR(20) DEFAULT 'classic'"),
    # P0.4 bcrypt 迁移
    _add_column("users.password_version", "users", "password_version",
                "INT NOT NULL DEFAULT 1"),
    # Q1 登出吊销
    _add_column("users.jwt_version", "users", "jwt_version",
                "INT NOT NULL DEFAULT 0"),
    # T18a/T20a：邀请制 + 停用账号（新增列，默认启用，不改变既有账号行为）
    _add_column("users.is_active", "users", "is_active",
                "BOOLEAN NOT NULL DEFAULT 1", "T18a 注册审批/停用账号所需"),
    _add_column("conversation_sessions.channel", "conversation_sessions", "channel",
                "VARCHAR(32) NULL"),
    _add_column("chat_history.tool_call_id", "chat_history", "tool_call_id",
                "VARCHAR(64) NULL"),
    _add_column("chat_history.tool_name", "chat_history", "tool_name",
                "VARCHAR(64) NULL"),
    _add_column("chat_history.tool_args", "chat_history", "tool_args", "JSON NULL"),
    _add_column("groups.organization_id", "groups", "organization_id", "INT NULL",
                "MySQL 8 中 groups 是保留字，DDL 必须加反引号"),
    _add_column("group_members.job_description", "group_members", "job_description",
                "TEXT NULL"),
    _add_column("group_members.status", "group_members", "status",
                "VARCHAR(16) DEFAULT 'dormant'"),
    _add_column("group_members.allow_dm", "group_members", "allow_dm",
                "BOOLEAN DEFAULT 1"),
    # P0.4 bcrypt：bcrypt 用户不需要 salt
    _modify("users.salt_nullable", "users", "salt", "VARCHAR(64) NULL",
            "bcrypt 用户 salt 为空，旧库是 NOT NULL"),
    # P1.3 agent_hash 列宽统一到 VARCHAR(8)（无损扩列；老 4 位 hash 数据不动）
    _modify("wechat_binding.agent_hash", "wechat_binding", "agent_hash", "VARCHAR(8)"),
    _modify("wechat_messages.agent_hash", "wechat_messages", "agent_hash", "VARCHAR(8)"),
    _modify("file_permissions.agent_hash", "file_permissions", "agent_hash", "VARCHAR(8)"),
    _modify("agent_config.agent_hash", "agent_config", "agent_hash", "VARCHAR(8)"),
    _modify("agent_usage_log.agent_hash", "agent_usage_log", "agent_hash", "VARCHAR(8)"),
    _modify("share_mappings.agent_hash", "share_mappings", "agent_hash", "VARCHAR(8)"),
    _modify("share_references.agent_hash", "share_references", "agent_hash", "VARCHAR(8)"),
    _modify("chat_history.agent_hash", "chat_history", "agent_hash", "VARCHAR(8)"),
    _modify("sandbox_tokens.agent_hash", "sandbox_tokens", "agent_hash", "VARCHAR(8)"),
    _modify("agent_profiles.hash", "agent_profiles", "hash", "VARCHAR(8)"),
    # FIX-E/P0-1：群消息发送者可能是 8 位 agent hash
    _modify("group_messages.sender_hash", "group_messages", "sender_hash",
            "VARCHAR(8) NULL"),
    # FIX-E/P0-1：share_hash 唯一索引（模型声明 unique，create_all 不给旧表补）
    Step(name="share_mappings.uq_share_hash", table="share_mappings", kind="add_index",
         index_name="uq_share_mappings_share_hash", index_columns=("share_hash",),
         unique=True, column="share_hash",
         note="有重复 share_hash 时 blocked，需人工批准去重策略"),
    Step(name="groups.idx_organization_id", table="groups", kind="add_index",
         index_name="idx_groups_organization_id", index_columns=("organization_id",),
         unique=False, column="organization_id"),
    # T18a/T20a：新用户默认 free（旧行不动；只改服务端默认值）
    Step(name="users.tier_default_free", table="users", kind="set_default",
         column="tier", mysql_only=True, sqlite_compatible=False,
         ddl="ALTER TABLE `users` ALTER COLUMN `tier` SET DEFAULT 'free'",
         note="新用户默认 free，既有行不迁移"),
)


# ── 内省工具 ────────────────────────────────────────────────────────────────

def is_mysql(engine: Engine) -> bool:
    return engine.dialect.name in ("mysql", "mariadb")


def table_columns(engine: Engine, table: str) -> Optional[List[str]]:
    """返回表的列名；表不存在返回 None。"""
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return None
    return [col["name"] for col in inspector.get_columns(table)]


def index_names(engine: Engine, table: str) -> List[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return []
    return [idx.get("name") or "" for idx in inspector.get_indexes(table)]


def has_equivalent_index(engine: Engine, table: str, columns: Sequence[str],
                         unique: bool) -> bool:
    """是否已存在等价的索引/唯一约束（不要求名字一致）。

    `create_all` 给 `unique=True` 的列建的是 `ix_<table>_<col>`，与手写迁移里的
    `uq_...` 名字不同；只按名字判断会把“已唯一”误判成 pending，然后重复建索引。
    """
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return False
    wanted = tuple(columns)
    for idx in inspector.get_indexes(table):
        if tuple(idx.get("column_names") or ()) != wanted:
            continue
        if bool(idx.get("unique")) == bool(unique):
            return True
    if unique:
        for uc in inspector.get_unique_constraints(table):
            if tuple(uc.get("column_names") or ()) == wanted:
                return True
    return False


def mysql_column(engine: Engine, table: str, column: str) -> Optional[Dict[str, Any]]:
    """读 information_schema 的列定义；非 MySQL 返回 None。"""
    if not is_mysql(engine):
        return None
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ), {"t": table, "c": column}).fetchone()
    if row is None:
        return None
    return {"type": row[0], "nullable": row[1], "default": row[2]}


def duplicate_share_hashes(engine: Engine) -> List[Tuple[str, int]]:
    """返回 (share_hash, 次数) 的重复项；表/列不存在时返回空。"""
    cols = table_columns(engine, "share_mappings")
    if not cols or "share_hash" not in cols:
        return []
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT share_hash, COUNT(*) AS c FROM `share_mappings` "
            "GROUP BY share_hash HAVING COUNT(*) > 1 ORDER BY c DESC LIMIT 20"
        )).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def schema_version_rows(engine: Engine) -> List[Dict[str, Any]]:
    """读 schema_version 记录（表不存在返回空）。"""
    cols = table_columns(engine, SCHEMA_VERSION_TABLE)
    if not cols:
        return []
    order = "applied_at" if "applied_at" in cols else "version"
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT * FROM {_q(SCHEMA_VERSION_TABLE)} ORDER BY {order}"
        )).mappings().fetchall()
    return [dict(r) for r in rows]


def current_version(engine: Engine) -> Optional[int]:
    rows = schema_version_rows(engine)
    versions = [int(r["version"]) for r in rows if r.get("version") is not None]
    return max(versions) if versions else None


# ── plan / apply ────────────────────────────────────────────────────────────

def _target_type(ddl: str, column: str) -> str:
    """从 `... MODIFY COLUMN `col` TYPE ...` 里抠出目标类型。"""
    upper = ddl.upper()
    marker = f"MODIFY COLUMN {_q(column).upper()} "
    idx = upper.find(marker)
    if idx < 0:
        return ""
    rest = ddl[idx + len(marker):].strip()
    tokens = rest.split()
    return tokens[0] if tokens else ""


def _step_state(engine: Engine, step: Step) -> str:
    """返回 pending / present / blocked / unsupported / missing_table。"""
    cols = table_columns(engine, step.table)
    if cols is None:
        return "missing_table"

    if step.kind == "add_column":
        return "present" if step.column in cols else "pending"

    if step.kind == "add_index":
        # 索引依赖的列可能由更早的 add_column 步骤补上：plan 阶段按 pending 报告，
        # 真正 apply 时再校验列是否存在（见 apply 中的 missing_table 分支）。
        if step.column and step.column not in cols:
            return "pending"
        if step.index_name in index_names(engine, step.table):
            return "present"
        if has_equivalent_index(engine, step.table, step.index_columns, step.unique):
            return "present"
        if step.unique and step.index_columns == ("share_hash",):
            if duplicate_share_hashes(engine):
                return "blocked"
        return "pending"

    if step.kind in ("modify_column", "set_default"):
        if step.column and step.column not in cols:
            return "missing_table"
        if step.mysql_only and not is_mysql(engine):
            return "unsupported"
        info = mysql_column(engine, step.table, step.column)
        if info is None:
            return "unsupported"
        if step.kind == "set_default":
            current = (info.get("default") or "").strip("'\"")
            return "present" if current == "free" else "pending"
        want_type = _target_type(step.ddl, step.column)
        upper = step.ddl.upper().rstrip()
        # 只把显式以 NULL 结尾的定义当“应该可空”；`NOT NULL` 结尾视为非空。
        want_nullable = upper.endswith("NULL") and not upper.endswith("NOT NULL")
        ok_type = not want_type or want_type.upper() == str(info.get("type", "")).upper()
        ok_null = not want_nullable or str(info.get("nullable", "YES")).upper() == "YES"
        return "present" if (ok_type and ok_null) else "pending"

    return "unsupported"


def plan(engine: Engine, steps: Optional[Sequence[Step]] = None) -> List[Dict[str, Any]]:
    """只读预览：每条步骤当前状态 + 需要执行的 DDL。不写库。"""
    rows: List[Dict[str, Any]] = []
    for step in (steps or SCHEMA_STEPS):
        state = _step_state(engine, step)
        rows.append({
            "version": SCHEMA_VERSION,
            "name": step.name,
            "table": step.table,
            "column": step.column,
            "kind": step.kind,
            "state": state,
            "ddl": step.ddl,
            "note": step.note,
        })
    return rows


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return counts


def _record(engine: Engine, step: Step, note: str = "") -> None:
    """写入一条 schema_version 记录（幂等：同 version+step 不重复）。"""
    from sqlalchemy.orm import Session

    from models.database import SchemaVersion

    SchemaVersion.__table__.create(bind=engine, checkfirst=True)
    with Session(engine) as session:
        exists = session.query(SchemaVersion).filter(
            SchemaVersion.version == SCHEMA_VERSION,
            SchemaVersion.step_name == step.name,
        ).first()
        if exists:
            return
        session.add(SchemaVersion(version=SCHEMA_VERSION, step_name=step.name,
                                  note=note or step.note or ""))
        session.commit()


def mark_release(engine: Engine, note: str = "") -> bool:
    """登记“本版本已完整应用”标记（幂等）。

    迁移步骤可能全部已经 present（例如刚 `init_db` 的新库、或上一次已 apply 过），
    这时没有任何 DDL 要跑，但 `/ready` 仍需要一个版本号；这里补一条 release 记录。
    """
    from sqlalchemy.orm import Session

    from models.database import SchemaVersion

    SchemaVersion.__table__.create(bind=engine, checkfirst=True)
    step_name = f"release:v{SCHEMA_VERSION}"
    with Session(engine) as session:
        exists = session.query(SchemaVersion).filter(
            SchemaVersion.version == SCHEMA_VERSION,
            SchemaVersion.step_name == step_name,
        ).first()
        if exists:
            return False
        session.add(SchemaVersion(version=SCHEMA_VERSION, step_name=step_name,
                                  note=note or "schema steps verified complete"))
        session.commit()
        return True


def apply(engine: Engine, steps: Optional[Sequence[Step]] = None,
          only: Optional[Iterable[str]] = None,
          stop_before_blocked: bool = False) -> Dict[str, Any]:
    """执行 pending 步骤并记录版本。

    返回 {"applied": [...], "skipped": [...], "blocked": [...],
    "unsupported": [...], "missing_table": [...]}。
    幂等：重复执行不会重复 DDL（每步先 inspect）。
    MySQL 的 DDL 可能隐式 commit，因此这里不承诺“失败整体回滚”。
    """
    selected = list(steps or SCHEMA_STEPS)
    if only:
        wanted = {s.strip() for s in only if s and s.strip()}
        selected = [s for s in selected if s.name in wanted]

    result: Dict[str, Any] = {"applied": [], "skipped": [], "blocked": [],
                              "unsupported": [], "missing_table": [],
                              "release_marked": False}
    for step in selected:
        state = _step_state(engine, step)
        if state == "present":
            result["skipped"].append(step.name)
            continue
        if state in ("blocked", "unsupported", "missing_table"):
            result[state].append(step.name)
            if state == "blocked" and stop_before_blocked:
                break
            continue
        if step.kind == "add_index":
            cols_now = table_columns(engine, step.table) or []
            if step.column and step.column not in cols_now:
                result["missing_table"].append(step.name)
                continue
            sql = "CREATE UNIQUE INDEX" if step.unique else "CREATE INDEX"
            cols = ", ".join(_q(c) for c in step.index_columns)
            ddl = f"{sql} {_q(step.index_name)} ON {_q(step.table)} ({cols})"
            with engine.begin() as conn:
                conn.execute(text(ddl))
        else:
            with engine.begin() as conn:
                conn.execute(text(step.ddl))
                for extra in step.extra_ddl:
                    conn.execute(text(extra))
        _record(engine, step)
        result["applied"].append(step.name)

    # 全部步骤已就绪（只剩 present/unsupported）时登记 release 标记，保证
    # `/ready` 能读到版本；还有 pending/blocked 时不写，避免“报喜不报忧”。
    remaining = {row["state"] for row in plan(engine)}
    if not remaining & {"pending", "blocked", "missing_table"}:
        result["release_marked"] = mark_release(engine)
    return result
