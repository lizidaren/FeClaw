"""
CommandRegistry - registers and resolves builtin commands.
"""
from typing import Dict, Type, List, Optional


class CommandRegistry:
    """
    Command registry - manages builtin commands similar to $PATH.

    Commands are registered by name and can be looked up dynamically.
    """

    def __init__(self):
        self._commands: Dict[str, Type["BuiltinCommand"]] = {}

    def register(self, name: str, cmd_class: Type["BuiltinCommand"]) -> None:
        """Register a command class with a name."""
        self._commands[name] = cmd_class

    def resolve(self, name: str) -> Optional[Type["BuiltinCommand"]]:
        """Resolve a command name to its class."""
        return self._commands.get(name)

    def list_commands(self) -> List[str]:
        """List all registered command names."""
        return sorted(self._commands.keys())

    def unregister(self, name: str) -> None:
        """Unregister a command by name."""
        self._commands.pop(name, None)


# Global registry instance
_registry = CommandRegistry()


def get_registry() -> CommandRegistry:
    """Get the global command registry."""
    return _registry


def register_command(name: str, cmd_class: Type["BuiltinCommand"]) -> None:
    """Register a command in the global registry."""
    _registry.register(name, cmd_class)
