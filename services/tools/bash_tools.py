"""
Agent 工具服务 - Bash/Python 执行工具
包含 bash 命令执行、Python 沙箱执行、后台任务等
"""

import re
import os
import shlex
import asyncio
import logging
from typing import List, Dict, Any, Optional

from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase

logger = logging.getLogger(__name__)

# 白名单命令
ALLOWED_BASH_COMMANDS = {"mkdir", "ls", "cat", "grep", "find", "head", "tail", "wc", "echo", "pwd", "cd", "cp", "mv", "rm", "fe"}
# Shell 元字符黑名单（防止通过 echo/cat 等配合重定向/管道绕过文件操作限制）
_SHELL_METACHARS = re.compile(r'[><|;&`$]')


class BashToolsMixin(AgentToolsServiceBase):
    """Bash/Python 执行工具 Mixin"""

    # ========== Bash 工具 ==========

    @tool(description='执行 bash 命令。支持：mkdir, ls, cat, grep, find, head, tail, wc, echo, pwd, cd, cp, mv, rm, python3（在沙箱中执行Python代码，支持 python3 -c "code" 单行执行和 python3 script.py 脚本执行，可访问 /workspace/ 目录下的文件）', category="code")
    async def bash(self, command: str) -> str:
        """执行 bash 命令（委托给 VirtualFileSystem 或 Python 沙箱）"""
        stripped = command.strip()

        # 强制白名单检查：提取命令名并验证
        cmd_parts = stripped.split()
        if cmd_parts:
            cmd_name = cmd_parts[0]
            if cmd_name in ("python3", "python") or stripped.startswith("python3 ") or stripped.startswith("python "):
                pass  # Python 命令单独处理
            elif cmd_name not in ALLOWED_BASH_COMMANDS:
                return f"Error: 命令 '{cmd_name}' 不在允许的白名单中。允许的命令: {', '.join(sorted(ALLOWED_BASH_COMMANDS))} | python3"

        # Shell 元字符检查（防止通过重定向/管道绕过文件操作限制）
        if _SHELL_METACHARS.search(stripped) and not (stripped.startswith("python3 ") or stripped.startswith("python ")):
            return f"Error: 命令包含不允许的 shell 元字符（><|;&`$），请使用 file_read/file_write 工具操作文件"

        # 检测 Python 命令（直接开头）
        if stripped.startswith("python3 ") or stripped.startswith("python ") or stripped == "python3" or stripped == "python":
            return await self._exec_python(stripped)

        # FeHub 命令（fe init / fe vcs / fe publish）
        if stripped.startswith("fe "):
            return await self._handle_fe_command(stripped)

        # 检测包含 python3/python 的复合命令
        if " python3 " in stripped or " python " in stripped or stripped.endswith(" python3") or stripped.endswith(" python"):
            return await self._exec_python_compound(stripped)

        # FUSE 模式：用 sandbox 的真实 bash 执行
        from services.vfs_fuse_daemon import check_fuse_available
        if check_fuse_available() and self._has_active_sandbox():
            return await asyncio.to_thread(self._exec_bash_in_sandbox, stripped)

        # Desktop 模式：bwrap 不可用时走 desktop_relay 请求授权
        from config import settings
        if settings.DESKTOP_ENABLED:
            # 检查 bwrap 是否可用（不可用时走 desktop_relay）
            import shutil
            bwrap_available = shutil.which("bwrap") is not None
            if not bwrap_available:
                # 走 Desktop relay 请求授权
                cmd_parts = stripped.split()
                command = cmd_parts[0] if cmd_parts else stripped
                args = cmd_parts[1:] if len(cmd_parts) > 1 else []
                cwd = getattr(self._vfs, '_cwd', '/workspace')
                # 风险等级：危险命令高风险，其他中风险
                risk_level = 2 if command in ("rm", "dd", "mkfs", ":(){:|:&};:", "shutdown", "reboot") else 1
                from services.desktop_relay import relay
                consent = await relay.request_consent(command, args, cwd, risk_level)
                if consent.get("decision") != "allow":
                    return f"Error: Desktop 拒绝执行命令 '{stripped}' ({consent.get('reason', 'unknown')})"
                # 授权通过，继续执行

        # 有活跃 sandbox 且是文件操作时，失效 MetadataCache 确保缓存与 sandbox 一致
        if self._has_active_sandbox() and self._is_file_operation(stripped):
            from services.cache_manager import MetadataCache
            meta_cache = MetadataCache()
            meta_cache.invalidate_all()

        return await asyncio.to_thread(self._vfs.execute, command)

    def _has_active_sandbox(self) -> bool:
        """检查是否有活跃的 sandbox"""
        return hasattr(self, "_sandbox")

    def _is_file_operation(self, command: str) -> bool:
        """检查命令是否涉及文件操作且目标路径以 /workspace 开头"""
        file_ops = {"cat", "echo", "cp", "mv", "rm", "touch", "head", "tail",
                    "grep", "find", "wc", "sort", "uniq", "sed", "awk", "cut", "diff"}
        parts = command.strip().split()
        first_word = parts[0] if parts else ""
        return first_word in file_ops and "/workspace" in command

    async def _exec_python_compound(self, command: str) -> str:
        """处理包含 Python 的复合命令（支持 && 分隔）

        cd 和非 python 命令用 VFS 执行，python 命令从 VFS 读取脚本内容后执行。
        """
        parts = [p.strip() for p in command.split("&&")]
        vfs_commands = []
        python_cmd = None

        for part in parts:
            if not part:
                continue
            if (part.startswith("python3 ") or part.startswith("python ")) and ".py" in part:
                python_cmd = part
            else:
                vfs_commands.append(part)

        for cmd in vfs_commands:
            await asyncio.to_thread(self._vfs.execute, cmd)

        if not python_cmd:
            return "(无 Python 命令)"

        if not hasattr(self, "_sandbox"):
            from services.sandbox_manager import SandboxManager
            self._sandbox = SandboxManager(self._vfs, self.user_id)

        m = re.match(r'^python3?\s+(.+\.py)\s*$', python_cmd.strip())
        if m:
            script_path = m.group(1).strip()
            if not script_path.startswith('/'):
                script_path = "/workspace/" + script_path
            content = await self._vfs.async_cat(script_path)
            if content.startswith("[Error") or not content:
                return f"(文件不存在或读取失败: {script_path})\n{content}"
            result = self._sandbox.exec_code(content, timeout=300, cwd="/workspace")
        else:
            result = self._sandbox.exec_command(python_cmd, cwd="/workspace")

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + f"[stderr] {result.stderr}"
        if result.timed_out:
            output += "\n[执行超时]"
        if result.exit_code != 0:
            output += f"\n[退出码: {result.exit_code}]"

        if hasattr(self._sandbox, '_sync_local_to_vfs'):
            self._sandbox._sync_local_to_vfs()
        return output if output else "(无输出)"

    async def _exec_python(self, command: str) -> str:
        """执行 Python 命令（通过沙箱）

        支持:
        - python3 -c "code"       → 直接执行代码
        - python3 script.py       → 从 VFS 读取脚本内容，执行
        - python3 (无参数)         → 交互模式（当前返回提示）
        """
        if not hasattr(self, "_sandbox"):
            from services.sandbox_manager import SandboxManager
            self._sandbox = SandboxManager(self._vfs, self.user_id)

        m = re.match(r'^python3?\s+-c\s+["\'](.+?)["\']', command, re.DOTALL)
        if m:
            code = m.group(1)
            result = self._sandbox.exec_code(code)
        else:
            m2 = re.match(r'^python3?\s+(.+\.py)\s*$', command.strip())
            if m2:
                script_path = m2.group(1).strip()
                if not script_path.startswith('/'):
                    script_path = self._vfs._cwd.rstrip('/') + '/' + script_path
                try:
                    content = await self._vfs.async_cat(script_path)
                    if content.startswith("[Error") or not content:
                        return f"(文件不存在或读取失败: {script_path})\n{content}"
                    result = self._sandbox.exec_code(content, timeout=300)
                except Exception as e:
                    return f"(读取脚本文件失败: {e})"
            else:
                result = self._sandbox.exec_code(command)

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + f"[stderr] {result.stderr}"
        if result.timed_out:
            output += "\n[执行超时]"
        if result.exit_code != 0:
            output += f"\n[退出码: {result.exit_code}]"

        if hasattr(self._sandbox, '_sync_local_to_vfs'):
            self._sandbox._sync_local_to_vfs()

        return output if output else "(无输出)"

    def _exec_bash_in_sandbox(self, command: str) -> str:
        """在沙箱中使用真实 bash 执行命令（FUSE 模式）"""
        if not hasattr(self, "_sandbox"):
            from services.sandbox_manager import SandboxManager
            self._sandbox = SandboxManager(self._vfs, self.user_id)
            self._sandbox.agent_hash = getattr(self._vfs, 'agent_id', None) or ''

        result = self._sandbox.exec_command(
            f"/bin/bash -c {shlex.quote(command)}",
            timeout=30,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + f"[stderr] {result.stderr}"
        if result.timed_out:
            output += "\n[执行超时]"
        return output if output else "(no output)"

    # ========== 后台 Python 任务 ==========

    @tool(description="在后台持续运行 Python 代码，返回 task_id", category="code")
    def python_background(self, code: str, name: str = None, port: int = None) -> str:
        """在后台持续运行 Python 代码"""
        if not hasattr(self, "_sandbox"):
            try:
                from services.wasm_python import WasmPythonSandbox
                self._sandbox = WasmPythonSandbox(self._vfs, self.user_id)
            except Exception:
                from services.sandbox_manager import SandboxManager
                self._sandbox = SandboxManager(self._vfs, self.user_id)

        task_id = self._sandbox.start_background(code, name, port)
        if task_id.startswith("Error:"):
            return task_id

        return f"OK: 后台任务已启动，task_id={task_id}"

    @tool(description="列出所有后台 Python 任务", category="code")
    def python_task_list(self) -> str:
        """列出所有后台 Python 任务"""
        if not hasattr(self, "_sandbox"):
            return "(无后台任务)"

        tasks = self._sandbox.list_background_tasks()
        if not tasks:
            return "(无后台任务)"

        lines = []
        for task in tasks:
            status = "运行中" if task["running"] else f"已结束(退出码:{task['exit_code']})"
            port_info = f", 端口:{task['port']}" if task["port"] else ""
            lines.append(f"[{task['task_id']}] {task['name']} - {status}{port_info}")

        return "\n".join(lines)

    @tool(description="停止后台 Python 任务", category="code")
    def python_task_stop(self, task_id: str) -> str:
        """停止后台任务"""
        if not hasattr(self, "_sandbox"):
            return "Error: 沙箱未初始化"

        success = self._sandbox.stop_background(task_id)
        if success:
            return f"OK: 已停止任务 {task_id}"
        return f"Error: 停止任务失败或任务不存在"

    @tool(description="获取后台 Python 任务的输出", category="code")
    def python_task_output(self, task_id: str, lines: int = 50) -> str:
        """获取后台任务输出"""
        if not hasattr(self, "_sandbox"):
            return "Error: 沙箱未初始化"

        output = self._sandbox.get_task_output(task_id, lines)
        return output if output else "(无输出)"

    # ========== FeHub 命令处理 ==========

    async def _handle_fe_command(self, command: str) -> str:
        """
        处理 fe 命令（fe init / fe vcs / fe publish / fe unpublish）。

        用法:
          fe init [--template=/path]
          fe vcs commit <message>
          fe vcs log [file_path]
          fe vcs diff <file> <ref_a> <ref_b>
          fe vcs restore <file> <ref>
          fe publish <tag> [--public]
          fe unpublish <tag>
        """
        import shlex

        parts = command.strip().split()
        if len(parts) < 2:
            return "Error: fe 命令用法:\n  fe init [--template=/path]\n  fe vcs commit <message>\n  fe vcs log [file_path]\n  fe vcs diff <file> <ref_a> <ref_b>\n  fe vcs restore <file> <ref>\n  fe publish <tag> [--public]\n  fe unpublish <tag>"

        subcmd = parts[1]

        try:
            svc = self._get_fehub_service()
        except Exception as e:
            return f"Error: 无法初始化 FeHub 服务: {e}"

        if subcmd == "init":
            # fe init [--template=/path]
            path = "/workspace"
            template_path = ""
            # Parse --template=xxx
            for p in parts[2:]:
                if p.startswith("--template="):
                    template_path = p.split("=", 1)[1]
                elif not p.startswith("-"):
                    path = p
            return await svc.init_project(path=path, template_path=template_path)

        elif subcmd == "vcs":
            if len(parts) < 3:
                return "Error: fe vcs 用法:\n  fe vcs commit <message>\n  fe vcs log [file_path]\n  fe vcs diff <file> <ref_a> <ref_b>\n  fe vcs restore <file> <ref>"
            vcs_subcmd = parts[2]
            if vcs_subcmd == "commit":
                if len(parts) < 4:
                    return "Error: 请提供提交消息: fe vcs commit <message>"
                message = " ".join(parts[3:])
                return await svc.commit(path="/workspace", message=message)
            elif vcs_subcmd == "log":
                file_path = parts[3] if len(parts) > 3 else ""
                return await svc.log(path="/workspace", file_path=file_path)
            elif vcs_subcmd == "diff":
                if len(parts) < 6:
                    return "Error: fe vcs diff <file> <ref_a> <ref_b>"
                file_path, ref_a, ref_b = parts[3], parts[4], parts[5]
                return await svc.diff(file_path=file_path, ref_a=ref_a, ref_b=ref_b)
            elif vcs_subcmd == "restore":
                if len(parts) < 5:
                    return "Error: fe vcs restore <file> <ref>"
                file_path, ref = parts[3], parts[4]
                return await svc.restore(file_path=file_path, ref=ref)
            else:
                return f"Error: 未知 vcs 子命令: {vcs_subcmd}，可用: commit, log, diff, restore"

        elif subcmd == "publish":
            if len(parts) < 3:
                return "Error: fe publish <tag> [--public]"
            tag = parts[2]
            is_public = "--public" in parts[3:]
            return await svc.publish(path="/workspace", tag=tag, is_public=is_public)

        elif subcmd == "unpublish":
            if len(parts) < 3:
                return "Error: fe unpublish <tag>"
            tag = parts[2]
            return await svc.unpublish(tag=tag)

        else:
            return f"Error: 未知 fe 子命令: {subcmd}，可用: init, vcs, publish, unpublish"

    def _get_fehub_service(self):
        """Lazily create FeHubService for this agent."""
        from services.fehub_service import FeHubService
        return FeHubService(agent_hash=self.agent_hash)
