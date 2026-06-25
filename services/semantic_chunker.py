"""
SemanticChunker — NotebookLM-inspired Semantic Chunking

Pure-NLP chunker based on adjacent-paragraph embedding similarity.
No VLM dependency; fast and cheap (DashScope text-embedding-v4).

Algorithm:
  1. Split text into paragraphs (double newline).
  2. Embed each paragraph via EmbeddingService.embed_batch (batch=10).
  3. Compute cosine similarity between adjacent paragraphs.
  4. Adaptive threshold = mean(sim) + THRESHOLD_MULTIPLIER * std(sim).
  5. Build L1 chunks by splitting where sim < threshold.
  6. Cohesion gate: forward-merge chunks shorter than MIN_CHUNK_CHARS.
  7. Force-split chunks exceeding MAX_CHUNK_CHARS.
  8. L2 = pairs of L1 for broader-context retrieval.

Usage:
    from services.semantic_chunker import chunk, Chunk
    chunks = await chunk(long_text)
    semantic = [c for c in chunks if c.level == 1]
"""

import re
import math
import logging
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

# ── Parameters ─────────────────────────────────────────────

MIN_CHUNK_CHARS = 100          # < this → merge into next non-short chunk
MAX_CHUNK_CHARS = 2000         # > this → force-split (paragraph or hard cut)
THRESHOLD_MULTIPLIER = 0.5     # threshold = mean + multiplier * std
EMBEDDING_MODEL = "text-embedding-v4"

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


# ── Chunk dataclass ────────────────────────────────────────

@dataclass
class Chunk:
    """A single chunk of text at a given semantic level.

    level: 0 = raw paragraph, 1 = semantic unit, 2 = super-chunk (pair of L1)
    idx:   position within the level's sequence (0-based)
    """
    text: str
    level: int
    idx: int

    def __repr__(self) -> str:
        snippet = self.text[:40].replace("\n", " ")
        return f"Chunk(L{self.level}#{self.idx}, {len(self.text)}c, {snippet!r}…)"


# ── Helpers ────────────────────────────────────────────────

def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity; 0.0 for empty / mismatched vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _split_paragraphs(text: str) -> List[str]:
    """Split on blank lines; trim each; drop empties."""
    return [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]


def _force_split(text: str, max_chars: int) -> List[str]:
    """Force-split an overlong chunk: try paragraphs first, then hard cut."""
    if len(text) <= max_chars:
        return [text]
    paras = _split_paragraphs(text)
    if len(paras) > 1:
        return paras
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


# ── Main entry point ───────────────────────────────────────

async def chunk(text: str) -> List[Chunk]:
    """
    Split `text` into multi-level semantic chunks (L0 / L1 / L2).

    Returns chunks across all three levels in a single flat list.
    Empty / whitespace-only input → empty list.
    Safe degradation: on embedding failure → returns L0 (raw paragraphs) only.
    """
    if not text or not text.strip():
        return []

    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return []

    # ── L0: raw paragraphs (always returned) ──
    l0 = [Chunk(text=p, level=0, idx=i) for i, p in enumerate(paragraphs)]

    # Single paragraph: still respect MAX_CHUNK_CHARS; emit one chunk per level
    if len(paragraphs) == 1:
        only = paragraphs[0]
        parts = _force_split(only, MAX_CHUNK_CHARS)
        l0_only = [Chunk(text=p, level=0, idx=i) for i, p in enumerate(parts)]
        l1_only = [Chunk(text=p, level=1, idx=i) for i, p in enumerate(parts)]
        l2_only = [
            Chunk(text="\n\n".join(parts[i:i + 2]), level=2, idx=i // 2)
            for i in range(0, len(parts), 2)
        ]
        return l0_only + l1_only + l2_only

    # ── Embed all paragraphs in one batched call ──
    from services.embedding_service import EmbeddingService
    try:
        embeddings = await EmbeddingService().embed_batch(paragraphs)
    except Exception as e:
        logger.warning(f"SemanticChunker: embed_batch failed, returning L0 only: {e}")
        return l0

    # Any empty embedding → safe degradation (don't risk garbage splits)
    if any(not e for e in embeddings):
        logger.warning("SemanticChunker: some embeddings empty, returning L0 only")
        return l0

    # ── Adjacent cosine similarities ──
    sims = [_cosine(embeddings[i], embeddings[i + 1]) for i in range(len(embeddings) - 1)]
    threshold = _mean(sims) + THRESHOLD_MULTIPLIER * _std(sims)

    # ── Build L1 paragraph groups: split where sim < threshold ──
    l1_groups: List[List[str]] = [[]]
    for i, p in enumerate(paragraphs):
        if i > 0 and sims[i - 1] < threshold and l1_groups[-1]:
            l1_groups.append([])
        l1_groups[-1].append(p)

    # ── Cohesion gate: short chunks forward-merge into next non-short chunk ──
    merged: List[List[str]] = []
    pending: List[str] = []
    for group in l1_groups:
        text_group = "\n\n".join(group)
        if len(text_group) < MIN_CHUNK_CHARS:
            pending.extend(group)
        else:
            merged.append(pending + group)
            pending = []
    if pending:
        # Trailing shorts → append to last chunk (or start one if list is empty)
        if merged:
            merged[-1].extend(pending)
        else:
            merged.append(pending)

    # ── Force-split any chunk still exceeding MAX_CHUNK_CHARS ──
    flat: List[str] = []
    for group in merged:
        flat.extend(_force_split("\n\n".join(group), MAX_CHUNK_CHARS))

    l1 = [Chunk(text=t, level=1, idx=i) for i, t in enumerate(flat)]

    # ── L2: super-chunks (concatenate pairs of L1 for broader context) ──
    l2 = [
        Chunk(text="\n\n".join(flat[i:i + 2]), level=2, idx=i // 2)
        for i in range(0, len(flat), 2)
    ]

    return l0 + l1 + l2
