"""
VFS Markdown Chunker - 按 Markdown 标题层级分块

输入: markdown 文本字符串
输出: List[Chunk] 每个块包含 heading_hierarchy, content, metadata
"""

import hashlib
import re
import logging
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# 单块最大字符数（≈2000 tokens）
MAX_CHUNK_CHARS = 4000


class Chunk:
    """分块数据结构"""

    def __init__(
        self,
        file_path: str,
        headings: List[str],
        content: str,
        agent_hash: str = "",
    ):
        self.chunk_id = hashlib.md5(content[:50].encode()).hexdigest()[:16]
        self.file_path = file_path
        self.agent_hash = agent_hash
        self.headings = headings
        self.content = content
        self.created_at = datetime.utcnow().isoformat() + "Z"
        self.updated_at = self.created_at

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "file_path": self.file_path,
            "agent_hash": self.agent_hash,
            "headings": self.headings,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MarkdownChunker:
    """Markdown 分块器"""

    # 匹配 h2 (##) 和 h3 (###) 标题的正则
    HEADING_PATTERN = re.compile(r'^(#{2,3})\s+(.+)$', re.MULTILINE)

    def chunk(self, content: str, file_path: str, agent_hash: str = "") -> List[Chunk]:
        """
        将 Markdown 文本按标题分块

        Args:
            content: Markdown 文本
            file_path: 来源文件路径
            agent_hash: Agent hash

        Returns:
            分块列表
        """
        if not content or not content.strip():
            return []

        # 查找所有标题位置
        matches = list(self.HEADING_PATTERN.finditer(content))
        if not matches:
            # 无标题：整体作为一块（若超长则按段落切分）
            return self._chunk_by_paragraph(content, file_path, agent_hash)

        chunks = []
        current_heading_hierarchy = []

        for i, match in enumerate(matches):
            start_pos = match.start()

            # 上一块的结束 = 当前标题的开始
            if i > 0:
                prev_end = start_pos
                chunk_content = content[matches[i - 1].start():prev_end].strip()

                # 使用上一个标题（matches[i-1]）而非当前标题构建 heading hierarchy
                prev_match = matches[i - 1]
                prev_level = len(prev_match.group(1))
                prev_heading_text = prev_match.group(2).strip()
                if prev_level == 2:
                    current_heading_hierarchy = [prev_heading_text]
                elif prev_level == 3:
                    if len(current_heading_hierarchy) >= 1:
                        current_heading_hierarchy = [current_heading_hierarchy[0], prev_heading_text]
                    else:
                        current_heading_hierarchy = [prev_heading_text]

                # 处理超长块
                sub_chunks = self._split_long_chunk(
                    chunk_content, file_path, agent_hash,
                    list(current_heading_hierarchy)
                )
                chunks.extend(sub_chunks)
            else:
                # 第一块从文件开头到第一个标题之间的内容（前言）
                if start_pos > 0:
                    preamble = content[:start_pos].strip()
                    if preamble:
                        # 前言作为一个独立块
                        preamble_chunks = self._chunk_by_paragraph(
                            preamble, file_path, agent_hash, []
                        )
                        chunks.extend(preamble_chunks)

                # 第一块从第一个标题开始
                continue

        # 最后一块：从最后一个标题到文件末尾，使用最后一个标题的层级
        if matches:
            last_match = matches[-1]
            last_level = len(last_match.group(1))
            last_heading = last_match.group(2).strip()
            if last_level == 2:
                current_heading_hierarchy = [last_heading]
            elif last_level == 3:
                if len(current_heading_hierarchy) >= 1:
                    current_heading_hierarchy = [current_heading_hierarchy[0], last_heading]
                else:
                    current_heading_hierarchy = [last_heading]

            last_content = content[matches[-1].start():].strip()
            sub_chunks = self._split_long_chunk(
                last_content, file_path, agent_hash,
                list(current_heading_hierarchy)
            )
            chunks.extend(sub_chunks)

        return chunks

    def _chunk_by_paragraph(
        self,
        content: str,
        file_path: str,
        agent_hash: str,
        headings: Optional[List[str]] = None,
    ) -> List[Chunk]:
        """按段落切分（无标题或前言区域）"""
        if headings is None:
            headings = []

        if len(content) <= MAX_CHUNK_CHARS:
            return [Chunk(file_path, headings, content, agent_hash)]

        # 超长时按空行切分
        paragraphs = re.split(r'\n\s*\n', content)
        result = []
        buffer = ""
        for para in paragraphs:
            if len(buffer) + len(para) > MAX_CHUNK_CHARS and buffer:
                result.append(Chunk(file_path, list(headings), buffer.strip(), agent_hash))
                buffer = para
            else:
                buffer += "\n\n" + para if buffer else para

        if buffer.strip():
            result.append(Chunk(file_path, list(headings), buffer.strip(), agent_hash))

        return result

    def _split_long_chunk(
        self,
        content: str,
        file_path: str,
        agent_hash: str,
        headings: List[str],
    ) -> List[Chunk]:
        """拆分超长块"""
        if len(content) <= MAX_CHUNK_CHARS:
            return [Chunk(file_path, headings, content, agent_hash)]
        # 超长则按段落切分
        paragraphs = re.split(r'\n\s*\n', content)
        result = []
        buffer = ""
        for para in paragraphs:
            if len(buffer) + len(para) > MAX_CHUNK_CHARS and buffer:
                result.append(Chunk(file_path, list(headings), buffer.strip(), agent_hash))
                buffer = para
            else:
                buffer += "\n\n" + para if buffer else para
        if buffer.strip():
            result.append(Chunk(file_path, list(headings), buffer.strip(), agent_hash))
        return result
