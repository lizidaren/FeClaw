"""
LlmChunker — LLM 驱动的语义分块器

用 LLM 分析文本语义边界，替代传统的段落相似度/正则分块方法。
对连续文本（转录稿、网页纯文本）尤其有效。

流程:
1. 给每行加上行号 → 2. LLM 分析主题边界 → 3. 解析 JSON 输出 → 4. 转换为 Chunk

与 MarkdownChunker 接口兼容，可直接替换。
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime
from typing import List, Optional

from services.vfs_markdown_chunker import Chunk

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个文本分块助手。请分析以下文本，在主题切换处分割成语义连贯的内容块。

## 要求
- 每个块聚焦一个连续的话题
- 每个块字数在 300-2000 字之间
- 相邻块的主题应有明显区别
- 如果全文只有一个主题，则输出一个块

## 输入格式
每行开头的数字是行号，如 " 123| text"
行号供你标注分割边界用，不要把它们当作文本内容。

## 输出格式
```json
[
  {
    "start_line": 1,
    "end_line": 45,
    "topic": "核心主题（10字以内）"
  }
]
```

## 重要：不要重复原文内容
只输出 `start_line`、`end_line` 和 `topic` 就够了，**不要**包含 `content` 字段。系统会根据行号自动提取原文。

## 注意事项
- 逐行阅读，识别主题切换点
- 宁可多分几块，不要合并不同主题
- 不要切分完整的段落/对话（避免在对话中间切断）"""


class LlmChunker:
    """LLM 驱动的语义分块器"""

    def __init__(self, model: str = None):
        from services.llm_service import llm_service
        self._llm = llm_service
        self._model = model

    def chunk(self, content: str, file_path: str = "", agent_hash: str = "") -> List[Chunk]:
        """同步入口：asyncio.run 包装"""
        return asyncio.run(self._async_chunk(content, file_path, agent_hash))

    async def _async_chunk(
        self, content: str, file_path: str, agent_hash: str
    ) -> List[Chunk]:
        """异步执行 LLM 分块"""
        if not content or not content.strip():
            return []

        # 保存原文（用于根据行号提取内容）
        self._original_lines = content.splitlines()

        # 1. 添加行号
        numbered_lines = [
            f"{i+1:4d}| {line}" for i, line in enumerate(self._original_lines)
        ]
        numbered_text = "\n".join(numbered_lines)

        # 2. 短文本直接返回全文
        if len(self._original_lines) <= 3:
            chunk = Chunk(
                file_path=file_path,
                headings=["全文"],
                content=content,
                agent_hash=agent_hash,
            )
            return [chunk]

        # 3. 调用 LLM
        user_prompt = f"文本如下（共 {len(self._original_lines)} 行）：\n\n{numbered_text}"
        try:
            response = await self._llm.chat_with_tools(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                tools=[],
                request_type="semantic_chunk",
            )
            raw = response.get("content", "")
        except Exception as e:
            logger.warning(f"[LlmChunker] LLM 调用失败，回退到段落分块: {e}")
            return self._fallback(content, file_path, agent_hash)

        # 4. 解析 JSON（只含 start_line, end_line, topic）
        chunks_data = self._parse_json(raw)
        if chunks_data is None:
            logger.warning("[LlmChunker] JSON 解析失败，回退到段落分块")
            return self._fallback(content, file_path, agent_hash)

        # 5. 根据行号从原文提取内容，并检查 embedding 上限
        from services.vector_search_service import MAX_CHARS
        result = []
        for i, cd in enumerate(chunks_data):
            start = cd.get("start_line", 1) - 1  # 转 0-based
            end = cd.get("end_line", len(self._original_lines))
            clean = "\n".join(self._original_lines[start:end]).strip()
            if not clean:
                continue

            # 6. 超长块分割：超过 MAX_CHARS 则按行数平分
            if len(clean) > MAX_CHARS:
                logger.info(f"[LlmChunker] 块超长 ({len(clean)}c > {MAX_CHARS}c), 自动分割")
                lines_clean = clean.splitlines()
                mid = len(lines_clean) // 2
                sub_headings = cd.get("topic", f"第{i+1}节")
                for j, part_lines in enumerate([lines_clean[:mid], lines_clean[mid:]]):
                    part = "\n".join(part_lines).strip()
                    if part:
                        chunk = Chunk(
                            file_path=file_path,
                            headings=[f"{sub_headings}({j+1}/2)"],
                            content=part,
                            agent_hash=agent_hash,
                        )
                        result.append(chunk)
            else:
                chunk = Chunk(
                    file_path=file_path,
                    headings=[cd.get("topic", f"第{i+1}节")],
                    content=clean,
                    agent_hash=agent_hash,
                )
                result.append(chunk)

        if not result:
            logger.warning("[LlmChunker] 解析后无有效块，回退到段落分块")
            return self._fallback(content, file_path, agent_hash)

        logger.info(
            f"[LlmChunker] 完成: {len(result)} 块 (文件 {file_path}, {len(self._original_lines)} 行)"
        )
        return result

    def _parse_json(self, raw: str) -> Optional[List[dict]]:
        """从 LLM 输出中提取 JSON"""
        # 尝试 markdown 代码块
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
        if m:
            raw = m.group(1)

        # 尝试直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 尝试找到 JSON 数组的开始和结尾
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end+1])
            except json.JSONDecodeError:
                pass

        return None

    def _fallback(self, content: str, file_path: str, agent_hash: str) -> List[Chunk]:
        """回退到简单的段落分块"""
        from services.vfs_markdown_chunker import MarkdownChunker
        chunker = MarkdownChunker()
        return chunker.chunk(content, file_path, agent_hash)
