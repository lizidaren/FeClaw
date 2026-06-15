"""
stat command - display file or file system status.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class StatCommand(BuiltinCommand):
    """stat - display file or file system status."""

    name = "stat"
    help_text = "stat <file> - display file status"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute stat."""
        if not args:
            return CommandResult(stdout="", stderr="Error: 用法: stat <文件>", exit_code=1)

        path = args[0]
        result = self.vfs.stat(path)

        if result.startswith("Error:"):
            return CommandResult(stdout="", stderr=result, exit_code=1)

        return CommandResult(stdout=result, stderr="", exit_code=0)
