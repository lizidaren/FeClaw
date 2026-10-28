"""T04 执行授权底座验收测试（services/execution_policy.py + tool_registry 元数据）。

覆盖（规格 §5 T04 / §9）：
1. 未知工具拒绝；已注册但未分类的工具拒绝（**不默认只读**）。
2. readonly 下 publish/delete/write/execute/schedule/delegate/system 全部拒绝，
   read 与“已声明无副作用”的计算类工具放行 —— 按允许清单，不靠黑名单。
3. capabilities 未声明 / 缺项一律拒绝（空配置不是放行理由）。
4. 模型不能用参数传 user_id/capabilities 等身份字段提权。
5. child() 只能缩小：capabilities/scopes 取交、deadline 不晚于父、budget 不高于父、
   policy_mode 只能更严格、身份不能换；任何放大抛 PolicyError(policy_escalation)。
6. 工具元数据覆盖：注册表里每个工具都必须在 tool_registry 显式分类（unknown 必须可枚举）。
7. reason 稳定可断言 + http_status 映射（400 非法 / 403 越权）。

运行：
    $env:PYTHONPATH='<repo>'; $env:SETUP_COMPLETE='true'; $env:JWT_SECRET='local-test-jwt-secret-not-for-prod'
    $env:DATABASE_URL='mysql+pymysql://root:@127.0.0.1:3307/feclaw?charset=utf8mb4'
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_execution_policy.py -q
"""

import os
import sys
from dataclasses import FrozenInstanceError

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

pytest.importorskip("sqlalchemy", reason="T04 执行授权测试需要仓库 .venv 依赖")

# 导入工具包以填充 TOOL_REGISTRY（测试直接针对真实注册表，不用 mock）
import services.tools  # noqa: F401,E402  （副作用：注册全部工具）

from services import tool_registry as registry  # noqa: E402
from services.execution_policy import (  # noqa: E402
    MODE_ALLOWED_EFFECTS,
    POLICY_MODE_FULL,
    POLICY_MODE_READONLY,
    POLICY_MODE_RESTRICTED,
    RESOURCE_SCOPES,
    ExecutionContext,
    PolicyError,
    RunBudget,
    authorize_tool,
    mode_allows_effect,
)
from services.resource_access import normalize_resource_path  # noqa: E402
from services.tool_registry import (  # noqa: E402
    ALL_CAPABILITIES,
    ALL_EFFECTS,
    CLASSIFICATION_DECLARED,
    CLASSIFICATION_UNKNOWN,
    TOOL_REGISTRY,
    is_retry_safe,
    iter_tool_metadata,
    supports_idempotency_key,
    tool_capabilities,
    tool_classification,
    tool_effects,
    tool_metadata,
    unknown_tool_names,
)

AGENT_HASH = "abcd1234"
OTHER_AGENT = "ffff0000"
OWNER_ID = "2"

ALL_SCOPES = frozenset(RESOURCE_SCOPES)


def _context(**overrides) -> ExecutionContext:
    """带全部能力/全部范围的“最强”上下文；用例按需收窄。"""
    base = dict(
        user_id=OWNER_ID,
        agent_hash=AGENT_HASH,
        channel="web",
        policy_mode=POLICY_MODE_FULL,
        capabilities=frozenset(ALL_CAPABILITIES),
        allowed_resource_scopes=ALL_SCOPES,
    )
    base.update(overrides)
    return ExecutionContext(**base)


def _readonly(**overrides) -> ExecutionContext:
    return _context(policy_mode=POLICY_MODE_READONLY, **overrides)


# ── 1. 未知 / 未分类工具 ────────────────────────────────────────────────────

def test_unknown_tool_is_denied():
    decision = authorize_tool(_context(), "definitely_not_a_tool", {})
    assert decision.allowed is False
    assert decision.reason == "unknown_tool"
    assert decision.http_status == 400


@pytest.mark.parametrize("bad_name", ["", "   ", None, 123])
def test_invalid_tool_name_is_denied(bad_name):
    assert authorize_tool(_context(), bad_name, {}) .reason == "unknown_tool"


def test_registered_but_unclassified_tool_is_denied_not_treated_as_readonly():
    """已注册却没有 effects 声明的工具必须拒绝（历史上“没分类=当只读”是漏洞）。"""

    @registry.tool(name="t04_probe_unclassified", description="probe", category="general")
    def _probe() -> str:  # pragma: no cover - 只为注册一个无元数据的工具
        return "ok"

    try:
        assert "t04_probe_unclassified" in TOOL_REGISTRY
        meta = tool_metadata("t04_probe_unclassified")
        assert meta.registered is True
        assert meta.classification == CLASSIFICATION_UNKNOWN
        assert meta.effects == frozenset()
        assert is_retry_safe("t04_probe_unclassified") is False

        decision = authorize_tool(_context(), "t04_probe_unclassified", {})
        assert decision.allowed is False
        assert decision.reason == "tool_unclassified"
    finally:
        TOOL_REGISTRY.pop("t04_probe_unclassified", None)
        registry.TOOL_EFFECT_METADATA.pop("t04_probe_unclassified", None)


# ── 2. 模式允许清单 ────────────────────────────────────────────────────────

DENIED_EFFECT_CASES = [
    ("file_write", {"path": "/workspace/a.md", "content": "x"}, "write"),
    ("file_delete", {"path": "/workspace/a.md"}, "delete"),
    ("bash", {"command": "echo hi"}, "execute"),
    ("create_post", {"title": "t", "content": "c"}, "publish"),
    ("reply_buffer_flush", {"channel": "web"}, "send"),
    ("create_cron_job", {"schedule": "in 5 minutes", "content": "x"}, "schedule"),
    ("spawn_subagent", {"model": "auto", "reasoning_effort": "low", "task": "t"}, "delegate"),
    ("cleanup_logs", {"retention_days": 1}, "system"),
]


@pytest.mark.parametrize("tool_name,arguments,expected_effect", DENIED_EFFECT_CASES)
def test_readonly_denies_every_non_read_effect(tool_name, arguments, expected_effect):
    """readonly 下写/删/执行/发布/发送/定时/委派/系统 全部拒绝（即使能力齐全）。"""
    decision = authorize_tool(_readonly(), tool_name, arguments)
    assert decision.allowed is False
    assert decision.reason == "policy_denied"
    assert expected_effect in decision.denied_effects
    assert decision.http_status == 403


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("file_delete", {"path": "/workspace/a.md"}),
        ("bash", {"command": "echo hi"}),
        ("create_post", {"title": "t", "content": "c"}),
        ("reply_buffer_flush", {"channel": "web"}),
        ("create_cron_job", {"schedule": "in 5 minutes"}),
        ("spawn_subagent", {"task": "t"}),
        ("cleanup_logs", {"retention_days": 1}),
    ],
)
def test_restricted_denies_outbound_system_and_destructive_effects(tool_name, arguments):
    """restricted 只允许 read/write；删除/执行/发布/发送/定时/委派/系统一律拒绝。"""
    decision = authorize_tool(_context(policy_mode=POLICY_MODE_RESTRICTED), tool_name, arguments)
    assert decision.allowed is False
    assert decision.reason == "policy_denied"


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("file_read", {"path": "/workspace/a.md"}),
        ("file_list", {"dir": "/workspace"}),
        ("list_reminders", {}),
        ("knowledge_search", {"query": "牛顿第二定律"}),
    ],
)
def test_readonly_allows_read_tools(tool_name, arguments):
    decision = authorize_tool(_readonly(), tool_name, arguments)
    assert decision.allowed is True, decision.to_dict()
    assert decision.reason == "ok"
    assert decision.effects <= frozenset({"read"})


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("text_summarize", {"content": "..."}),
        ("text_translate", {"content": "..."}),
        ("generate_summary", {"messages_json": "[]"}),
    ],
)
def test_readonly_allows_declared_side_effect_free_tools(tool_name, arguments):
    """effects=() 且**已显式分类**的纯计算工具在 readonly 放行。"""
    decision = authorize_tool(_readonly(), tool_name, arguments)
    assert decision.allowed is True, decision.to_dict()
    assert tool_classification(tool_name) == CLASSIFICATION_DECLARED
    assert tool_effects(tool_name) == frozenset()


def test_restricted_allows_local_write_but_not_publish():
    restricted = _context(policy_mode=POLICY_MODE_RESTRICTED)
    assert authorize_tool(restricted, "file_write", {"path": "/workspace/a.md"}).allowed is True
    assert authorize_tool(restricted, "create_post", {}).reason == "policy_denied"
    assert authorize_tool(restricted, "reply_buffer_flush", {}).reason == "policy_denied"


def test_full_allows_every_declared_effect_when_capabilities_present():
    full = _context()
    for tool_name in sorted(TOOL_REGISTRY):
        decision = authorize_tool(full, tool_name, {})
        assert decision.allowed is True, (tool_name, decision.to_dict())


def test_mode_allowed_effects_table_is_allow_list():
    assert MODE_ALLOWED_EFFECTS[POLICY_MODE_READONLY] == frozenset({"read"})
    assert MODE_ALLOWED_EFFECTS[POLICY_MODE_RESTRICTED] == frozenset({"read", "write"})
    assert MODE_ALLOWED_EFFECTS[POLICY_MODE_FULL] == ALL_EFFECTS
    assert mode_allows_effect(POLICY_MODE_READONLY, "write") is False
    assert mode_allows_effect("nonsense-mode", "read") is False


# ── 3. 能力 fail closed ────────────────────────────────────────────────────

def test_capabilities_unset_fails_closed():
    ctx = _context(capabilities=None)
    decision = authorize_tool(ctx, "file_read", {"path": "/workspace/a.md"})
    assert decision.allowed is False
    assert decision.reason == "capabilities_unset"
    assert decision.http_status == 403


def test_empty_capabilities_is_not_a_free_pass():
    ctx = _context(capabilities=frozenset())
    decision = authorize_tool(ctx, "file_read", {"path": "/workspace/a.md"})
    assert decision.allowed is False
    assert decision.reason == "capability_missing"
    assert decision.missing_capabilities == tool_capabilities("file_read")


def test_missing_capability_is_reported():
    ctx = _context(capabilities=frozenset({"file.read"}))
    decision = authorize_tool(ctx, "file_write", {"path": "/workspace/a.md"})
    assert decision.allowed is False
    assert decision.reason == "capability_missing"
    assert decision.missing_capabilities == frozenset({"file.write"})
    assert decision.required_capabilities == frozenset({"file.write"})


def test_unknown_capability_in_context_is_policy_invalid():
    with pytest.raises(PolicyError) as excinfo:
        _context(capabilities=frozenset({"file.read", "file.sudo"}))
    assert excinfo.value.reason == "policy_invalid"


# ── 4. 参数面提权 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "/workspace/a.md", "user_id": 999},
        {"path": "/workspace/a.md", "agent_hash": OTHER_AGENT},
        {"path": "/workspace/a.md", "capabilities": ["file.write"]},
        {"path": "/workspace/a.md", "policy_mode": "full"},
        {"path": "/workspace/a.md", "allowed_resource_scopes": ["group"]},
    ],
)
def test_identity_arguments_are_forbidden(arguments):
    decision = authorize_tool(_readonly(), "file_read", arguments)
    assert decision.allowed is False
    assert decision.reason == "forbidden_argument"
    assert decision.http_status == 400


def test_declared_parameter_is_not_treated_as_privilege_argument():
    """既有 API 的地址参数（reply_buffer_flush.user_id）不算“提权参数”。

    注意：这是把**寻址**问题留给调用点/后续切片收口的已知缺口（见交付报告），
    本测试只锁定“不会因为参数名撞上保留字就误杀合法工具”。
    """
    decision = authorize_tool(
        _context(), "reply_buffer_flush", {"channel": "web", "user_id": 2, "session_id": "s1"}
    )
    assert decision.allowed is True, decision.to_dict()


def test_arguments_must_be_mapping():
    decision = authorize_tool(_context(), "file_read", ["/workspace/a.md"])  # type: ignore[arg-type]
    assert decision.allowed is False
    assert decision.reason == "arguments_invalid"


# ── 5. 资源范围门禁 ────────────────────────────────────────────────────────

GROUP_RESOURCE = normalize_resource_path("/mnt/group/g1/.share/x.md", group_id="g1")


def test_resource_scope_unset_fails_closed():
    ctx = _context(allowed_resource_scopes=None)
    decision = authorize_tool(ctx, "file_read", {"path": "/mnt/group/g1/.share/x.md"}, resource=GROUP_RESOURCE)
    assert decision.allowed is False
    assert decision.reason == "resource_scope_unset"


def test_resource_scope_outside_allowlist_is_denied():
    ctx = _context(allowed_resource_scopes=frozenset({"agent"}))
    decision = authorize_tool(ctx, "file_read", {"path": "/mnt/group/g1/.share/x.md"}, resource=GROUP_RESOURCE)
    assert decision.allowed is False
    assert decision.reason == "scope_not_allowed"


def test_resource_scope_inside_allowlist_passes():
    ctx = _context(allowed_resource_scopes=frozenset({"group"}))
    decision = authorize_tool(ctx, "file_read", {"path": "/mnt/group/g1/.share/x.md"}, resource=GROUP_RESOURCE)
    assert decision.allowed is True, decision.to_dict()


# ── 6. 截止时间 ────────────────────────────────────────────────────────────

def test_deadline_is_enforced_with_injected_clock():
    ctx = _context(deadline=1_000.0)
    assert authorize_tool(ctx, "file_read", {}, now=999.0).allowed is True
    decision = authorize_tool(ctx, "file_read", {}, now=1_001.0)
    assert decision.allowed is False
    assert decision.reason == "deadline_exceeded"


# ── 7. child() 只能缩小 ────────────────────────────────────────────────────

def test_child_inherits_by_default():
    parent = _context(run_id="run-1", session_id="sess-1", deadline=5_000.0, budget=RunBudget(max_tool_calls=10))
    child = parent.child(run_id="run-2")
    assert child.run_id == "run-2"
    assert child.parent_run_id == "run-1"
    assert child.user_id == parent.user_id
    assert child.agent_hash == parent.agent_hash
    assert child.channel == parent.channel
    assert child.session_id == parent.session_id
    assert child.policy_mode == parent.policy_mode
    assert child.capabilities == parent.capabilities
    assert child.allowed_resource_scopes == parent.allowed_resource_scopes
    assert child.deadline == parent.deadline
    assert child.budget == parent.budget


def test_child_can_narrow_capabilities_scopes_mode_deadline_budget():
    parent = _context(
        run_id="run-1",
        capabilities=frozenset({"file.read", "file.write"}),
        allowed_resource_scopes=frozenset({"agent", "group"}),
        deadline=5_000.0,
        budget=RunBudget(max_tool_calls=10, max_child_runs=2),
    )
    child = parent.child(
        capabilities=frozenset({"file.read"}),
        allowed_resource_scopes=frozenset({"agent"}),
        policy_mode=POLICY_MODE_READONLY,
        deadline=4_000.0,
        budget=RunBudget(max_tool_calls=3, max_child_runs=1),
    )
    assert child.capabilities == frozenset({"file.read"})
    assert child.allowed_resource_scopes == frozenset({"agent"})
    assert child.policy_mode == POLICY_MODE_READONLY
    assert child.deadline == 4_000.0
    assert child.budget == RunBudget(max_tool_calls=3, max_child_runs=1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"capabilities": frozenset({"file.read", "file.write", "sandbox.execute"})},
        {"allowed_resource_scopes": frozenset({"agent", "public"})},
        {"policy_mode": POLICY_MODE_FULL},  # parent=restricted → full 是放宽
        {"deadline": 9_000.0},
        {"deadline": None},
        {"budget": RunBudget(max_tool_calls=99)},
        {"budget": None},
        {"user_id": "3"},
        {"agent_hash": OTHER_AGENT},
        {"channel": "wechat"},
        {"session_id": "another-session"},
    ],
)
def test_child_cannot_widen_anything(overrides):
    parent = _context(
        run_id="run-1",
        session_id="sess-1",
        policy_mode=POLICY_MODE_RESTRICTED,
        capabilities=frozenset({"file.read", "file.write"}),
        allowed_resource_scopes=frozenset({"agent"}),
        deadline=5_000.0,
        budget=RunBudget(max_tool_calls=10),
    )
    with pytest.raises(PolicyError) as excinfo:
        parent.child(**overrides)
    assert excinfo.value.reason == "policy_escalation"
    assert excinfo.value.http_status == 403


def test_child_of_unset_capabilities_cannot_grant_new_capability():
    parent = _context(capabilities=None)
    with pytest.raises(PolicyError) as excinfo:
        parent.child(capabilities=frozenset({"file.read"}))
    assert excinfo.value.reason == "policy_escalation"
    # 显式空集不是“新授予”，允许
    assert parent.child(capabilities=frozenset()).capabilities == frozenset()


def test_child_equal_scope_values_are_allowed():
    parent = _context(capabilities=frozenset({"file.read"}), allowed_resource_scopes=frozenset({"agent"}))
    child = parent.child(
        capabilities=frozenset({"file.read"}), allowed_resource_scopes=frozenset({"agent"})
    )
    assert child.capabilities == frozenset({"file.read"})


# ── 8. 策略格式自检 ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "overrides",
    [
        {"policy_mode": "god-mode"},
        {"allowed_resource_scopes": frozenset({"universe"})},
        {"deadline": "tomorrow"},
        {"budget": {"max_tool_calls": 1}},
    ],
)
def test_malformed_context_is_rejected(overrides):
    with pytest.raises(PolicyError) as excinfo:
        _context(**overrides)
    assert excinfo.value.reason == "policy_invalid"


@pytest.mark.parametrize(
    "budget_kwargs",
    [{"max_tool_calls": -1}, {"max_seconds": "soon"}, {"max_cost_cny": -0.1}],
)
def test_malformed_budget_is_rejected(budget_kwargs):
    with pytest.raises(PolicyError) as excinfo:
        RunBudget(**budget_kwargs)
    assert excinfo.value.reason == "policy_invalid"


def test_budget_is_within_semantics():
    parent = RunBudget(max_tool_calls=10, max_seconds=60.0)
    assert RunBudget(max_tool_calls=10, max_seconds=60.0).is_within(parent)
    # 收紧个别维度时用 tightened()：未给出的维度保持不变（None = 不限额，不是继承）
    assert parent.tightened(max_tool_calls=5).is_within(parent)
    assert not RunBudget(max_tool_calls=5).is_within(parent)  # max_seconds 变不限额 = 放宽
    assert not RunBudget(max_tool_calls=11, max_seconds=60.0).is_within(parent)
    assert not RunBudget(max_tool_calls=10, max_seconds=None).is_within(parent)
    assert RunBudget(max_tool_calls=999).is_within(None)  # 父不限额 → 任何子都更窄
    with pytest.raises(PolicyError):
        parent.tightened(unknown_dimension=1)


def test_child_budget_can_narrow_one_dimension_via_tightened():
    parent = _context(budget=RunBudget(max_tool_calls=10, max_seconds=60.0))
    child = parent.child(budget=parent.budget.tightened(max_tool_calls=2))
    assert child.budget == RunBudget(max_tool_calls=2, max_seconds=60.0)


# ── 9. 不可变 + 结果结构 ───────────────────────────────────────────────────

def test_integer_user_id_is_accepted_and_normalized():
    """DB 里的 user_id 是 int，ExecutionContext 统一成字符串（不因此放宽判定）。"""
    ctx = ExecutionContext(
        user_id=12345,
        agent_hash=AGENT_HASH,
        channel="web",
        policy_mode=POLICY_MODE_READONLY,
        capabilities=frozenset({"file.read"}),
    )
    assert ctx.user_id == "12345"
    assert authorize_tool(ctx, "file_read", {}).allowed is True


def test_context_and_decision_are_immutable():
    ctx = _context()
    with pytest.raises(FrozenInstanceError):
        ctx.capabilities = frozenset()  # type: ignore[misc]
    decision = authorize_tool(ctx, "file_read", {})
    with pytest.raises(FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]


def test_decision_structure_and_require():
    ctx = _context(capabilities=frozenset({"file.read"}))
    decision = authorize_tool(ctx, "file_write", {"path": "/workspace/a.md"})
    payload = decision.to_dict()
    assert payload["allowed"] is False
    assert payload["reason"] == "capability_missing"
    assert payload["tool_name"] == "file_write"
    assert payload["effects"] == ["write"]
    assert payload["required_capabilities"] == ["file.write"]
    with pytest.raises(PolicyError) as excinfo:
        decision.require()
    assert excinfo.value.reason == "capability_missing"

    ok = authorize_tool(_context(), "file_read", {})
    assert ok.require() is ok
    assert ok.http_status == 200


def test_reason_to_http_status_mapping():
    assert authorize_tool(_readonly(), "nope", {}).http_status == 400           # unknown_tool
    assert authorize_tool(_readonly(), "file_write", {}).http_status == 403     # policy_denied
    assert authorize_tool(_readonly(), "file_read", {}).http_status == 200      # ok


# ── 10. 按参数追加效果（web_search.allow_images） ──────────────────────────

def test_conditional_effect_blocks_image_download_in_readonly():
    allowed = authorize_tool(_readonly(), "web_search", {"query": "x", "allow_images": False})
    assert allowed.allowed is True, allowed.to_dict()
    denied = authorize_tool(_readonly(), "web_search", {"query": "x", "allow_images": True})
    assert denied.allowed is False
    assert denied.reason == "policy_denied"
    assert "write" in denied.denied_effects


# ── 11. 别名与元数据覆盖度 ────────────────────────────────────────────────

def test_alias_metadata_and_authorization_follow_canonical_name():
    meta = tool_metadata("schedule_reminder")
    assert meta.name == "create_cron_job"
    assert meta.classification == CLASSIFICATION_DECLARED
    assert meta.effects == tool_effects("create_cron_job")
    decision = authorize_tool(_readonly(), "schedule_reminder", {"schedule": "in 5 minutes"})
    assert decision.reason == "policy_denied"
    assert decision.tool_name == "create_cron_job"


def test_every_registered_tool_has_enumerable_metadata():
    assert len(TOOL_REGISTRY) > 0
    assert unknown_tool_names() == [], "有工具未在 tool_registry 分类（新增工具必须登记元数据）"
    for meta in iter_tool_metadata():
        assert meta.registered is True
        assert meta.classification == CLASSIFICATION_DECLARED
        assert meta.effects <= ALL_EFFECTS
        assert meta.required_capabilities <= ALL_CAPABILITIES


def test_declared_metadata_has_no_stale_entries():
    stale = sorted(set(registry.TOOL_EFFECT_METADATA) - set(TOOL_REGISTRY))
    assert stale == [], f"元数据里登记了未注册的工具: {stale}"


def test_effects_and_capability_vocabularies_are_stable():
    assert ALL_EFFECTS == frozenset(
        {"read", "write", "delete", "execute", "publish", "send", "schedule", "delegate", "system"}
    )
    assert "file.read" in ALL_CAPABILITIES
    assert "file.write" in ALL_CAPABILITIES
    assert "agent.publish" in ALL_CAPABILITIES
    assert "group.send" in ALL_CAPABILITIES
    assert "sandbox.execute" in ALL_CAPABILITIES
    assert "system.logs" in ALL_CAPABILITIES


def test_unregistered_name_metadata_is_unknown_and_not_retry_safe():
    meta = tool_metadata("no_such_tool_anywhere")
    assert meta.registered is False
    assert meta.classification == CLASSIFICATION_UNKNOWN
    assert meta.effects == frozenset()
    assert is_retry_safe("no_such_tool_anywhere") is False
    assert supports_idempotency_key("no_such_tool_anywhere") is False


def test_readonly_safe_tools_are_not_retry_safe_by_default():
    """未分类/未知一律 False；已分类的只读工具可为 True（保留信息供 T06/T09 使用）。"""
    assert is_retry_safe("file_read") is True
    assert is_retry_safe("bash") is False
    assert supports_idempotency_key("file_delete") is True
