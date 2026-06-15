"""
rmdir command - remove empty directories.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand
from ..registry import register_command


class RmdirCommand(BuiltinCommand):
    """rmdir - remove empty directories."""

    name = "rmdir"
    help_text = "rmdir [-p] <directory>... - remove empty directories"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """
        Execute rmdir command.
        
        Args:
            args: command arguments
            stdin: input from previous pipe (unused)
        
        Returns:
            CommandResult
        """
        if not args:
            return CommandResult(stdout="", stderr="Error: 用法: rmdir <目录>...", exit_code=1)
        
        # 支持 -p 选项（递归删除父目录）
        parents = False
        paths = []
        
        for arg in args:
            if arg == "-p":
                parents = True
            elif not arg.startswith("-"):
                paths.append(arg)
        
        if not paths:
            return CommandResult(stdout="", stderr="Error: 用法: rmdir <目录>...", exit_code=1)
        
        results = []
        errors = []
        
        for path in paths:
            result = self.vfs.rmdir(path)
            if result.startswith("Error:"):
                errors.append(result)
            else:
                results.append(result)
        
        if errors and not results:
            return CommandResult(stdout="", stderr=errors[0], exit_code=1)
        
        return CommandResult(stdout="\n".join(results), stderr="\n".join(errors) if errors else "", exit_code=1 if errors else 0)


# Register the command
register_command("rmdir", RmdirCommand)
