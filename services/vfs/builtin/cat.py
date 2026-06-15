"""
cat command - concatenate and print files.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class CatCommand(BuiltinCommand):
    """cat - concatenate and print files."""

    name = "cat"
    help_text = "cat <file>... - concatenate and print files"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute cat."""
        if not args:
            return CommandResult(stdout="", stderr="Error: 用法: cat <文件>...", exit_code=1)

        # If stdin is provided and no args, use stdin
        if stdin and not args:
            return CommandResult(stdout=stdin, stderr="", exit_code=0)

        results = []
        errors = []

        for p in args:
            content = self.vfs.read_file(p)
            if content.startswith("Error:"):
                errors.append(content)
            else:
                results.append(content)

        if errors and not results:
            return CommandResult(stdout="", stderr=errors[0], exit_code=1)

        return CommandResult(stdout="\n".join(results), stderr="\n".join(errors) if errors else "", exit_code=1 if errors else 0)
