"""
touch command - change file timestamps or create empty file.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class TouchCommand(BuiltinCommand):
    """touch - change file timestamps or create empty file."""

    name = "touch"
    help_text = "touch <file>... - create empty file or update timestamp"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute touch."""
        if not args:
            return CommandResult(stdout="", stderr="Error: 用法: touch <文件>", exit_code=1)

        results = []
        errors = []

        for path in args:
            result = self.vfs.touch(path)
            if result.startswith("Error:"):
                errors.append(result)
            else:
                results.append(result)

        if errors and not results:
            return CommandResult(stdout="", stderr=errors[0], exit_code=1)

        return CommandResult(stdout="\n".join(results), stderr="\n".join(errors) if errors else "", exit_code=1 if errors else 0)
