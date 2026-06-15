"""
sed command - stream editor for filtering and transforming text.
"""
import re
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class SedCommand(BuiltinCommand):
    """sed - stream editor for filtering and transforming text."""

    name = "sed"
    help_text = "sed <pattern|action> <file> - stream editor"

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute sed."""
        if not args:
            return CommandResult(stdout="", stderr="Error: sed 用法: sed <pattern|action> <文件>", exit_code=1)

        path = ""
        script = ""

        cmd_str = " ".join(args)

        # Find quoted content as script
        quote_match = re.search(r'''(['"])(.+?)\1''', cmd_str)
        if quote_match:
            script = quote_match.group(2)
            after_quote = cmd_str[quote_match.end():].strip()
            if after_quote:
                path = after_quote.split()[0] if after_quote.split() else ""
        else:
            parts = cmd_str.split()
            if len(parts) >= 2:
                script = parts[0]
                path = parts[1]
            elif len(parts) == 1:
                script = parts[0]

        if not path and not stdin:
            return CommandResult(stdout="", stderr="Error: sed 用法: sed <pattern|action> <文件>", exit_code=1)

        if not stdin:
            content = self.vfs.read_file(path)
            if content.startswith("Error:"):
                return CommandResult(stdout="", stderr=content, exit_code=1)
        else:
            content = stdin

        lines = content.split("\n")
        result_lines = []

        # s/old/new/ - substitution
        if script.startswith("s/"):
            parts = script[2:].rsplit("/", 2)
            if len(parts) >= 2:
                old = parts[0]
                new = parts[1]
                global_replace = len(parts) > 2 and parts[2] == "g"

                for line in lines:
                    if global_replace:
                        result_lines.append(re.sub(old, new, line))
                    else:
                        result_lines.append(re.sub(old, new, line, count=1))
                return CommandResult(stdout="\n".join(result_lines), stderr="", exit_code=0)

        # Nd - delete Nth line
        nd_match = re.match(r'(\d+)d', script)
        if nd_match:
            line_num = int(nd_match.group(1))
            for i, line in enumerate(lines, 1):
                if i != line_num:
                    result_lines.append(line)
            return CommandResult(stdout="\n".join(result_lines), stderr="", exit_code=0)

        # /pattern/d - delete matching lines
        if script.startswith("/") and script.endswith("/d"):
            pattern = script[1:-2]
            for line in lines:
                if pattern not in line:
                    result_lines.append(line)
            return CommandResult(stdout="\n".join(result_lines), stderr="", exit_code=0)

        # /pattern/s/old/new/ - substitute on matching lines
        pattern_match = re.match(r'/(.+)/s/(.+)/(.+)/(.*)', script)
        if pattern_match:
            pat, old, new, flags = pattern_match.groups()
            global_replace = 'g' in flags
            for line in lines:
                if pat in line:
                    if global_replace:
                        result_lines.append(re.sub(old, new, line))
                    else:
                        result_lines.append(re.sub(old, new, line, count=1))
                else:
                    result_lines.append(line)
            return CommandResult(stdout="\n".join(result_lines), stderr="", exit_code=0)

        return CommandResult(stdout=content, stderr="", exit_code=0)
