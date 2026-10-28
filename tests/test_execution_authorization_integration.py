"""T04 调用点接入：AgentExecutor 的授权门禁（不依赖真实 LLM/DB）。

验证：
* 有 ExecutionContext 时，execute_tool 在 special-case/blocked/方法查找之前授权；
* readonly 上下文拒绝写/执行，工具方法根本不会被调用；
* 构造上下文失败时不放行（fail closed）；
* 工具参数里的身份字段（user_id 等）被拒绝；
* enabled tools 配置解析失败时 fail closed（不再“None=全工具”）。
"""

import asyncio
import os
import sys

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

pytest.importorskip("sqlalchemy")

from services import execution_context as ec  # noqa: E402
from services.agent_executor import AgentExecutor  # noqa: E402
from services.execution_policy import POLICY_MODE_FULL, POLICY_MODE_READONLY  # noqa: E402


class _Tools:
    """最小工具服务：记录真正被调用的工具。"""

    def __init__(self):
        self.user_id = "42"
        self.agent_hash = "abcd"
        self.calls = []

    async def file_read(self, path):
        self.calls.append(("file_read", path))
        return "content"

    async def file_write(self, path, content=""):
        self.calls.append(("file_write", path))
        return "ok"

    async def _truncate_tool_result(self, result, tool_name="", tool_args=None, call_id=None):
        return result


def _run(coro):
    return asyncio.run(coro)


def test_without_context_keeps_legacy_behavior():
    tools = _Tools()
    executor = AgentExecutor("abcd", tools)

    result = _run(executor.execute_tool("file_write", {"path": "workspace/a.md", "content": "x"}))

    assert result == "ok"
    assert tools.calls == [("file_write", "workspace/a.md")]


def test_readonly_context_blocks_write_before_method_call():
    tools = _Tools()
    ctx = ec.build_context(42, "abcd", "web", policy_mode=POLICY_MODE_READONLY)
    executor = AgentExecutor("abcd", tools, execution_context=ctx)

    result = _run(executor.execute_tool("file_write", {"path": "workspace/a.md", "content": "x"}))

    assert result.startswith("Error:")
    assert "能力" in result or "拒绝" in result
    assert tools.calls == []  # 方法没被调用


def test_readonly_context_allows_read():
    tools = _Tools()
    ctx = ec.build_context(42, "abcd", "web", policy_mode=POLICY_MODE_READONLY)
    executor = AgentExecutor("abcd", tools, execution_context=ctx)

    result = _run(executor.execute_tool("file_read", {"path": "workspace/a.md"}))

    assert result == "content"
    assert tools.calls == [("file_read", "workspace/a.md")]


def test_full_context_allows_write():
    tools = _Tools()
    ctx = ec.build_context(42, "abcd", "web", policy_mode=POLICY_MODE_FULL)
    executor = AgentExecutor("abcd", tools, execution_context=ctx)

    result = _run(executor.execute_tool("file_write", {"path": "workspace/a.md", "content": "x"}))

    assert result == "ok"
    assert tools.calls == [("file_write", "workspace/a.md")]


def test_context_factory_failure_is_fail_closed():
    tools = _Tools()

    def _boom():
        raise RuntimeError("no identity")

    executor = AgentExecutor("abcd", tools, context_factory=_boom)

    result = _run(executor.execute_tool("file_write", {"path": "workspace/a.md", "content": "x"}))

    # 构造失败 = 无法证明身份 → fail closed；工具方法不得被调用
    assert result.startswith("Error:")
    assert tools.calls == []


def test_forged_identity_argument_is_rejected():
    tools = _Tools()
    ctx = ec.build_context(42, "abcd", "web")
    executor = AgentExecutor("abcd", tools, execution_context=ctx)

    result = _run(executor.execute_tool(
        "file_read", {"path": "workspace/a.md", "user_id": "99"},
    ))

    assert result.startswith("Error:")
    assert tools.calls == []


def test_unknown_tool_with_context_is_denied():
    tools = _Tools()
    ctx = ec.build_context(42, "abcd", "web")
    executor = AgentExecutor("abcd", tools, execution_context=ctx)

    result = _run(executor.execute_tool("definitely_not_a_tool", {"x": 1}))

    assert result.startswith("Error:")
    assert tools.calls == []


# ── enabled tools 配置：解析失败必须 fail closed ──────────────────────────────

class _Row:
    def __init__(self, value):
        self.value = value


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row

    def query(self, *a, **k):
        return _Query(self._row)

    def close(self):
        pass


def _patch_session(monkeypatch, row):
    """`_get_enabled_tools` 在函数内 `from models.database import SessionLocal`，
    所以要 patch models.database 上的名字（不是 agent_executor 模块）。"""
    import models.database as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _Session(row))


def test_enabled_tools_malformed_json_fails_closed(monkeypatch):
    _patch_session(monkeypatch, _Row("not-json"))
    executor = AgentExecutor("abcd", _Tools())

    assert executor._get_enabled_tools() == set()


def test_enabled_tools_wrong_type_fails_closed(monkeypatch):
    _patch_session(monkeypatch, _Row('{"enabled": "file_read"}'))
    executor = AgentExecutor("abcd", _Tools())

    assert executor._get_enabled_tools() == set()


def test_enabled_tools_explicit_empty_means_no_tools(monkeypatch):
    _patch_session(monkeypatch, _Row('{"enabled": []}'))
    executor = AgentExecutor("abcd", _Tools())

    assert executor._get_enabled_tools() == set()


def test_enabled_tools_missing_config_keeps_none(monkeypatch):
    _patch_session(monkeypatch, None)
    executor = AgentExecutor("abcd", _Tools())

    assert executor._get_enabled_tools() is None
