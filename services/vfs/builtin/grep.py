"""
grep command - search for patterns in files.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class GrepCommand(BuiltinCommand):
    """grep - search for patterns in files."""

    name = "grep"
    help_text = "grep [-ivrlo] <pattern> <file>... - search for pattern in files"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute grep."""
        if not args:
            return CommandResult(stdout="", stderr="Error: 用法: grep <pattern> <文件>...", exit_code=1)

        # Use stdin if no file provided
        if stdin and len(args) == 1:
            pattern = args[0].strip("\"'")
            lines = stdin.split("\n")
            matching = [line for line in lines if pattern in line]
            return CommandResult(stdout="\n".join(matching), stderr="", exit_code=0 if matching else 1)

        ignore_case = False
        show_line_no = True
        invert = False
        recursive = False
        name_only = False
        only_matching = False

        pattern = ""
        paths = []

        i = 0
        while i < len(args):
            p = args[i]
            if p == "-i":
                ignore_case = True
            elif p == "-n":
                show_line_no = True
            elif p == "-v":
                invert = True
            elif p == "-r":
                recursive = True
            elif p == "-l":
                name_only = True
            elif p == "-o":
                only_matching = True
            elif p.startswith("-"):
                pass  # Ignore other flags
            elif not pattern:
                pattern = p.strip("\"'")
            else:
                paths.append(p)
            i += 1

        if not pattern:
            return CommandResult(stdout="", stderr="Error: 用法: grep <pattern> <文件>...", exit_code=1)
        if not paths:
            return CommandResult(stdout="", stderr="Error: 用法: grep <pattern> <文件>...", exit_code=1)

        result = self.vfs.grep(
            pattern,
            *paths,
            ignore_case=ignore_case,
            show_line_no=show_line_no,
            invert=invert,
            recursive=recursive,
            name_only=name_only,
            only_matching=only_matching
        )

        if result.startswith("Error:"):
            return CommandResult(stdout="", stderr=result, exit_code=2)

        exit_code = 0 if result else 1
        return CommandResult(stdout=result, stderr="", exit_code=exit_code)
