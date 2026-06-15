"""
tail command - print last lines of a file.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class TailCommand(BuiltinCommand):
    """tail - print last lines of a file."""

    name = "tail"
    help_text = "tail [-n N] <file> - print last N lines"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute tail."""
        n = 10
        path = ""

        # Use stdin if no file provided
        if not args and stdin:
            lines = stdin.split("\n")
            result = "\n".join(lines[-n:])
            return CommandResult(stdout=result, stderr="", exit_code=0)

        i = 0
        while i < len(args):
            p = args[i]
            if p == "-n" and i + 1 < len(args):
                try:
                    n = int(args[i + 1])
                except ValueError:
                    return CommandResult(stdout="", stderr="Error: tail -n N 文件", exit_code=1)
                path = args[i + 2] if i + 2 < len(args) else ""
                break
            elif p == "-n":
                continue
            elif not p.startswith("-"):
                path = p
                break
            i += 1

        if not path:
            return CommandResult(stdout="", stderr="Error: 用法: tail [-n N] <文件>", exit_code=1)

        result = self.vfs.tail(path, n)

        if result.startswith("Error:"):
            return CommandResult(stdout="", stderr=result, exit_code=1)

        return CommandResult(stdout=result, stderr="", exit_code=0)
