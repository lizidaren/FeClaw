"""ExecutionContext 工厂（T04 接入层）的测试。

工厂只做“从服务端身份/模式派生上下文”，判定由 execution_policy 负责；
这里卡住的关键点是：**模式只能收紧能力**、**默认范围不含 share**、
**enabled_tools 只能缩小能力**、**子上下文不能放大**。
"""

import os
import sys

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

pytest.importorskip("sqlalchemy")

from services import execution_context as ec  # noqa: E402
from services.execution_policy import (  # noqa: E402
    POLICY_MODE_FULL,
    POLICY_MODE_READONLY,
    POLICY_MODE_RESTRICTED,
    PolicyError,
    SCOPE_AGENT,
    SCOPE_SHARE,
    authorize_tool,
)
from services.tool_registry import (  # noqa: E402
    ALL_CAPABILITIES,
    CAP_FILE_READ,
    CAP_FILE_WRITE,
    CAP_SANDBOX_EXECUTE,
)


def test_full_mode_gets_all_capabilities():
    assert ec.capabilities_for_mode(POLICY_MODE_FULL) == ALL_CAPABILITIES


def test_readonly_mode_cannot_write_or_execute():
    """readonly 下：能力里不含 file.write，且授权层按 effects 拒绝写/执行。

    注意：capability 名是“能力域”，只读工具也可能声明同名能力
    （如 python_task_list 只是读，却需要 sandbox.execute 能力去查任务）。
    因此真正的边界是 authorize_tool 的 effects 允许清单，不是能力名本身。
    """
    ctx = ec.build_context(1, "abcd", "web", policy_mode=POLICY_MODE_READONLY)

    assert CAP_FILE_WRITE not in ctx.capabilities
    assert authorize_tool(ctx, "file_write", {}).allowed is False
    assert authorize_tool(ctx, "bash", {"command": "id"}).allowed is False
    assert authorize_tool(ctx, "file_read", {"path": "workspace/a.md"}).allowed is True


def test_restricted_mode_allows_write_but_not_execute_or_publish():
    ctx = ec.build_context(1, "abcd", "web", policy_mode=POLICY_MODE_RESTRICTED)

    assert authorize_tool(ctx, "file_write", {"path": "workspace/a.md"}).allowed is True
    denied_exec = authorize_tool(ctx, "bash", {"command": "id"})
    assert denied_exec.allowed is False
    assert "execute" in denied_exec.denied_effects
    assert authorize_tool(ctx, "create_post", {"content": "x"}).allowed is False
    assert authorize_tool(ctx, "create_cron_job", {"content": "x"}).allowed is False


def test_capabilities_for_tools_only_union_of_named_tools():
    caps = ec.capabilities_for_tools(["file_read"])

    assert CAP_FILE_READ in caps
    assert CAP_FILE_WRITE not in caps


def test_capabilities_for_unknown_tool_is_empty():
    assert ec.capabilities_for_tools(["definitely_not_a_tool"]) == frozenset()


def test_build_context_defaults_are_fail_closed_but_usable():
    ctx = ec.build_context(42, "abcd", "web", session_id="sess-1")

    assert ctx.user_id == "42"
    assert ctx.agent_hash == "abcd"
    assert ctx.channel == "web"
    assert ctx.policy_mode == POLICY_MODE_FULL
    assert ctx.capabilities == ALL_CAPABILITIES
    assert SCOPE_AGENT in ctx.allowed_resource_scopes
    assert SCOPE_SHARE not in ctx.allowed_resource_scopes  # 分享必须由 mapping 解析


def test_enabled_tools_only_shrink_capabilities():
    ctx = ec.build_context(1, "abcd", "web", enabled_tools=["file_read"])

    assert CAP_FILE_READ in ctx.capabilities
    assert CAP_FILE_WRITE not in ctx.capabilities


def test_explicit_capabilities_cannot_exceed_mode():
    ctx = ec.build_context(
        1, "abcd", "web",
        policy_mode=POLICY_MODE_READONLY,
        capabilities=frozenset({CAP_FILE_READ, CAP_FILE_WRITE}),
    )

    assert CAP_FILE_READ in ctx.capabilities
    assert CAP_FILE_WRITE not in ctx.capabilities


def test_readonly_child_shrinks_and_cannot_escalate():
    parent = ec.build_context(7, "abcd", "web")

    child = ec.readonly_child(parent)

    assert child.policy_mode == POLICY_MODE_READONLY
    assert CAP_FILE_WRITE not in child.capabilities
    assert child.parent_run_id == parent.run_id  # child() 自动写 parent_run_id
    with pytest.raises(PolicyError):
        child.child(policy_mode=POLICY_MODE_FULL)


def test_restricted_child_is_restricted():
    parent = ec.build_context(7, "abcd", "web")

    child = ec.restricted_child(parent)

    assert child.policy_mode == POLICY_MODE_RESTRICTED
    assert authorize_tool(child, "file_write", {"path": "workspace/a.md"}).allowed is True
    assert authorize_tool(child, "bash", {"command": "id"}).allowed is False


def test_scheduler_context_is_restricted():
    ctx = ec.scheduler_context(9, "abcd")

    assert ctx.policy_mode == POLICY_MODE_RESTRICTED
    assert authorize_tool(ctx, "file_write", {"path": "workspace/a.md"}).allowed is True
    assert authorize_tool(ctx, "bash", {"command": "id"}).allowed is False


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        ec.build_context(1, "abcd", "web", policy_mode="everything")
