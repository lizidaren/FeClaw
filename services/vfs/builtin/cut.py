"""
cut command - cut out selected portions of each line of a file.
"""
from typing import List, Tuple
from ..result import CommandResult
from ..builtin_base import BuiltinCommand


class CutCommand(BuiltinCommand):
    """cut - cut out selected portions of each line."""

    name = "cut"
    help_text = "cut [-d delim] [-f fields] [-c chars] <file> - cut out portions of lines"

    def _parse_fields(self, fields_str: str) -> List[Tuple[int, int]]:
        """Parse field specification like '1', '1-3', '1,3,5'."""
        ranges = []
        for part in fields_str.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                ranges.append((int(start), int(end) if end else -1))
            else:
                ranges.append((int(part), int(part)))
        return ranges

    def _cut_by_fields(self, line: str, fields: List[Tuple[int, int]], delimiter: str) -> str:
        """Cut by field."""
        parts = line.split(delimiter)
        result = []
        for start, end in fields:
            if end == -1:
                end = len(parts)
            for i in range(start, end + 1):
                if 0 < i <= len(parts):
                    result.append(parts[i - 1])
        return delimiter.join(result)

    def _cut_by_chars(self, line: str, chars: List[Tuple[int, int]]) -> str:
        """Cut by character position."""
        result = []
        for start, end in chars:
            if end == -1:
                end = len(line)
            for i in range(start, end + 1):
                if 0 < i <= len(line):
                    result.append(line[i - 1])
        return "".join(result)

    def execute(self, args: list, stdin: str) -> CommandResult:
        """Execute cut."""
        if not args:
            return CommandResult(stdout="", stderr="Error: 用法: cut [-d delim] [-f fields] [-c chars] <文件>", exit_code=1)

        delimiter = "\t"
        fields = None
        chars = None
        path = ""

        i = 0
        while i < len(args):
            p = args[i]
            if p == "-d" and i + 1 < len(args):
                delimiter = args[i + 1]
                i += 2
            elif p == "-f" and i + 1 < len(args):
                fields = self._parse_fields(args[i + 1])
                i += 2
            elif p == "-c" and i + 1 < len(args):
                chars = self._parse_fields(args[i + 1])
                i += 2
            elif p.startswith("-d"):
                delimiter = p[2:]
                i += 1
            elif p.startswith("-f"):
                fields = self._parse_fields(p[2:])
                i += 1
            elif p.startswith("-c"):
                chars = self._parse_fields(p[2:])
                i += 1
            else:
                path = p
                i += 1

        if not path and not stdin:
            return CommandResult(stdout="", stderr="Error: 用法: cut <文件>", exit_code=1)

        if not stdin:
            content = self.vfs.read_file(path)
            if content.startswith("Error:"):
                return CommandResult(stdout="", stderr=content, exit_code=1)
        else:
            content = stdin

        if fields is None and chars is None:
            return CommandResult(stdout="", stderr="Error: cut 必须指定 -f 或 -c", exit_code=1)

        lines = content.split("\n")
        result_lines = []

        if chars is not None:
            for line in lines:
                result = self._cut_by_chars(line, chars)
                if result:
                    result_lines.append(result)
        elif fields is not None:
            for line in lines:
                result = self._cut_by_fields(line, fields, delimiter)
                if result:
                    result_lines.append(result)

        return CommandResult(stdout="\n".join(result_lines), stderr="", exit_code=0)
