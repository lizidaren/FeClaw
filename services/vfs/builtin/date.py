"""
date command - print or set system date.
"""
from datetime import datetime
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class DateCommand(BuiltinCommand):
    """date - print or set system date and time."""

    name = "date"
    help_text = "date [+format] - print or set system date"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute date."""
        fmt = ""
        for arg in args:
            if arg.startswith("+"):
                fmt = arg[1:]
                break

        now = datetime.now()

        if not fmt:
            result = now.strftime("%a %b %d %H:%M:%S %Y")
        else:
            try:
                result = now.strftime(fmt)
            except Exception:
                result = now.strftime("%a %b %d %H:%M:%S %Y")

        return CommandResult(stdout=result, stderr="", exit_code=0)
