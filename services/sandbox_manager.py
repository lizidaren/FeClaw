"""
SandboxManager - 带 bubblewrap 支持的安全沙箱执行环境
如果 bwrap 未安装，自动回退到普通 subprocess

Phase 1: VFS monkey-patch 注入（不走 FUSE）
Phase 2: FUSE 挂载（未来）
"""

import os
import re
import uuid
import time
import shutil
import base64
import signal
import logging
import ctypes as _ctypes_lib
import threading
import subprocess
import resource
from typing import Dict, List, Set

from config import settings

from .sandbox.concurrency import (
    SandboxConfig,
    ExecResult,
    BackgroundTask,
    _global_concurrency_limiter,
    register_sandbox_token,
    unregister_sandbox_token,
    validate_sandbox_token,
)
from .sandbox.base import (
    _SECCOMP_BPF,
    SECCOMP_SETUP_CODE_SHORT,
    VFSBootstrap,
    _check_fuse_cached,
)
from .network_isolation import NetworkIsolationManager

logger = logging.getLogger(__name__)


# ============================================================================
# SandboxManager
# ============================================================================


class SandboxManager:
    """
    管理安全的沙箱 Python 执行

    流程:
    1. CodeFileAnalyzer.extract_file_refs(code) → 提取文件路径
    2. PrefetchEngine.prefetch_files(refs) → 并行预热缓存
    3. 构建注入代码（VFS monkey-patching bootstrap）
    4. 检查全局并发限制（最多 5 个）
    5. 如果 bwrap 已安装: 用 bwrap 执行（带 no-new-privs + 资源限制）
    6. 如果 bwrap 未安装: 回退到 subprocess.run（带 ulimit）
    7. 检查文件冲突锁
    8. 执行 + 返回结果
    """

    # 全局文件锁管理器（所有 SandboxManager 实例共享）
    _file_lock_manager = None

    def __init__(self, vfs, user_id: str):
        from services.cache_manager import MemoryCache, DiskCache, MetadataCache, WriteBuffer
        from services.file_locker import DistributedFileLock
        from services.code_analyzer import CodeFileAnalyzer, PrefetchEngine
        from services.rate_limiter import TokenBucket

        self.vfs = vfs
        self.user_id = str(user_id)
        self.agent_hash = getattr(vfs, 'agent_id', None)
        self.config = SandboxConfig()
        self._sandbox_dir = f"/tmp/sandbox/{user_id}"

        # 缓存层
        self.mem_cache = MemoryCache(max_size_mb=64)
        self.disk_cache = DiskCache(path=f"/tmp/vfs-cache/{user_id}/", max_size_mb=512)
        self.meta_cache = MetadataCache(ttl_seconds=60)

        # 速率限制器
        self.read_rate_limiter = TokenBucket(
            rate=settings.SANDBOX_READ_RATE_LIMIT,
            burst=settings.SANDBOX_READ_RATE_LIMIT * 5
        )
        self.write_rate_limiter = TokenBucket(
            rate=settings.SANDBOX_WRITE_RATE_LIMIT,
            burst=settings.SANDBOX_WRITE_RATE_LIMIT * 2
        )

        # 写入缓冲区
        self.write_buffer = WriteBuffer(self.vfs.storage, self.meta_cache)

        # 初始化全局文件锁管理器（懒加载，所有实例共享）
        if SandboxManager._file_lock_manager is None:
            SandboxManager._file_lock_manager = DistributedFileLock()

        # 代码分析器
        self.code_analyzer = CodeFileAnalyzer()

        # 预取引擎
        self.prefetcher = PrefetchEngine(
            self.mem_cache, self.disk_cache, self.meta_cache,
            self.vfs.storage, self.vfs
        )

        # 后台任务
        self._background_tasks: Dict[str, BackgroundTask] = {}

        # 检查 bwrap 是否可用
        self._bwrap_available = shutil.which("bwrap") is not None

        # seccomp BPF 文件（供 /seccomp-enforcer 在 bwrap 沙箱内读取）
        self._init_seccomp_bpf_file()

    # ========================================================================
    # 主执行接口
    # ========================================================================

    def exec_code(self, code: str, timeout: int = None,
                  parallel_sandbox: bool = False,
                  lock_behavior: str = "wait_3s") -> ExecResult:
        """
        安全执行 Python 代码

        Args:
            code: Python 代码
            timeout: 超时秒数
            parallel_sandbox: 是否允许多个并行 sandbox
            lock_behavior: 文件锁行为 "eagain" | "wait_3s"

        Returns:
            ExecResult 对象
        """
        sandbox_id = uuid.uuid4().hex[:12]
        if timeout is None:
            timeout = self.config.execution_timeout

        # 1. 分析代码提取文件路径
        refs = self.code_analyzer.extract_file_refs(code)

        # 2. 并行预热缓存（最多等 5 秒）
        if refs:
            try:
                self.prefetcher.prefetch_files(refs)
            except Exception as e:
                logger.debug(f"[Sandbox] Prefetch skipped: {e}")

        # 3. 检查全局并发限制
        if not _global_concurrency_limiter.acquire(sandbox_id):
            return ExecResult(
                stdout="",
                stderr=f"Error: 沙箱并发已达上限 ({_global_concurrency_limiter.max_concurrent})，请稍后重试",
                exit_code=1,
                sandbox_id=sandbox_id
            )

        sandbox_token = None
        try:
            # 4. 确保 sandbox 专属 FUSE 已启动（懒加载，FUSE_ENABLED 时）
            self._ensure_sandbox_fuse()

            # 标记 FUSE 活跃（防止被 cleanup 回收）
            if settings.FUSE_ENABLED and _check_fuse_cached() and hasattr(self, '_fuse_mount_dir') and self._fuse_mount_dir:
                from services.vfs_fuse_daemon import touch_sandbox_fuse
                touch_sandbox_fuse(self._fuse_mount_dir)

            # 5. 生成 sandbox token 并注册
            sandbox_token = register_sandbox_token(self.agent_hash or "")

            # 6. 构建 VFS bootstrap 注入代码（FUSE 已挂载且可用时跳过）
            #    注意：seccomp BPF 白名单在 Python bootstrap 内安装
            #    （bwrap 的 NO_NEW_PRIVS 与 unshare(CLONE_NEWUSER) 不兼容，
            #     seccomp 必须在 bwrap 完成 user ns 创建后安装）
            if self._is_fuse_ready():
                # FUSE 模式：沙箱直接通过 bind mount 看到 /workspace，无需 monkey-patch
                full_code = SECCOMP_SETUP_CODE_SHORT + "\n\n# === User Code ===\n" + code
            else:
                # 回退模式：注入 VFSBootstrap monkey-patch
                bootstrap = VFSBootstrap.build(
                    user_id=self.user_id,
                    agent_hash=self.agent_hash,
                    max_file_size=settings.SANDBOX_MAX_FILE_SIZE,
                    sandbox_token=sandbox_token
                )
                full_code = bootstrap + "\n\n" + SECCOMP_SETUP_CODE_SHORT + "\n\n# === User Code ===\n" + code

            # 6. 检查文件冲突锁
            locked_violations = self._check_file_locks(refs, sandbox_id, lock_behavior)
            if locked_violations:
                return ExecResult(
                    stdout="",
                    stderr=f"Error: 文件被锁定: {', '.join(locked_violations)}",
                    exit_code=1,
                    sandbox_id=sandbox_id
                )

            # 7. 执行
            result = self._execute_with_sandbox(full_code, timeout, sandbox_id)
            return result

        finally:
            # 释放文件锁
            if refs:
                self._release_file_locks(refs, sandbox_id)
            _global_concurrency_limiter.release(sandbox_id)
            if sandbox_token:
                unregister_sandbox_token(sandbox_token)

    def exec_command(self, command: str, timeout: int = None,
                     parallel_sandbox: bool = False,
                     lock_behavior: str = "wait_3s") -> ExecResult:
        """
        支持 python3 -c, python3 script.py, /bin/bash -c 格式

        Args:
            command: 命令字符串
            timeout: 超时秒数

        Returns:
            ExecResult 对象
        """
        command = command.strip()

        if command.startswith("python3 -c ") or command.startswith("python -c "):
            parts = command.split(" -c ", 1)
            if len(parts) == 2:
                code = parts[1].strip().strip("\"'")
                return self.exec_code(code, timeout, parallel_sandbox, lock_behavior)
            return ExecResult(
                stdout="",
                stderr="Error: Invalid command format",
                exit_code=1
            )

        elif command.startswith("python3 ") or command.startswith("python "):
            parts = command.split(None, 1)
            if len(parts) < 2:
                return ExecResult(stdout="", stderr="Error: No script specified", exit_code=1)

            script_arg = parts[1].strip()

            if script_arg.startswith("-m "):
                module_name = script_arg[3:].strip()
                code = f"import {module_name}"
                return self.exec_code(code, timeout, parallel_sandbox, lock_behavior)

            # 从 VFS 获取脚本内容
            script_path = script_arg.split()[0]
            try:
                cos_key, err = self.vfs._resolve_path(script_path)
                if err:
                    return ExecResult(stdout="", stderr=err, exit_code=1)
                content = self.vfs.storage.get_file_content(cos_key)
                if content is None:
                    return ExecResult(stdout="", stderr=f"Error: Script not found: {script_path}", exit_code=1)
                code = content.decode("utf-8", errors="replace")
                return self.exec_code(code, timeout, parallel_sandbox, lock_behavior)
            except Exception as e:
                return ExecResult(stdout="", stderr=f"Error reading script: {e}", exit_code=1)

        elif command.startswith("/bin/bash -c ") or command.startswith("bash -c "):
            # FUSE 模式：通过 bwrap 执行真实 bash 命令
            self._ensure_sandbox_fuse()
            import shlex
            bash_cmd = command.split(" -c ", 1)[1] if " -c " in command else ""
            # 用 shlex 正确解析引号包裹的命令
            try:
                tokens = shlex.split(command)
                # tokens = ['/bin/bash', '-c', 'actual command']
                if len(tokens) >= 3:
                    bash_cmd = tokens[2]
            except ValueError:
                # shlex 失败时的回退：strip 引号
                if bash_cmd.startswith("'") and bash_cmd.endswith("'"):
                    bash_cmd = bash_cmd[1:-1]
                elif bash_cmd.startswith('"') and bash_cmd.endswith('"'):
                    bash_cmd = bash_cmd[1:-1]
            return self._exec_bash_via_sandbox(bash_cmd, timeout)

        else:
            return ExecResult(
                stdout="",
                stderr="Error: Unsupported command format. Use python3 -c 'code', python3 script.py, or /bin/bash -c 'command'",
                exit_code=1
            )

    def _exec_bash_via_sandbox(self, bash_command: str, timeout: int = None) -> ExecResult:
        """在 bwrap 沙箱中执行真实 bash 命令（FUSE 模式）"""
        sandbox_id = uuid.uuid4().hex[:12]
        if timeout is None:
            timeout = 30

        if not _global_concurrency_limiter.acquire(sandbox_id):
            return ExecResult(
                stdout="", stderr="Error: sandbox concurrency limit reached",
                exit_code=1, sandbox_id=sandbox_id
            )

        try:
            # 标记 FUSE 活跃（防止被 cleanup 回收）
            if settings.FUSE_ENABLED and _check_fuse_cached() and hasattr(self, '_fuse_mount_dir') and self._fuse_mount_dir:
                from services.vfs_fuse_daemon import touch_sandbox_fuse
                touch_sandbox_fuse(self._fuse_mount_dir)

            script_path = f"/tmp/sandbox_bash_{sandbox_id}.sh"
            os.makedirs(os.path.dirname(script_path), exist_ok=True)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write("#!/bin/bash\nset -e\n" + bash_command)
            os.chmod(script_path, 0o755)

            bwrap_cmd = self._build_bwrap_bash_command(script_path, timeout)

            result = subprocess.run(
                bwrap_cmd, capture_output=True, text=True, timeout=timeout,
            )

            return self._make_result(
                stdout=result.stdout, stderr=result.stderr,
                returncode=result.returncode, sandbox_id=sandbox_id
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                stdout="", stderr="执行超时", exit_code=124,
                timed_out=True, sandbox_id=sandbox_id
            )
        except Exception as e:
            return ExecResult(
                stdout="", stderr=f"Error: {e}", exit_code=1,
                sandbox_id=sandbox_id
            )
        finally:
            _global_concurrency_limiter.release(sandbox_id)

    def _get_fuse_bind_source(self) -> str:
        """获取 FUSE bind mount 源路径

        优先使用 sandbox 专属 FUSE（其根直接包含 workspace/config/public）。
        回退到全局 FUSE（带完整路径）。
        """
        # 优先使用 sandbox 专属 FUSE
        if hasattr(self, '_fuse_mount_dir') and self._fuse_mount_dir:
            return self._fuse_mount_dir  # sandbox FUSE 根直接作为 /workspace
        # 回退：使用全局 FUSE（带了完整路径）
        if self.agent_hash:
            if not re.match(r'^[a-f0-9\-]+$', self.agent_hash):
                logger.error(f"Invalid agent_hash: {self.agent_hash}")
                return settings.FUSE_MOUNT_DIR  # fallback to safe path
            return f"{settings.FUSE_MOUNT_DIR}/agents/{self.agent_hash}/workspace"
        else:
            safe_uid = re.sub(r'[^a-zA-Z0-9\-_]', '_', str(self.user_id))
            return f"{settings.FUSE_MOUNT_DIR}/user_workspaces/{safe_uid}/workspace"

    def _is_fuse_ready(self) -> bool:
        """检查 FUSE 是否实际已挂载且可用（检查关键子目录是否存在）"""
        if not settings.FUSE_ENABLED:
            return False
        if not _check_fuse_cached():
            return False
        try:
            mount_dir = self._get_fuse_bind_source()
            if os.path.isdir(mount_dir):
                items = os.listdir(mount_dir)
                if any(d in items for d in ("agents", "config", "public", "user_workspaces")):
                    return True
        except Exception:
            pass
        return False

    def _ensure_sandbox_fuse(self):
        """为当前 sandbox 创建专属 FUSE 实例（懒加载）"""
        if not (settings.FUSE_ENABLED and _check_fuse_cached()):
            return
        if hasattr(self, '_fuse_mount_dir') and self._fuse_mount_dir:
            return
        sandbox_id = uuid.uuid4().hex[:8]
        self._fuse_mount_dir = f"/tmp/feclaw-sandbox-{sandbox_id}"
        os.makedirs(self._fuse_mount_dir, exist_ok=True)

        from services.vfs_fuse_daemon import start_fuse_background as _start_fuse
        self._fuse_thread = _start_fuse(
            self.vfs, self._fuse_mount_dir, 60,
            cos_prefix="feclaw/", agent_hash=self.agent_hash
        )
        for _ in range(20):
            if os.path.isdir(self._fuse_mount_dir):
                break
            time.sleep(0.05)
        else:
            logger.warning(f"FUSE mount directory {self._fuse_mount_dir} not ready after 1s")

        # 注册到全局注册表
        from services.vfs_fuse_daemon import register_sandbox_fuse
        register_sandbox_fuse(self._fuse_mount_dir, agent_hash=self.agent_hash)

        logger.info(f"Sandbox FUSE mounted at {self._fuse_mount_dir} "
                    f"(agent_hash={self.agent_hash})")

    def _cleanup_sandbox_fuse(self):
        """清理 sandbox 专属 FUSE"""
        if hasattr(self, '_fuse_mount_dir') and self._fuse_mount_dir:
            from services.vfs_fuse_daemon import unmount_fuse
            unmount_fuse(self._fuse_mount_dir)
            try:
                os.rmdir(self._fuse_mount_dir)
            except OSError:
                pass
            logger.info(f"Sandbox FUSE cleaned up: {self._fuse_mount_dir}")
            self._fuse_mount_dir = None

    def cleanup(self):
        """清理 sandbox 资源（FUSE unmount 等）"""
        self._cleanup_sandbox_fuse()

    def __del__(self):
        """安全网：确保 FUSE 被清理"""
        try:
            self._cleanup_sandbox_fuse()
        except Exception:
            pass

    def _build_bwrap_bash_command(self, script_path: str, timeout: int) -> List[str]:
        """构建用于执行 bash 脚本的 bubblewrap 命令（含 netns + seccomp enforcer）"""
        opts = self._build_bwrap_base_opts(script_path)

        # 入口：/seccomp-enforcer → 装 seccomp 白名单 → exec bash
        entry = ["/seccomp-enforcer", "/bin/bash", script_path]

        if NetworkIsolationManager.check():
            return NetworkIsolationManager.get_netns_prefix() + opts + entry
        else:
            return opts + entry

    # ========================================================================
    # 友好错误转译
    # ========================================================================

    def _make_result(self, stdout: str, stderr: str, returncode: int,
                     sandbox_id: str, timed_out: bool = False) -> ExecResult:
        """构建 ExecResult，含信号转译为友好中文提示"""
        if returncode == 0:
            return ExecResult(
                stdout=stdout, stderr=stderr, exit_code=0,
                timed_out=timed_out, sandbox_id=sandbox_id
            )

        # 推断信号
        if returncode < 0:
            sig = -returncode
        elif returncode > 128:
            sig = returncode - 128
        else:
            # 正常非零退出
            return ExecResult(
                stdout=stdout, stderr=stderr, exit_code=returncode,
                timed_out=timed_out, sandbox_id=sandbox_id
            )

        if sig == signal.SIGSYS:  # 31 — seccomp 拦截
            friendly = (
                "Security sandbox blocked: code attempted a forbidden operation.\n"
                "Common causes:\n"
                "  - Network access (HTTP requests, socket connections)\n"
                "  - System command execution (os.system(), subprocess.run())\n"
                "  - Privilege escalation or kernel resource access\n"
                "Please remove the offending code and try again."
            )
            return ExecResult(
                stdout=stdout, stderr=friendly, exit_code=returncode,
                timed_out=False, sandbox_id=sandbox_id
            )

        if sig == signal.SIGKILL:  # 9 — OOM/超时强制杀死
            return ExecResult(
                stdout=stdout,
                stderr="Execution killed (memory limit exceeded or timeout)",
                exit_code=returncode, timed_out=True, sandbox_id=sandbox_id
            )

        if sig == signal.SIGXCPU:  # 24 — CPU 时间超限
            return ExecResult(
                stdout=stdout,
                stderr="Execution killed (CPU time limit exceeded)",
                exit_code=returncode, timed_out=True, sandbox_id=sandbox_id
            )

        if sig == signal.SIGSEGV:  # 11 — 段错误
            return ExecResult(
                stdout=stdout,
                stderr="Execution crashed (segmentation fault)\n"
                       "This may be caused by invalid memory access in native extensions.",
                exit_code=returncode, timed_out=False, sandbox_id=sandbox_id
            )

        # 其他信号
        return ExecResult(
            stdout=stdout,
            stderr=f"Execution terminated by signal {sig}",
            exit_code=returncode, timed_out=False, sandbox_id=sandbox_id
        )

    # ========================================================================
    # 执行引擎
    # ========================================================================

    def _execute_with_sandbox(self, code: str, timeout: int,
                              sandbox_id: str) -> ExecResult:
        """选择 bwrap 或 subprocess 执行（bwrap 失败自动回退）"""

        if self._bwrap_available:
            result = self._execute_with_bwrap(code, timeout, sandbox_id)
            if result.exit_code == 0:
                return result
            if "bwrap" in (result.stderr or "").lower():
                logger.warning(f"[Sandbox] bwrap failed, fallback: {result.stderr[:200]}")
        return self._execute_with_subprocess_safe(code, timeout, sandbox_id)

    def _execute_with_bwrap(self, code: str, timeout: int,
                            sandbox_id: str) -> ExecResult:
        """通过 bubblewrap 安全执行"""
        if not NetworkIsolationManager.check():
            logger.warning("Netns not available, falling back to --share-net")
        try:
            # 写入临时脚本
            script_path = f"/tmp/sandbox_exec_{sandbox_id}.py"
            os.makedirs(os.path.dirname(script_path), exist_ok=True)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(code)

            # 构建 bwrap 命令
            bwrap_cmd = self._build_bwrap_command(script_path, timeout)

            # 执行（helper 负责 netns + rlimits，seccomp 由 Python bootstrap 安装）
            result = subprocess.run(
                bwrap_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            return self._make_result(
                stdout=result.stdout, stderr=result.stderr,
                returncode=result.returncode, sandbox_id=sandbox_id
            )

        except subprocess.TimeoutExpired:
            return ExecResult(
                stdout="", stderr="执行超时",
                exit_code=124, timed_out=True, sandbox_id=sandbox_id
            )
        except FileNotFoundError:
            return ExecResult(
                stdout="", stderr="Error: bwrap 执行失败",
                exit_code=1, sandbox_id=sandbox_id
            )

    def _execute_with_subprocess_safe(self, code: str, timeout: int,
                                      sandbox_id: str) -> ExecResult:
        """通过 subprocess.run 执行（带回退资源限制 + chdir + env 清理）"""
        try:
            script_path = f"/tmp/sandbox_exec_{sandbox_id}.py"
            os.makedirs(os.path.dirname(script_path), exist_ok=True)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(code)

            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            # 清理危险环境变量
            env.pop("LD_PRELOAD", None)
            env.pop("LD_LIBRARY_PATH", None)

            result = subprocess.run(
                ["python3", script_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd="/tmp",
                preexec_fn=self._make_preexec_fn()
            )

            return self._make_result(
                stdout=result.stdout, stderr=result.stderr,
                returncode=result.returncode, sandbox_id=sandbox_id
            )

        except subprocess.TimeoutExpired:
            return ExecResult(
                stdout="", stderr=f"执行超时 ({timeout}s)",
                exit_code=124, timed_out=True, sandbox_id=sandbox_id
            )
        except Exception as e:
            return ExecResult(
                stdout="", stderr=f"Error: {e}",
                exit_code=1, sandbox_id=sandbox_id
            )

    # seccomp BPF 文件（供 bwrap 内的 /seccomp-enforcer 读取）
    BWRAP_BPF_PATH = "/tmp/seccomp_bpf.bin"
    SECCOMP_ENFORCER_PATH = "/usr/local/libexec/feclaw/seccomp-enforcer"

    def _init_seccomp_bpf_file(self):
        """确保 seccomp BPF 文件存在"""
        if not os.path.exists(self.BWRAP_BPF_PATH):
            os.makedirs(os.path.dirname(self.BWRAP_BPF_PATH), exist_ok=True)
            with open(self.BWRAP_BPF_PATH, "wb") as f:
                f.write(_SECCOMP_BPF)

    # ========================================================================
    # bubblewrap 命令构建
    # ========================================================================

    def _build_bwrap_base_opts(self, script_path: str) -> List[str]:
        """构建 bwrap 公共挂载选项（两个沙箱执行路径共享）"""
        host_path = os.environ.get("PATH", "/usr/bin:/bin")
        python_bin = os.path.realpath(shutil.which("python3") or "/usr/local/bin/python3.12")
        real_python_dir = os.path.dirname(python_bin)

        # Python stdlib 路径: /usr/local/bin/python3.12 → /usr/local/lib/python3.12/
        _py_ver = os.path.basename(python_bin).replace("python", "", 1)
        python_lib = os.path.join(os.path.dirname(real_python_dir), "lib", f"python{_py_ver}")

        # 收集需显式挂载的 Python 路径，覆盖符号链 + 二进制目录 + stdlib
        _py_mounts = set()
        # 符号链解析路径的每个分量
        _py_mounts.add(real_python_dir)       # /usr/local/bin
        _py_mounts.add(python_lib)            # /usr/local/lib/python3.12
        _py_mounts.add(os.path.dirname(python_lib))  # /usr/local/lib（若 /usr/local 是独立挂载点）

        opts = [
            "bwrap", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--unshare-cgroup", "--die-with-parent",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            # 系统二进制（标准路径，非递归 bind-mount 覆盖不到子挂载点如 /usr/local）
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind-try", "/lib64", "/lib64",
            # Python 二进制和 stdlib 的显式绑定（解决 /usr/local 是独立挂载点的问题）
            *[mnt for p in _py_mounts if os.path.exists(p) for mnt in ("--ro-bind-try", p, p)],
            # 符号链解析
            "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
            # 动态链接器
            "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
            # HTTPS 证书
            "--ro-bind-try", "/etc/ssl", "/etc/ssl",
            # 时区信息
            "--ro-bind-try", "/etc/localtime", "/etc/localtime",
            "--ro-bind-try", "/usr/share/zoneinfo", "/usr/share/zoneinfo",
        ]

        # /etc/resolv.conf 和 /etc/hosts — 若存在则绑定
        for etc_path in ["/etc/resolv.conf", "/etc/hosts"]:
            if os.path.exists(etc_path):
                opts += ["--ro-bind", etc_path, etc_path]

        # 条件路径绑定：自动发现当前 Python 的 site-packages 路径
        _site_pkgs = next(
            (p for p in __import__("sys").path if "site-packages" in p),
            os.path.join(python_lib, "site-packages"),
        )
        for extra_path in [
            _site_pkgs,
            os.path.dirname(_site_pkgs),
            real_python_dir,
        ]:
            if extra_path and os.path.exists(extra_path):
                opts += ["--ro-bind-try", extra_path, extra_path]

        # 检查全局 FUSE 是否可用（独立于 per-agent workspace）
        _global_fuse_ok = settings.FUSE_ENABLED and _check_fuse_cached()

        if self._is_fuse_ready():
            self._ensure_sandbox_fuse()
            opts += ["--bind", self._get_fuse_bind_source(), "/workspace"]
            opts += ["--setenv", "FECLAW_USE_FUSE", "1"]

        # /public 只读挂载（独立于 workspace，只要全局 FUSE 存活即可）
        if _global_fuse_ok:
            public_src = f"{settings.FUSE_MOUNT_DIR}/public"
            if os.path.isdir(public_src):
                opts += ["--ro-bind", public_src, "/public"]

        # seccomp 强制执行器 + BPF 白名单（绑进沙箱，/seccomp-enforcer 读取）
        enforcer_path = self.SECCOMP_ENFORCER_PATH
        if os.path.exists(enforcer_path):
            opts += [
                "--ro-bind", enforcer_path, "/seccomp-enforcer",
                "--ro-bind", self.BWRAP_BPF_PATH, "/seccomp.bpf",
            ]

        # 确保 python3 指向 python3.12（系统默认 python3 通常是 3.8）
        p12 = shutil.which("python3.12") or "/usr/local/bin/python3.12"
        p3 = os.path.join(os.path.dirname(p12), "python3")
        if os.path.exists(p12) and not os.path.exists(p3):
            opts += ["--ro-bind", p12, p3]

        # 环境变量设置
        opts += [
            "--setenv", "PATH", host_path,
            "--setenv", "HOME", "/tmp",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--setenv", "PYTHONPATH", _site_pkgs,
            "--bind", script_path, script_path,  # 覆写 --tmpfs /tmp
        ]

        return opts

    def _build_bwrap_command(self, script_path: str, timeout: int) -> List[str]:
        """构建 bubblewrap 隔离命令（含 netns 网络隔离 + seccomp enforcer）"""
        python_bin = os.path.realpath(shutil.which("python3") or "/usr/local/bin/python3.12")
        opts = self._build_bwrap_base_opts(script_path)

        # 入口：/seccomp-enforcer → 装 seccomp 白名单 → exec python
        entry = ["/seccomp-enforcer", python_bin, script_path]

        if NetworkIsolationManager.check():
            return NetworkIsolationManager.get_netns_prefix() + opts + entry
        else:
            return opts + entry

    def _make_preexec_fn(self):
        """创建 preexec_fn：资源限制 + no_new_privs + cap_eff_drop + seccomp"""
        config = self.config
        bpf_prog = _SECCOMP_BPF

        def preexec():
            # 1. 资源限制
            memory_bytes = config.memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (config.execution_timeout, config.execution_timeout)
            )
            resource.setrlimit(
                resource.RLIMIT_NPROC,
                (config.max_processes, config.max_processes)
            )
            resource.setrlimit(
                resource.RLIMIT_NOFILE,
                (config.max_open_files, config.max_open_files)
            )

            # 2. no_new_privs + cap_eff + seccomp（仅 x86_64）
            if bpf_prog is None:
                return

            libc = _ctypes_lib.CDLL("libc.so.6", use_errno=True)

            # PR_SET_NO_NEW_PRIVS — 禁止 execve 提权
            PR_SET_NO_NEW_PRIVS = 38
            libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)

            # PR_CAPBSET_DROP — 清空 capability bounding set
            PR_CAPBSET_DROP = 24
            for cap in range(64):
                try:
                    libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)
                except Exception:
                    pass

            # PR_SET_SECCOMP — 应用 BPF seccomp 过滤器
            PR_SET_SECCOMP = 22
            SECCOMP_MODE_FILTER = 2

            ninsns = len(bpf_prog) // 8
            FilterArray = _ctypes_lib.c_ubyte * len(bpf_prog)
            filter_arr = FilterArray.from_buffer_copy(bpf_prog)

            class SockFprog(_ctypes_lib.Structure):
                _fields_ = [
                    ("len", _ctypes_lib.c_ushort),
                    ("filter_ptr", _ctypes_lib.c_void_p),
                ]

            prog = SockFprog()
            prog.len = ninsns
            prog.filter_ptr = _ctypes_lib.cast(filter_arr, _ctypes_lib.c_void_p)

            libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, _ctypes_lib.byref(prog))

        return preexec

    # ========================================================================
    # 后台任务
    # ========================================================================

    def start_background(self, code: str, name: str = None,
                         port: int = None) -> str:
        """后台任务（长期运行，不受 12h 限制但受 max_concurrent 限制）"""
        task_id = uuid.uuid4().hex[:12]
        if not name:
            name = f"python_task_{task_id}"

        # 检查并发限制
        if not _global_concurrency_limiter.acquire(task_id):
            return f"Error: 沙箱并发已满"

        sandbox_token = register_sandbox_token(self.agent_hash or "")
        bootstrap = VFSBootstrap.build(
            user_id=self.user_id,
            agent_hash=self.agent_hash,
            max_file_size=settings.SANDBOX_MAX_FILE_SIZE,
            sandbox_token=sandbox_token
        )
        full_code = bootstrap + "\n\n# === Background Task ===\n" + code

        script_path = f"/tmp/sandbox_bg_{task_id}.py"
        os.makedirs(os.path.dirname(script_path), exist_ok=True)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(full_code)

        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        process = subprocess.Popen(
            ["python3", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            preexec_fn=self._make_preexec_fn(),
        )

        task = BackgroundTask(id=task_id, name=name, process=process, port=port, sandbox_token=sandbox_token)

        # 启动输出读取线程
        def _read_output(stream, buffer, prefix=""):
            try:
                for line in stream:
                    buffer.append(f"{prefix}{line.rstrip()}")
            except Exception:
                pass

        threading.Thread(
            target=_read_output, args=(process.stdout, task.output_buffer),
            daemon=True
        ).start()
        threading.Thread(
            target=_read_output, args=(process.stderr, task.output_buffer, "[stderr] "),
            daemon=True
        ).start()

        self._background_tasks[task_id] = task
        logger.info(f"[Sandbox] Started background task {task_id}: {name}")
        return task_id

    def stop_background(self, task_id: str) -> bool:
        """停止后台任务"""
        task = self._background_tasks.get(task_id)
        if not task:
            return False

        try:
            task.process.terminate()
            try:
                task.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                task.process.kill()
                task.process.wait()

            del self._background_tasks[task_id]
            _global_concurrency_limiter.release(task_id)
            if task.sandbox_token:
                unregister_sandbox_token(task.sandbox_token)
            logger.info(f"[Sandbox] Stopped background task {task_id}")
            return True
        except Exception as e:
            logger.error(f"[Sandbox] Failed to stop task {task_id}: {e}")
            return False

    def list_background_tasks(self) -> List[dict]:
        """列出所有后台任务"""
        result = []
        for t in self._background_tasks.values():
            poll_result = t.process.poll()
            result.append({
                "task_id": t.id,
                "name": t.name,
                "running": poll_result is None,
                "exit_code": poll_result if poll_result is not None else None,
                "port": t.port,
            })
        return result

    def get_task_output(self, task_id: str, lines: int = 50) -> str:
        """获取后台任务输出"""
        task = self._background_tasks.get(task_id)
        if not task:
            return f"Error: Task {task_id} not found"
        buffer_lines = list(task.output_buffer)
        return "\n".join(buffer_lines[-lines:])

    # ========================================================================
    # 文件锁检查
    # ========================================================================

    def _check_file_locks(self, refs: Set[str], owner: str,
                          lock_behavior: str) -> List[str]:
        """检查文件冲突锁，返回被锁定的文件列表"""
        locked = []
        acquired = []  # 追踪已成功获取的锁，用于失败时回滚
        for ref in refs:
            success = self._file_lock_manager.acquire_write(
                self.user_id, ref, owner, timeout=30.0
            )
            if not success and lock_behavior == "wait_3s":
                # 等待最多 3 秒
                for _ in range(30):
                    time.sleep(0.1)
                    if self._file_lock_manager.acquire_write(
                        self.user_id, ref, owner, timeout=30.0
                    ):
                        success = True
                        break

            if success:
                acquired.append(ref)
            else:
                locked.append(ref)
                # 回滚所有已获取的锁
                for acq in acquired:
                    self._file_lock_manager.release(self.user_id, acq, owner)
                acquired.clear()  # 避免 _release_file_locks 重复释放
                return locked  # 提前返回，不继续拿了
        
        return []  # 全部成功，返回空列表

    def _release_file_locks(self, locked_refs: Set[str], owner: str):
        """释放之前获取的所有文件锁"""
        for ref in locked_refs:
            try:
                self._file_lock_manager.release(self.user_id, ref, owner)
            except Exception:
                pass

    # ========================================================================
    # VFS 内部 API 处理方法（供子进程 httpx 调用）
    # ========================================================================

    def handle_vfs_api(self, endpoint: str, params: dict = None,
                       data: dict = None) -> dict:
        """处理子进程通过 HTTP 发来的 VFS 操作请求"""
        if params is None:
            params = {}
        if data is None:
            data = {}

        # 解析路径
        path = params.get("path") or data.get("path", "")

        try:
            if endpoint == "/api/sandbox/vfs/file":
                return self._vfs_file_handler(path, params, data)
            elif endpoint == "/api/sandbox/vfs/listdir":
                return self._vfs_listdir_handler(path)
            elif endpoint == "/api/sandbox/vfs/stat":
                return self._vfs_stat_handler(path)
            elif endpoint == "/api/sandbox/vfs/mkdir":
                return self._vfs_mkdir_handler(path, data)
            elif endpoint == "/api/sandbox/vfs/dir":
                return self._vfs_dir_handler(path, params)
            elif endpoint == "/api/sandbox/vfs/rename":
                return self._vfs_rename_handler(data.get("src", ""), data.get("dst", ""))
            else:
                return {"error": f"Unknown endpoint: {endpoint}"}
        except Exception as e:
            return {"error": str(e)}

    def _vfs_file_handler(self, path: str, params: dict, data: dict) -> dict:
        """处理文件读写请求（内部 API）"""
        mode = data.get("mode", "read")
        cos_key, err = self.vfs._resolve_path(path)
        if err:
            return {"error": err}

        if mode == "upload" and "content" in data:
            content = base64.b64decode(data["content"])
            self.vfs.storage.put_object(cos_key, content)
            self.meta_cache.invalidate_dir(
                cos_key.rsplit("/", 1)[0] if "/" in cos_key else ""
            )
            return {"ok": True}

        # 读取
        # 检查缓存
        cache_key = cos_key
        cached = self.mem_cache.get(cache_key)
        if cached is not None:
            return {"exists": True, "content": base64.b64encode(cached).decode(), "cached": True}

        content = self.vfs.storage.get_file_content(cos_key)
        if content is not None:
            # 写入缓存
            self.mem_cache.put(cache_key, content)
            self.disk_cache.put(cache_key, content)
            return {"exists": True, "content": base64.b64encode(content).decode(), "cached": False}
        return {"exists": False}

    def _vfs_listdir_handler(self, path: str) -> dict:
        """处理目录列表请求"""
        # 检查元数据缓存
        cached = self.meta_cache.get_dir(path)
        if cached is not None:
            return {"entries": [e.name for e in cached]}

        cos_prefix, err = self.vfs._resolve_to_dir_prefix(path)
        if err:
            return {"error": err}

        entries = self.vfs._parse_dir_contents(cos_prefix)
        self.meta_cache.set_dir(path, entries)
        return {"entries": [e.name for e in entries]}

    def _vfs_stat_handler(self, path: str) -> dict:
        """处理 stat 请求"""
        cached = self.meta_cache.get_stat(path)
        if cached is not None:
            return cached

        cos_prefix = path.rstrip("/").rsplit("/", 1)[0] + "/" if "/" in path else self.vfs.base_path
        entries = self.vfs._parse_dir_contents(cos_prefix)
        name = path.rstrip("/").split("/")[-1]

        for e in entries:
            if e.name == name:
                result = {
                    "mode": 0o40755 if e.type == "directory" else 0o100644,
                    "inode": e.inode,
                    "nlink": e.nlink,
                    "size": e.size,
                    "mtime": e.mtime,
                    "is_dir": e.type == "directory",
                }
                self.meta_cache.set_stat(path, result)
                return result

        return {"error": "not found"}

    def _vfs_mkdir_handler(self, path: str, data: dict) -> dict:
        """处理 mkdir 请求"""
        parents = data.get("parents", False)
        cos_key, err = self.vfs._resolve_path(path)
        if err:
            return {"error": err}

        dir_key = cos_key.rstrip("/") + "/.directory"
        
        if parents:
            # 递归创建父目录
            parts = path.strip("/").split("/")
            for i in range(1, len(parts) + 1):
                partial = "/" + "/".join(parts[:i])
                partial_key, _ = self.vfs._resolve_path(partial)
                if partial_key:
                    partial_dir = partial_key.rstrip("/") + "/.directory"
                    try:
                        self.vfs.storage.put_object(partial_dir, b"")
                    except Exception:
                        pass
        else:
            self.vfs.storage.put_object(dir_key, b"")
            
        self.meta_cache.invalidate_dir(path.rsplit("/", 1)[0] if "/" in path else path)
        return {"ok": True}

    def _vfs_dir_handler(self, path: str, params: dict) -> dict:
        """处理目录删除请求"""
        recursive = params.get("recursive") == "true"
        cos_key, err = self.vfs._resolve_path(path)
        if err:
            return {"error": err}

        if recursive:
            objects = self.vfs.storage.list_objects(cos_key.rstrip("/") + "/")
            if objects:
                for obj in objects:
                    self.vfs.storage.delete_file_by_key(obj["Key"])

        self.vfs.storage.delete_file_by_key(cos_key.rstrip("/") + "/.directory")
        self.meta_cache.invalidate_dir(path)
        return {"ok": True}

    def _vfs_rename_handler(self, src: str, dst: str) -> dict:
        """处理重命名请求"""
        src_key, err = self.vfs._resolve_path(src)
        if err:
            return {"error": err}
        dst_key, err = self.vfs._resolve_path(dst)
        if err:
            return {"error": err}

        content = self.vfs.storage.get_file_content(src_key)
        if content is not None:
            self.vfs.storage.put_object(dst_key, content)
            self.vfs.storage.delete_file_by_key(src_key)
            self.meta_cache.invalidate_all()
            return {"ok": True}
        return {"error": "source not found"}
