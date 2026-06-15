"""
awk command - pattern scanning and processing language.
"""
import re
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class AwkCommand(BuiltinCommand):
    """awk - pattern scanning and processing language."""

    name = "awk"
    help_text = "awk <pattern|action> <file> - pattern scanning and processing"

    def _match(self, line: str, pattern: str, field_sep: str) -> bool:
        """Check if line matches pattern."""
        if not pattern:
            return True
        nr_match = re.match(r'NR\s*==\s*(\d+)', pattern.strip())
        if nr_match:
            return False
        test_line = line
        test_pattern = pattern.strip('"\'')
        return test_pattern in test_line

    def _action(self, line: str, action: str, field_sep: str) -> str:
        """Execute awk action."""
        match = re.search(r'\{(.+)\}', action)
        if not match:
            return line

        expr = match.group(1).strip()

        if expr.startswith("print"):
            print_expr = expr[5:].strip()
            fields = line.split(field_sep) if field_sep else line.split()

            if not print_expr:
                return line

            result_parts = []
            i = 0
            while i < len(print_expr):
                if print_expr[i] == '$':
                    j = i + 1
                    while j < len(print_expr) and print_expr[j].isdigit():
                        j += 1
                    if j > i + 1:
                        field_num = int(print_expr[i+1:j])
                        if 0 < field_num <= len(fields):
                            result_parts.append(fields[field_num - 1])
                        i = j
                    elif j < len(print_expr) and print_expr[j] == 'N' and j + 1 < len(print_expr) and print_expr[j+1] == 'F':
                        if fields:
                            result_parts.append(fields[-1])
                        i = j + 2
                    else:
                        i += 1
                else:
                    i += 1

            if result_parts:
                return " ".join(result_parts)
            return line

        return line

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute awk."""
        if not args:
            return CommandResult(stdout="", stderr="Error: awk 用法: awk <pattern|action> <文件>", exit_code=1)

        field_sep = None
        path = ""
        pattern = ""

        cmd_str = " ".join(args)

        # Find -F
        fs_match = re.search(r'-F(\S+)|-F\s+(\S+)', cmd_str)
        if fs_match:
            field_sep = fs_match.group(1) or fs_match.group(2)
            cmd_str = cmd_str.replace(fs_match.group(0), "").strip()

        # Find quoted content as pattern/action
        quote_match = re.search(r'''(['"])(.+?)\1''', cmd_str)
        if quote_match:
            pattern = quote_match.group(2)
            after_quote = cmd_str[quote_match.end():].strip()
            if after_quote:
                path = after_quote.split()[0] if after_quote.split() else ""
        else:
            parts = cmd_str.split()
            if len(parts) >= 2:
                pattern = parts[0]
                path = parts[1]
            elif len(parts) == 1:
                pattern = parts[0]

        if not path and not stdin:
            return CommandResult(stdout="", stderr="Error: awk 用法: awk <pattern|action> <文件>", exit_code=1)

        if not stdin:
            content = self.vfs.read_file(path)
            if content.startswith("Error:"):
                return CommandResult(stdout="", stderr=content, exit_code=1)
        else:
            content = stdin

        lines = content.split("\n")
        result_lines = []
        has_action = "{" in pattern and "}" in pattern

        if has_action:
            for line in lines:
                result = self._action(line, pattern, field_sep or None)
                if result is not None:
                    result_lines.append(result)
        elif pattern:
            for line in lines:
                if self._match(line, pattern, field_sep or None):
                    result_lines.append(line)

        return CommandResult(stdout="\n".join(result_lines), stderr="", exit_code=0)
