"""
ls command - list directory contents.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class LsCommand(BuiltinCommand):
    """ls - list directory contents."""

    name = "ls"
    help_text = "ls [-alRthSr] [path] - list directory contents"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute ls."""
        # Parse flags and path
        show_all = False
        long_format = False
        human = False
        recursive = False
        classify = False
        sort_by = "name"
        reverse = False
        directory_only = False
        show_inode = False
        oneline = False
        path = ""

        i = 0
        while i < len(args):
            p = args[i]
            if p in ("-a", "--all"):
                show_all = True
            elif p in ("-A", "--almost-all"):
                show_all = True
            elif p in ("-l", "--long"):
                long_format = True
            elif p in ("-h", "--human-readable"):
                human = True
            elif p in ("-R", "--recursive"):
                recursive = True
            elif p in ("-F", "--classify"):
                classify = True
            elif p in ("-t",):
                sort_by = "time"
            elif p in ("-S",):
                sort_by = "size"
            elif p in ("-r", "--reverse"):
                reverse = True
            elif p in ("-d", "--directory"):
                directory_only = True
            elif p in ("-1",):
                oneline = True
            elif p in ("-i", "--inode"):
                show_inode = True
            elif p.startswith("-") and len(p) > 1:
                # Handle combined flags like -la
                for c in p[1:]:
                    if c == "a":
                        show_all = True
                    elif c == "l":
                        long_format = True
                    elif c == "h":
                        human = True
                    elif c == "R":
                        recursive = True
                    elif c == "F":
                        classify = True
                    elif c == "t":
                        sort_by = "time"
                    elif c == "S":
                        sort_by = "size"
                    elif c == "r":
                        reverse = True
                    elif c == "d":
                        directory_only = True
                    elif c == "1":
                        oneline = True
                    elif c == "i":
                        show_inode = True
            else:
                path = p
            i += 1

        result = self.vfs.ls(
            path=path,
            show_all=show_all,
            long_format=long_format,
            human=human,
            recursive=recursive,
            classify=classify,
            sort_by=sort_by,
            reverse=reverse,
            directory_only=directory_only,
            show_inode=show_inode,
            oneline=oneline
        )

        # Check if result is an error
        if result.startswith("Error:"):
            return CommandResult(stdout="", stderr=result, exit_code=1)
        elif result.startswith("错误"):
            return CommandResult(stdout="", stderr=result, exit_code=1)

        return CommandResult(stdout=result, stderr="", exit_code=0)
