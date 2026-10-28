"""T04 资源授权底座验收测试（services/resource_access.py）。

覆盖（规格 §5 T04 / §9）：
1. 归一化拒绝所有已知绕过向量：`..`、编码 `%2e%2e`/`%2f`/`%5c`、反斜杠、
   盘符/UNC 绝对路径、`file://`/`http(s)://`、NUL/控制字符、`~`、超长。
2. 同一逻辑文件的多种写法 → 同一 canonical_path / storage_key（agent/group/user/public）。
3. resolve_for_user：跨 agent、跨用户、非群成员一律拒绝；群主/成员放行；群不存在 404。
4. 存在性只读探测：用真实 LocalStorage（不 mock 被测逻辑），命中 True / 未命中 False，
   且**不存在回退**（换个租户前缀再试）。
5. can_read / can_write 接 ExecutionContext：readonly 不能写；敏感文件（identity.md、
   soul.md 及大写变体）写被拒（复用 permission_service 语义）；`.logs` 不可写；
   公共区只读；群 `.share` 需审批、`.ref/.attach` 永不可写；
   DB 里 FilePermission 的 read 覆盖同样生效。
6. 分享映射 → scope="share" 的只读资源（复用 share_service 边界）。

运行：
    $env:PYTHONPATH='<repo>'; $env:SETUP_COMPLETE='true'; $env:JWT_SECRET='local-test-jwt-secret-not-for-prod'
    $env:DATABASE_URL='mysql+pymysql://root:@127.0.0.1:3307/feclaw?charset=utf8mb4'
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_resource_access.py -q
"""

import importlib
import os
import sys
import uuid

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

pytest.importorskip("sqlalchemy", reason="T04 资源授权测试需要仓库 .venv 依赖")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models.agent_profile import AgentProfile  # noqa: E402
from models.database import Base, FilePermission, ShareMapping  # noqa: E402
from models.group import Group, GroupMember  # noqa: E402
from services.execution_policy import (  # noqa: E402
    POLICY_MODE_FULL,
    POLICY_MODE_READONLY,
    POLICY_MODE_RESTRICTED,
    RESOURCE_SCOPES,
    SCOPE_AGENT,
    SCOPE_GROUP,
    SCOPE_PUBLIC,
    SCOPE_USER,
    ExecutionContext,
)
from services.local_storage import LocalStorage  # noqa: E402
from services.resource_access import (  # noqa: E402
    KIND_DIR,
    KIND_FILE,
    ResolvedResource,
    ResourceAccessError,
    access_reason,
    can_read,
    can_write,
    normalize_resource_path,
    resolve_for_user,
    resolve_share_mapping_resource,
)

AGENT = "abcd1234"
OTHER_AGENT = "ffff0000"
OWNER_ID = 2
OTHER_USER_ID = 3
GROUP_ID = "11111111-1111-1111-1111-111111111111"
OTHER_GROUP_ID = "22222222-2222-2222-2222-222222222222"


# ── 基础设施 ────────────────────────────────────────────────────────────────

@pytest.fixture()
def session_factory(tmp_path):
    """临时 SQLite 会话（真实 SQL，不是 mock）。"""
    for _model_name in (
        "database", "agent_profile", "agent_buffer", "chat", "chat_input",
        "fehub", "group", "kaoxiang_models", "organization", "zentrim",
    ):
        try:
            importlib.import_module(f"models.{_model_name}")
        except ImportError:  # 可选模型缺失不阻塞资源授权测试
            continue

    engine = create_engine(
        f"sqlite:///{tmp_path / 'resource_access.sqlite'}",
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
def storage(tmp_path):
    """真实 LocalStorage（root/public 都指向临时目录）。"""
    return LocalStorage(
        root_dir=str(tmp_path / "store"),
        public_root=str(tmp_path / "public"),
    )


def add_agent(db, agent_hash=AGENT, user_id=OWNER_ID):
    agent = AgentProfile(hash=agent_hash, user_id=user_id, totp_secret="JBSWY3DPEHPK3PXP")
    db.add(agent)
    db.flush()
    return agent


def add_group(db, group_id=GROUP_ID, owner_user_id=OWNER_ID):
    group = Group(id=group_id, name="t04 group", owner_user_id=owner_user_id)
    db.add(group)
    db.flush()
    return group


def add_member(db, group_id=GROUP_ID, agent_hash=AGENT):
    member = GroupMember(group_id=group_id, agent_hash=agent_hash)
    db.add(member)
    db.flush()
    return member


def _ctx(**overrides) -> ExecutionContext:
    base = dict(
        user_id=str(OWNER_ID),
        agent_hash=AGENT,
        channel="web",
        policy_mode=POLICY_MODE_FULL,
        capabilities=frozenset({"file.read", "file.write", "file.delete"}),
        allowed_resource_scopes=frozenset(RESOURCE_SCOPES),
    )
    base.update(overrides)
    return ExecutionContext(**base)


# ── 1. 绕过向量全部拒绝 ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected_reason",
    [
        ("", "path_missing"),
        ("   ", "path_missing"),
        ("/workspace/../../etc/passwd", "path_traversal"),
        ("/agents/abcd1234/../../user_workspaces/2/x.md", "path_traversal"),
        ("/workspace/a/..", "path_traversal"),
        ("/workspace/%2e%2e/secret.md", "path_encoded_traversal"),
        ("/workspace/%2E%2E/secret.md", "path_encoded_traversal"),
        ("/workspace/a%2Fb.md", "path_encoded_traversal"),
        ("/workspace/a%2fb.md", "path_encoded_traversal"),
        ("/workspace/a%5Cb.md", "path_encoded_traversal"),
        ("/workspace/%252e%252e/x.md", "path_encoded_traversal"),
        ("/workspace/%00x.md", "path_encoded_traversal"),
        ("agents\\abcd1234\\workspace\\x.md", "path_backslash"),
        ("..\\..\\windows\\system32", "path_backslash"),
        ("C:/Windows/System32/config", "path_absolute_escape"),
        ("//server/share/x.md", "path_absolute_escape"),
        ("file:///etc/passwd", "path_scheme"),
        ("http://evil.example/x.md", "path_scheme"),
        ("https://evil.example/x.md", "path_scheme"),
        ("/workspace/a\x00b.md", "path_control_char"),
        ("/workspace/a\nb.md", "path_control_char"),
        ("/workspace/a\tb.md", "path_control_char"),
        ("~/workspace/x.md", "path_tilde_unsupported"),
        ("agents", "path_scope_missing"),
        ("/agents//x", "agent_hash_invalid"),
        ("/user_workspaces", "path_scope_missing"),
        ("/mnt/group", "group_id_missing"),
        ("/groups", "group_id_missing"),
        ("/workspace/" + "a" * 300, "path_segment_too_long"),
        ("/workspace/" + "/".join(["d"] * 600), "path_too_long"),
    ],
)
def test_bypass_vectors_are_rejected(raw, expected_reason):
    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path(raw, agent_hash=AGENT, user_id=str(OWNER_ID))
    assert excinfo.value.reason == expected_reason
    assert excinfo.value.status_code in (400, 403)


def test_relative_path_without_tenant_is_unresolvable():
    """没有任何服务端身份时，相对路径无法确定租户 → 拒绝（不猜）。"""
    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path("a.md")
    assert excinfo.value.reason == "path_unresolvable"
    assert excinfo.value.status_code == 400


@pytest.mark.parametrize(
    "raw",
    [
        f"/Agents/{AGENT}/workspace/x.md",
        f"/AGENTS/{AGENT}/workspace/x.md",
        "/Workspace/x.md",
        f"/USER_WORKSPACES/{OTHER_USER_ID}/workspace/x.md",
        "/Public/index.md",
    ],
)
def test_case_variants_of_root_segments_cannot_switch_tenant(raw):
    """根段大小写不是“另一个租户”，只会被当成工作区内的普通路径（不跨租户）。"""
    resource = normalize_resource_path(raw, agent_hash=AGENT)
    assert resource.scope == SCOPE_AGENT
    assert resource.agent_hash == AGENT
    assert resource.storage_key.startswith(f"feclaw/agents/{AGENT}/workspace/")


def test_scope_hint_is_authoritative_for_relative_paths():
    """同时给了 group_id 与 agent_hash 时，scope_hint 决定解释方向。"""
    resource = normalize_resource_path("a/b.md", agent_hash=AGENT, group_id=GROUP_ID, scope_hint="agent")
    assert resource.canonical_path == f"/agents/{AGENT}/workspace/a/b.md"

    resource = normalize_resource_path("a/b.md", agent_hash=AGENT, group_id=GROUP_ID, scope_hint="group")
    assert resource.canonical_path == f"/groups/{GROUP_ID}/a/b.md"

    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path("a/b.md", group_id=GROUP_ID, scope_hint="agent")
    assert excinfo.value.reason == "path_unresolvable"


@pytest.mark.parametrize("raw", [None, 123, ["/workspace/a.md"], {"path": "x"}])
def test_non_string_path_is_rejected(raw):
    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path(raw, agent_hash=AGENT)
    assert excinfo.value.reason == "path_invalid"


def test_invalid_tenant_identifiers_are_rejected():
    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path("/agents/not-hex/workspace/x.md")
    assert excinfo.value.reason == "agent_hash_invalid"
    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path("/groups/ok/.share/x.md", group_id="bad/id")
    assert excinfo.value.reason == "group_id_invalid"


# ── 2. 唯一规范形态 ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw",
    [
        f"/agents/{AGENT}/workspace/a/b.md",
        f"agents/{AGENT}/workspace/a/b.md",
        f"/agents/{AGENT}/workspace/./a/b.md",
        f"/agents/{AGENT}//workspace/a/b.md",
        "/workspace/a/b.md",
        "workspace/a/b.md",
        "a/b.md",
        "./a/b.md",
    ],
)
def test_agent_path_variants_share_one_canonical_form(raw):
    resource = normalize_resource_path(raw, agent_hash=AGENT)
    assert resource.scope == SCOPE_AGENT
    assert resource.agent_hash == AGENT
    assert resource.canonical_path == f"/agents/{AGENT}/workspace/a/b.md"
    assert resource.storage_key == f"feclaw/agents/{AGENT}/workspace/a/b.md"
    assert resource.kind == KIND_FILE


def test_agent_hash_case_is_normalized():
    resource = normalize_resource_path(f"/agents/{AGENT.upper()}/workspace/x.md", agent_hash=AGENT.upper())
    assert resource.agent_hash == AGENT
    assert resource.canonical_path == f"/agents/{AGENT}/workspace/x.md"
    assert resource.storage_key == f"feclaw/agents/{AGENT}/workspace/x.md"


def test_agent_root_relative_paths_keep_their_root():
    logs = normalize_resource_path("/.logs/tools/2025-01-01/a.log", agent_hash=AGENT)
    assert logs.canonical_path == f"/agents/{AGENT}/.logs/tools/2025-01-01/a.log"
    config = normalize_resource_path("agent/config.md", agent_hash=AGENT)
    assert config.canonical_path == f"/agents/{AGENT}/agent/config.md"


def test_file_names_keep_case_but_path_identity_is_stable():
    resource = normalize_resource_path("/workspace/MyNotes.MD", agent_hash=AGENT)
    assert resource.canonical_path == f"/agents/{AGENT}/workspace/MyNotes.MD"
    assert resource.storage_key.endswith("/workspace/MyNotes.MD")


@pytest.mark.parametrize(
    "raw",
    [
        f"/mnt/group/{GROUP_ID}/.share/x.md",
        f"/groups/{GROUP_ID}/.share/x.md",
        f"groups/{GROUP_ID}/.share/x.md",
        ".share/x.md",
    ],
)
def test_group_path_variants_share_one_canonical_form(raw):
    resource = normalize_resource_path(raw, group_id=GROUP_ID)
    assert resource.scope == SCOPE_GROUP
    assert resource.group_id == GROUP_ID
    assert resource.canonical_path == f"/groups/{GROUP_ID}/.share/x.md"
    assert resource.storage_key == f"feclaw/groups/{GROUP_ID}/.share/x.md"


@pytest.mark.parametrize(
    "raw", ["/user_workspaces/2/workspace/x.md", "user_workspaces/2/workspace/x.md", "workspace/x.md", "x.md"]
)
def test_user_workspace_variants_share_one_canonical_form(raw):
    resource = normalize_resource_path(raw, user_id=str(OWNER_ID))
    assert resource.scope == SCOPE_USER
    assert resource.user_id == str(OWNER_ID)
    assert resource.canonical_path == f"/user_workspaces/{OWNER_ID}/workspace/x.md"
    assert resource.storage_key == f"feclaw/user_workspaces/{OWNER_ID}/workspace/x.md"


@pytest.mark.parametrize("raw", ["/public/feclaw/index.md", "public/feclaw/index.md"])
def test_public_variants_share_one_canonical_form(raw):
    resource = normalize_resource_path(raw)
    assert resource.scope == SCOPE_PUBLIC
    assert resource.canonical_path == "/public/feclaw/index.md"
    assert resource.storage_key == "feclaw/public/feclaw/index.md"


def test_directory_and_root_paths():
    assert normalize_resource_path("/workspace/", agent_hash=AGENT).kind == KIND_DIR
    root = normalize_resource_path(f"/agents/{AGENT}", agent_hash=AGENT)
    assert root.canonical_path == f"/agents/{AGENT}"
    assert root.kind == KIND_DIR
    assert normalize_resource_path("/public").kind == KIND_DIR


def test_scope_hint_conflicts_and_share_scope():
    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path(f"/agents/{AGENT}/workspace/x.md", agent_hash=AGENT, scope_hint="group")
    assert excinfo.value.reason == "scope_conflict"

    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path("/workspace/x.md", agent_hash=AGENT, scope_hint="share")
    assert excinfo.value.reason == "path_scope_share_requires_mapping"


def test_explicit_tenant_mismatch_is_denied():
    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path(f"/agents/{AGENT}/workspace/x.md", agent_hash=OTHER_AGENT)
    assert excinfo.value.reason == "agent_scope_mismatch"
    assert excinfo.value.status_code == 403

    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path(f"/user_workspaces/{OTHER_USER_ID}/workspace/x.md", user_id=str(OWNER_ID))
    assert excinfo.value.reason == "user_scope_mismatch"
    assert excinfo.value.status_code == 403

    with pytest.raises(ResourceAccessError) as excinfo:
        normalize_resource_path(f"/groups/{GROUP_ID}/.share/x.md", group_id=OTHER_GROUP_ID)
    assert excinfo.value.reason == "group_scope_mismatch"


def test_resolved_resource_validates_its_inputs():
    with pytest.raises(ResourceAccessError):
        ResolvedResource(canonical_path="/workspace/x", scope="universe", storage_key="k")
    with pytest.raises(ResourceAccessError):
        ResolvedResource(canonical_path="workspace/x", scope=SCOPE_AGENT, storage_key="k")
    with pytest.raises(ResourceAccessError):
        ResolvedResource(canonical_path="/workspace/x", scope=SCOPE_AGENT, storage_key="")


# ── 3. owner / 成员校验 ────────────────────────────────────────────────────

def test_resolve_for_user_allows_own_agent(db):
    add_agent(db)
    resource = resolve_for_user(db, OWNER_ID, f"/agents/{AGENT}/workspace/x.md", agent_hash=AGENT)
    assert resource.scope == SCOPE_AGENT
    assert resource.owner_user_id == str(OWNER_ID)


def test_resolve_for_user_rejects_other_users_agent(db):
    add_agent(db, agent_hash=OTHER_AGENT, user_id=OTHER_USER_ID)
    with pytest.raises(ResourceAccessError) as excinfo:
        resolve_for_user(db, OWNER_ID, f"/agents/{OTHER_AGENT}/workspace/x.md")
    assert excinfo.value.reason == "agent_forbidden"
    assert excinfo.value.status_code == 403


def test_resolve_for_user_rejects_cross_agent_without_fallback(db):
    """同一用户的另一个 Agent 也不能借道访问（不做存在性回退）。"""
    add_agent(db)
    add_agent(db, agent_hash=OTHER_AGENT, user_id=OWNER_ID)
    with pytest.raises(ResourceAccessError) as excinfo:
        resolve_for_user(db, OWNER_ID, f"/agents/{OTHER_AGENT}/workspace/x.md", agent_hash=AGENT)
    assert excinfo.value.reason == "agent_scope_mismatch"
    assert excinfo.value.status_code == 403


def test_resolve_for_user_rejects_other_users_workspace(db):
    with pytest.raises(ResourceAccessError) as excinfo:
        resolve_for_user(db, OWNER_ID, f"/user_workspaces/{OTHER_USER_ID}/workspace/x.md")
    assert excinfo.value.reason == "user_scope_mismatch"
    assert excinfo.value.status_code == 403


def test_resolve_for_user_own_workspace(db):
    resource = resolve_for_user(db, OWNER_ID, f"/user_workspaces/{OWNER_ID}/workspace/x.md")
    assert resource.scope == SCOPE_USER
    assert resource.owner_user_id == str(OWNER_ID)


def test_group_owner_is_allowed(db):
    add_agent(db)
    add_group(db)
    resource = resolve_for_user(db, OWNER_ID, f"/mnt/group/{GROUP_ID}/.share/x.md", group_id=GROUP_ID)
    assert resource.scope == SCOPE_GROUP
    assert resource.owner_user_id == str(OWNER_ID)


def test_group_member_agent_is_allowed(db):
    add_agent(db)
    add_group(db)
    add_member(db)
    resource = resolve_for_user(db, OWNER_ID, f"/groups/{GROUP_ID}/.ref/a.md", agent_hash=AGENT)
    assert resource.group_id == GROUP_ID


def test_non_member_is_rejected(db):
    add_agent(db)
    add_group(db, owner_user_id=OTHER_USER_ID)
    with pytest.raises(ResourceAccessError) as excinfo:
        resolve_for_user(db, OWNER_ID, f"/mnt/group/{GROUP_ID}/.share/x.md", agent_hash=AGENT)
    assert excinfo.value.reason == "group_membership_required"
    assert excinfo.value.status_code == 403


def test_missing_group_is_404(db):
    with pytest.raises(ResourceAccessError) as excinfo:
        resolve_for_user(db, OWNER_ID, f"/mnt/group/{GROUP_ID}/.share/x.md")
    assert excinfo.value.reason == "group_not_found"
    assert excinfo.value.status_code == 404


def test_member_agent_of_another_user_is_rejected(db):
    """成员表里有这个 Agent，但该 Agent 不属于当前用户 → 仍然拒绝。"""
    add_agent(db, agent_hash=OTHER_AGENT, user_id=OTHER_USER_ID)
    add_group(db, owner_user_id=OTHER_USER_ID)
    add_member(db, agent_hash=OTHER_AGENT)
    with pytest.raises(ResourceAccessError) as excinfo:
        resolve_for_user(db, OWNER_ID, f"/mnt/group/{GROUP_ID}/.share/x.md", agent_hash=OTHER_AGENT)
    assert excinfo.value.reason == "group_membership_required"


def test_public_is_readable_by_any_authenticated_user(db):
    resource = resolve_for_user(db, OWNER_ID, "/public/feclaw/index.md")
    assert resource.scope == SCOPE_PUBLIC
    assert resource.owner_user_id is None


def test_resolve_requires_user_identity(db):
    with pytest.raises(ResourceAccessError) as excinfo:
        resolve_for_user(db, None, "/public/x.md")
    assert excinfo.value.reason == "user_required"


# ── 4. 只读存在性探测（真实 LocalStorage） ─────────────────────────────────

def test_storage_probe_marks_existing_file(db, storage):
    add_agent(db)
    storage.put_object(f"feclaw/agents/{AGENT}/workspace/notes.md", b"# hi")
    resource = resolve_for_user(
        db, OWNER_ID, f"/agents/{AGENT}/workspace/notes.md", agent_hash=AGENT, storage=storage
    )
    assert resource.exists is True
    assert resource.kind == KIND_FILE


def test_storage_probe_marks_missing_file_without_fallback(db, storage):
    add_agent(db)
    # 别的 Agent 目录里确实有同名文件 —— 但解析结果只能用自己的 storage_key
    storage.put_object(f"feclaw/agents/{OTHER_AGENT}/workspace/notes.md", b"# other")
    resource = resolve_for_user(
        db, OWNER_ID, f"/agents/{AGENT}/workspace/notes.md", agent_hash=AGENT, storage=storage
    )
    assert resource.exists is False
    assert resource.storage_key == f"feclaw/agents/{AGENT}/workspace/notes.md"
    assert storage.file_exists(f"feclaw/agents/{OTHER_AGENT}/workspace/notes.md") is not None


def test_storage_probe_detects_directory(db, storage):
    add_agent(db)
    storage.put_object(f"feclaw/agents/{AGENT}/workspace/sub/a.md", b"x")
    resource = resolve_for_user(
        db, OWNER_ID, f"/agents/{AGENT}/workspace/sub", agent_hash=AGENT, storage=storage
    )
    assert resource.exists is True
    assert resource.kind == KIND_DIR


def test_storage_probe_failure_is_unknown_not_absent(db):
    class _BrokenStorage:
        def file_exists(self, key):  # noqa: D401 - 故意抛错
            raise RuntimeError("storage down")

    add_agent(db)
    resource = resolve_for_user(
        db, OWNER_ID, f"/agents/{AGENT}/workspace/x.md", agent_hash=AGENT, storage=_BrokenStorage()
    )
    assert resource.exists is None  # 读取失败 ≠ 不存在


# ── 5. can_read / can_write ────────────────────────────────────────────────

def _resource(db, raw, *, agent_hash=AGENT, group_id=None, storage=None):
    return resolve_for_user(db, OWNER_ID, raw, agent_hash=agent_hash, group_id=group_id, storage=storage)


def test_normal_file_read_write_allowed_in_full_mode(db):
    add_agent(db)
    resource = _resource(db, f"/agents/{AGENT}/workspace/notes.md")
    assert can_read(resource, _ctx()) is True
    assert can_write(resource, _ctx()) is True
    assert access_reason(resource, _ctx(), action="write") == "ok"


def test_readonly_cannot_write(db):
    add_agent(db)
    resource = _resource(db, f"/agents/{AGENT}/workspace/notes.md")
    readonly = _ctx(policy_mode=POLICY_MODE_READONLY)
    assert can_read(resource, readonly) is True
    assert can_write(resource, readonly) is False
    assert access_reason(resource, readonly, action="write") == "policy_denied"


def test_restricted_can_write_local_file(db):
    add_agent(db)
    resource = _resource(db, f"/agents/{AGENT}/workspace/notes.md")
    assert can_write(resource, _ctx(policy_mode=POLICY_MODE_RESTRICTED)) is True


@pytest.mark.parametrize(
    "path",
    [
        f"/agents/{AGENT}/workspace/identity.md",
        f"/agents/{AGENT}/workspace/IDENTITY.MD",
        f"/agents/{AGENT}/workspace/Soul.Md",
        f"/workspace/soul.md",
        f"/workspace/.env",
        f"/workspace/secret.txt",
        f"/workspace/password.md",
    ],
)
def test_sensitive_files_are_not_writable(db, path):
    """复用 permission_service 的敏感文件清单（不在此复制一套）。"""
    add_agent(db)
    resource = _resource(db, path)
    assert can_read(resource, _ctx()) is True
    assert can_write(resource, _ctx()) is False
    assert access_reason(resource, _ctx(), action="write") == "file_permission_denied"


def test_tool_logs_are_not_writable(db):
    add_agent(db)
    resource = _resource(db, f"/agents/{AGENT}/.logs/tools/2025-01-01/run.log")
    assert can_read(resource, _ctx()) is True
    assert can_write(resource, _ctx()) is False


def test_write_requires_file_write_capability(db):
    add_agent(db)
    resource = _resource(db, f"/agents/{AGENT}/workspace/notes.md")
    read_only_caps = _ctx(capabilities=frozenset({"file.read"}))
    assert can_read(resource, read_only_caps) is True
    assert access_reason(resource, read_only_caps, action="write") == "capability_missing"
    assert access_reason(resource, _ctx(capabilities=None), action="read") == "capabilities_unset"


def test_scope_allow_list_is_enforced(db):
    add_agent(db)
    resource = _resource(db, f"/agents/{AGENT}/workspace/notes.md")
    assert access_reason(resource, _ctx(allowed_resource_scopes=frozenset({"group"})), action="read") == "scope_not_allowed"
    assert access_reason(resource, _ctx(allowed_resource_scopes=None), action="read") == "resource_scope_unset"


def test_cross_agent_resource_is_not_readable(db):
    add_agent(db)
    add_agent(db, agent_hash=OTHER_AGENT, user_id=OWNER_ID)
    resource = normalize_resource_path(f"/agents/{OTHER_AGENT}/workspace/x.md", agent_hash=OTHER_AGENT)
    assert can_read(resource, _ctx()) is False
    assert access_reason(resource, _ctx(), action="read") == "agent_scope_mismatch"


def test_cross_user_workspace_is_not_readable(db):
    resource = normalize_resource_path(f"/user_workspaces/{OTHER_USER_ID}/workspace/x.md")
    assert can_read(resource, _ctx()) is False


def test_owner_mismatch_blocks_read(db):
    resource = ResolvedResource(
        canonical_path=f"/agents/{AGENT}/workspace/x.md",
        scope=SCOPE_AGENT,
        agent_hash=AGENT,
        storage_key=f"feclaw/agents/{AGENT}/workspace/x.md",
        kind=KIND_FILE,
        owner_user_id=str(OTHER_USER_ID),
    )
    assert access_reason(resource, _ctx(), action="read") == "owner_mismatch"


def test_public_is_read_only():
    resource = normalize_resource_path("/public/feclaw/index.md")
    assert can_read(resource, _ctx()) is True
    assert can_write(resource, _ctx()) is False
    assert access_reason(resource, _ctx(), action="write") == "public_readonly"


def test_group_share_space_requires_approval(db):
    add_agent(db)
    add_group(db)
    shared = _resource(db, f"/mnt/group/{GROUP_ID}/.share/doc.md", group_id=GROUP_ID)
    assert can_read(shared, _ctx()) is True
    assert can_write(shared, _ctx()) is False
    assert access_reason(shared, _ctx(), action="write") == "approval_required"


@pytest.mark.parametrize("scope_dir", [".ref", ".attach"])
def test_group_readonly_scopes_are_never_writable(db, scope_dir):
    add_agent(db)
    add_group(db)
    resource = _resource(db, f"/mnt/group/{GROUP_ID}/{scope_dir}/x.md", group_id=GROUP_ID)
    assert can_read(resource, _ctx()) is True
    assert access_reason(resource, _ctx(), action="write") == "group_readonly_scope"


def test_db_permission_override_is_respected(db):
    """FilePermission 的只读覆盖在 db-backed 判定里生效（仍走 permission_service）。"""
    add_agent(db)
    db.add(FilePermission(user_id=str(OWNER_ID), agent_hash=AGENT,
                          file_path="workspace/locked.md", permission="read"))
    db.flush()
    resource = _resource(db, f"/agents/{AGENT}/workspace/locked.md")
    assert can_write(resource, _ctx()) is True            # 默认 readwrite
    assert can_write(resource, _ctx(), db=db) is False    # DB 覆盖为 read
    assert access_reason(resource, _ctx(), action="write", db=db) == "file_permission_denied"


def test_access_reason_rejects_unknown_action(db):
    add_agent(db)
    resource = _resource(db, f"/agents/{AGENT}/workspace/x.md")
    with pytest.raises(ValueError):
        access_reason(resource, _ctx(), action="execute")


def test_unresolved_group_or_share_resource_is_denied():
    """手工构造（未经 DB 鉴权）的 group/share 资源不能被当成已授权资源。"""
    group_resource = normalize_resource_path(f"/groups/{GROUP_ID}/.share/x.md", group_id=GROUP_ID)
    assert access_reason(group_resource, _ctx(), action="read") == "group_not_resolved"

    share_resource = ResolvedResource(
        canonical_path=f"/agents/{AGENT}/workspace/x.md",
        scope="share",
        agent_hash=AGENT,
        storage_key=f"feclaw/agents/{AGENT}/workspace/x.md",
        kind=KIND_FILE,
    )
    assert access_reason(share_resource, _ctx(), action="read") == "share_not_resolved"


# ── 6. 分享映射 → 只读 share 资源 ─────────────────────────────────────────

def test_share_mapping_resolves_to_read_only_resource(db):
    add_agent(db)
    mapping = ShareMapping(
        user_id=str(OWNER_ID),
        agent_hash=AGENT,
        vfs_path="/workspace/shared/report.md",
        share_hash="abc12345",
        mode="share",
    )
    db.add(mapping)
    db.flush()

    resource = resolve_share_mapping_resource(db, mapping)
    assert resource.scope == "share"
    assert resource.canonical_path == f"/agents/{AGENT}/workspace/shared/report.md"
    assert resource.storage_key == f"feclaw/agents/{AGENT}/workspace/shared/report.md"
    assert resource.owner_user_id == str(OWNER_ID)

    # 分享的意义就是跨租户只读：别的用户/Agent 上下文可读、不可写
    visitor = _ctx(user_id=str(OTHER_USER_ID), agent_hash=OTHER_AGENT)
    assert can_read(resource, visitor) is True
    assert can_write(resource, visitor) is False
    assert access_reason(resource, visitor, action="write") == "share_readonly"


def test_share_mapping_with_unsafe_path_is_rejected(db):
    add_agent(db)
    mapping = ShareMapping(
        user_id=str(OWNER_ID),
        agent_hash=AGENT,
        vfs_path="/workspace/../user_workspaces/2/secret.md",
        share_hash="def67890",
        mode="share",
    )
    db.add(mapping)
    db.flush()
    with pytest.raises(ResourceAccessError) as excinfo:
        resolve_share_mapping_resource(db, mapping)
    assert excinfo.value.reason == "share_path_escape"


# ── 7. 真实 MySQL（并发/行语义门禁用例；缺配置时 skip） ───────────────────

@pytest.mark.mysql
def test_mysql_group_membership_resolution(mysql_session):
    """在真实 MySQL 上跑一遍 owner/成员判定（非提交写入，fixture 回滚）。"""
    db = mysql_session
    agent_hash = format(uuid.uuid4().int, "08x")[:8]
    group_id = str(uuid.uuid4())
    owner_user_id = 990_001

    db.add(AgentProfile(hash=agent_hash, user_id=owner_user_id, totp_secret="JBSWY3DPEHPK3PXP"))
    db.add(Group(id=group_id, name="t04 mysql group", owner_user_id=owner_user_id))
    db.flush()

    resource = resolve_for_user(
        db, owner_user_id, f"/mnt/group/{group_id}/.share/x.md", agent_hash=agent_hash
    )
    assert resource.scope == SCOPE_GROUP
    assert resource.storage_key == f"feclaw/groups/{group_id}/.share/x.md"
    assert resource.owner_user_id == str(owner_user_id)

    with pytest.raises(ResourceAccessError) as excinfo:
        resolve_for_user(db, owner_user_id + 1, f"/mnt/group/{group_id}/.share/x.md")
    assert excinfo.value.reason == "group_membership_required"
