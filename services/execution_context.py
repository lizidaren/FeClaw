"""ExecutionContext 工厂（T04 接入层，主 agent 维护）。

`services/execution_policy.py` 定义“能不能执行”的判定；本模块只负责**构造**上下文：

* 能力集合从 `services/tool_registry.py` 的声明派生，避免在调用点手写第二份词表；
* 默认是 fail-closed 的：不传 `policy_mode`/能力就不放行，绝不因“没配置”而全开；
* 只提供**服务端可控**的维度（身份、渠道、模式、资源范围、预算）；模型输出永远不能
  参与构造 `capabilities` / `policy_mode` / `allowed_resource_scopes`。

调用点示例（接入 executor 后）：

    ctx = build_context(user_id, agent_hash, channel="web", session_id=session_id)
    decision = authorize_tool(ctx, "file_write", args, resource=resource)
    decision.require()  # 拒绝时抛 PolicyError（reason 稳定，路由映射 400/403）
"""

from __future__ import annotations

from typing import Any, FrozenSet, Iterable, Optional

from services.execution_policy import (
    POLICY_MODE_FULL,
    POLICY_MODE_READONLY,
    POLICY_MODE_RESTRICTED,
    POLICY_MODES,
    SCOPE_AGENT,
    SCOPE_GROUP,
    SCOPE_PUBLIC,
    SCOPE_USER,
    ExecutionContext,
    RunBudget,
)
from services.tool_registry import (
    ALL_CAPABILITIES,
    CLASSIFICATION_UNKNOWN,
    EFFECT_READ,
    EFFECT_WRITE,
    TOOL_EFFECT_METADATA,
    tool_metadata,
)

# 普通执行默认可触碰的资源范围。`share` 故意不在默认集合里：
# 分享资源必须由 share mapping 解析（见 resource_access.resolve_share_mapping_resource），
# 不能凭“上下文里有 share 范围”直接读任意分享路径。
DEFAULT_RESOURCE_SCOPES: FrozenSet[str] = frozenset({
    SCOPE_AGENT, SCOPE_USER, SCOPE_GROUP, SCOPE_PUBLIC,
})


def _declared(name: str):
    """取某个工具的已声明元数据；未声明/未分类返回 None。"""
    meta = tool_metadata(name)
    if meta is None or getattr(meta, "classification", CLASSIFICATION_UNKNOWN) == CLASSIFICATION_UNKNOWN:
        return None
    effects = frozenset(getattr(meta, "effects", ()) or ())
    caps = frozenset(getattr(meta, "required_capabilities", ()) or ())
    return effects, caps


def _caps_for_effects(allowed_effects: FrozenSet[str],
                      include_effectless: bool = True) -> FrozenSet[str]:
    """把“效果 ⊆ allowed_effects”的工具所需能力并起来（按声明派生）。"""
    caps = set()
    for name in TOOL_EFFECT_METADATA:
        declared = _declared(name)
        if declared is None:
            continue
        effects, required = declared
        if effects <= allowed_effects or (include_effectless and not effects):
            caps.update(required)
    return frozenset(caps & ALL_CAPABILITIES)


def full_capabilities() -> FrozenSet[str]:
    """full 模式：全部已声明能力（工具本身仍受 effects/模式/注册表约束）。"""
    return ALL_CAPABILITIES


def restricted_capabilities() -> FrozenSet[str]:
    """restricted 模式：只包含“仅读/写或无副作用”工具的能力。"""
    return _caps_for_effects(frozenset({EFFECT_READ, EFFECT_WRITE}))


def readonly_capabilities() -> FrozenSet[str]:
    """readonly 模式：只包含“只读或无副作用”工具的能力。

    显式排除任何声明了写/删/执行/发布/发送/调度/委派/系统效果的工具 ——
    即便某个只读工具与写工具共用一个能力名（例如 file.read vs file.write 不同，
    但同能力名跨工具时），readonly 下也不会拿到写能力。
    """
    return _caps_for_effects(frozenset({EFFECT_READ}))


def capabilities_for_mode(policy_mode: str) -> FrozenSet[str]:
    if policy_mode == POLICY_MODE_FULL:
        return full_capabilities()
    if policy_mode == POLICY_MODE_RESTRICTED:
        return restricted_capabilities()
    if policy_mode == POLICY_MODE_READONLY:
        return readonly_capabilities()
    raise ValueError(f"未知 policy_mode: {policy_mode!r}")


def capabilities_for_tools(tool_names: Iterable[str],
                           *, include_unknown: bool = False) -> FrozenSet[str]:
    """指定工具集合所需能力的并集（用于按 Agent 的 enabled_tools 收敛能力）。

    未注册/未分类工具默认跳过（它们本来就会被 `authorize_tool` 拒绝）；
    `include_unknown=True` 时也不加能力（不因未知而放行）。
    """
    caps = set()
    for name in tool_names or ():
        declared = _declared(str(name))
        if declared is None:
            if include_unknown:
                continue
            continue
        caps.update(declared[1])
    return frozenset(caps & ALL_CAPABILITIES)


def build_context(
    user_id: Any,
    agent_hash: Any,
    channel: str,
    *,
    session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    parent_run_id: Optional[str] = None,
    policy_mode: str = POLICY_MODE_FULL,
    enabled_tools: Optional[Iterable[str]] = None,
    capabilities: Optional[FrozenSet[str]] = None,
    allowed_resource_scopes: Optional[Iterable[str]] = None,
    deadline: Optional[float] = None,
    budget: Optional[RunBudget] = None,
) -> ExecutionContext:
    """构造一个执行上下文（服务端身份，不接受模型输入）。

    * `policy_mode` 缺省 `full`（普通 Web/IM 会话）；子 Agent 请用 `readonly_child()`
      或显式传 `restricted`/`readonly`。
    * `enabled_tools` 给了就按这些工具的能力**交集**收敛（只缩小，不放大）。
    * `capabilities` 显式传入时以它为准（测试/特殊入口用），但仍会被 `enabled_tools` 截断。
    """
    if policy_mode not in POLICY_MODES:
        raise ValueError(f"未知 policy_mode: {policy_mode!r}")
    if not isinstance(channel, str) or not channel.strip():
        raise ValueError("channel 必须是非空字符串（服务端确定，不接受模型输入）")

    mode_caps = capabilities_for_mode(policy_mode)
    if capabilities is None:
        caps = mode_caps
    else:
        caps = frozenset(capabilities) & mode_caps  # 不允许超出模式上限

    if enabled_tools is not None:
        caps = caps & capabilities_for_tools(enabled_tools)

    scopes = DEFAULT_RESOURCE_SCOPES if allowed_resource_scopes is None else frozenset(
        allowed_resource_scopes
    )

    return ExecutionContext(
        user_id=str(user_id) if user_id is not None else "",
        agent_hash=str(agent_hash or ""),
        channel=channel.strip(),
        session_id=session_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        policy_mode=policy_mode,
        capabilities=caps,
        allowed_resource_scopes=scopes,
        deadline=deadline,
        budget=budget,
    )


def readonly_child(
    parent: ExecutionContext,
    *,
    run_id: Optional[str] = None,
    enabled_tools: Optional[Iterable[str]] = None,
    deadline: Optional[float] = None,
) -> ExecutionContext:
    """派生只读子上下文（能力/范围只会更小，由 `ExecutionContext.child` 强制）。"""
    caps = readonly_capabilities()
    if enabled_tools is not None:
        caps = caps & capabilities_for_tools(enabled_tools)
    kwargs: dict = {"policy_mode": POLICY_MODE_READONLY, "capabilities": caps}
    if run_id is not None:
        kwargs["run_id"] = run_id
    if deadline is not None:
        kwargs["deadline"] = deadline
    return parent.child(**kwargs)


def restricted_child(
    parent: ExecutionContext,
    *,
    run_id: Optional[str] = None,
    enabled_tools: Optional[Iterable[str]] = None,
) -> ExecutionContext:
    """派生子上下文（read+write，无发布/发送/执行/调度/系统效果）。"""
    caps = restricted_capabilities()
    if enabled_tools is not None:
        caps = caps & capabilities_for_tools(enabled_tools)
    kwargs: dict = {"policy_mode": POLICY_MODE_RESTRICTED, "capabilities": caps}
    if run_id is not None:
        kwargs["run_id"] = run_id
    return parent.child(**kwargs)


def scheduler_context(user_id: Any, agent_hash: Any, *,
                      channel: str = "scheduler") -> ExecutionContext:
    """定时/后台任务的上下文：restricted，无发布/发送/执行能力。"""
    return build_context(
        user_id, agent_hash, channel, policy_mode=POLICY_MODE_RESTRICTED,
    )


def denial_message(decision) -> str:
    """把授权拒绝的稳定 reason 映射成给终端用户看的短句。

    不回显参数、不暴露内部细节；日志里应记录 `decision.reason` 与 tool_name。
    """
    reason = getattr(decision, "reason", "")
    if reason == "unknown_tool":
        return "未知工具，已拒绝执行"
    if reason == "tool_unclassified":
        return "该工具未登记执行效果，出于安全考虑已拒绝"
    if reason in ("forbidden_argument", "arguments_invalid", "policy_invalid"):
        return "工具调用参数不合法（包含身份/策略字段），已拒绝"
    if reason in ("capabilities_unset", "capability_missing"):
        return "当前 Agent 未被授予该工具所需能力"
    if reason in ("scope_not_allowed", "resource_scope_unset"):
        return "该工具不允许访问此资源范围"
    if reason == "deadline_exceeded":
        return "任务已超过截止时间，工具调用被拒绝"
    if reason in ("policy_denied", "policy_escalation"):
        effects = ",".join(sorted(getattr(decision, "denied_effects", ()) or ()))
        return f"当前任务权限不允许 {effects or '该'} 类操作"
    return "该工具调用被策略拒绝"
