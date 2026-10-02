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

        # Q21/L6：`old` 正则来自 Agent 的 bash 命令、`content` 是任意文件内容，
        # 此前无超时无上限 —— 灾难性回溯可阻塞线程池（纯 DoS）。这里做有界缓解：
        # 限制输入大小 + 限制 pattern 长度 + 捕获 re.error（完整超时封顶见报告）。
        _MAX_SED_INPUT = 2 * 1024 * 1024
        _MAX_PATTERN = 512
        if len(content) > _MAX_SED_INPUT:
            return CommandResult(stdout="", stderr="Error: sed 输入超过 2MB 上限", exit_code=1)

        def _safe_sub(pattern: str, repl: str, line: str, count: int = 0) -> str:
            if len(pattern) > _MAX_PATTERN:
                return line
            try:
                if count:
                    return re.sub(pattern, repl, line, count=count)
                return re.sub(pattern, repl, line)
            except re.error:
                return line

        # s/old/new/ - substitution
        if script.startswith("s/"):
            parts = script[2:].rsplit("/", 2)
            if len(parts) >= 2:
                old = parts[0]
                new = parts[1]
                global_replace = len(parts) > 2 and parts[2] == "g"

                for line in lines:
                    result_lines.append(_safe_sub(old, new, line, count=0 if global_replace else 1))
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
                    result_lines.append(_safe_sub(old, new, line, count=0 if global_replace else 1))
                else:
                    result_lines.append(line)
            return CommandResult(stdout="\n".join(result_lines), stderr="", exit_code=0)

        return CommandResult(stdout=content, stderr="", exit_code=0)
