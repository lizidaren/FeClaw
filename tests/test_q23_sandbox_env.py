"""
FIX-C 沙箱与环境回归测试（H5 网络隔离 fail-closed / H6/N11 bwrap 最小环境 /
C4 STS 条件键大小写 / N9 超时钳制 / N10 槽位竞态 / C1 死代码删除）。

每条断言都「能失败」：改坏对应修复即红。
"""
from unittest.mock import patch, MagicMock

import pytest


# ======================================================================
# H5 —— 网络隔离不可用 ⇒ 拒绝执行（fail-closed）
# ======================================================================

class TestNetnsFailClosed:
    def test_prefix_none_when_netns_missing(self):
        from services.network_isolation import NetworkIsolationManager
        with patch.object(NetworkIsolationManager, "check", return_value=False):
            assert NetworkIsolationManager.get_netns_prefix() is None

    def test_prefix_none_when_helper_missing(self):
        from services.network_isolation import NetworkIsolationManager
        with patch.object(NetworkIsolationManager, "check", return_value=True), \
             patch("services.network_isolation.os.path.exists", return_value=False):
            assert NetworkIsolationManager.get_netns_prefix() is None

    def test_prefix_present_when_both_available(self):
        from services import network_isolation
        with patch.object(network_isolation.NetworkIsolationManager, "check", return_value=True), \
             patch("services.network_isolation.os.path.exists", return_value=True), \
             patch("services.network_isolation.os.access", return_value=True):
            assert network_isolation.NetworkIsolationManager.get_netns_prefix() == [
                network_isolation.HELPER_PATH
            ]

    def test_execute_with_bwrap_refuses(self):
        from services.sandbox_manager import SandboxManager
        mgr = object.__new__(SandboxManager)
        with patch("services.sandbox_manager.NetworkIsolationManager.get_netns_prefix",
                   return_value=None):
            result = mgr._execute_with_bwrap("print(1)", 10, "sid")
        assert result.exit_code == 1
        assert "拒绝执行" in result.stderr

    def test_bash_path_refuses(self):
        from services.sandbox_manager import SandboxManager
        mgr = object.__new__(SandboxManager)
        with patch("services.sandbox_manager.NetworkIsolationManager.get_netns_prefix",
                   return_value=None), \
             patch("services.sandbox_manager._global_concurrency_limiter") as lim:
            lim.acquire.return_value = True
            result = mgr._exec_bash_via_sandbox("echo hi", 10)
        assert result.exit_code == 1
        assert "拒绝执行" in result.stderr

    def test_background_refuses(self):
        from services.sandbox_manager import SandboxManager
        mgr = object.__new__(SandboxManager)
        mgr._bwrap_available = True
        with patch("services.sandbox_manager._global_concurrency_limiter") as lim, \
             patch("services.sandbox_manager.NetworkIsolationManager.get_netns_prefix",
                   return_value=None):
            lim.acquire.return_value = True
            result = mgr.start_background("print(1)", name="t")
        assert isinstance(result, str)
        assert "拒绝执行" in result


# ======================================================================
# H6/N11 —— bwrap 自身（PID 1）只继承最小环境
# ======================================================================

class TestBwrapMinEnv:
    def test_min_env_contains_no_secrets(self):
        from services.sandbox_manager import _BWRAP_MIN_ENV
        assert set(_BWRAP_MIN_ENV.keys()) == {"PATH"}
        for secret in ("JWT_SECRET", "DATABASE_URL", "TENCENT_COS_SECRET_KEY"):
            assert secret not in _BWRAP_MIN_ENV

    def test_execute_with_bwrap_passes_min_env(self):
        from services.sandbox_manager import SandboxManager, _BWRAP_MIN_ENV
        mgr = object.__new__(SandboxManager)
        mgr._build_bwrap_command = MagicMock(return_value=["bwrap", "x"])
        with patch("services.sandbox_manager.NetworkIsolationManager.get_netns_prefix",
                   return_value=[]), \
             patch("services.sandbox_manager.subprocess.run") as run:
            run.return_value = type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
            mgr._execute_with_bwrap("print(1)", 10, "sid")
        assert run.call_args.kwargs["env"] == _BWRAP_MIN_ENV

    def test_bash_path_passes_min_env(self):
        from services.sandbox_manager import SandboxManager, _BWRAP_MIN_ENV
        mgr = object.__new__(SandboxManager)
        mgr._build_bwrap_bash_command = MagicMock(return_value=["bwrap", "x"])
        with patch("services.sandbox_manager.NetworkIsolationManager.get_netns_prefix",
                   return_value=[]), \
             patch("services.sandbox_manager._global_concurrency_limiter") as lim, \
             patch("services.sandbox_manager.subprocess.run") as run:
            lim.acquire.return_value = True
            run.return_value = type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
            mgr._exec_bash_via_sandbox("echo hi", 10)
        assert run.call_args.kwargs["env"] == _BWRAP_MIN_ENV


# ======================================================================
# C4 —— STS 条件键统一小写 cos:prefix
# ======================================================================

class TestStsConditionKeyCase:
    def test_custom_policy_unified_lowercase_prefix(self):
        from services.storage_service import CosStorage

        captured = {}

        class FakeSts:
            def __init__(self, config):
                captured["policy"] = config["policy"]

            def get_credential(self):
                return {
                    "credentials": {
                        "tmpSecretId": "a", "tmpSecretKey": "b", "sessionToken": "c",
                    },
                    "expiredTime": 1,
                    "expiration": "2026-01-01T00:00:00Z",
                }

        s = object.__new__(CosStorage)
        with patch("sts.sts.Sts", FakeSts):
            r = s.generate_sts_credential("7", prefix="feclaw/agents/5656/")

        assert r is not None
        policy = captured["policy"]
        for stmt in policy["statement"]:
            cond = stmt["condition"]["string_like"]
            # 条件键必须全部为小写 cos:prefix（大小写敏感，大写必然失效）
            assert list(cond.keys()) == ["cos:prefix"]
            assert "cos:Prefix" not in cond


# ======================================================================
# N9 —— agent 自撰 routes.json timeout 钳制
# ======================================================================

class TestTimeoutClamp:
    def test_clamp_timeout_bounds(self):
        from services.apps_service import _clamp_timeout, MAX_CODE_TIMEOUT
        assert _clamp_timeout(100000000) == MAX_CODE_TIMEOUT
        assert _clamp_timeout(0) == 1
        assert _clamp_timeout(-5) == 1
        assert _clamp_timeout("not-a-number") == MAX_CODE_TIMEOUT
        assert _clamp_timeout(None) == MAX_CODE_TIMEOUT
        assert _clamp_timeout(7) == 7


# ======================================================================
# N10 —— 后台任务槽位「恰好释放一次」（reaper vs stop_background 竞态）
# ======================================================================

class TestBackgroundSlotRelease:
    def test_release_background_slot_idempotent(self):
        from services.sandbox_manager import SandboxManager
        from services.sandbox.concurrency import BackgroundTask

        releases = []
        limiter = type("L", (), {"release": lambda self, tid: releases.append(tid)})()

        mgr = object.__new__(SandboxManager)
        task = BackgroundTask(id="t1", name="n", process=MagicMock())
        with patch("services.sandbox_manager._global_concurrency_limiter", limiter), \
             patch("services.sandbox_manager.unregister_sandbox_token"):
            mgr._release_background_slot("t1", task)
            mgr._release_background_slot("t1", task)
        assert releases == ["t1"]

    def test_stop_background_releases_once_when_reaper_wins(self):
        """确定性复现竞态：reaper 在 poll() 快照之后、stop 释放之前释放。"""
        from services.sandbox_manager import SandboxManager
        from services.sandbox.concurrency import BackgroundTask

        releases = []
        limiter = type("L", (), {"release": lambda self, tid: releases.append(tid)})()

        mgr = object.__new__(SandboxManager)
        mgr._background_tasks = {}

        class FakeProcess:
            def poll(self):
                return None  # stop_background 快照：仍在运行

            def terminate(self):
                # reaper 观察到进程退出并（通过同一幂等入口）释放了槽位
                mgr._release_background_slot("t1", task)

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        task = BackgroundTask(id="t1", name="n", process=FakeProcess())
        mgr._background_tasks["t1"] = task

        with patch("services.sandbox_manager._global_concurrency_limiter", limiter), \
             patch("services.sandbox_manager.unregister_sandbox_token"):
            assert mgr.stop_background("t1") is True
        assert releases == ["t1"]  # 绝不出现 ["t1", "t1"]


# ======================================================================
# C1 —— _execute_with_subprocess_safe 已删除（无隔离回退路径）
# ======================================================================

class TestDeadCodeRemoved:
    def test_execute_with_subprocess_safe_absent(self):
        from services.sandbox_manager import SandboxManager
        assert not hasattr(SandboxManager, "_execute_with_subprocess_safe")
