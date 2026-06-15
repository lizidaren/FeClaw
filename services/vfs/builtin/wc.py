"""
wc command - print line, word, and byte counts.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class WcCommand(BuiltinCommand):
    """wc - print line, word, and byte counts."""

    name = "wc"
    help_text = "wc [-lwc] <file> - print line, word, and byte counts"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute wc."""
        # Use stdin if no file provided and no args
        if not args and stdin:
            content = stdin
            path = ""
        else:
            mode = "lwc"
            path = ""

            for p in args:
                if p.startswith("-"):
                    mode = p[1:]
                else:
                    path = p

            if not path:
                return CommandResult(stdout="", stderr="Error: 用法: wc [-lwc] <文件>", exit_code=1)

            content = self.vfs.read_file(path)
            if content.startswith("Error:"):
                return CommandResult(stdout="", stderr=content, exit_code=1)

        lines = content.split("\n")
        words = content.split()
        bytes_count = len(content.encode("utf-8"))

        results = []

        if "l" in mode:
            results.append(str(len(lines)))
        if "w" in mode:
            results.append(str(len(words)))
        if "c" in mode:
            results.append(str(bytes_count))

        if len(mode) > 1 and path:
            results.append(path)

        result = " ".join(results)
        return CommandResult(stdout=result, stderr="", exit_code=0)
