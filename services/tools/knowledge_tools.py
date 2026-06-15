#!/usr/bin/env python3
"""
知识库工具 - 读取知识库条目完整原文
"""

import asyncio
import json
import logging

from config import settings
from services.tool_registry import tool
from services.model_registry import resolve
from services.tools.base import AgentToolsServiceBase

logger = logging.getLogger(__name__)


# COS 文件桶路径前缀映射
SOURCE_PREFIX = {
    "public": "feclaw/public/kb",      # 公共知识库（TENCENT_COS_PREFIX 已在路径中）
    "agent": "feclaw/kb/agent",  # Agent 私有知识库（预留）
    "shared": "feclaw/kb/shared",  # 共享知识库（预留）
}

# 知识库 source 参数 → VectorSearch index 后缀映射
SEARCH_INDEXES = {
    "public": "idx-public-kb",
    "agent": "kb",    # idx-{hash}-kb
    "sessions": "conv",  # idx-{hash}-conv
}


# 模块级缓存 COS 客户端（线程安全，避免每次在线程池重建连接池）
_COS_CLIENT = None


class ToolResult(str):
    """带展示摘要元数据的字符串。
    
    str() 或直接使用时返回完整内容（Agent 看到全量信息）；
    .summary 属性提供精简摘要（微信端展示用）。
    """
    def __new__(cls, content: str, summary: str = None):
        obj = str.__new__(cls, content)
        obj._summary = summary
        return obj

    @property
    def summary(self) -> str:
        return self._summary

    def __str__(self) -> str:
        return str.__str__(self)

    def __repr__(self) -> str:
        return repr(str.__str__(self))


def _wrap_kb_summary(content: str, source_label: str, results: list = None) -> ToolResult:
    """将知识库搜索结果包装为带展示摘要的 ToolResult。
    
    str() → Agent 看到完整格式内容
    .summary → 微信端展示精简摘要
    """
    if not results or content.startswith(("未找到", "错误")):
        return ToolResult(content, summary=content)
    counts = _count_by_source(results, source_label)
    count_items = [f"{v}条{n}" for n, v in sorted(counts.items())]
    summary = f"📊 {len(results)} 条知识库结果：{'，'.join(count_items)}"
    return ToolResult(content, summary=summary)
def _get_cos_client():
    global _COS_CLIENT
    if _COS_CLIENT is None:
        from qcloud_cos import CosConfig, CosS3Client
        config = CosConfig(
            Region=settings.TENCENT_COS_REGION,
            SecretId=settings.TENCENT_COS_SECRET_ID,
            SecretKey=settings.TENCENT_COS_SECRET_KEY,
            Scheme="https",
        )
        _COS_CLIENT = CosS3Client(config)
    return _COS_CLIENT


def _smart_crop(text: str, max_head: int = 400, max_tail: int = 300) -> str:
    """智能头尾裁剪：短文本全文保留，长文本截取头+尾"""
    if not text or len(text) <= 600:
        return text
    head_end = text.rfind("\n", 0, max_head)
    if head_end < max_head // 2:
        head_end = max_head
    tail_start = text.find("\n", len(text) - max_tail)
    if tail_start == -1 or tail_start > len(text) - max_tail // 2:
        tail_start = len(text) - max_tail
    return (
        f"{text[:head_end]}\n"
        f"……（中间省略，可使用 knowledge_get 工具查看全文）……\n"
        f"{text[tail_start:]}"
    )


def _sanitize_wechat_text(text: str, max_len: int = 400) -> str:
    """清理可能导致微信 Markdown 渲染异常的片段。
    
    微信的 Markdown 渲染对不成对的 { }、行首 # 号、$$ 等符号敏感。
    此函数确保截断后的片段不会破坏微信的渲染。
    """
    if not text:
        return ""
    if len(text) > max_len:
        # 在安全位置截断：优先找句号/换行/逗号
        safe = text.rfind("。", 0, max_len)
        if safe < max_len // 2:
            safe = text.rfind("\n", 0, max_len)
        if safe < max_len // 2:
            safe = text.rfind("。", 0, max_len + 50)
        if safe > 0:
            text = text[:safe + 1]
        else:
            text = text[:max_len]
    # 确保无不成对的大括号（会破坏微信渲染）
    # 注意：内联公式 ${...}$ 中的 { } 应该是成对的
    # 只修复最外层的不成对情况
    open_braces = text.count("{")
    close_braces = text.count("}")
    if open_braces > close_braces:
        text += "}" * (open_braces - close_braces)
    return text


SOURCE_LABEL_MAP = {
    "gaokao": "高考题",
    "textbook": "课本内容",
    "questions": "练习题",
    "trends": "知识趋势",
    "knowledge_base": "私有知识",
    "public": "知识库",
    "subject": "课本内容",
}


def _count_by_source(results: list, source_label: str) -> dict:
    """按 source 统计结果数量"""
    counts = {}
    for r in results:
        src = r.get("source", source_label)
        cn = SOURCE_LABEL_MAP.get(src, src)
        counts[cn] = counts.get(cn, 0) + 1
    return counts


QUESTION_SUBJECT_INDEXES = [
    "idx-public-biology-questions",
    "idx-public-chemistry-questions",
    "idx-public-math-questions",
    "idx-public-physics-questions",
]


class KnowledgeToolsMixin(AgentToolsServiceBase):
    """知识库工具 Mixin"""

    @tool(
        description="获取知识库条目的完整原文。"
        "当【相关知识库】中的内容被截断或需要查看完整细节时，"
        "调用此工具获取完整正文。参数 key 和 source 来自注入标注。",
        category="knowledge",
    )
    async def knowledge_get(self, key: str, source: str = "public") -> str:
        """
        获取知识库条目的完整原文。

        source="public" 时从 COS 文件桶读取；
        key 为 VFS 路径（"vfs::..."）时从本地 VFS 直接读取。
        """
        # 处理 VFS 路径 key（来自私有知识库或会话记忆）
        if key.startswith("vfs::"):
            # 格式: vfs::/path/to/file.md::content_hash
            vfs_path = key.replace("vfs::", "", 1)
            if "::" in vfs_path:
                vfs_path = vfs_path.split("::")[0]
            vfs_path = vfs_path.lstrip("/")
            try:
                content = await self.vfs.async_cat(vfs_path)
                if content and not content.startswith("Error"):
                    _name = vfs_path.rsplit("/", 1)[-1] if "/" in vfs_path else vfs_path
                    return ToolResult(content, summary=f"成功读取 {_name}")
            except Exception as e:
                logger.warning(
                    "knowledge_get vfs read failed: key=%s path=%s error=%s",
                    key, vfs_path, e,
                )
            return f"错误：无法读取知识库条目（key={key}），VFS 路径不存在。"

        if source not in SOURCE_PREFIX:
            return f"错误：未知的知识库来源 '{source}'，可用选项: {', '.join(SOURCE_PREFIX.keys())}"

        cos_path = f"{SOURCE_PREFIX[source]}/{key}.json"

        # 兼容旧数据：部分老数据没有 feclaw/ 前缀
        alt_paths = [cos_path]
        if source == "public":
            if cos_path.startswith("feclaw/"):
                alt_paths.append(cos_path[len("feclaw/"):])
            else:
                alt_paths.insert(0, f"feclaw/{cos_path}")

        for path in alt_paths:
            try:
                client = _get_cos_client()
                resp = client.get_object(
                    Bucket=settings.TENCENT_COS_BUCKET,
                    Key=path,
                )
                body = resp["Body"].get_raw_stream().read()
                data = json.loads(body)
                text = data.get("text", data.get("content", ""))
                citation = data.get("citation", "")
                if citation:
                    _cit_clean = citation.strip().rstrip("。").rstrip("\n")
                    _summary = f"成功读取{_cit_clean[:120]}"
                    return ToolResult(f"{citation}\n\n{text}", summary=_summary)
                _key_name = key.rsplit("/", 1)[-1].replace("_", " ")
                return ToolResult(text, summary=f"成功读取知识库条目：{_key_name[:60]}")
            except Exception:
                continue

        logger.warning(
            "knowledge_get failed: key=%s source=%s path=%s no valid path found",
            key, source, cos_path,
        )
        return f"错误：无法读取知识库条目（key={key}, source={source}），请确认该条目已导入。"

    # Exam-related keywords for auto-detection in knowledge_search
    EXAM_KEYWORDS = [
        "高考", "中考", "考试", "真题", "试题", "考题",
        "模拟", "模考", "联考", "统考", "月考", "期中", "期末",
        "选择题", "多选题", "单选题", "填空题", "判断题", "简答题", "解答题",
        "阅读理解", "完形填空", "听力", "作文", "实验题", "计算题",
        "卷", "科目", "考点", "题型", "命题", "阅卷", "评分",
    ]

    @staticmethod
    def _detect_index_intent(query: str) -> str:
        """检测查询意图：gaokao or textbook

        Returns:
            "gaokao" if the query mentions exam-related keywords, otherwise "textbook"
        """
        query_lower = query.lower()
        for kw in KnowledgeToolsMixin.EXAM_KEYWORDS:
            if kw in query_lower:
                return "gaokao"
        return "textbook"

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

权重决定各学科搜索多少结果。总和可以是0-1之间的任意值，0=不搜。

返回JSON：
{"routes": [{"index": "chemistry", "weight": 0.6}, {"index": "biology", "weight": 0.4}], "reasoning": "涉及葡萄糖，属于化学有机物和生物代谢"}

规则：
- 精确匹配（如"复数的三角形式"）→ 单一学科高权重
- 跨学科（如"葡萄糖"）→ 多学科合理分配
- 泛知识/常识（如"飞机"）→ 返回空routes列表，走全库搜索"""

    # 路由名→实际 COS 索引名映射
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

    # 科目英文→中文映射（用于高考库的 metadata filter 和索引选择）
    _SUBJECT_CN_MAP = {
        "chemistry": ["化学"],
        "math": ["数学", "数学-人教A版", "数学-湘教版"],
        "math-xj": ["数学-湘教版"],
        "physics": ["物理"],
        "biology": ["生物"],
        "chinese": ["语文"],
        "english": ["英语"],
        "geography": ["地理"],
        "politics": ["政治"],
    }

    def _build_gaokao_filter(self, routes: list) -> dict:
        """构建高考库的 source filter（过滤掉教材污染）"""
        return {"source": {"$in": ["53-gaokao", "gaokao-bench"]}}

    async def _route_with_qwen(self, query: str) -> tuple:
        """用Qwen路由决定搜索哪些学科及权重。
        Returns: (routes_list, should_all)
        routes_list: [{"index": "chemistry", "weight": 0.6}, ...]
        should_all: True if should search all indexes (rerank decides)
        """
        from services.llm_service import llm_service

        messages = [
            {"role": "system", "content": self._QWEN_ROUTING_PROMPT},
            {"role": "user", "content": query},
        ]
        try:
            result = await llm_service.chat_json(
                messages=messages,
                provider=resolve("qwen3.6-flash")["provider"],
                model="qwen3.6-flash",
                disable_thinking=True,
                request_type="knowledge_router",
            )
            routes = result.get("routes", []) if isinstance(result, dict) else []
            if not routes:
                return [], True
            # Validate routes
            valid_indexes = {
                "chemistry", "math", "math-xj", "physics", "biology",
                "chinese", "english", "geography", "politics",
            }
            valid_routes = []
            for r in routes:
                if isinstance(r, dict) and r.get("index") in valid_indexes and isinstance(r.get("weight"), (int, float)):
                    valid_routes.append({"index": r["index"], "weight": float(r["weight"])})
            return valid_routes, False
        except Exception as e:
            logger.warning("_route_with_qwen failed: %s, fallback to all indexes", e)
            return [], True

    @tool(
        description="在知识库中搜索与查询相关的内容。"
        "注意：请使用语义明确的自然语言问题查询（如'什么是氧化还原反应'、'牛顿第二定律公式'），避免使用零散关键词。"
        "支持搜索范围：public（公共知识库）、sessions（历史会话）、agent（Agent私有知识库）、all（全部）。"
        "支持索引选择：all（全部）、textbook（教材）、gaokao（高考）、questions（题库）、auto（智能路由）。"
        "返回匹配结果列表（自动重排序），可配合 knowledge_get 查看完整内容。",
        category="knowledge",
    )
    async def knowledge_search(
        self, query: str, source: str = "public", top_k: int = 5,
        index: str = "auto",
    ) -> str:
        """
        在知识库中进行语义搜索（自动重排序）。

        Args:
            query: 搜索关键词
            source: 搜索范围
                - "public"   公共知识库（默认）
                - "sessions" 历史会话记录
                - "agent"    Agent私有知识库
                - "all"      全部
            top_k: 返回结果数量（默认5，最大10）
            index: 索引选择（仅 source="public" 时生效）
                - "auto"     自动检测意图（默认：高考关键词→gaokao，否则→textbook）
                - "all"      搜索全部公共知识库
                - "gaokao"   仅搜索高考题库
                - "textbook" 仅搜索教材知识库
                - "questions" 仅搜索练习题库

        Returns:
            匹配结果列表，每条包含 key、相似度、内容预览

        :param query: 搜索关键词
        :param source: 搜索范围（"public" | "sessions" | "agent" | "all"）
        :param top_k: 返回结果数量
        :param index: 索引选择（"auto" | "all" | "gaokao" | "textbook" | "questions"），仅 source="public" 时生效
        """
        if not query or len(query.strip()) < 2:
            return "错误：搜索关键词至少需要2个字符"

        if source not in ("public", "sessions", "agent", "all"):
            return f"错误：未知的搜索范围 '{source}'，可用选项: public, sessions, agent, all"

        if index not in ("auto", "all", "gaokao", "textbook", "questions"):
            return f"错误：未知的索引选择 '{index}'，可用选项: auto, all, gaokao, textbook, questions"

        top_k = min(top_k, 10)
        query = query.strip()

        if source != "public" and index != "auto":
            logger.warning("knowledge_search: index=%r ignored (only applies to source='public', got source=%r)", index, source)

        from services.vector_search_service import VectorSearchService, TEXTBOOK_SUBJECT_INDEXES

        vs = VectorSearchService(agent_hash=getattr(self, 'agent_hash', None))

        if source == "public":
            if index == "auto":
                index = "textbook"  # default to textbook with Qwen routing

            if index == "gaokao":
                results = await vs.search_gaokao(query, top_k=top_k)
                if not results:
                    return "高考题库中未找到匹配结果。"
                return _wrap_kb_summary(self._format_kb_results(results, "gaokao"), "gaokao", results)

            elif index == "textbook":
                # Qwen-based routing for subject indexes
                routes, should_all = await self._route_with_qwen(query)
                tasks = []

                if should_all or not routes:
                    # 泛知识/无路由结果：搜所有 TEXTBOOK_SUBJECT_INDEXES
                    vec = await vs.embed(query)
                    if vec:
                        for idx in TEXTBOOK_SUBJECT_INDEXES:
                            tasks.append(
                                ("subject", idx, vs._query_index(vec, idx, 50))
                            )
                else:
                    # 按 Qwen 权重搜索各学科索引
                    vec = await vs.embed(query)
                    if vec:
                        for route in routes:
                            idx = self._ROUTE_TO_INDEX.get(route['index'])
                            if not idx:
                                continue
                            _tk = max(5, int(route['weight'] * 50))
                            tasks.append(
                                ("subject", idx, vs._query_index(vec, idx, _tk))
                            )

                # Also search trends
                if vec:
                    tasks.append(
                        ("trends", "idx-public-math-trends", vs._query_index(vec, "idx-public-math-trends", 50))
                    )

                if not tasks:
                    results = []
                else:
                    labels_and_tasks = [(t[1], t[2]) for t in tasks]
                    results_list = await asyncio.gather(
                        *[t for _, t in labels_and_tasks], return_exceptions=True
                    )

                    # Merge, dedup
                    seen = set()
                    all_docs = []
                    idx_labels = [l for l, _ in labels_and_tasks]
                    for idx_label, results in zip(idx_labels, results_list):
                        if isinstance(results, Exception):
                            logger.warning("knowledge_search textbook source %s failed: %s", idx_label, results)
                            continue
                        for r in results:
                            k = r.get("key", "")
                            if k and k not in seen:
                                seen.add(k)
                                all_docs.append(r)

                    # Re-rank
                    if all_docs:
                        from services.rerank_service import RerankService
                        reranker = RerankService()
                        docs = []
                        for c in all_docs:
                            docs.append({
                                "text": c.get("metadata", {}).get("text", ""),
                                "metadata": c.get("metadata", {}),
                                "key": c.get("key", ""),
                                "score": c.get("score", 0),
                                "source": c.get("source", "textbook"),
                            })
                        results = await reranker.rerank(query, docs, top_n=top_k)
                    else:
                        results = []

                if not results:
                    return "教材知识库中未找到匹配结果。"
                return _wrap_kb_summary(self._format_kb_results(results, "textbook"), "textbook", results)

            elif index == "questions":
                vec = await vs.embed(query)
                if not vec:
                    return "向量编码失败。"

                routes, should_all = await self._route_with_qwen(query)
                tasks = []
                total_budget = 30
                min_per_idx = 3

                if should_all or not routes:
                    for idx in QUESTION_SUBJECT_INDEXES:
                        tasks.append(("questions", idx, vs._query_index(vec, idx, 8)))
                else:
                    total_weight = sum(r["weight"] for r in routes) or 1.0
                    for route in routes:
                        subj_idx = f"idx-public-{route['index']}-questions"
                        if subj_idx not in QUESTION_SUBJECT_INDEXES:
                            continue
                        per_idx = max(min_per_idx, int(route["weight"] / total_weight * total_budget))
                        tasks.append(("questions", subj_idx, vs._query_index(vec, subj_idx, per_idx)))

                results_list = await asyncio.gather(*[t[2] for t in tasks], return_exceptions=True)

                all_results = []
                seen = set()
                for task_key, results in zip(tasks, results_list):
                    tag, idx_label = task_key[0], task_key[1]
                    if isinstance(results, Exception):
                        logger.warning("questions source %s failed: %s", idx_label, results)
                        continue
                    for r in results:
                        k = r.get("key", "")
                        if k and k not in seen:
                            seen.add(k)
                            all_results.append({
                                "text": r.get("metadata", {}).get("text", ""),
                                "key": k,
                                "score": r.get("score", 0),
                                "source": "questions",
                            })

                if not all_results:
                    return "题库中未找到匹配结果。"

                from services.rerank_service import RerankService
                reranker = RerankService()
                results = await reranker.rerank(query, all_results)
                results = results[:top_k]

                return _wrap_kb_summary(self._format_kb_results(results, "questions"), "questions", results)

            elif index == "all":
                from services.rerank_service import RerankService
                vec = await vs.embed(query)
                if not vec:
                    return "公共知识库中未找到匹配结果。"

                # Qwen 路由：判断搜索科目范围
                routes, should_all = await self._route_with_qwen(query)

                tasks = []
                task_labels = []

                if should_all or not routes:
                    # 泛知识/无明确路由：搜全部学科 + 高考
                    for idx in TEXTBOOK_SUBJECT_INDEXES:
                        tasks.append(vs._query_index(vec, idx, 50))
                        task_labels.append(idx)
                    tasks.append(vs._query_index(vec, "idx-public-gaokao-kb", 50))
                    task_labels.append("idx-public-gaokao-kb")
                else:
                    # 按路由结果搜索限定科目 + 高考库 subject filter
                    for route in routes:
                        idx_name = self._ROUTE_TO_INDEX.get(route['index'])
                        if not idx_name:
                            continue
                        _tk = max(5, int(route['weight'] * 50))
                        tasks.append(vs._query_index(vec, idx_name, _tk))
                        task_labels.append(idx_name)
                    tasks.append(vs._query_index(vec, "idx-public-gaokao-kb", 50))
                    task_labels.append("idx-public-gaokao-kb")

                # Trends + agent KB 总是搜索
                tasks.append(vs._query_index(vec, "idx-public-math-trends", 50))
                task_labels.append("idx-public-math-trends")
                agent_hash = getattr(self, 'agent_hash', None)
                if agent_hash:
                    tasks.append(vs._query_index(vec, f"idx-{agent_hash}-kb", 10))
                    task_labels.append(f"idx-{agent_hash}-kb")

                results_list = await asyncio.gather(*tasks, return_exceptions=True)

                seen = set()
                all_docs = []
                for idx_label, results in zip(task_labels, results_list):
                    if isinstance(results, Exception):
                        logger.warning("knowledge_search all source %s failed: %s", idx_label, results)
                        continue
                    for r in results:
                        k = r.get("key", "")
                        # 后过滤 gaokao 结果：排除教材污染
                        if idx_label == "idx-public-gaokao-kb":
                            src = r.get("metadata", {}).get("source", "")
                            if src not in ("53-gaokao", "gaokao-bench"):
                                continue
                        if k and k not in seen:
                            seen.add(k)
                            text = r.get("metadata", {}).get("text", "")
                            all_docs.append({
                                "text": text,
                                "key": k,
                                "score": r.get("score", 0),
                                "source": r.get("source", "textbook"),
                                "metadata": r.get("metadata", {}),
                            })

                if not all_docs:
                    return "公共知识库中未找到匹配结果。"

                reranker = RerankService()
                results = await reranker.rerank(query, all_docs, top_n=top_k)
                return _wrap_kb_summary(self._format_kb_results(results, "public"), "public", results)

        elif source == "sessions":
            if not vs.agent_hash:
                return "错误：搜索会话需要 Agent 上下文。"
            results = await vs.search(query, index=f"idx-{vs.agent_hash}-conv", top_k=top_k * 3)
            return self._format_session_results(results)

        elif source == "agent":
            if not vs.agent_hash:
                return "错误：搜索 Agent 私有知识库需要 Agent 上下文。"
            results = await vs.search(query, index=f"idx-{vs.agent_hash}-kb", top_k=top_k)
            if not results:
                return "Agent 私有知识库中未找到匹配结果。"
            return _wrap_kb_summary(self._format_kb_results(results, "agent"), "agent", results)

        elif source == "all":
            results = await vs.search_with_rerank(query, top_k=top_k, rerank=True)
            if not results:
                return "未找到匹配结果。"
            return _wrap_kb_summary(self._format_kb_results(results, "all"), "all", results)

        return "错误：未知的搜索范围。"

    def _format_kb_results(self, results: list, source_label: str) -> str:
        """格式化知识库搜索结果（public / agent / all / gaokao / textbook / questions）"""
        if not results:
            return f"【知识库搜索结果（{source_label}）】\n📊 共返回 0 条结果。"

        # 总览行：按来源统计
        counts = _count_by_source(results, source_label)
        summary_parts = [f"【知识库搜索结果（{source_label}）】"]
        count_items = [f"{v}条{n}" for n, v in sorted(counts.items())]
        if count_items:
            summary_parts.append(f"📊 {len(results)} 条结果：{'，'.join(count_items)}")
        summary_parts.append(f"💡 可使用 knowledge_get(key, source=...) 查看完整内容。")
        summary = "\n".join(summary_parts)

        # 按来源分组展示
        groups: dict[str, list] = {}
        for r in results:
            src = r.get("source", source_label)
            cn = SOURCE_LABEL_MAP.get(src, src)
            groups.setdefault(cn, []).append(r)

        lines = [summary, ""]
        for group_name, group_results in groups.items():
            lines.append(f"▎{group_name}（{len(group_results)}条）")
            for i, r in enumerate(group_results):
                key = r.get("key", "")
                score = r.get("rerank_score", r.get("score", 0))
                metadata = r.get("metadata", {})
                text = metadata.get("text", "")

                if not text:
                    continue

                # 智能裁剪：超长文本头取300字，尾取400字
                display = _smart_crop(text, max_head=300, max_tail=400)

                lines.append(f"  {i + 1}. [{score:.2f}] (key: {key}) {display}")

        return "\n".join(lines)

    def _format_session_results(self, results: list) -> str:
        """格式化会话搜索结果（sessions）"""
        from models.database import ConversationSession, SessionLocal

        # 按 session_id 去重
        session_ids = []
        metadata_map = {}
        for r in results:
            meta = r.get("metadata", {})
            if meta and meta.get("session_id"):
                sid = meta["session_id"]
                if sid not in session_ids:
                    session_ids.append(sid)
                    metadata_map[sid] = {
                        "score": r.get("score", 0),
                        "summary": meta.get("summary", ""),
                    }

        if not session_ids:
            return "未找到匹配的会话记录。"

        try:
            db = SessionLocal()
            try:
                sessions = db.query(ConversationSession).filter(
                    ConversationSession.session_id.in_(session_ids),
                    ConversationSession.user_id == int(self.user_id),
                ).all()

                session_map = {s.session_id: s for s in sessions}
                ordered = [(sid, metadata_map[sid]) for sid in session_ids if sid in session_map]

                if not ordered:
                    return "未找到匹配的会话记录（无权限访问）。"

                lines = ["【会话搜索结果】"]
                for sid, meta in ordered:
                    s = session_map[sid]
                    summary = meta.get("summary", "") or "（无摘要）"
                    topic = s.topic or "（无标题）"
                    msg_count = len(json.loads(s.messages)) if s.messages else 0
                    date_str = s.updated_at.strftime("%Y-%m-%d %H:%M") if s.updated_at else "未知"

                    lines.append(f"")
                    lines.append(f"--- 会话 ---")
                    lines.append(f"  相似度: {meta['score']:.2f}")
                    lines.append(f"  session_id: {sid}")
                    lines.append(f"  话题: {topic}")
                    ellipsis = "…" if len(summary) > 200 else ""
                    lines.append(f"  摘要: {summary[:200]}{ellipsis}")
                    lines.append(f"  消息数: {msg_count} | 最后更新: {date_str}")

                lines.append(f"")
                lines.append(f"📊 共找到 {len(ordered)} 个匹配会话。可使用 load_conversation(session_id) 查看完整对话。")

                return "\n".join(lines)
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"_format_session_results failed: {e}")
            return f"错误：读取会话记录失败: {e}"
