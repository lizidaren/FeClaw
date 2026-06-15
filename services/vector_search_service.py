"""
向量搜索服务
- Embedding: Qwen3 text-embedding-v4 (1024d) via DashScope OpenAI 兼容接口
- 存储: COS 向量存储桶 CosVectorsClient
"""

import asyncio
import contextlib
import functools
import json
import logging
import os
import socket
import sqlite3
from typing import List, Optional

import httpx

from config import settings
from services.model_registry import resolve as _reg_resolve

logger = logging.getLogger(__name__)

# 向量存储桶名称
VECTOR_BUCKET = "firstentrance-gzvec-1257148458"
# 向量维度
VECTOR_DIMENSION = 1024
# COS 向量存储域名 & IP（WSL DNS 兜底）
VECTOR_ENDPOINT = "vectors.ap-guangzhou.coslake.com"
VECTOR_ENDPOINT_SUFFIX = "." + VECTOR_ENDPOINT
VECTOR_IP = "222.79.123.48"

_is_wsl = "microsoft" in __import__("platform").uname().release.lower()
_original_getaddrinfo = socket.getaddrinfo


@contextlib.contextmanager
def _vector_dns_scope():
    """局部 DNS patch：在 socket 层将 VECTOR_ENDPOINT 解析为 VECTOR_IP。

    仅 WSL 下生效。URL 保持域名不变（SSL 证书匹配），只在建立 TCP 连接时
    将域名解析指向硬编码 IP。退出 scope 后自动恢复。
    """
    if not _is_wsl:
        yield
        return

    @functools.wraps(_original_getaddrinfo)
    def _patched(host, port, family=0, type_=0, proto=0, flags=0):
        if host is not None:
            host_str = host.decode() if isinstance(host, bytes) else host
            if host_str == VECTOR_ENDPOINT or host_str.endswith(VECTOR_ENDPOINT_SUFFIX):
                host = VECTOR_IP
        return _original_getaddrinfo(host, port, family, type_, proto, flags)

    socket.getaddrinfo = _patched
    try:
        yield
    finally:
        socket.getaddrinfo = _original_getaddrinfo


class _VectorClientWrapper:
    """CosVectorsClient 包装器：每次方法调用自动套上 _vector_dns_scope。

    WSL 下 COS SDK 无法解析自定义 DNS 域名，需要在建立连接时临时劫持 DNS。
    非 WSL 环境直接透传。
    """

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if _is_wsl and callable(attr):
            @functools.wraps(attr)
            def wrapped(*args, **kwargs):
                with _vector_dns_scope():
                    return attr(*args, **kwargs)
            return wrapped
        return attr


# Embedding API
EMBEDDING_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
EMBEDDING_MODEL = "text-embedding-v4"
# Embedding 限制
MAX_BATCH_SIZE = 10
MAX_TOKENS = 8000
MAX_RETRIES = 3
RETRY_DELAY = 1.0
# 文本 token 估算：保守取 1 token ≈ 2 chars
CHARS_PER_TOKEN = 2
MAX_CHARS = MAX_TOKENS * CHARS_PER_TOKEN


TEXTBOOK_SUBJECT_INDEXES = [
    "idx-public-chemistry-textbook",
    "idx-public-math-rja-textbook",
    "idx-public-math-xj-textbook",
    "idx-public-physics-textbook",
    "idx-public-biology-textbook",
    "idx-public-chinese-textbook",
    "idx-public-english-textbook",
    "idx-public-geography-textbook",
    "idx-public-politics-textbook",
]


class VectorSearchService:
    """向量搜索服务"""

    __slots__ = ('agent_hash', '_client')

    def __init__(self, agent_hash: str = None):
        self.agent_hash = agent_hash
        self._client = None

    # ----- COS Client -----

    def _get_client(self):
        """懒加载 COS Vector Client"""
        if self._client is not None:
            return self._client

        from qcloud_cos import CosConfig, CosVectorsClient

        config = CosConfig(
            Region="ap-guangzhou",
            SecretId=settings.TENCENT_COS_SECRET_ID,
            SecretKey=settings.TENCENT_COS_SECRET_KEY,
            Endpoint=VECTOR_ENDPOINT,
            Scheme="https",
        )
        # WSL: 包装客户端，每次方法调用自动套上 DNS scope（保持 URL 域名不变）
        with _vector_dns_scope():
            raw = CosVectorsClient(config)
        self._client = _VectorClientWrapper(raw) if _is_wsl else raw
        return self._client

    def _get_index_name(self, prefix: str) -> str:
        """生成 index 名: idx-{agent_hash}-{prefix} 或 idx-{prefix}"""
        if self.agent_hash:
            return f"idx-{self.agent_hash}-{prefix}"
        return f"idx-{prefix}"

    def _ensure_index(self, index: str):
        """确保 index 存在，不存在则自动创建（1024d, float32, cosine）"""
        try:
            client = self._get_client()
            _, data = client.get_index(Bucket=VECTOR_BUCKET, Index=index)
            if data and isinstance(data, dict) and "indexName" in data:
                return  # 已存在
        except Exception as e:
            if "not found" in str(e).lower():
                pass  # index 不存在，后面创建
            else:
                logger.warning(f"_ensure_index check_index failed: {e}")
                return  # 其他错误直接返回

        try:
            client = self._get_client()
            client.create_index(
                Bucket=VECTOR_BUCKET,
                Index=index,
                DataType="float32",
                Dimension=VECTOR_DIMENSION,
                DistanceMetric="cosine",
            )
            logger.info("Created index %s", index)
        except Exception as e:
            logger.warning("create_index %s failed: %s", index, e)

    # ----- Embedding -----

    async def _call_embedding_api(self, texts: List[str]) -> Optional[List[List[float]]]:
        """调用 DashScope Embedding API（同步 httpx，最大 10 条/批）"""
        api_key = settings.QWEN_API_KEY or os.getenv("QWEN_API_KEY")
        if not api_key:
            logger.error("QWEN_API_KEY not configured")
            return None

        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        EMBEDDING_API_URL,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": EMBEDDING_MODEL,
                            "input": texts,
                            "dimensions": VECTOR_DIMENSION,
                        },
                    )

                    if resp.status_code == 429:
                        if attempt < MAX_RETRIES - 1:
                            logger.warning("Embedding API 429, retrying in %.1fs", RETRY_DELAY)
                            await asyncio.sleep(RETRY_DELAY)
                            continue
                        logger.error("Embedding API 429, exhausted retries")
                        return None

                    resp.raise_for_status()
                    data = resp.json()

                    embeddings = [item.get("embedding", []) for item in data.get("data", [])]
                    if not embeddings:
                        logger.error("Empty embedding response data")
                        return None
                    return embeddings

            except Exception as e:
                logger.warning("Embedding API call failed (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    logger.error("Embedding API exhausted retries")
                    return None

        return None

    async def embed(self, text: str) -> List[float]:
        """单条文本 → 1024d 向量"""
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]

        result = await self._call_embedding_api([text])
        return result[0] if result else []

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量向量化（最多 10 条一批）"""
        all_embeddings: List[List[float]] = []

        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i:i + MAX_BATCH_SIZE]
            batch = [t[:MAX_CHARS] if len(t) > MAX_CHARS else t for t in batch]

            result = await self._call_embedding_api(batch)
            if result:
                all_embeddings.extend(result)
            else:
                all_embeddings.extend([[] for _ in batch])

        return all_embeddings

    # ----- Search -----

    async def search(self, query: str, index: str = None, top_k: int = 5) -> List[dict]:
        """搜索相似内容

        1. embed(query) → vec
        2. 确定搜索哪些 index
           - 指定 index 则搜指定 index
           - 有 agent_hash：搜 idx-{hash}-kb + idx-public-kb
           - 无 agent_hash：搜 idx-public-kb
        3. COS query_vectors() 搜索
        4. 按 score 降序返回 top_k
        """
        vec = await self.embed(query)
        if not vec:
            logger.warning("embed() returned empty vector for query=%r", query[:60])
            return []

        # 确定搜索 index 列表
        if index:
            indexes = [index]
        else:
            indexes = []
            if self.agent_hash:
                indexes.append(self._get_index_name("kb"))
                indexes.append(self._get_index_name("conv"))
            indexes.append("idx-public-kb")

        # 并行搜索所有 index
        tasks = [self._query_index(vec, idx, top_k) for idx in indexes]
        results_list = await asyncio.gather(*tasks)

        # 合并、标记来源、加权、排序、截断
        source_map = {
            self._get_index_name("kb"): "knowledge_base",
            self._get_index_name("conv"): "conversation_memory",
        }
        merged = []
        for idx, results in zip(indexes, results_list):
            source = source_map.get(idx, "unknown")
            for r in results:
                r["source"] = source
                # 根据来源加权
                if source == "public_knowledge":
                    r["score"] = min(1.0, r["score"] * 1.1)
                elif source == "conversation_memory":
                    r["score"] = r["score"] * 0.9
                merged.append(r)
        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return merged[:top_k]

    async def search_with_rerank(
        self, query: str, index: str = None, top_k: int = 5, rerank: bool = True,
    ) -> List[dict]:
        """搜索 + 可选重排序

        Args:
            query: 查询文本
            index: 指定索引（None 则自动选择）
            top_k: 最终返回结果数
            rerank: 是否启用重排序（True 时先取 top_k*10 候选再用 RerankService 精排）

        Returns:
            排序后的文档列表，每条含 rerank_score（如果 rerank=True）
        """
        if rerank:
            # 先取 50 条候选，再用 reranker 精排到 top_k
            candidate_k = max(top_k, 50)
            candidates = await self.search(query, index=index, top_k=candidate_k)
            if not candidates:
                return []

            from services.rerank_service import RerankService

            reranker = RerankService()
            # 构造 documents 格式：{text, metadata, ...}
            docs = []
            for c in candidates:
                docs.append({
                    "text": c.get("metadata", {}).get("text", ""),
                    "metadata": c.get("metadata", {}),
                    "key": c.get("key", ""),
                    "score": c.get("score", 0),
                    "source": c.get("source", ""),
                })

            reranked = await reranker.rerank(query, docs, top_n=top_k)
            return reranked

        return await self.search(query, index=index, top_k=top_k)

    async def search_gaokao(self, query: str, top_k: int = 5) -> List[dict]:
        """搜索 idx-public-gaokao-kb（高考题库），带重排序

        如果 idx-public-gaokao-kb 不存在或为空，回退到 idx-public-kb 并按 source 过滤。
        """
        from services.rerank_service import RerankService

        # 尝试从 idx-public-gaokao-kb 搜索
        try:
            candidates = await self.search(query, index="idx-public-gaokao-kb", top_k=50)
            gaokao_candidates = [
                c for c in candidates
                if c.get("metadata", {}).get("source", "") in ("53-gaokao", "gaokao-bench", "gaokao")
            ]
            if gaokao_candidates:
                reranker = RerankService()
                docs = self._build_rerank_docs(gaokao_candidates)
                return await reranker.rerank(query, docs, top_n=top_k)
        except Exception as e:
            logger.warning("search_gaokao on idx-public-gaokao-kb failed: %s, falling back", e)

        # 回退：从 idx-public-kb 搜索并过滤 gaokao 来源
        candidates = await self.search(query, index="idx-public-kb", top_k=50)
        gaokao_candidates = [
            c for c in candidates
            if c.get("metadata", {}).get("source", "") in ("53-gaokao", "gaokao-bench", "gaokao")
        ]
        if not gaokao_candidates:
            logger.info("search_gaokao: no gaokao results found in idx-public-kb either")
            return []

        reranker = RerankService()
        docs = self._build_rerank_docs(gaokao_candidates)
        return await reranker.rerank(query, docs, top_n=top_k)

    @staticmethod
    def _build_rerank_docs(candidates: List[dict]) -> List[dict]:
        """Build rerank document list from search candidates."""
        return [
            {
                "text": c.get("metadata", {}).get("text", ""),
                "metadata": c.get("metadata", {}),
                "key": c.get("key", ""),
                "score": c.get("score", 0),
                "source": c.get("source", ""),
            }
            for c in candidates
        ]

    async def search_textbook(self, query: str, top_k: int = 5) -> List[dict]:
        """搜索 idx-public-textbook-kb（教材知识库），带重排序
        迁移完成后将完全使用 idx-public-textbook-kb，目前以 idx-public-kb 为回退
        """
        from services.rerank_service import RerankService

        try:
            candidates = await self.search(query, index="idx-public-textbook-kb", top_k=50)
            if candidates:
                reranker = RerankService()
                docs = self._build_rerank_docs(candidates)
                return await reranker.rerank(query, docs, top_n=top_k)
        except Exception as e:
            logger.warning("search_textbook: idx-public-textbook-kb failed, falling back: %s", e)

        candidates = await self.search(query, index="idx-public-kb", top_k=50)
        _GAOKAO_SOURCES = ("gaokao-bench",)
        textbook_candidates = [
            c for c in candidates
            if c.get("metadata", {}).get("source", "") not in _GAOKAO_SOURCES
        ]
        if not textbook_candidates:
            return []
        reranker = RerankService()
        docs = self._build_rerank_docs(textbook_candidates)
        return await reranker.rerank(query, docs, top_n=top_k)

    async def search_public(self, query: str, top_k: int = 5) -> List[dict]:
        """多源分层搜索：合并库 top_10 + 教材 top_50 + 高考 top_50 → 去重 → 重排序"""
        from services.rerank_service import RerankService

        tasks = [
            self.search(query, index="idx-public-kb", top_k=10),
            self.search(query, index="idx-public-textbook-kb", top_k=50),
            self.search(query, index="idx-public-gaokao-kb", top_k=50),
            self.search(query, index="idx-public-math-trends", top_k=50),
        ]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        all_docs = []
        seen = set()
        for results in results_list:
            if isinstance(results, Exception):
                logger.warning("search_public source failed: %s", results)
                continue
            for r in results:
                k = r.get("key", "")
                if k and k not in seen:
                    seen.add(k)
                    text = r.get("metadata", {}).get("text", "")
                    all_docs.append({
                        "text": text,
                        "key": r.get("key", ""),
                        "score": r.get("score", 0),
                        "source": r.get("source", ""),
                    })

        # 3. SQLite kaoxiang kaodian 结构化数据查询
        try:
            kd_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'kaoxiang.db')
            if os.path.exists(kd_path):
                conn = sqlite3.connect(kd_path)
                cursor = conn.cursor()
                like = f'%{query}%'
                cursor.execute(
                    'SELECT kaodian, exam_examples, exam_trend, exam_frequency, core_competency, topic_full '
                    'FROM kaoxiang_kaodian WHERE kaodian LIKE ? OR exam_trend LIKE ? OR topic_full LIKE ? LIMIT 10',
                    (like, like, like)
                )
                for row in cursor.fetchall():
                    name, examples_raw, trend, freq, competency, topic = row
                    examples = ', '.join(json.loads(examples_raw or '[]')[:5])
                    text = f'【考频数据】考点「{name}」近4年考频{freq}，核心素养{competency}，考向：{trend}。真题示例：{examples}'
                    key = f'kaoxiang-sqlite-{name}'
                    if key not in seen:
                        seen.add(key)
                        all_docs.append({
                            'text': text,
                            'key': key,
                            'score': 0.5,
                            'source': 'kaoxiang_sqlite',
                            'metadata': {'type': 'kaodian', 'source': 'kaoxiang_sqlite'},
                        })
                conn.close()
        except Exception as e:
            logger.warning('kaoxiang_sqlite search failed: %s', e)

        if not all_docs:
            return []

        reranker = RerankService()
        return await reranker.rerank(query, all_docs, top_n=top_k)

    async def search_quality_textbook(self, query: str, top_k: int = 50, agent_hash: str = None, min_score: float = 0.0) -> List[dict]:
        """Search all 9 high-quality per-subject textbook indexes + trends + agent KB. Parallel, merge, dedup, return."""
        vec = await self.embed(query)
        if not vec:
            return []
        tasks = [self._query_index(vec, idx, top_k) for idx in TEXTBOOK_SUBJECT_INDEXES]
        if agent_hash:
            tasks.append(self._query_index(vec, f"idx-{agent_hash}-kb", 10))
        tasks.append(self._query_index(vec, "idx-public-math-trends", top_k))

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        merged = []
        seen = set()
        for results in results_list:
            if isinstance(results, Exception):
                logger.warning("search_quality_textbook source failed: %s", results)
                continue
            for r in results:
                k = r.get("key", "")
                score = r.get("score", 0)
                if score < min_score:
                    continue
                if k and k not in seen:
                    seen.add(k)
                    merged.append(r)

        return merged

    # ── Qwen subject routing ──────────────────────────────────────────
    _QWEN_ROUTING_PROMPT = """你是AI知识库的路由器。判断用户搜索内容属于哪个学科，分配搜索权重。

可用学科和对应索引：
- chemistry: 化学
- math: 数学（人教A版）
- math-xj: 数学（湘教版）
- physics: 物理
- biology: 生物
- chinese: 语文
- english: 英语
- geography: 地理
- politics: 政治

权重决定各学科搜索多少结果。总和可以不是1，但不要差异过大。

返回JSON：
{"routes": [{"index": "chemistry", "weight": 0.6}, {"index": "biology", "weight": 0.4}], "reasoning": "涉及葡萄糖，属于化学有机物和生物代谢"}

规则：
- 精确匹配（如"复数的三角形式"）→ 单一学科权重接近1
- 跨学科（如"葡萄糖"）→ 多学科合理分配
- 泛知识/常识（如"飞机"）→ 返回空routes列表，均衡搜索所有学科"""

    _ROUTE_TO_INDEX = {
        "chemistry": "idx-public-chemistry-textbook",
        "math": "idx-public-math-rja-textbook",
        "math-xj": "idx-public-math-xj-textbook",
        "physics": "idx-public-physics-textbook",
        "biology": "idx-public-biology-textbook",
        "chinese": "idx-public-chinese-textbook",
        "english": "idx-public-english-textbook",
        "geography": "idx-public-geography-textbook",
        "politics": "idx-public-politics-textbook",
    }

    async def _route_subjects(self, query: str) -> tuple:
        """用Qwen判断查询关联学科及权重。
        Returns: (routes, should_all)
        """
        from services.llm_service import llm_service

        messages = [
            {"role": "system", "content": self._QWEN_ROUTING_PROMPT},
            {"role": "user", "content": query},
        ]
        try:
            result = await llm_service.chat_json(
                messages=messages,
                provider=_reg_resolve("qwen3.6-flash")["provider"],
                model="qwen3.6-flash",
                disable_thinking=True,
                request_type="knowledge_router",
            )
            routes = result.get("routes", []) if isinstance(result, dict) else []
            if not routes:
                return [], True
            valid_routes = []
            valid_indexes = {
                "chemistry", "math", "math-xj", "physics", "biology",
                "chinese", "english", "geography", "politics",
            }
            for r in routes:
                if isinstance(r, dict) and r.get("index") in valid_indexes and isinstance(r.get("weight"), (int, float)):
                    valid_routes.append({"index": r["index"], "weight": float(r["weight"])})
            return valid_routes, False
        except Exception as e:
            logger.warning("_route_subjects failed: %s, fallback to all indexes", e)
            return [], True

    async def search_public_with_quality(self, query: str, top_k: int = 5, agent_hash: str = None, min_score: float = 0.0) -> List[dict]:
        """Multi-source quality search with Qwen-routed stratified sampling.
        Returns results suitable for prompt injection."""
        from services.rerank_service import RerankService

        # Run embedding and Qwen routing in parallel (independent tasks)
        vec_task = asyncio.create_task(self.embed(query))
        route_task = asyncio.create_task(self._route_subjects(query))

        vec = await vec_task
        if not vec:
            return []

        routes, should_all = await route_task
        # Qwen routing: determine which subjects to search and allocate budget

        TOTAL_BUDGET = 100   # total rerank candidate budget for subject indexes
        MIN_PER_INDEX = 3    # minimum when a subject has non-zero weight

        tasks = []
        task_labels = []
        source_map = {}

        if should_all or not routes:
            # Fallback: search all subjects evenly (small top_k to stay under 500 limit)
            for idx in TEXTBOOK_SUBJECT_INDEXES:
                tasks.append(self._query_index(vec, idx, 10))
                task_labels.append(idx)
                source_map[idx] = "textbook"
        else:
            # Stratified: allocate budget by Qwen weights
            total_weight = sum(r["weight"] for r in routes) or 1.0
            for route in routes:
                idx = self._ROUTE_TO_INDEX.get(route["index"])
                if not idx:
                    continue
                per_index = max(MIN_PER_INDEX, int(route["weight"] / total_weight * TOTAL_BUDGET))
                tasks.append(self._query_index(vec, idx, per_index))
                task_labels.append(idx)
                source_map[idx] = "textbook"

        # Always search gaokao and trends (cross-subject data)
        tasks.append(self._query_index(vec, "idx-public-math-trends", 20))
        task_labels.append("idx-public-math-trends")
        source_map["idx-public-math-trends"] = "math_trends"

        tasks.append(self._query_index(vec, "idx-public-gaokao-kb", 30))
        task_labels.append("idx-public-gaokao-kb")
        source_map["idx-public-gaokao-kb"] = "gaokao"

        # Agent private KB and conv
        if agent_hash:
            tasks.append(self._query_index(vec, f"idx-{agent_hash}-kb", 10))
            task_labels.append(f"idx-{agent_hash}-kb")
            source_map[f"idx-{agent_hash}-kb"] = "knowledge_base"
            tasks.append(self._query_index(vec, f"idx-{agent_hash}-conv", 10))
            task_labels.append(f"idx-{agent_hash}-conv")
            source_map[f"idx-{agent_hash}-conv"] = "conversation_memory"

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge, filter by min_score, dedup
        all_docs = []
        seen = set()
        for idx_label, results in zip(task_labels, results_list):
            if isinstance(results, Exception):
                logger.warning("search_public_with_quality source %s failed: %s", idx_label, results)
                continue
            for r in results:
                score = r.get("score", 0)
                if score < min_score:
                    continue
                k = r.get("key", "")
                if k and k not in seen:
                    seen.add(k)
                    text = r.get("metadata", {}).get("text", "")
                    source = source_map.get(idx_label, "textbook")
                    all_docs.append({
                        "text": text,
                        "key": k,
                        "score": score,
                        "source": source,
                        "metadata": r.get("metadata", {}),
                    })

        if not all_docs:
            return []

        reranker = RerankService()
        reranked = await reranker.rerank(query, all_docs, top_n=top_k)
        return reranked[:top_k]

    async def search_memory(self, query: str, top_k: int = 3) -> List[dict]:
        """搜索 Agent 对话记忆（仅当有 agent_hash）"""
        if not self.agent_hash:
            return []

        vec = await self.embed(query)
        if not vec:
            return []

        idx = self._get_index_name("conv")
        return await self._query_index(vec, idx, top_k)

    async def _query_index(self, vec: List[float], index: str, top_k: int, filter: dict = None, timeout: float = 15.0) -> List[dict]:
        """对单个 index 执行向量查询

        Args:
            vec: 查询向量
            index: 索引名
            top_k: 返回数量
            filter: 元数据过滤条件（dict），如 {"source": {"$in": ["53-gaokao"]}}
            timeout: 超时秒数（默认15s）
        """
        try:
            client = self._get_client()
            kwargs = {
                "Bucket": VECTOR_BUCKET,
                "Index": index,
                "QueryVector": {"float32": vec},
                "TopK": top_k,
                "ReturnDistance": True,
                "ReturnMetaData": True,
            }
            if filter:
                kwargs["Filter"] = filter
            # COS SDK 是同步的，用线程包装避免阻塞事件循环
            resp_headers, resp_data = await asyncio.wait_for(
                asyncio.to_thread(client.query_vectors, **kwargs),
                timeout=timeout
            )
            return self._parse_query_response(resp_data)
        except asyncio.TimeoutError:
            logger.warning("_query_index timeout (%ss) on %s (top_k=%d)", timeout, index, top_k)
            return []
        except Exception as e:
            logger.error("COS query_vectors failed on index %s: %s", index, e)
            return []

    # ----- Index -----

    async def index_text(self, key: str, text: str, index: str, metadata: dict = None):
        """索引一条文本"""
        vec = await self.embed(text)
        if not vec:
            logger.warning("embed() empty for key=%s, skip index", key)
            return

        await self.index_batch([{"key": key, "text": text, "metadata": metadata or {}}], index)

    async def index_batch(self, items: List[dict], index: str):
        """批量索引多条 [{key, text, metadata}]"""
        texts = [item["text"] for item in items]
        vectors = await self.embed_batch(texts)

        cos_vectors = []
        for item, vec in zip(items, vectors):
            if not vec:
                continue
            meta = dict(item.get("metadata", {}))
            if "text" not in meta:
                meta["text"] = item["text"]
            cos_vectors.append({
                "key": item["key"],
                "data": {"float32": vec},
                "metadata": meta,
            })

        if not cos_vectors:
            return

        # 确保 index 存在
        await asyncio.to_thread(self._ensure_index, index)

        try:
            client = self._get_client()
            await asyncio.to_thread(
                client.put_vectors,
                Bucket=VECTOR_BUCKET,
                Index=index,
                Vectors=cos_vectors,
            )
            logger.info("Indexed %d vectors to %s", len(cos_vectors), index)
        except Exception as e:
            err_str = str(e)
            if "duplicate" in err_str.lower():
                logger.warning("Duplicate key in batch for %s, falling back to individual puts", index)
                success = 0
                for v in cos_vectors:
                    try:
                        await asyncio.to_thread(
                            client.put_vectors,
                            Bucket=VECTOR_BUCKET,
                            Index=index,
                            Vectors=[v],
                        )
                        success += 1
                    except Exception as ve:
                        logger.error("Individual put_vectors failed for key=%s: %s", v["key"], ve)
                logger.info("Indexed %d/%d vectors to %s (fallback mode)", success, len(cos_vectors), index)
            else:
                logger.error("COS put_vectors failed on index %s: %s", index, e)

    # ----- Delete -----

    async def delete(self, keys: List[str], index: str):
        """删除向量数据"""
        if not keys:
            return
        keys = list(dict.fromkeys(keys))  # 去重，保留顺序
        try:
            client = self._get_client()
            await asyncio.to_thread(
                client.delete_vectors,
                Bucket=VECTOR_BUCKET,
                Index=index,
                Keys=keys,
            )
            logger.info("Deleted %d vectors from %s", len(keys), index)
        except Exception as e:
            logger.error("COS delete_vectors failed on index %s: %s", index, e)

    # ----- Response Parsing -----

    def _parse_query_response(self, resp_data) -> List[dict]:
        """解析 COS query_vectors 的响应

        入参: {vectors: [{key, distance, metadata}]}
        返回: [{key, score(1-distance), metadata}]
        """
        if not resp_data or not isinstance(resp_data, dict):
            return []

        vectors = resp_data.get("vectors", [])
        results = []
        for v in vectors:
            metadata = v.get("metadata", {})
            # COS SDK returns metadata as string (JSON or Python repr), parse to dict
            if isinstance(metadata, str):
                import json
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    try:
                        import ast
                        metadata = ast.literal_eval(metadata)
                    except (ValueError, SyntaxError):
                        pass
            results.append({
                "key": v.get("key", ""),
                "score": max(0.0, min(1.0, 1.0 - v.get("distance", 0))),
                "metadata": metadata,
            })
        return results
