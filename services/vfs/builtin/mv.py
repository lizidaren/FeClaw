"""
mv command - move/rename files and directories.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class MvCommand(BuiltinCommand):
    """mv - move or rename files."""

    name = "mv"
    help_text = "mv <src> <dst> - move or rename files"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute mv."""
        if len(args) < 2:
            return CommandResult(stdout="", stderr="Error: 用法: mv <源> <目标>", exit_code=1)

        src, dst = args[0], args[1]
        result = self.vfs.mv(src, dst)

        if result.startswith("Error:"):
            return CommandResult(stdout="", stderr=result, exit_code=1)

        return CommandResult(stdout=result, stderr="", exit_code=0)
