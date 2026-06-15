"""
pwd command - print working directory.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class PwdCommand(BuiltinCommand):
    """pwd - print working directory."""

    name = "pwd"
    help_text = "pwd - print working directory"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute pwd."""
        cwd = self.vfs.pwd()
        return CommandResult(stdout=cwd, stderr="", exit_code=0)
