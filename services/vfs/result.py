"""
CommandResult dataclass - represents the result of a command execution.
"""
from dataclasses import dataclass


@dataclass
class CommandResult:
    """Result of a command execution with stdout, stderr, and exit_code."""
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0

    def __bool__(self) -> bool:
        """Return True if exit_code is 0."""
        return self.exit_code == 0

    def combine(self) -> str:
        """Combine stdout and stderr for backward-compatible output."""
        if self.stderr and self.stdout:
            return f"{self.stdout}\n{self.stderr}"
        elif self.stderr:
            return self.stderr
        return self.stdout