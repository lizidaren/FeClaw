"""
echo command - display a line of text.
"""
import re
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class EchoCommand(BuiltinCommand):
    """echo - display a line of text."""

    name = "echo"
    help_text = "echo [text] [> file] - display text, optionally redirect to file"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute echo."""
        if not args:
            return CommandResult(stdout="", stderr="", exit_code=0)

        # Reconstruct the command string for parsing
        cmd_str = " ".join(args)

        # Handle echo "text" > file
        match = re.match(r'^"(.*)"\s+>>\s+(.+)$', cmd_str)
        if match:
            text, path = match.group(1), match.group(2).strip()
            result = self.vfs.echo(text, path, append=True)
            if result.startswith("Error:"):
                return CommandResult(stdout="", stderr=result, exit_code=1)
            return CommandResult(stdout=result, stderr="", exit_code=0)

        match = re.match(r'^"(.*)"\s+>\s+(.+)$', cmd_str)
        if match:
            text, path = match.group(1), match.group(2).strip()
            result = self.vfs.echo(text, path, append=False)
            if result.startswith("Error:"):
                return CommandResult(stdout="", stderr=result, exit_code=1)
            return CommandResult(stdout=result, stderr="", exit_code=0)

        # Handle echo 'text' > file
        match = re.match(r"^'(.*)'\s+>>\s+(.+)$", cmd_str)
        if match:
            text, path = match.group(1), match.group(2).strip()
            result = self.vfs.echo(text, path, append=True)
            if result.startswith("Error:"):
                return CommandResult(stdout="", stderr=result, exit_code=1)
            return CommandResult(stdout=result, stderr="", exit_code=0)

        match = re.match(r"^'(.*)'\s+>\s+(.+)$", cmd_str)
        if match:
            text, path = match.group(1), match.group(2).strip()
            result = self.vfs.echo(text, path, append=False)
            if result.startswith("Error:"):
                return CommandResult(stdout="", stderr=result, exit_code=1)
            return CommandResult(stdout=result, stderr="", exit_code=0)

        # Handle echo text > file (unquoted)
        match = re.match(r"^(\S+)\s+>>\s+(.+)$", cmd_str)
        if match:
            text, path = match.group(1), match.group(2).strip()
            result = self.vfs.echo(text, path, append=True)
            if result.startswith("Error:"):
                return CommandResult(stdout="", stderr=result, exit_code=1)
            return CommandResult(stdout=result, stderr="", exit_code=0)

        match = re.match(r"^(\S+)\s+>\s+(.+)$", cmd_str)
        if match:
            text, path = match.group(1), match.group(2).strip()
            result = self.vfs.echo(text, path, append=False)
            if result.startswith("Error:"):
                return CommandResult(stdout="", stderr=result, exit_code=1)
            return CommandResult(stdout=result, stderr="", exit_code=0)

        # Simple echo without redirection
        text = cmd_str.strip('"').strip("'")
        return CommandResult(stdout=text, stderr="", exit_code=0)
