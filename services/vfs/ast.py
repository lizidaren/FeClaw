"""
AST nodes for bash-like command parsing.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


class ASTNode:
    """Base class for AST nodes."""
    pass


@dataclass
class Redirection:
    """Redirection specification."""
    type: str  # "stdout", "stderr", "stdout_stderr", "stdin"
    target: str  # file path or "/dev/null"
    mode: str = "w"  # "w" for overwrite, "a" for append


@dataclass
class SimpleCommand(ASTNode):
    """
    Simple command: ls -la /workspace

    Represents a single command with its arguments and redirections.
    """
    command: str
    args: List[str] = field(default_factory=list)
    stdin_redir: Optional[Redirection] = None
    stdout_redir: Optional[Redirection] = None
    stderr_redir: Optional[Redirection] = None


@dataclass
class Pipeline(ASTNode):
    """
    Pipeline: cmd1 | cmd2 | cmd3

    A sequence of commands connected by pipes.
    """
    commands: List[SimpleCommand] = field(default_factory=list)


@dataclass
class AndOrItem(ASTNode):
    """
    AND-OR list item: cmd1 && cmd2 || cmd3

    Represents a node combined with an operator.
    """
    node: ASTNode
    operator: str = ""  # "&&", "||", or "" for the last item


@dataclass
class Sequence(ASTNode):
    """
    Sequence: cmd1; cmd2 & cmd3

    A list of commands to execute sequentially.
    """
    items: List[Tuple[ASTNode, str]] = field(default_factory=list)  # (node, operator)
