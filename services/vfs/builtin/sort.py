"""
sort command - sort lines of text.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class SortCommand(BuiltinCommand):
    """sort - sort lines of text."""

    name = "sort"
    help_text = "sort [-r] [-u] <file> - sort lines of text"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute sort."""
        reverse = False
        unique = False
        path = ""

        # Use stdin if no file provided
        if not args and stdin:
            lines = stdin.split("\n")
        else:
            for p in args:
                if p == "-r":
                    reverse = True
                elif p == "-u":
                    unique = True
                elif not p.startswith("-"):
                    path = p

            if not path:
                return CommandResult(stdout="", stderr="Error: 用法: sort [-r] [-u] <文件>", exit_code=1)

            content = self.vfs.read_file(path)
            if content.startswith("Error:"):
                return CommandResult(stdout="", stderr=content, exit_code=1)
            lines = content.split("\n")

        if unique:
            seen = set()
            result_lines = []
            for line in lines:
                if line not in seen:
                    seen.add(line)
                    result_lines.append(line)
            lines = result_lines
        else:
            lines = sorted(lines, reverse=reverse)

        result = "\n".join(lines)
        return CommandResult(stdout=result, stderr="", exit_code=0)
