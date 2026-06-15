"""
exit command - exit the shell.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class ExitCommand(BuiltinCommand):
    """exit - exit the shell."""

    name = "exit"
    help_text = "exit [n] - exit with status n"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute exit."""
        exit_code = 0
        if args:
            try:
                exit_code = int(args[0])
            except ValueError:
                return CommandResult(stdout="", stderr=f"exit: {args[0]}: numeric argument required", exit_code=1)
        # Note: In VFS context, we don't actually exit the process
        return CommandResult(stdout="", stderr="", exit_code=exit_code)
