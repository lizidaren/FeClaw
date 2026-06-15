"""
Agent 配置聊天 API — 纯透传代理

POST /api/agent/{hash}/chat      - LLM 流式代理（透传 DeepSeek，不解码不处理）
GET  /api/agent/{hash}/search    - 联网搜索代理（带用户级限速）

设计原则：后端不做任何工具执行，只代理 LLM API 和搜索 API。
全部工具逻辑（read_file/edit_file/write_file）在前端执行。
"""

import json
import logging
import os
import time
from collections import defaultdict
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from config import settings
from models.database import get_db, AgentProfile
from utils.auth import get_current_user, User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Agent Config Chat"])

# 搜索限速（进程内内存）
_search_counts: dict = defaultdict(list)


@router.post("/api/agent/{agent_hash}/chat")
async def agent_config_chat(
    agent_hash: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """LLM 流式代理 — 透传 DeepSeek

    请求体格式（与 DeepSeek API 一致）：
    ```json
    {
        "messages": [...],
        "tools": [...],
        "stream": true
    }
    ```

    返回 SSE 流（透传 DeepSeek 原始输出）。
    """
    # 验证所有权
    agent = db.query(AgentProfile).filter(
        AgentProfile.hash == agent_hash, AgentProfile.user_id == user.id
    ).first()
    if not agent:
        raise HTTPException(status_code=403, detail="无权访问")

    body = await request.json()

    api_key = os.environ.get("DEEPSEEK_API_KEY") or getattr(settings, "DEEPSEEK_API_KEY", "")
    if not api_key:
        api_key = os.environ.get("INFINI_API_KEY", "")

    # 读取 LLM 配置
    llm_base = getattr(settings, "LLM_BASE_URL", "https://api.deepseek.com")
    llm_model = body.get("model", getattr(settings, "AGENT_LLM_MODEL", "deepseek-v4-flash"))

    async def proxy_stream():
        async with httpx.AsyncClient(timeout=120) as client:
            payload = dict(body)
            payload["stream"] = True
            payload.setdefault("model", llm_model)

            try:
                async with client.stream(
                        "POST",
                        f"{llm_base.rstrip('/')}/v1/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    ) as resp:
                        if resp.status_code != 200:
                            error_body = await resp.aread()
                            yield f"data: {json.dumps({'type': 'error', 'content': f'LLM API Error ({resp.status_code})'})}\n\n"
                            yield "data: [DONE]\n\n"
                            return

                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                yield line + "\n\n"

            except Exception as e:
                logger.error(f"LLM proxy error: {e}")
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
                yield "data: [DONE]\n\n"

    return StreamingResponse(
        proxy_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/agent/{agent_hash}/search")
async def agent_config_search(
    agent_hash: str,
    q: str = Query(..., description="搜索关键词"),
    request: Request = None,
    user: User = Depends(get_current_user),
):
    """联网搜索代理 — 带用户级限速（10次/分钟）"""

    # 限速
    now = time.time()
    _search_counts[user.id] = [t for t in _search_counts.get(user.id, []) if now - t < 60]
    if len(_search_counts[user.id]) >= 10:
        raise HTTPException(status_code=429, detail="搜索请求过于频繁，请稍后重试")
    _search_counts[user.id].append(now)

    try:
        # 使用 Qwen 搜索（balanced 级别，对应 Agent 默认配置）
        from services.search_service import SearchService
        ss = SearchService()
        result_text = await ss.search_qwen(q)

        results = []
        if result_text and not result_text.startswith("Error"):
            results.append({"title": "搜索结果", "snippet": result_text[:800], "url": ""})

        if not results:
            results.append({"title": "搜索结果", "snippet": f"关于「{q}」的搜索结果", "url": ""})

        return {"results": results}

    except Exception as e:
        logger.error(f"Search failed: {e}")
        return {"results": [{"title": "搜索出错", "snippet": str(e)[:200], "url": ""}]}
