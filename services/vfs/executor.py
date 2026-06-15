"""
Executor - executes AST nodes and returns CommandResults.
"""
from typing import Optional

from .result import CommandResult
from .ast import ASTNode, SimpleCommand, Pipeline, Sequence
from .registry import CommandRegistry
from .parser import AndOrList  # AndOrList is defined in parser.py

# Import builtin commands for registration
from .builtin import register_all_commands


class Executor:
    """
    Executes AST nodes and returns CommandResults.

    The executor walks the AST and dispatches to the appropriate
    command implementations registered in the CommandRegistry.
    """

    def __init__(self, vfs, registry: CommandRegistry):
        self.vfs = vfs
        self.registry = registry

    def execute_sequence(self, seq: Sequence) -> CommandResult:
        """
        Execute a sequence of commands separated by ; or &.

        Args:
            seq: Sequence AST node

        Returns:
            CommandResult from the last command executed
        """
        last_result = CommandResult()

        for node, op in seq.items:
            result = self._execute_node(node)
            last_result = result

            # Handle short-circuit operators
            if op == "&&" and result.exit_code != 0:
                break
            if op == "||" and result.exit_code == 0:
                break

        return last_result

    def _execute_node(self, node: ASTNode) -> CommandResult:
        """Execute an AST node based on its type."""
        if isinstance(node, Sequence):
            return self.execute_sequence(node)
        elif isinstance(node, (AndOrList,)):
            return self._execute_and_or_list(node)
        elif isinstance(node, Pipeline):
            return self._execute_pipeline(node)
        elif isinstance(node, SimpleCommand):
            return self._execute_simple_command(node, "")
        else:
            return CommandResult(stderr=f"Error: Unknown node type: {type(node)}", exit_code=1)

    def _execute_and_or_list(self, node: AndOrList) -> CommandResult:
        """Execute an AND-OR list (cmd1 && cmd2 || cmd3)."""
        last_result = CommandResult()

        for i, (child_node, op) in enumerate(node.items):
            result = self._execute_node(child_node)
            last_result = result

            if op == "&&" and result.exit_code != 0:
                break
            if op == "||" and result.exit_code == 0:
                break

        return last_result

    def _execute_pipeline(self, pipeline: Pipeline) -> CommandResult:
        """
        Execute a pipeline: cmd1 | cmd2 | cmd3

        Each command's stdout becomes the next command's stdin.
        """
        stdin = ""
        last_result = CommandResult()

        for cmd in pipeline.commands:
            result = self._execute_simple_command(cmd, stdin)
            last_result = result
            # Pipe: stdout becomes next command's stdin
            stdin = result.stdout

        return last_result

    def _execute_simple_command(self, cmd: SimpleCommand, stdin: str) -> CommandResult:
        """
        Execute a simple command with optional stdin input.

        Args:
            cmd: SimpleCommand AST node
            stdin: Input from previous pipe (if any)

        Returns:
            CommandResult
        """
        # Resolve command from registry
        cmd_class = self.registry.resolve(cmd.command)
        if not cmd_class:
            return CommandResult(
                stderr=f"Error: 命令不存在: {cmd.command}",
                exit_code=127
            )

        # Create command instance and execute
        instance = cmd_class(self.vfs)

        # Use only the args (skip command name which is at index 0)
        args = cmd.args[1:] if len(cmd.args) > 1 else []

        result = instance.execute(args, stdin)

        # Apply redirections
        result = self._apply_redirects(cmd, result)

        return result

    def _apply_redirects(self, cmd: SimpleCommand, result: CommandResult) -> CommandResult:
        """
        Apply output redirections to a command result.

        Args:
            cmd: SimpleCommand with redirection specs
            result: CommandResult to modify

        Returns:
            Modified CommandResult
        """
        # Collect all redirects from the command
        redirects = []
        if cmd.stdout_redir:
            redirects.append(cmd.stdout_redir)
        if cmd.stderr_redir:
            redirects.append(cmd.stderr_redir)
        if cmd.stdin_redir:
            redirects.append(cmd.stdin_redir)

        for redir in redirects:
            if redir.target == "/dev/null":
                if redir.type == "stdout":
                    result.stdout = ""
                elif redir.type == "stderr":
                    result.stderr = ""
            elif redir.target == "&1":
                # 2>&1: redirect stderr to stdout
                result.stdout = (result.stdout + "\n" + result.stderr).strip()
                result.stderr = ""
            elif redir.type in ("stdout", "stderr"):
                content = result.stdout if redir.type == "stdout" else result.stderr
                mode = "a" if redir.mode == "a" else "w"

                # Write to file via VFS
                write_result = self._write_file_raw(redir.target, content, append=(mode == "a"))

                # If write failed (e.g. /public/ read-only), preserve error in stderr
                if write_result and write_result.startswith("Error:"):
                    result.stderr = write_result
                    if redir.type == "stdout":
                        result.stdout = ""
                else:
                    # Clear the redirected output
                    if redir.type == "stdout":
                        result.stdout = ""
                    else:
                        result.stderr = ""

        return result

    def _write_file_raw(self, path: str, content: str, append: bool = False) -> str:
        """
        Write content to a VFS path (for redirection).

        Args:
            path: Target VFS path
            content: Content to write
            append: If True, append; otherwise overwrite

        Returns:
            Error message if write failed, otherwise empty string
        """
        if not path:
            return ""

        try:
            # Use echo command's write functionality
            result = self.vfs.echo(content.rstrip("\n"), path, append=append)
            # If write was blocked (e.g. /public/ read-only), return the error
            if result.startswith("Error:"):
                return result
            return ""
        except Exception as e:
            return f"Error: {e}"