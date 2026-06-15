"""
Apps Gateway — Agent App 路由网关

处理 /apps/{app_id} 路径下的请求，按 routes.json 定义分派到 static / ai / code 处理器。

请求流程：
1. 从 Host 头提取 agent_hash（子域名机制）
2. 匹配 /apps/{app_id}/{path}
3. 加载 routes.json 决定怎么处理
"""

import json
import logging
import re
from typing import Optional, Dict
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from routers.feclaw_domain import extract_hash_from_host, get_user_for_page
from services.apps_service import (
    handle_static, handle_ai, handle_code, handle_data,
    get_app_config, list_registered_apps, check_rate_limit,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Apps 网关"])

# app_id 和 agent_hash 校验正则
_VALID_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')  # 1-32 字符，字母数字+连字符

MAX_BODY_SIZE = 1 * 1024 * 1024  # 1MB


def _validate_id(id_str: str, field: str = "app_id") -> bool:
    """校验 app_id / path 的合法性"""
    return bool(_VALID_ID_RE.match(id_str))


def _get_agent_hash(request: Request) -> Optional[str]:
    """从请求 Host 提取 agent_hash"""
    host = request.headers.get("X-Forwarded-Host", "") or request.headers.get("host", "")
    return extract_hash_from_host(host)


# ── Agent API 端点 ─────────────────────────────────────


NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>应用未找到</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f5f7;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;color:#1d1d1f}}
.card{{background:#fff;border-radius:20px;padding:48px;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:420px}}
.icon{{font-size:64px;margin-bottom:16px}}
h1{{font-size:24px;margin:0 0 8px;font-weight:600}}
p{{font-size:16px;color:#86868b;margin:0 0 24px;line-height:1.5}}
.btn{{display:inline-block;background:#0071e3;color:#fff;text-decoration:none;padding:12px 28px;border-radius:40px;font-size:15px;font-weight:500;transition:background .2s}}
.btn:hover{{background:#0077ed}}
</style></head>
<body><div class="card"><div class="icon">{icon}</div><h1>{title}</h1><p>{message}</p><a href="/" class="btn">返回首页</a></div></body></html>"""


def _html_error(icon: str, title: str, message: str, status: int = 404) -> HTMLResponse:
    """返回友好 HTML 错误页面"""
    html = NOT_FOUND_HTML.format(icon=icon, title=title, message=message)
    return HTMLResponse(content=html, status_code=status, media_type="text/html; charset=utf-8")


class RegisterRequest(BaseModel):
    """注册 App 请求"""
    app_id: str


@router.post("/api/apps/register")
async def api_register_app(body: RegisterRequest, request: Request):
    """注册 App（Route Register Tool 的后端接口）"""
    agent_hash = _get_agent_hash(request)
    if not agent_hash or not _validate_id(agent_hash, "agent_hash"):
        raise HTTPException(status_code=400, detail="Invalid agent hash")
    if not _validate_id(body.app_id):
        raise HTTPException(status_code=400, detail="Invalid app_id (1-32 chars, letters/digits/hyphens)")

    from services.apps_service import register_app
    result = await register_app(agent_hash, body.app_id)  # async now
    if not result:
        raise HTTPException(status_code=400, detail=f"Failed to register app: {body.app_id}")

    return {"status": "registered", "app_id": body.app_id}


@router.delete("/api/apps/{app_id}")
async def api_unregister_app(app_id: str, request: Request):
    """注销 App"""
    agent_hash = _get_agent_hash(request)
    if not agent_hash or not _validate_id(agent_hash, "agent_hash"):
        raise HTTPException(status_code=400, detail="Invalid agent hash")
    if not _validate_id(app_id):
        raise HTTPException(status_code=400, detail="Invalid app_id")

    from services.apps_service import unregister_app
    if await unregister_app(agent_hash, app_id):  # async now
        return {"status": "unregistered", "app_id": app_id}
    raise HTTPException(status_code=404, detail=f"App not found: {app_id}")


@router.get("/api/apps")
async def api_list_apps(request: Request):
    """列出已注册的 App"""
    agent_hash = _get_agent_hash(request)
    if not agent_hash or not _validate_id(agent_hash, "agent_hash"):
        raise HTTPException(status_code=400, detail="Invalid agent hash")

    return {"apps": list_registered_apps(agent_hash)}


# ── 公开 App 访问路由 ─────────────────────────────────


@router.get("/apps")
@router.get("/apps/")
async def list_apps_page(request: Request):
    """App 列表页"""
    agent_hash = _get_agent_hash(request)
    if not agent_hash:
        from fastapi.responses import HTMLResponse
        html = "<html><body><h1>Apps</h1><p>请在 Agent 子域名下访问此页面。</p></body></html>"
        return HTMLResponse(html)

    apps = list_registered_apps(agent_hash)

    # 构建简单的列表 HTML
    cards_html = ""
    for app_id in apps:
        config = get_app_config(agent_hash, app_id)
        desc = config.get("description", "") if config else ""
        app_url = f"/apps/{app_id}/"
        cards_html += f"""
        <a href="{app_url}" class="app-card">
            <h3>{app_id}</h3>
            <p>{desc}</p>
            <span class="url">{agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/</span>
        </a>"""

    if not cards_html:
        cards_html = '<p class="empty">暂无已注册的 App。</p>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Apps - {agent_hash}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#f0f2f5;color:#1a1a2e;padding:24px;max-width:900px;margin:auto}}
h1{{font-size:1.6rem;margin-bottom:8px}}
.sub{{color:#666;margin-bottom:24px}}
.app-card{{display:block;background:#fff;border-radius:12px;padding:20px;margin-bottom:12px;
  text-decoration:none;color:inherit;transition:box-shadow .2s;border:1px solid #e5e7eb}}
.app-card:hover{{box-shadow:0 4px 12px rgba(0,0,0,.1)}}
.app-card h3{{font-size:1.2rem;margin-bottom:4px;color:#2563eb}}
.app-card p{{color:#555;font-size:.9rem;margin-bottom:8px}}
.app-card .url{{color:#888;font-size:.8rem;font-family:monospace}}
.empty{{color:#999;text-align:center;padding:40px}}
</style></head><body>
<h1>📱 {agent_hash} Apps</h1>
<p class="sub">Agent 自部署的 Web 应用</p>
{cards_html}
</body></html>"""
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")


@router.get("/apps/{app_id}")
@router.get("/apps/{app_id}/")
async def get_app_home(app_id: str, request: Request):
    """App 首页（自动 serve index.html）"""
    return await _route_request(app_id, "index.html", request, None)


@router.get("/apps/{app_id}/{path:path}")
async def get_app_path(app_id: str, path: str, request: Request):
    """App 路径路由"""
    return await _route_request(app_id, path, request, None)


@router.post("/apps/{app_id}/api/{path:path}")
async def post_app_api(
    app_id: str,
    path: str,
    request: Request,
):
    """App API 端点（POST）"""
    return await _route_request(app_id, f"api/{path}", request, None)


async def _route_request(app_id: str, path: str, request: Request, body: Optional[Dict]) -> Response:
    """路由请求到对应的处理函数"""
    agent_hash = _get_agent_hash(request)
    if not agent_hash or not _validate_id(agent_hash, "agent_hash"):
        raise HTTPException(status_code=400, detail="Invalid agent hash")
    if not _validate_id(app_id):
        raise HTTPException(status_code=400, detail="Invalid app_id")

    config = get_app_config(agent_hash, app_id)
    if not config:
        return _html_error("🔍", "应用不存在", f"Agent <code>{agent_hash}</code> 没有注册名为 <code>{app_id}</code> 的应用。请确认应用 ID 是否正确。")

    routes = config.get("routes", [])
    if not routes:
        return _html_error("📭", "路由为空", f"应用 <code>{app_id}</code> 的 routes.json 中没有定义任何路由。")

    # 匹配路由
    matched_route = None
    for route in routes:
        # Match data type routes as prefixes (everything after endpoint is sub-path)
        if route.get("type") == "data":
            endpoint_norm = route.get("endpoint", "").lstrip("/")
            if path == endpoint_norm or path.startswith(endpoint_norm + "/"):
                matched_route = route
                break

        endpoint = route.get("endpoint", "").lstrip("/")
        if path == endpoint:
            matched_route = route
            break
        # 前缀匹配（如 /api/words → /api/{path}）
        if "/" in endpoint and endpoint.endswith("/{path}"):
            prefix = endpoint[:-6]  # 去掉 {path}
            if path.startswith(prefix):
                matched_route = route
                break

    if not matched_route and path == "index.html":
        # 首页：尝试匹配 "/" 端点
        for route in routes:
            if route.get("endpoint", "") == "/":
                matched_route = route
                break

    if not matched_route:
        return _html_error("🚧", "页面不存在", f"应用 <code>{app_id}</code> 没有定义路径 <code>/{path}</code>。")

    # 执行中间件（auth 检查）
    route_middleware = matched_route.get("middleware", [])
    global_middleware = config.get("middleware", {})
    for mw_name in route_middleware:
        mw_config = global_middleware.get(mw_name, {})
        if not mw_config:
            continue
        mw_type = mw_config.get("type", "")
        if mw_type == "platform_auth":
            try:
                user_id = await get_user_for_page(request)
                if not user_id:
                    raise HTTPException(status_code=401, detail="Authentication required")
            except Exception:
                raise HTTPException(status_code=401, detail="Authentication required")
        elif mw_type == "bash":
            pass  # 未来支持 bash 鉴权脚本

    # 分派到对应处理器
    route_type = matched_route.get("type", "static")

    # AI 和 code 端点限速
    if route_type in ("ai", "code"):
        from services.apps_service import check_rate_limit
        if not check_rate_limit(agent_hash):
            raise HTTPException(status_code=429, detail="Rate limit exceeded (30 req/min)")

    if route_type == "static":
        file_path = matched_route.get("file", "index.html")
        content, status_code, content_type = await handle_static(agent_hash, app_id, file_path, matched_route)
        if status_code != 200:
            return JSONResponse(content=content, status_code=status_code)
        return Response(content=content, media_type=content_type)

    elif route_type == "ai":
        if body is None:
            raw = await request.body()
            if len(raw) > MAX_BODY_SIZE:
                raise HTTPException(status_code=413, detail="Request body too large (max 1MB)")
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
        result, status_code = await handle_ai(agent_hash, app_id, body, matched_route)
        return JSONResponse(content=result, status_code=status_code)

    elif route_type == "code":
        if body is None:
            raw = await request.body()
            if len(raw) > MAX_BODY_SIZE:
                raise HTTPException(status_code=413, detail="Request body too large (max 1MB)")
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
        result, status_code = await handle_code(agent_hash, app_id, body, matched_route)
        return JSONResponse(content=result, status_code=status_code)

    elif route_type == "data":
        # sub_path is everything after the endpoint prefix
        endpoint = matched_route.get("endpoint", "/data").lstrip("/")
        sub_path = path
        if sub_path.startswith(endpoint):
            sub_path = sub_path[len(endpoint):]
        if not sub_path.startswith("/"):
            sub_path = "/" + sub_path
        result, status_code = await handle_data(agent_hash, app_id, sub_path, matched_route, request)
        return JSONResponse(content=result, status_code=status_code)

    raise HTTPException(status_code=400, detail=f"Unknown route type: {route_type}")
