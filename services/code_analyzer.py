"""
AST 代码分析 + 智能预取
- CodeFileAnalyzer: 用 ast.parse 提取代码中硬编码的文件路径
- PrefetchEngine: 并行预取文件到缓存
"""

import ast
import os
import time
import logging
from typing import Set, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


class CodeFileAnalyzer:
    """
    静态分析 Python 代码，提取所有文件路径引用
    使用 AST 解析（安全，不执行代码）
    """

    _FILE_OPS = {
        'open', 'read_text', 'write_text', 'read_bytes', 'write_bytes',
        'iterdir', 'glob', 'rglob', 'stat', 'unlink', 'rename',
        'copy', 'move', 'exists', 'isfile', 'isdir',
    }

    @classmethod
    def extract_file_refs(cls, code: str) -> Set[str]:
        """
        从代码中提取所有可能引用的文件路径

        返回相对于 /workspace 的路径列表
        """
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return set()

        refs = set()

        for node in ast.walk(tree):
            # 1. open("data.json") 或 open(f"data_{i}.json")
            if isinstance(node, ast.Call):
                func = cls._get_call_name(node)
                if func == 'open':
                    if node.args:
                        path = cls._try_extract_string(node.args[0])
                        if path:
                            refs.add(path)

                # 2. pathlib: Path("data.json").read_text()
                if func in cls._FILE_OPS and isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Call):
                        if cls._get_call_name(node.func.value) == 'Path':
                            if node.func.value.args:
                                path = cls._try_extract_string(node.func.value.args[0])
                                if path:
                                    refs.add(path)

            # 3. f-string 常量部分提取
            if isinstance(node, ast.JoinedStr):
                prefix = cls._extract_fstring_prefix(node)
                if prefix:
                    refs.add(prefix)

        return refs

    @classmethod
    def extract_patterns(cls, code: str) -> Set[str]:
        """
        提取路径模式（用于通配符预取）

        例: f"data_{i}.json" → "data_" 前缀
        """
        patterns = set()
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return patterns

        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                parts = []
                for value in node.values:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        parts.append(value.value)
                if parts:
                    prefix = "".join(parts)
                    patterns.add(prefix)

        return patterns

    @classmethod
    def _get_call_name(cls, node: ast.Call) -> str:
        """获取函数名，如 open, Path, json.load"""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            return node.func.attr
        return ""

    @classmethod
    def _try_extract_string(cls, node) -> Optional[str]:
        """尝试从 AST 节点提取字符串值"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return None  # 变量，无法静态确定
        return None

    @classmethod
    def _extract_fstring_prefix(cls, node: ast.JoinedStr) -> Optional[str]:
        """从 f-string 提取常量前缀"""
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return "".join(parts) if parts else None


class PrefetchEngine:
    """
    智能预读引擎
    - 收到代码执行请求时，分析代码后并行预取文件到缓存
    - 最大 20 个文件，5 秒超时
    """

    MAX_PREFETCH_FILES = 20
    MAX_PREFETCH_TOTAL_BYTES = 50 * 1024 * 1024  # 50MB
    PREFETCH_TIMEOUT = 5.0  # 5 秒

    def __init__(self, mem_cache, disk_cache, meta_cache, storage, vfs):
        self.mem_cache = mem_cache
        self.disk_cache = disk_cache
        self.meta_cache = meta_cache
        self.storage = storage
        self.vfs = vfs

    def prefetch_files(self, refs: Set[str]) -> int:
        """
        并行预取文件到缓存

        Returns:
            成功预取的文件数
        """
        if not refs:
            return 0

        refs_list = list(refs)[:self.MAX_PREFETCH_FILES]
        fetched = 0
        total_bytes = 0

        def _fetch_one(ref: str) -> Optional[tuple]:
            """预取单个文件"""
            try:
                # 通过 VFS 解析路径
                cos_key, err = self.vfs._resolve_path(ref)
                if err:
                    return None

                # 检查缓存是否已有
                cache_key = cos_key
                if self.mem_cache.get(cache_key) is not None:
                    return None  # 已在内存缓存
                if self.disk_cache.get(cache_key) is not None:
                    return None  # 已在磁盘缓存

                # 从 COS 获取
                content = self.storage.get_file_content(cos_key)
                if content is None:
                    return None

                return (cos_key, content)
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=min(8, len(refs_list))) as executor:
            futures = {executor.submit(_fetch_one, ref): ref for ref in refs_list}

            for future in as_completed(futures, timeout=self.PREFETCH_TIMEOUT):
                try:
                    result = future.result()
                    if result:
                        cos_key, content = result
                        if total_bytes + len(content) > self.MAX_PREFETCH_TOTAL_BYTES:
                            continue
                        self.mem_cache.put(cos_key, content)
                        self.disk_cache.put(cos_key, content)
                        total_bytes += len(content)
                        fetched += 1
                except Exception:
                    continue

        return fetched
