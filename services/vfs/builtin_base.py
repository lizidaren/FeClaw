"""
BuiltinCommand - base class for all builtin commands.
"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .result import CommandResult
    from ..virtual_filesystem import VirtualFileSystem


class BuiltinCommand(ABC):
    """
    Base class for all builtin commands.

    Each command must implement the `execute` method.
    """

    name: str = "command"
    help_text: str = ""

    def __init__(self, vfs: "VirtualFileSystem"):
        """
        Initialize the builtin command.

        Args:
            vfs: The VirtualFileSystem instance to operate on.
        """
        self.vfs = vfs

    @abstractmethod
    def execute(self, args: list, stdin: str) -> "CommandResult":
        """
        Execute the command.

        Args:
            args: List of command arguments.
            stdin: Input from previous pipe (if any).

        Returns:
            CommandResult with stdout, stderr, and exit_code.
        """
        pass

    def get_help(self) -> str:
        """Return help text for the command."""
        return self.help_text
