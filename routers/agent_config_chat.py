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
from models.database import get_db
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
    # 验证所有权（M1：收敛到 user_owns_agent）
    from utils.agent_access import user_owns_agent
    if not user_owns_agent(db, agent_hash, user.id):
        raise HTTPException(status_code=403, detail="无权访问")

    body = await request.json()

    # 从配置好的模型注册表解析 LLM（支持 MiMo / DeepSeek / Qwen / GLM 等）
    from services.model_registry import resolve as resolve_model, PROVIDER_META
    llm_model = body.get("model", getattr(settings, "MAIN_TEXT_MODEL", "mimo-v2.5-pro-ultraspeed"))
    model_info = resolve_model(llm_model)
    if model_info:
        provider_id = model_info.get("provider", "deepseek")
        api_key_attr = model_info.get("api_key_attr", "DEEPSEEK_API_KEY")
        api_key = getattr(settings, api_key_attr, "") or ""
        meta = PROVIDER_META.get(provider_id, {})
        llm_base = meta.get("base_url", "https://api.deepseek.com")
        if provider_id == "kimi" and not llm_base:
            llm_base = getattr(settings, "KIMI_BASE_URL", "https://api.moonshot.cn/v1")
        # 如果 Key 为空，fallback 到 MAIN_TEXT_MODEL
        if not api_key:
            fallback_model = getattr(settings, "MAIN_TEXT_MODEL", "")
            if fallback_model and fallback_model != llm_model:
                fb_info = resolve_model(fallback_model)
                if fb_info:
                    fb_key_attr = fb_info.get("api_key_attr", "")
                    api_key = getattr(settings, fb_key_attr, "") or ""
                    fb_meta = PROVIDER_META.get(fb_info.get("provider", ""), {})
                    llm_base = fb_meta.get("base_url", llm_base)
                    llm_model = fallback_model
    # 最后兜底（所有 Key 都为空时）
    if not api_key:
        api_key = getattr(settings, "DEEPSEEK_API_KEY", "") or ""
    if not api_key:
        api_key = getattr(settings, "QWEN_API_KEY", "") or ""
    if not api_key:
        api_key = getattr(settings, "MIMO_API_KEY", "") or ""
    if not llm_base:
        llm_base = "https://api.deepseek.com"

    async def proxy_stream():
        async with httpx.AsyncClient(timeout=120) as client:
            payload = dict(body)
            payload["stream"] = True
            payload.setdefault("model", llm_model)

            try:
                _url = f"{llm_base.rstrip('/')}/chat/completions"
                _model = payload.get("model", "unknown")
                _has_key = bool(api_key)
                logger.info(f"[ConfigChat] -> POST {_url} model={_model} has_key={_has_key}")
                async with client.stream(
                        "POST",
                        f"{llm_base.rstrip('/')}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    ) as resp:
                        if resp.status_code != 200:
                            error_body = await resp.aread()
                            err_text = f"❌ LLM API Error ({resp.status_code})"
                            try:
                                err_json = json.loads(error_body)
                                msg = err_json.get("error", {}).get("message", "") or err_json.get("message", "")
                                if msg:
                                    err_text += f": {msg}"
                            except Exception:
                                if error_body:
                                    err_text += f": {error_body.decode()[:200]}"
                            # 把错误当文本发给前端（前端只认 choices[0].delta.content）
                            yield f"data: {json.dumps({'choices': [{'delta': {'content': err_text}}]})}\n\n"
                            yield "data: [DONE]\n\n"
                            return

                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                yield line + "\n\n"

            except Exception as e:
                logger.error(f"LLM proxy error: {e}")
                yield f"data: {json.dumps({'choices': [{'delta': {'content': f"❌ 请求失败: {e}"}}]})}\n\n"
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
