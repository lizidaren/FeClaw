"""
diff command - compare files line by line.
"""
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class DiffCommand(BuiltinCommand):
    """diff - compare files line by line."""

    name = "diff"
    help_text = "diff <file1> <file2> - compare files"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute diff."""
        if len(args) < 2:
            return CommandResult(stdout="", stderr="Error: 用法: diff <文件1> <文件2>", exit_code=1)

        file1, file2 = args[0], args[1]

        content1 = self.vfs.read_file(file1)
        if content1.startswith("Error:"):
            return CommandResult(stdout="", stderr=content1, exit_code=1)

        content2 = self.vfs.read_file(file2)
        if content2.startswith("Error:"):
            return CommandResult(stdout="", stderr=content2, exit_code=1)

        lines1 = content1.split("\n")
        lines2 = content2.split("\n")

        result = []
        i = 0
        j = 0

        # Find first difference
        first_diff = None
        while i < len(lines1) and j < len(lines2):
            if lines1[i] != lines2[j]:
                first_diff = (i, j)
                break
            i += 1
            j += 1

        if first_diff is None:
            if len(lines1) != len(lines2):
                first_diff = (i, j)

        if first_diff is None:
            return CommandResult(stdout="", stderr="", exit_code=0)

        start1, start2 = first_diff
        i = start1
        j = start2

        while i < len(lines1) or j < len(lines2):
            line1 = lines1[i] if i < len(lines1) else None
            line2 = lines2[j] if j < len(lines2) else None

            if line1 is not None and line2 is not None and line1 == line2:
                result.append(f"  {line1}")
                i += 1
                j += 1
            elif line1 is None:
                result.append(f"> {line2}")
                j += 1
            elif line2 is None:
                result.append(f"< {line1}")
                i += 1
            else:
                result.append(f"< {line1}")
                result.append(f"> {line2}")
                i += 1
                j += 1

            if len(result) > 100:
                result.append("... (diff truncated)")
                break

        exit_code = 0 if not result else 1
        return CommandResult(stdout="\n".join(result), stderr="", exit_code=exit_code)
