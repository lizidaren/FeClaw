"""T03a — 具体接口漂移的可回归证明（tests/test_interface_drift.py）

覆盖规格 §5 T03 验收：
  1. 同步工具副作用只发生**一次**（群聊执行路径不再先调一次、再在线程池调第二次）；
  2. 提醒工具标准名 create_cron_job / 旧名 schedule_reminder 是同一实现，
     禁掉任一个都不能靠另一个绕过；默认配置/工具目录只用标准名；
  3. `PermissionService(agent_hash=...)` 用关键字构造（位置参数会落到 user_id）；
  4. `_resolve_group_path` 只有一份实现（模块级），审批导入的是同一个对象；
  5. 重命名（alias）不改人格、不改初始化状态；初始化不再是重命名的通道。

运行条件：本文件需要 fastapi/sqlalchemy 等依赖（`.venv` 装齐后 `python -m pytest
tests/test_interface_drift.py -q`）。依赖缺失时每个用例 `skip` 并给出原因，不算通过。
没有 pytest 时可 `python tests/test_interface_drift.py` 走内置 runner。
"""

import asyncio
import importlib
import inspect
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:  # 依赖未装齐时也允许本模块被手工 runner 导入
    import pytest
except ImportError:  # pragma: no cover
    pytest = None


def _skip(reason):
    if pytest is not None:
        pytest.skip(reason)
    raise unittest.SkipTest(reason)


def _load(module_path):
    """导入服务模块；缺依赖时 skip（明确报告，不假装通过）。"""
    try:
        return importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001
        _skip(f"{module_path} 不可导入（依赖未装齐？）：{type(exc).__name__}: {exc}")


# ─────────────── 1. 同步工具副作用只发生一次 ───────────────

class _FakeToolsService:
    """最小的 AgentToolsService 替身：记录被调用次数。"""

    def __init__(self):
        self.calls = 0
        self.mode = "sync"
        # T04：群聊执行前会构造 ExecutionContext，需要服务端身份字段。
        self.user_id = "42"
        self.agent_hash = "abcd1234"

    async def _truncate_tool_result(self, result, tool_name="", tool_args=None, call_id=None):
        return result

    # 三个假工具：同步、异步、同步但返回 coroutine
    def t_sync_once(self):
        self.calls += 1
        return "ok-sync"

    async def t_async_once(self):
        self.calls += 1
        return "ok-async"

    def t_sync_coro(self):
        self.calls += 1
        return self._inner_coro()

    async def _inner_coro(self):
        return "ok-sync-coro"


def _register_fake_tools(registry_mod, names):
    """把假工具塞进 TOOL_REGISTRY 并声明执行元数据。

    T04 起 `authorize_tool` 对“未登记执行效果”的工具一律拒绝（fail closed），
    所以测试替身必须同时声明 effects/capabilities —— 否则测的就不再是
    “工具被调用几次”，而是“未分类工具被拒”。这里声明成无副作用的纯计算工具。
    """
    added = {}
    for name in names:
        entry = {
            "name": name,
            "func": None,
            "description": "test-only",
            "category": "test",
            "schema": {"type": "function", "function": {"name": name,
                                                        "description": "test",
                                                        "parameters": {"type": "object",
                                                                       "properties": {}}}},
            "param_names": [],
        }
        added[name] = registry_mod.TOOL_REGISTRY.get(name)
        registry_mod.TOOL_REGISTRY[name] = entry

        # T04：同时声明执行元数据（无副作用纯工具），否则授权层按 unknown 拒绝。
        previous_meta = registry_mod.TOOL_EFFECT_METADATA.get(name)
        added.setdefault("__meta__", {})[name] = previous_meta
        registry_mod.TOOL_EFFECT_METADATA[name] = registry_mod.ToolEffectMetadata(
            name=name,
            classification="declared",
            effects=frozenset(),
            required_capabilities=frozenset(),
            retry_safe=True,
            supports_idempotency_key=False,
            registered=True,
        )
    return added


def _restore_registry(registry_mod, added):
    meta_map = added.get("__meta__", {}) if isinstance(added, dict) else {}
    for name, previous in added.items():
        if name == "__meta__":
            continue
        if previous is None:
            registry_mod.TOOL_REGISTRY.pop(name, None)
        else:
            registry_mod.TOOL_REGISTRY[name] = previous
    for name, previous in meta_map.items():
        if previous is None:
            registry_mod.TOOL_EFFECT_METADATA.pop(name, None)
        else:
            registry_mod.TOOL_EFFECT_METADATA[name] = previous


def _run_group_tool(tool_name, timeout=5.0):
    gs = _load("services.group_service")
    registry_mod = _load("services.tool_registry")

    added = _register_fake_tools(registry_mod, [tool_name])
    gs.GROUP_ALLOWED_TOOLS.add(tool_name)
    try:
        fake = _FakeToolsService()
        dispatch = gs.GroupDispatchService()

        async def _get_tools(agent_hash, group_id):
            return fake

        dispatch._get_or_create_tools = _get_tools
        result = asyncio.run(
            dispatch._execute_group_tool("abcd1234", "group-1", tool_name, {}, timeout=timeout)
        )
        return result, fake
    finally:
        gs.GROUP_ALLOWED_TOOLS.discard(tool_name)
        _restore_registry(registry_mod, added)


def test_group_tool_sync_called_exactly_once():
    result, fake = _run_group_tool("t_sync_once")
    assert result == "ok-sync", result
    assert fake.calls == 1, f"同步工具有副作用却被调用 {fake.calls} 次"


def test_group_tool_async_called_exactly_once():
    result, fake = _run_group_tool("t_async_once")
    assert result == "ok-async", result
    assert fake.calls == 1, f"异步工具被调用 {fake.calls} 次"


def test_group_tool_sync_returning_coroutine_awaited_not_recalled():
    result, fake = _run_group_tool("t_sync_coro")
    assert result == "ok-sync-coro", result
    assert fake.calls == 1, f"同步包装器被调用 {fake.calls} 次（应只调用一次并 await 其 coroutine）"


def test_group_tool_not_allowed_is_rejected_without_call():
    gs = _load("services.group_service")
    fake = _FakeToolsService()
    dispatch = gs.GroupDispatchService()

    async def _get_tools(agent_hash, group_id):
        return fake

    dispatch._get_or_create_tools = _get_tools
    result = asyncio.run(
        dispatch._execute_group_tool("abcd1234", "group-1", "t_sync_once", {}, timeout=5.0)
    )
    assert result.startswith("Error:"), result
    assert fake.calls == 0


# ─────────────── 2. 提醒工具命名不可绕过 ───────────────

def test_reminder_alias_is_registry_mapping_not_second_tool():
    registry_mod = _load("services.tool_registry")
    _load("services.agent_tools_service")  # 触发全部 @tool 注册，否则 registry 是空的
    assert "create_cron_job" in registry_mod.TOOL_REGISTRY
    # 旧名不是第二个 @tool 注册
    assert "schedule_reminder" not in registry_mod.TOOL_REGISTRY
    canonical = registry_mod.TOOL_REGISTRY["create_cron_job"]
    assert registry_mod.get_tool("schedule_reminder") is canonical
    assert registry_mod.normalize_tool_name("schedule_reminder") == "create_cron_job"
    # 未知名字原样返回，不会被静默改写成别的工具
    assert registry_mod.normalize_tool_name("file_read") == "file_read"


def test_reminder_tool_allowed_normalizes_both_names():
    cron_mod = _load("services.tools.cron_tools")
    allowed = cron_mod.reminder_tool_allowed

    assert allowed(None) is True                      # 无配置 ⇒ 不过滤
    assert allowed(["create_cron_job"]) is True
    assert allowed(["schedule_reminder"]) is True      # 老配置启用旧名仍可用
    assert allowed([]) is False                        # 显式空 = 禁用
    assert allowed(["file_read"]) is False

    # 显式 disabled 优先，且两个名字不能互相绕过
    assert allowed(None, ["schedule_reminder"]) is False
    assert allowed(["create_cron_job"], ["create_cron_job"]) is False
    assert allowed(["schedule_reminder"], ["create_cron_job"]) is False
    assert allowed(["create_cron_job"], ["schedule_reminder"]) is False


def _make_cron_service(config):
    cron_mod = _load("services.tools.cron_tools")
    svc = object.__new__(cron_mod.CronToolsMixin)  # 绕过 __init__（会连 DB/VFS）
    svc.agent_hash = "abcd1234"
    svc._user_id = 1
    svc._agent_profile = None
    svc._tool_config = lambda: config
    return cron_mod, svc


def test_reminder_disabled_config_blocks_canonical_call():
    cron_mod, svc = _make_cron_service({"enabled": ["file_read"], "disabled": []})
    with mock.patch.object(cron_mod, "create_reminder") as create_reminder:
        result = cron_mod.CronToolsMixin.create_cron_job(svc, prompt="复习数学", schedule="07:30")
    assert result.startswith("Error:"), result
    create_reminder.assert_not_called()


def test_reminder_legacy_enabled_config_still_works():
    cron_mod, svc = _make_cron_service({"enabled": ["schedule_reminder"], "disabled": []})
    with mock.patch.object(cron_mod, "create_reminder",
                           return_value={"id": 7, "scheduled_at": "2026-10-26 07:30"}) as create_reminder:
        result = cron_mod.CronToolsMixin.create_cron_job(svc, prompt="复习数学", schedule="07:30")
    assert result.startswith("✅"), result
    create_reminder.assert_called_once()


def test_default_tool_config_uses_canonical_reminder_name():
    registry_mod = _load("services.tool_registry")
    init_mod = _load("services.agent_init_service")
    _load("services.agent_tools_service")  # 触发全部 @tool 注册

    defaults = init_mod.AgentInitService.DEFAULT_ENABLED_TOOLS
    assert "create_cron_job" in defaults
    assert "schedule_reminder" not in defaults, "默认配置必须用标准名"
    for name in defaults:
        assert name in registry_mod.TOOL_REGISTRY, f"默认工具 {name!r} 未注册"
    # 旧配置校验仍然接受旧名（否则老用户一保存就报 Invalid tool name）
    assert "schedule_reminder" in init_mod.AgentInitService.AVAILABLE_TOOLS


def test_console_tool_catalog_uses_canonical_name():
    console_mod = _load("routers.console")
    response = asyncio.run(console_mod.get_available_tools(user=object()))
    payload = json.loads(response.body)
    ids = [t["id"] for group in payload["tool_groups"] for t in group["tools"]]
    assert "create_cron_job" in ids
    assert "schedule_reminder" not in ids


def test_executor_normalizes_alias_before_blocked_check():
    executor_mod = _load("services.agent_executor")
    registry_mod = _load("services.tool_registry")

    blocked = list(executor_mod.SUBAGENT_PERMISSION_SETS[
        executor_mod.SubagentPermission.READONLY]["blocked"])
    ex = executor_mod.AgentExecutor(agent_hash="abcd1234", tools=object(), blocked_tools=blocked)
    assert "create_cron_job" in ex.blocked_tools
    # 只传旧名也一样被拦
    ex2 = executor_mod.AgentExecutor(agent_hash="abcd1234", tools=object(),
                                     blocked_tools=["schedule_reminder"])
    assert ex2.blocked_tools == {"create_cron_job"}
    # 归一化后 getattr 能命中原实现
    entry = registry_mod.get_tool("schedule_reminder")
    assert entry is registry_mod.TOOL_REGISTRY["create_cron_job"]
    assert entry["func"].__name__ == "create_cron_job"


# ─────────────── 3. PermissionService 关键字构造 ───────────────

def test_tools_base_constructs_permission_service_with_keyword():
    base_mod = _load("services.tools.base")
    recorded = {}

    class _Recorder:
        def __init__(self, *args, **kwargs):
            recorded["args"] = args
            recorded["kwargs"] = kwargs

    with mock.patch.object(base_mod, "PermissionService", _Recorder), \
         mock.patch.object(base_mod, "VirtualFileSystem", lambda **kw: object()):
        base_mod.AgentToolsServiceBase("abcd1234")

    assert recorded["args"] == (), "位置参数会被当成 user_id（老 bug）"
    assert recorded["kwargs"] == {"agent_hash": "abcd1234"}


def test_permission_service_constructor_keeps_user_first_compat():
    perm_mod = _load("services.permission_service")
    svc = perm_mod.PermissionService("user-1")
    assert svc.user_id == "user-1"
    assert svc.agent_hash == ""


# ─────────────── 4. 群路径解析单一实现 ───────────────

def test_group_path_resolver_single_implementation():
    file_ops = _load("services.tools.file_ops")
    permission_tools = _load("services.tools.permission_tools")

    resolver = file_ops.resolve_group_path
    assert resolver.__module__ == "services.tools.file_ops"
    # 审批模块导入的就是同一个对象，不是复制品
    assert permission_tools.resolve_group_path is resolver
    # Mixin 的旧入口只是转调（staticmethod 经类访问就是底层函数）
    assert file_ops.FileOpsMixin._resolve_group_path("/mnt/group/g1/.share/a.md") == \
        resolver("/mnt/group/g1/.share/a.md")
    assert "resolve_group_path" in inspect.getsource(
        file_ops.FileOpsMixin._resolve_group_path)

    key, err = resolver("/mnt/group/g1/.share/doc.md")
    assert err is None and key == "feclaw/groups/g1/.share/doc.md"
    assert resolver("/workspace/x.md")[0] is None
    assert resolver("/mnt/group/g1/../etc/passwd")[0] is None


# ─────────────── 5. 重命名不改人格 / 初始化 ───────────────

def test_resolve_init_persona_never_overwrites_existing():
    init_mod = _load("services.agent_init_service")
    resolve = init_mod.AgentInitService.resolve_init_persona

    assert resolve("用户自己的人格", "", None, "默认人格") == ("用户自己的人格", False)
    assert resolve("用户自己的人格", "新人格", "模板人格", "默认人格")[1] is False
    assert resolve(None, "", None, "默认人格") == ("默认人格", True)
    assert resolve(None, "", "模板人格", "默认人格") == ("模板人格", True)
    assert resolve("  ", "请求人格", None, "默认人格") == ("请求人格", True)


def test_rename_agent_only_touches_name():
    init_mod = _load("services.agent_init_service")
    svc = init_mod.AgentInitService()
    agent = mock.MagicMock()
    agent.name = "旧名字"
    agent.status = "initialized"
    agent.initialized_at = "2026-01-01T00:00:00"
    db = mock.MagicMock()

    svc.rename_agent(db, agent, "  新名字  ")

    assert agent.name == "新名字"
    assert agent.status == "initialized", "重命名不得改初始化状态"
    assert agent.initialized_at == "2026-01-01T00:00:00"
    assert db.commit.called
    # 只 add/commit/refresh 了 agent，没有写 settings/config 之类的第二套
    assert db.mock_calls[0][1][0] is agent


def test_rename_agent_rejects_invalid_alias():
    init_mod = _load("services.agent_init_service")
    svc = init_mod.AgentInitService()
    for bad in ("", "   ", None, "x" * 101, 123):
        try:
            svc.normalize_alias(bad)
        except ValueError:
            continue
        raise AssertionError(f"非法 alias 未被拒绝: {bad!r}")


def _console_route_body(payload):
    request = mock.MagicMock()
    request.headers = {"content-length": str(len(json.dumps(payload)))}

    async def _json():
        return payload

    request.json = _json
    return request


def test_console_settings_patch_renames_via_alias_and_not_initialize():
    console_mod = _load("routers.console")
    agent = mock.MagicMock()
    agent.id = 1
    agent.hash = "abcd1234"
    agent.name = "旧名字"
    agent.status = "initialized"
    agent.updated_at = None

    with mock.patch.object(console_mod, "_get_owned_agent", lambda *a, **k: agent), \
         mock.patch.object(console_mod.agent_init_service, "rename_agent") as rename, \
         mock.patch.object(console_mod.agent_init_service, "initialize_agent") as initialize:
        response = asyncio.run(console_mod.update_agent_settings_by_hash(
            "abcd1234", _console_route_body({"alias": "新名字"}),
            db=mock.MagicMock(), user=mock.MagicMock(),
        ))

    rename.assert_called_once()
    initialize.assert_not_called()
    payload = json.loads(response.body)
    assert payload["status"] == "ok"
    assert payload["updated"] == ["alias"]
    assert payload["initialization_state_unchanged"] is True


def test_console_settings_patch_rejects_initialization_fields():
    console_mod = _load("routers.console")
    agent = mock.MagicMock()
    fastapi_mod = _load("fastapi")

    with mock.patch.object(console_mod, "_get_owned_agent", lambda *a, **k: agent), \
         mock.patch.object(console_mod.agent_init_service, "rename_agent") as rename:
        try:
            asyncio.run(console_mod.update_agent_settings_by_hash(
                "abcd1234", _console_route_body({"persona": "新人格", "name": "新名字"}),
                db=mock.MagicMock(), user=mock.MagicMock(),
            ))
        except fastapi_mod.HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("应拒绝用初始化/人格字段冒充重命名")
    rename.assert_not_called()


MANUAL_TESTS = [
    test_reminder_alias_is_registry_mapping_not_second_tool,
    test_group_tool_sync_called_exactly_once,
    test_group_tool_async_called_exactly_once,
    test_group_tool_sync_returning_coroutine_awaited_not_recalled,
    test_group_tool_not_allowed_is_rejected_without_call,
    test_reminder_tool_allowed_normalizes_both_names,
    test_reminder_disabled_config_blocks_canonical_call,
    test_reminder_legacy_enabled_config_still_works,
    test_default_tool_config_uses_canonical_reminder_name,
    test_console_tool_catalog_uses_canonical_name,
    test_executor_normalizes_alias_before_blocked_check,
    test_tools_base_constructs_permission_service_with_keyword,
    test_permission_service_constructor_keeps_user_first_compat,
    test_group_path_resolver_single_implementation,
    test_resolve_init_persona_never_overwrites_existing,
    test_rename_agent_only_touches_name,
    test_rename_agent_rejects_invalid_alias,
    test_console_settings_patch_renames_via_alias_and_not_initialize,
    test_console_settings_patch_rejects_initialization_fields,
]


def _manual_run():
    """无 pytest 时的兜底执行器（依赖未装齐时会把需要依赖的用例标 SKIP）。"""
    failed = skipped = passed = 0
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
