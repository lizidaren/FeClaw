"""执行授权（execution policy）—— **全仓唯一的“这次工具调用能不能执行”判定**。

T04 底座切片。本模块只负责“授权”，不负责执行、不查数据库、不写全局状态。

一条铁律
--------
任何执行路径（`services/agent_executor.py`、群聊 `GroupDispatchService`、
子助手 `spawn_subagent`、审批通过后的执行、预取/自动调用）都必须**在**“特殊分支 /
找到方法 / 真正调用”之前调用 `authorize_tool(...)`，并用返回值决定是否继续。
不要在别的模块再写一套 `if tool_name in allowed` / `if category != "file"` 判定 ——
第二套判定必然与这里漂移，历史上“配置里禁用了、换个入口还能调”就是这么来的。

身份只能由服务端给出
--------------------
`ExecutionContext` 的字段（尤其是 `capabilities` / `policy_mode` /
`allowed_resource_scopes`）必须由服务端在任务开始时构造，**绝不能**从模型 JSON、
工具参数或聊天内容复制。子任务通过 `ExecutionContext.child(...)` 继承：
只能与父一致或更窄（能力交、范围交、deadline 更早、budget 更低），放大直接抛
`PolicyError`。

fail closed 清单（缺省即拒绝，不因“没配置”而放行）
-------------------------------------------------
- 工具未注册 → `unknown_tool`
- 工具已注册但未分类（registry 里没有 effects 声明）→ `tool_unclassified`
  （**不默认只读**：无法证明只读就拒绝）
- `capabilities is None`（未声明）→ `capabilities_unset`；缺少所需能力 → `capability_missing`
- 给了 resource 但 `allowed_resource_scopes is None` → `resource_scope_unset`
- 参数里出现身份/策略类保留键（`user_id`、`capabilities`…）且该工具未声明此参数 → `forbidden_argument`
- 模式白名单不包含该效果 → `policy_denied`
- 当前时间已过 deadline → `deadline_exceeded`

模式（policy_mode）语义 —— 按**允许清单**，不是黑名单
---------------------------------------------------
- `full`       ：允许全部 9 种效果（能力仍逐项校验）
- `restricted` ：只允许 `read` + `write`（本地、可回滚的副作用）。
                 delete / execute / publish / send / schedule / delegate / system 一律拒绝
                 （删除、发群、发布、代跑代码应由审批（T08）在明确的受控上下文里执行）
- `readonly`   ：只允许 `read`，外加**已显式声明为无副作用（effects=()）**的计算类工具。
                 发布 / 删除 / 写文件 / 执行 / 定时 / 系统日志清理全部拒绝。

与资源授权的关系
----------------
`authorize_tool` 只回答“工具 + 模式 + 能力 + 资源范围”这一层；对**具体文件**的
owner / ACL / 敏感文件判定由 `services/resource_access.py` 的 `can_read` / `can_write`
负责。两者都由同一条执行路径调用，不要互相替代。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping, Optional

from services.tool_registry import (
    EFFECT_READ,
    EFFECT_WRITE,
    ALL_CAPABILITIES,
    ALL_EFFECTS,
    CLASSIFICATION_UNKNOWN,
    CAP_FILE_READ,
    CAP_FILE_WRITE,
    get_tool,
    normalize_tool_name,
    tool_conditional_effects,
    tool_metadata,
)

__all__ = [
    "POLICY_MODE_FULL",
    "POLICY_MODE_RESTRICTED",
    "POLICY_MODE_READONLY",
    "POLICY_MODES",
    "POLICY_MODE_STRICTNESS",
    "MODE_ALLOWED_EFFECTS",
    "RESOURCE_SCOPES",
    "SCOPE_AGENT",
    "SCOPE_USER",
    "SCOPE_GROUP",
    "SCOPE_PUBLIC",
    "SCOPE_SHARE",
    "RESERVED_ARGUMENT_KEYS",
    "REASON_HTTP_STATUS",
    "CAPABILITY_FILE_READ",
    "CAPABILITY_FILE_WRITE",
    "PolicyError",
    "RunBudget",
    "ExecutionContext",
    "AuthorizationDecision",
    "mode_allows_effect",
    "authorize_tool",
]

# ── 策略模式 ────────────────────────────────────────────────────────────────
POLICY_MODE_FULL = "full"
POLICY_MODE_RESTRICTED = "restricted"
POLICY_MODE_READONLY = "readonly"

POLICY_MODES: FrozenSet[str] = frozenset({
    POLICY_MODE_FULL,
    POLICY_MODE_RESTRICTED,
    POLICY_MODE_READONLY,
})

# 数字越大越严格。子任务只能“不放松”父任务：child_strictness >= parent_strictness。
POLICY_MODE_STRICTNESS: Mapping[str, int] = {
    POLICY_MODE_FULL: 0,
    POLICY_MODE_RESTRICTED: 1,
    POLICY_MODE_READONLY: 2,
}

# 每个模式**允许**的 effects（允许清单；不在清单里的效果一律拒绝）。
# 注意：effects=() 的已分类工具（纯计算）在三个模式下都放行，因为它们不产生副作用。
MODE_ALLOWED_EFFECTS: Mapping[str, FrozenSet[str]] = {
    POLICY_MODE_FULL: ALL_EFFECTS,
    POLICY_MODE_RESTRICTED: frozenset({EFFECT_READ, EFFECT_WRITE}),
    POLICY_MODE_READONLY: frozenset({EFFECT_READ}),
}

# ── 资源范围词表（与 services/resource_access.py 共用） ──────────────────────
SCOPE_AGENT = "agent"
SCOPE_USER = "user"
SCOPE_GROUP = "group"
SCOPE_PUBLIC = "public"
SCOPE_SHARE = "share"

RESOURCE_SCOPES: FrozenSet[str] = frozenset({
    SCOPE_AGENT,
    SCOPE_USER,
    SCOPE_GROUP,
    SCOPE_PUBLIC,
    SCOPE_SHARE,
})

# ── 资源授权的默认能力名（resource_access 复用同一对标量） ────────────────────
CAPABILITY_FILE_READ = CAP_FILE_READ
CAPABILITY_FILE_WRITE = CAP_FILE_WRITE

# 模型**不允许**通过工具参数提高权限的身份/策略字段（C1）。
# 只有当工具自己声明了同名参数（例如 reply_buffer_flush 的历史参数 user_id）时才放行，
# 那种情况属于既有 API 的地址选择问题，需在调用点单独收口（见报告跨界清单）。
RESERVED_ARGUMENT_KEYS: FrozenSet[str] = frozenset({
    "user_id",
    "agent_hash",
    "capabilities",
    "required_capabilities",
    "policy_mode",
    "allowed_resource_scopes",
    "run_id",
    "session_id",
    "parent_run_id",
    "budget",
    "deadline",
})

# 授权原因码 → 建议 HTTP 状态（路由层据此映射 400/403，不要自己再猜）
REASON_HTTP_STATUS: Mapping[str, int] = {
    "ok": 200,
    "unknown_tool": 400,
    "tool_unclassified": 400,
    "policy_invalid": 400,
    "forbidden_argument": 400,
    "arguments_invalid": 400,
    "resource_invalid": 400,
    "capabilities_unset": 403,
    "capability_missing": 403,
    "resource_scope_unset": 403,
    "scope_not_allowed": 403,
    "policy_denied": 403,
    "deadline_exceeded": 403,
    "policy_escalation": 403,
}


class PolicyError(Exception):
    """策略/授权失败（格式非法、模式未知、子任务放大权限等）。

    reason 是机器可读的稳定原因码；detail 只用于日志，不直接回给终端用户。
    路由层可用 `http_status` 直接映射，避免各处自己判断 400/403。
    """

    def __init__(self, reason: str, detail: str = "", *, decision: "Optional[AuthorizationDecision]" = None):
        self.reason = reason
        self.detail = detail or reason
        self.decision = decision
        super().__init__(f"{reason}: {self.detail}")

    @property
    def http_status(self) -> int:
        return REASON_HTTP_STATUS.get(self.reason, 403)


# ── 预算 ────────────────────────────────────────────────────────────────────

def _check_non_negative(name: str, value: Any, *, integer: bool) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int,) if integer else (int, float)):
        raise PolicyError("policy_invalid", f"{name} 类型非法: {value!r}")
    if value < 0:
        raise PolicyError("policy_invalid", f"{name} 不能为负: {value!r}")


@dataclass(frozen=True)
class RunBudget:
    """一次运行的预算上限（`None` 表示该维度不限额）。

    T04 底座只负责“子任务不能放宽预算”的**静态**校验；真正的 reserve-before-call /
    settle-after-call 记账属于 T07（`tests/test_run_budget.py`）。

    注意 `None` 的语义是“不限额”，不是“继承父值”：`is_within(parent)` 会把
    “父有限额、子为 None”判成放宽。要基于父预算收紧个别维度，用
    `parent.tightened(max_tool_calls=5)`，它会保留其余维度。
    """

    max_tool_calls: Optional[int] = None
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    max_child_runs: Optional[int] = None
    max_seconds: Optional[float] = None
    max_cost_cny: Optional[float] = None

    def __post_init__(self) -> None:
        _check_non_negative("max_tool_calls", self.max_tool_calls, integer=True)
        _check_non_negative("max_input_tokens", self.max_input_tokens, integer=True)
        _check_non_negative("max_output_tokens", self.max_output_tokens, integer=True)
        _check_non_negative("max_child_runs", self.max_child_runs, integer=True)
        _check_non_negative("max_seconds", self.max_seconds, integer=False)
        _check_non_negative("max_cost_cny", self.max_cost_cny, integer=False)

    def tightened(self, **overrides: Any) -> "RunBudget":
        """在当前预算上收紧若干维度（未给出的维度保持不变）。"""
        values = {dim: getattr(self, dim) for dim in BUDGET_DIMENSIONS}
        for key, value in overrides.items():
            if key not in values:
                raise PolicyError("policy_invalid", f"未知预算维度: {key!r}")
            values[key] = value
        return RunBudget(**values)

    def is_within(self, parent: "Optional[RunBudget]") -> bool:
        """本预算是否“不高于” parent（每一维都不放宽）。parent 为 None 视为不限额。"""
        if parent is None:
            return True
        for dim in BUDGET_DIMENSIONS:
            parent_value = getattr(parent, dim)
            if parent_value is None:
                continue
            own_value = getattr(self, dim)
            if own_value is None:
                return False  # 父有限额、子不限额 = 放宽
            if own_value > parent_value:
                return False
        return True


# ── 执行上下文 ──────────────────────────────────────────────────────────────

_UNSET = object()

# RunBudget 的维度（顺序稳定，便于 is_within / tightened 复用）
BUDGET_DIMENSIONS = (
    "max_tool_calls",
    "max_input_tokens",
    "max_output_tokens",
    "max_child_runs",
    "max_seconds",
    "max_cost_cny",
)


def _as_frozenset_str(value: Any, *, field_name: str) -> Optional[FrozenSet[str]]:
    if value is None:
        return None
    if isinstance(value, str) or not hasattr(value, "__iter__"):
        raise PolicyError("policy_invalid", f"{field_name} 必须是字符串集合")
    items = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PolicyError("policy_invalid", f"{field_name} 含非法条目: {item!r}")
        items.add(item.strip())
    return frozenset(items)


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError("policy_invalid", f"{field_name} 必须是非空字符串")
    return value.strip()


def _require_identifier(value: Any, *, field_name: str) -> str:
    """账号类 id：DB 里是 int，其它地方是 str —— 统一成字符串但拒绝空/布尔。"""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise PolicyError("policy_invalid", f"{field_name} 必须是字符串或整数 id")
    text = str(value).strip()
    if not text:
        raise PolicyError("policy_invalid", f"{field_name} 不能为空")
    return text


@dataclass(frozen=True)
class ExecutionContext:
    """一次执行任务的**不可变**服务端身份与权限。

    字段语义：
        user_id: 账号 id（服务端解析，不接受模型输入）
        agent_hash: Agent hash（服务端解析；执行必须绑定某个 Agent）
        channel: 执行渠道（web / wechat / desktop / group / cron / system …）
        session_id: 会话标识（可空）
        run_id: 本次运行 id（可空；T05 的 AgentRun 会成为权威来源）
        parent_run_id: 父运行 id（`child()` 自动写入）
        policy_mode: full / restricted / readonly（见模块 docstring）
        capabilities: 允许的能力集合。**None 表示“未声明”**，一律 fail closed；
                     显式空集 `frozenset()` 表示“没有任何能力”，同样不放行需要能力的工具
        allowed_resource_scopes: 允许触碰的资源范围（agent/user/group/public/share）。
                                 None 表示未声明；一旦要触碰具体资源就必须显式给出
        deadline: 绝对截止时间（epoch 秒，UTC）。子任务不得晚于父任务
        budget: RunBudget（子任务不得放宽）
    """

    user_id: str
    agent_hash: str
    channel: str
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    policy_mode: str = POLICY_MODE_RESTRICTED
    capabilities: Optional[FrozenSet[str]] = None
    allowed_resource_scopes: Optional[FrozenSet[str]] = None
    deadline: Optional[float] = None
    budget: Optional[RunBudget] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _require_identifier(self.user_id, field_name="user_id"))
        object.__setattr__(self, "agent_hash", _require_text(self.agent_hash, field_name="agent_hash"))
        object.__setattr__(self, "channel", _require_text(self.channel, field_name="channel"))
        for name in ("session_id", "run_id", "parent_run_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_text(value, field_name=name))

        if self.policy_mode not in POLICY_MODES:
            raise PolicyError("policy_invalid", f"未知 policy_mode: {self.policy_mode!r}")

        caps = _as_frozenset_str(self.capabilities, field_name="capabilities")
        if caps is not None:
            unknown = caps - ALL_CAPABILITIES
            if unknown:
                raise PolicyError("policy_invalid", f"未知 capabilities: {sorted(unknown)}")
        object.__setattr__(self, "capabilities", caps)

        scopes = _as_frozenset_str(self.allowed_resource_scopes, field_name="allowed_resource_scopes")
        if scopes is not None:
            unknown_scopes = scopes - RESOURCE_SCOPES
            if unknown_scopes:
                raise PolicyError("policy_invalid", f"未知资源范围: {sorted(unknown_scopes)}")
        object.__setattr__(self, "allowed_resource_scopes", scopes)

        if self.deadline is not None:
            if isinstance(self.deadline, bool) or not isinstance(self.deadline, (int, float)):
                raise PolicyError("policy_invalid", "deadline 必须是 epoch 秒（数字）")
            object.__setattr__(self, "deadline", float(self.deadline))

        if self.budget is not None and not isinstance(self.budget, RunBudget):
            raise PolicyError("policy_invalid", "budget 必须是 RunBudget")

    # -- 派生 / 传递 --------------------------------------------------------

    def child(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Any = _UNSET,
        capabilities: Any = _UNSET,
        allowed_resource_scopes: Any = _UNSET,
        policy_mode: Optional[str] = None,
        deadline: Any = _UNSET,
        budget: Any = _UNSET,
        user_id: Optional[str] = None,
        agent_hash: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> "ExecutionContext":
        """派生一个**只能更窄**的子任务上下文；任何放宽都抛 `PolicyError`。

        与父一致是允许的（幂等）；只提供服务端可控的维度。身份字段（user/agent/channel）
        若显式传入，必须与父相同 —— 子任务不能换身份、不能换 Agent。
        """
        # 身份：只能相同
        for field_name, requested in (
            ("user_id", user_id),
            ("agent_hash", agent_hash),
            ("channel", channel),
        ):
            if requested is not None and str(requested) != str(getattr(self, field_name)):
                raise PolicyError(
                    "policy_escalation",
                    f"子任务不能更换 {field_name}: {getattr(self, field_name)!r} → {requested!r}",
                )

        # 会话：可继承或显式指定同一会话；换一个会话属于状态混淆，拒绝
        if session_id is _UNSET:
            child_session = self.session_id
        else:
            child_session = session_id
            if child_session is not None and self.session_id is not None and child_session != self.session_id:
                raise PolicyError(
                    "policy_escalation",
                    f"子任务不能切换到另一个会话: {self.session_id!r} → {child_session!r}",
                )

        # 模式：只能更严格
        child_mode = self.policy_mode if policy_mode is None else policy_mode
        if child_mode not in POLICY_MODES:
            raise PolicyError("policy_invalid", f"未知 policy_mode: {child_mode!r}")
        if POLICY_MODE_STRICTNESS[child_mode] < POLICY_MODE_STRICTNESS[self.policy_mode]:
            raise PolicyError(
                "policy_escalation",
                f"子任务不能放宽 policy_mode: {self.policy_mode} → {child_mode}",
            )

        # 能力 / 资源范围：只能取交集（父未声明时子也只能未声明或为空）
        child_caps = self.capabilities if capabilities is _UNSET else _as_frozenset_str(
            capabilities, field_name="capabilities"
        )
        if child_caps is not None:
            unknown_caps = child_caps - ALL_CAPABILITIES
            if unknown_caps:
                raise PolicyError("policy_invalid", f"未知 capabilities: {sorted(unknown_caps)}")
        _assert_no_widening(child_caps, self.capabilities, label="capabilities")

        child_scopes = self.allowed_resource_scopes if allowed_resource_scopes is _UNSET else _as_frozenset_str(
            allowed_resource_scopes, field_name="allowed_resource_scopes"
        )
        if child_scopes is not None:
            unknown_scopes = child_scopes - RESOURCE_SCOPES
            if unknown_scopes:
                raise PolicyError("policy_invalid", f"未知资源范围: {sorted(unknown_scopes)}")
        _assert_no_widening(child_scopes, self.allowed_resource_scopes, label="allowed_resource_scopes")

        # 截止时间：只能更早（或不晚于父）；父有 deadline 时子不能取消
        if deadline is _UNSET:
            child_deadline = self.deadline
        else:
            child_deadline = deadline
            if child_deadline is not None:
                if isinstance(child_deadline, bool) or not isinstance(child_deadline, (int, float)):
                    raise PolicyError("policy_invalid", "deadline 必须是 epoch 秒（数字）")
                child_deadline = float(child_deadline)
            if self.deadline is not None:
                if child_deadline is None:
                    raise PolicyError("policy_escalation", "子任务不能取消父任务的 deadline")
                if child_deadline > self.deadline:
                    raise PolicyError(
                        "policy_escalation",
                        f"子任务 deadline 晚于父任务: {child_deadline} > {self.deadline}",
                    )

        # 预算：只能更低（不复用父的 unlimited）
        if budget is _UNSET:
            child_budget = self.budget
        else:
            child_budget = budget
            if child_budget is not None and not isinstance(child_budget, RunBudget):
                raise PolicyError("policy_invalid", "budget 必须是 RunBudget")
            if not (child_budget.is_within(self.budget) if child_budget is not None else self.budget is None):
                raise PolicyError(
                    "policy_escalation",
                    "子任务预算高于父任务（或父有限额而子未设限）",
                )

        return ExecutionContext(
            user_id=self.user_id,
            agent_hash=self.agent_hash,
            channel=self.channel,
            session_id=child_session,
            run_id=run_id,
            parent_run_id=self.run_id,
            policy_mode=child_mode,
            capabilities=child_caps,
            allowed_resource_scopes=child_scopes,
            deadline=child_deadline,
            budget=child_budget,
        )


def _assert_no_widening(
    child: Optional[FrozenSet[str]],
    parent: Optional[FrozenSet[str]],
    *,
    label: str,
) -> None:
    """子集合必须包含于父集合；父未声明（None）时子只能 None 或空集。"""
    if parent is None:
        if child is None or not child:
            return
        raise PolicyError("policy_escalation", f"父任务未声明 {label}，子任务不能新授予: {sorted(child)}")
    if child is None:
        raise PolicyError("policy_escalation", f"子任务不能把 {label} 变成未声明状态")
    extra = child - parent
    if extra:
        raise PolicyError("policy_escalation", f"子任务 {label} 超出父任务: {sorted(extra)}")


# ── 授权结果 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AuthorizationDecision:
    """一次工具授权的结构化结果。

    为什么不只返回 bool：调用方需要把 `reason` 映射成 400/403、写审计日志、给用户
    可解释的提示，并在测试里断言**具体**拒绝原因（而不是“没通过”）。
    """

    allowed: bool
    reason: str
    required_capabilities: FrozenSet[str] = field(default=frozenset())
    effects: FrozenSet[str] = field(default=frozenset())
    tool_name: str = ""
    classification: str = ""
    missing_capabilities: FrozenSet[str] = field(default=frozenset())
    denied_effects: FrozenSet[str] = field(default=frozenset())
    detail: str = ""

    @property
    def http_status(self) -> int:
        return REASON_HTTP_STATUS.get(self.reason, 403)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "tool_name": self.tool_name,
            "classification": self.classification,
            "effects": sorted(self.effects),
            "required_capabilities": sorted(self.required_capabilities),
            "missing_capabilities": sorted(self.missing_capabilities),
            "denied_effects": sorted(self.denied_effects),
            "detail": self.detail,
        }

    def require(self) -> "AuthorizationDecision":
        """拒绝时抛 PolicyError；调用方可 `decision = authorize_tool(...).require()`。"""
        if not self.allowed:
            raise PolicyError(self.reason, self.detail, decision=self)
        return self


def mode_allows_effect(policy_mode: str, effect: str) -> bool:
    """该模式是否允许这类效果（resource_access 复用同一份允许清单）。"""
    allowed = MODE_ALLOWED_EFFECTS.get(policy_mode)
    if allowed is None:
        return False
    return effect in allowed


# ── 唯一授权入口 ────────────────────────────────────────────────────────────

def _deny(
    reason: str,
    *,
    tool_name: str = "",
    classification: str = "",
    effects: FrozenSet[str] = frozenset(),
    required: FrozenSet[str] = frozenset(),
    missing: FrozenSet[str] = frozenset(),
    denied: FrozenSet[str] = frozenset(),
    detail: str = "",
) -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=False,
        reason=reason,
        required_capabilities=frozenset(required),
        effects=frozenset(effects),
        tool_name=tool_name,
        classification=classification,
        missing_capabilities=frozenset(missing),
        denied_effects=frozenset(denied),
        detail=detail,
    )


def _resource_scope_of(resource: Any) -> Optional[str]:
    """从 resource 上取 scope（ResolvedResource 或字符串均可）。"""
    if resource is None:
        return None
    if isinstance(resource, str):
        return resource
    scope = getattr(resource, "scope", None)
    return scope if isinstance(scope, str) else None


def authorize_tool(
    context: ExecutionContext,
    tool_name: str,
    arguments: Optional[Mapping[str, Any]] = None,
    *,
    resource: Any = None,
    now: Optional[float] = None,
) -> AuthorizationDecision:
    """**唯一执行授权入口**：这次工具调用能不能执行。

    纯函数：不访问数据库、不发网络请求、不写全局状态（`now` 可注入便于测试）。
    返回 `AuthorizationDecision`；绝不因为“没有配置/没有分类/没见过”而放行。
    """
    if not isinstance(context, ExecutionContext):
        return _deny("policy_invalid", detail="context 必须是 ExecutionContext")

    if not isinstance(tool_name, str) or not tool_name.strip():
        return _deny("unknown_tool", detail=f"非法工具名: {tool_name!r}")

    canonical = normalize_tool_name(tool_name)
    meta = tool_metadata(canonical)

    # 1) 未知工具 / 未分类工具：都拒绝（“没见过”不等于“只读”）
    if not meta.registered:
        return _deny("unknown_tool", tool_name=canonical, detail=f"工具未注册: {canonical}")
    if meta.classification == CLASSIFICATION_UNKNOWN:
        return _deny(
            "tool_unclassified",
            tool_name=canonical,
            classification=meta.classification,
            detail=f"工具 {canonical} 未在 tool_registry 声明执行效果，按不可信处理",
        )

    # 2) 参数面：不允许模型用参数提高身份/策略（C1）
    if arguments is not None and not isinstance(arguments, Mapping):
        return _deny(
            "arguments_invalid",
            tool_name=canonical,
            classification=meta.classification,
            detail="arguments 必须是映射",
        )
    declared_params = set((get_tool(canonical) or {}).get("param_names") or [])
    if isinstance(arguments, Mapping):
        reserved_hits = sorted(
            key for key in arguments.keys()
            if isinstance(key, str) and key in RESERVED_ARGUMENT_KEYS and key not in declared_params
        )
        if reserved_hits:
            return _deny(
                "forbidden_argument",
                tool_name=canonical,
                classification=meta.classification,
                detail=f"工具 {canonical} 不接受身份/策略参数: {reserved_hits}",
            )

    # 3) 能力：未声明 → 拒绝；缺少 → 拒绝（空 capability 集不是放行理由）
    required = frozenset(meta.required_capabilities)
    if context.capabilities is None:
        return _deny(
            "capabilities_unset",
            tool_name=canonical,
            classification=meta.classification,
            effects=meta.effects,
            required=required,
            detail="ExecutionContext.capabilities 未声明（不允许默认放开）",
        )
    missing = required - context.capabilities
    if missing:
        return _deny(
            "capability_missing",
            tool_name=canonical,
            classification=meta.classification,
            effects=meta.effects,
            required=required,
            missing=missing,
            detail=f"缺少能力: {sorted(missing)}",
        )

    # 4) 资源范围（只做 scope 门禁；具体 owner/ACL 由 resource_access 判定）
    scope = _resource_scope_of(resource)
    if scope is not None:
        if context.allowed_resource_scopes is None:
            return _deny(
                "resource_scope_unset",
                tool_name=canonical,
                classification=meta.classification,
                effects=meta.effects,
                required=required,
                detail="未声明 allowed_resource_scopes，不能触碰具体资源",
            )
        if scope not in context.allowed_resource_scopes:
            return _deny(
                "scope_not_allowed",
                tool_name=canonical,
                classification=meta.classification,
                effects=meta.effects,
                required=required,
                detail=f"资源范围 {scope!r} 不在允许集合内",
            )

    # 5) 模式允许清单（含按参数追加的效果）
    effects = frozenset(meta.effects) | tool_conditional_effects(canonical, arguments)
    allowed_effects = MODE_ALLOWED_EFFECTS[context.policy_mode]
    denied_effects = effects - allowed_effects
    if denied_effects:
        return _deny(
            "policy_denied",
            tool_name=canonical,
            classification=meta.classification,
            effects=effects,
            required=required,
            denied=denied_effects,
            detail=(
                f"policy_mode={context.policy_mode} 不允许效果 "
                f"{sorted(denied_effects)}（工具 {canonical}）"
            ),
        )

    # 6) 截止时间
    if context.deadline is not None:
        current = time.time() if now is None else float(now)
        if current > context.deadline:
            return _deny(
                "deadline_exceeded",
                tool_name=canonical,
                classification=meta.classification,
                effects=effects,
                required=required,
                detail=f"已超过 deadline（now={current} > {context.deadline}）",
            )

    return AuthorizationDecision(
        allowed=True,
        reason="ok",
        required_capabilities=required,
        effects=effects,
        tool_name=canonical,
        classification=meta.classification,
    )
