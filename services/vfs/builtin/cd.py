"""
cd command - change directory.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class CdCommand(BuiltinCommand):
    """cd - change directory."""

    name = "cd"
    help_text = "cd [dir] - change directory"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute cd."""
        path = args[0] if args else ""
        success, err = self.vfs.cd(path)
        if success:
            return CommandResult(stdout="", stderr="", exit_code=0)
        else:
            return CommandResult(stdout="", stderr=err, exit_code=1)
