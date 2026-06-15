"""
head command - print first lines of a file.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class HeadCommand(BuiltinCommand):
    """head - print first lines of a file."""

    name = "head"
    help_text = "head [-n N] <file> - print first N lines"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute head."""
        n = 10
        path = ""

        # Use stdin if no file provided
        if not args and stdin:
            lines = stdin.split("\n")
            result = "\n".join(lines[:n])
            return CommandResult(stdout=result, stderr="", exit_code=0)

        i = 0
        while i < len(args):
            p = args[i]
            if p == "-n" and i + 1 < len(args):
                try:
                    n = int(args[i + 1])
                except ValueError:
                    return CommandResult(stdout="", stderr="Error: head -n N 文件", exit_code=1)
                path = args[i + 2] if i + 2 < len(args) else ""
                break
            elif p == "-n":
                continue
            elif not p.startswith("-"):
                path = p
                break
            i += 1

        if not path:
            return CommandResult(stdout="", stderr="Error: 用法: head [-n N] <文件>", exit_code=1)

        result = self.vfs.head(path, n)

        if result.startswith("Error:"):
            return CommandResult(stdout="", stderr=result, exit_code=1)

        return CommandResult(stdout=result, stderr="", exit_code=0)
