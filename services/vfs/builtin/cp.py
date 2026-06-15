"""
cp command - copy files and directories.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class CpCommand(BuiltinCommand):
    """cp - copy files and directories."""

    name = "cp"
    help_text = "cp [-r] <src> <dst> - copy files or directories"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute cp."""
        if len(args) < 2:
            return CommandResult(stdout="", stderr="Error: 用法: cp <源> <目标>", exit_code=1)

        recursive = False
        paths = []

        for p in args:
            if p == "-r":
                recursive = True
            else:
                paths.append(p)

        if len(paths) < 2:
            return CommandResult(stdout="", stderr="Error: 用法: cp <源> <目标>", exit_code=1)

        src, dst = paths[0], paths[1]
        result = self.vfs.cp(src, dst, recursive=recursive)

        if result.startswith("Error:"):
            return CommandResult(stdout="", stderr=result, exit_code=1)

        return CommandResult(stdout=result, stderr="", exit_code=0)
