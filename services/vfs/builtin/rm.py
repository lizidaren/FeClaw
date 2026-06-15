"""
rm command - remove files or directories.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class RmCommand(BuiltinCommand):
    """rm - remove files or directories."""

    name = "rm"
    help_text = "rm [-rf] <file>... - remove files or directories"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute rm."""
        if not args:
            return CommandResult(stdout="", stderr="Error: 用法: rm <文件>", exit_code=1)

        recursive = False
        force = False
        paths = []

        for p in args:
            if p in ("-r", "-rf", "-fr"):
                recursive = True
            elif p == "-f":
                force = True
            elif not p.startswith("-"):
                paths.append(p)

        if not paths:
            return CommandResult(stdout="", stderr="Error: 用法: rm <文件>", exit_code=1)

        results = []
        errors = []

        for path in paths:
            result = self.vfs.rm(path, recursive=recursive, force=force)
            if result.startswith("Error:"):
                errors.append(result)
            else:
                results.append(result)

        if errors and not results:
            return CommandResult(stdout="", stderr=errors[0], exit_code=1)

        return CommandResult(stdout="\n".join(results), stderr="\n".join(errors) if errors else "", exit_code=1 if errors else 0)
