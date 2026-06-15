"""
find command - search for files.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class FindCommand(BuiltinCommand):
    """find - search for files."""

    name = "find"
    help_text = "find [path] [-name pattern] [-type d|f] - search for files"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute find."""
        path = "."
        name_pattern = ""
        find_type = ""

        i = 0
        while i < len(args):
            p = args[i]
            if p == "-name" and i + 1 < len(args):
                name_pattern = args[i + 1].strip('"').strip("'")
                i += 2
            elif p == "-type" and i + 1 < len(args):
                find_type = args[i + 1]
                i += 2
            elif not p.startswith("-"):
                path = p
                i += 1
            else:
                i += 1

        result = self.vfs.find(path, name_pattern, find_type)

        if result.startswith("Error:"):
            return CommandResult(stdout="", stderr=result, exit_code=1)

        return CommandResult(stdout=result, stderr="", exit_code=0)
