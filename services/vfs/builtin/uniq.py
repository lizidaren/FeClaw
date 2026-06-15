"""
uniq command - report or filter out repeated lines.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class UniqCommand(BuiltinCommand):
    """uniq - report or filter out repeated lines."""

    name = "uniq"
    help_text = "uniq [-c] <file> - report or filter out repeated lines"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute uniq."""
        count = False
        path = ""

        # Use stdin if no file provided
        if not args and stdin:
            lines = stdin.split("\n")
        else:
            for p in args:
                if p == "-c":
                    count = True
                elif not p.startswith("-"):
                    path = p

            if not path:
                return CommandResult(stdout="", stderr="Error: 用法: uniq [-c] <文件>", exit_code=1)

            content = self.vfs.read_file(path)
            if content.startswith("Error:"):
                return CommandResult(stdout="", stderr=content, exit_code=1)
            lines = content.split("\n")

        if not lines:
            return CommandResult(stdout="", stderr="", exit_code=0)

        result_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            count_val = 1
            while i + count_val < len(lines) and lines[i + count_val] == line:
                count_val += 1
            if count:
                result_lines.append(f"{count_val} {line}")
            else:
                result_lines.append(line)
            i += count_val

        result = "\n".join(result_lines)
        return CommandResult(stdout=result, stderr="", exit_code=0)
