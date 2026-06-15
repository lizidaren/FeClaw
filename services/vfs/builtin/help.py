"""
help command - display help information.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand
from ..registry import get_registry


class HelpCommand(BuiltinCommand):
    """help - display help information."""

    name = "help"
    help_text = "help [command] - display help for commands"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute help."""
        registry = get_registry()
        lines = []

        if not args:
            # List all available commands
            lines.append("Available commands:")
            for cmd_name in registry.list_commands():
                cmd_class = registry.resolve(cmd_name)
                if cmd_class:
                    instance = cmd_class(self.vfs)
                    if instance.help_text:
                        lines.append(f"  {instance.help_text}")
            return CommandResult(stdout="\n".join(lines), stderr="", exit_code=0)

        # Help for specific command
        cmd_name = args[0]
        cmd_class = registry.resolve(cmd_name)
        if not cmd_class:
            return CommandResult(stdout="", stderr=f"help: no help for {cmd_name}", exit_code=1)

        instance = cmd_class(self.vfs)
        return CommandResult(stdout=instance.help_text, stderr="", exit_code=0)
