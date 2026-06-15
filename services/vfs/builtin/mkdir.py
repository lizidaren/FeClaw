"""
mkdir command - make directories.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class MkdirCommand(BuiltinCommand):
    """mkdir - make directories."""

    name = "mkdir"
    help_text = "mkdir [-p] <dir> - make directories"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute mkdir."""
        if not args:
            return CommandResult(stdout="", stderr="Error: mkdir: missing operand", exit_code=1)

        parents = False
        paths = []

        i = 0
        while i < len(args):
            p = args[i]
            if p == "-p":
                parents = True
            elif p.startswith("-"):
                pass  # Ignore unknown flags
            else:
                paths.append(p)
            i += 1

        if not paths:
            return CommandResult(stdout="", stderr="Error: mkdir: missing operand", exit_code=1)

        results = []
        for path in paths:
            # Check if trying to create /public/ directory
            if path.rstrip("/") == "public" or path.rstrip("/") == "/public":
                results.append("Error: /public is a read-only system directory")
                continue
                
            result = self.vfs.mkdir(path, parents=parents)
            if result.startswith("Error:"):
                results.append(f"mkdir: {result}")
            else:
                results.append(result)

        stderr = "\n".join(results) if any(r.startswith("Error:") or r.startswith("mkdir:") for r in results) else ""
        stdout = "\n".join(r for r in results if not r.startswith("Error:") and not r.startswith("mkdir:"))
        
        return CommandResult(stdout=stdout, stderr=stderr, exit_code=1 if stderr else 0)
