"""
App Service — Agent 自部署 Web 应用系统

Agent 通过 route_register 注册路由端点，将 Web 应用部署到 `https://{agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/`。

三种路由类型：
- static: 直接返回文件内容
- ai: 创建隔离 SubAgent 处理请求，返回 JSON
- code: bwrap 沙箱执行 Python 脚本，返回 stdout
"""

import json
import logging
import os
import time
import asyncio
from collections import defaultdict
from typing import Optional, Dict, Any, List

from services.virtual_filesystem import VirtualFileSystem
from services.tool_registry import get_tool_schemas

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────

APPS_DIR = "/workspace/apps"
PUBLIC_TEMPLATES_DIR = "/public/feclaw/templates"
MAX_APPS_PER_AGENT = 10
MAX_CODE_TIMEOUT = 30
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# ── 路由注册（内存索引，重启后由 route_register 重建）──

_registered_apps: Dict[str, Dict] = {}  # {agent_hash: {app_id: config}}
_registered_apps_lock = asyncio.Lock()

# ── 简单令牌桶限速 ──────────────────────────────────────

_rate_limit_buckets: Dict[str, list] = defaultdict(list)


def check_rate_limit(agent_hash: str, max_reqs: int = 30, window: int = 60) -> bool:
    """每 agent 每窗口最多 max_reqs 次请求"""
    now = time.time()
    bucket = _rate_limit_buckets[agent_hash]
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= max_reqs:
        return False
    bucket.append(now)
    return True


# ── 核心函数 ────────────────────────────────────────────


def _get_vfs(agent_hash: str) -> VirtualFileSystem:
    return VirtualFileSystem(agent_hash=agent_hash)


def _load_app_config(agent_hash: str, app_id: str) -> Optional[Dict]:
    """从 VFS 加载 App 配置（不修改 _registered_apps）"""
    vfs = _get_vfs(agent_hash)
    config = vfs.cat(f"{APPS_DIR}/{app_id}/routes.json")
    if config and not config.startswith("Error:"):
        try:
            return json.loads(config)
        except json.JSONDecodeError:
            return None
    return None


def get_app_config(agent_hash: str, app_id: str) -> Optional[Dict]:
    """加载 App 配置（只读，不修改 _registered_apps）"""
    # 1. 先查注册的内存索引
    agent_apps = _registered_apps.get(agent_hash, {})
    if app_id in agent_apps:
        return agent_apps[app_id]

    # 2. 查 VFS 工作区
    return _load_app_config(agent_hash, app_id)


async def register_app(agent_hash: str, app_id: str) -> Optional[Dict]:
    """注册 App（异步，带锁）"""
    async with _registered_apps_lock:
        apps = _registered_apps.setdefault(agent_hash, {})
        if len(apps) >= MAX_APPS_PER_AGENT:
            return None

        config = _load_app_config(agent_hash, app_id)
        if not config:
            return None

        apps[app_id] = config
        return config


def register_app_sync(agent_hash: str, app_id: str) -> Optional[Dict]:
    """同步注册 App（供工具调用）"""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        return asyncio.run_coroutine_threadsafe(
            register_app(agent_hash, app_id), loop
        ).result(timeout=30)
    except RuntimeError:
        # 没有运行中的事件循环 → 创建临时循环
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(register_app(agent_hash, app_id))
        except Exception:
            return None
        finally:
            loop.close()


async def unregister_app(agent_hash: str, app_id: str) -> bool:
    """注销 App（异步，带锁）"""
    async with _registered_apps_lock:
        apps = _registered_apps.get(agent_hash)
        if apps and app_id in apps:
            del apps[app_id]
            return True
    return False


def unregister_app_sync(agent_hash: str, app_id: str) -> bool:
    """同步注销 App（供工具调用）"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run_coroutine_threadsafe(
                unregister_app(agent_hash, app_id), loop
            ).result()
    except RuntimeError:
        pass
    return False


def list_registered_apps(agent_hash: str) -> List[str]:
    """列出已注册的 App ID"""
    return list(_registered_apps.get(agent_hash, {}).keys())


# ── 路由处理 ────────────────────────────────────────────


async def handle_static(agent_hash: str, app_id: str, file_path: str, config: Dict) -> tuple:
    """处理 static 类型请求：返回文件内容"""
    # 安全检查：防止 path traversal（双重校验）
    if ".." in file_path:
        return {"error": "Invalid path"}, 400
    normalized = os.path.normpath(file_path)
    if normalized.startswith("..") or normalized != file_path.strip("/"):
        return {"error": "Invalid path"}, 400

    full_path = f"{APPS_DIR}/{app_id}/{file_path}"
    vfs = _get_vfs(agent_hash)
    content = vfs.cat(full_path)

    if content.startswith("Error:"):
        # 尝试从公共模板读取
        public_path = f"{PUBLIC_TEMPLATES_DIR}/{app_id}/{file_path}"
        content = vfs.cat(public_path)
        if content.startswith("Error:"):
            return {"error": "File not found"}, 404

    # 判断 Content-Type
    ext = os.path.splitext(file_path)[1].lower()
    content_types = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }
    content_type = content_types.get(ext, "application/octet-stream")

    # 为 HTML 注入 <base> 标签，确保相对路径跳转正确
    if ext == ".html" and "<head" in content[:200]:
        base_tag = f'<base href="/apps/{app_id}/">'
        if base_tag not in content[:300]:
            content = content.replace("<head>", f"<head>\n    {base_tag}", 1)

    return content, 200, content_type


async def handle_ai(agent_hash: str, app_id: str, body: Dict, route_config: Dict) -> tuple:
    """处理 ai 类型请求：创建 SubAgent 返回 JSON"""
    subagent_cfg = route_config.get("subagent", {})
    system_prompt = subagent_cfg.get("system_prompt", "")
    tools = subagent_cfg.get("tools", ["web_search"])
    tool_filter_cfg = subagent_cfg.get("tool_filter", {})
    max_turns = subagent_cfg.get("max_turns", 3)
    timeout = subagent_cfg.get("timeout", 30)
    model_cfg = subagent_cfg.get("model", {})

    # 构建 tool_filter
    allowed = tool_filter_cfg.get("allow", tools)

    def route_tool_filter(name: str, args: dict) -> bool:
        return name in allowed

    # 调用 LLM
    from services.llm_service import llm_service
    prompt = body.get("query", "") or body.get("word", "") or json.dumps(body, ensure_ascii=False)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    max_rounds = max_turns
    try:
        async with asyncio.timeout(timeout):
            for round_num in range(max_rounds):
                response = await llm_service.chat_with_tools(
                    messages=messages,
                    provider=model_cfg.get("provider"),
                    model=model_cfg.get("name"),
                    tools=get_tool_schemas(),
                    request_type=f"app_{app_id}",
                    tool_filter=route_tool_filter,
                )

                tool_calls = response.get("tool_calls")
                content = response.get("content", "")

                if not tool_calls:
                    break

                # 执行工具
                for tc in tool_calls:
                    func_name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, KeyError):
                        args = {}

                    from services.search_service import SearchService
                    if func_name == "web_search":
                        query = args.get("query", prompt)
                        result = await SearchService().search_qwen(query)
                    else:
                        result = f"Error: 不允许使用 {func_name}"

                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": str(result)[:3000]})

    except asyncio.TimeoutError:
        logger.warning("[Apps] AI handler timeout: app=%s", app_id)
        return {"error": "AI endpoint timeout", "timeout": timeout}, 504

    # 尝试解析 JSON
    result_dict = {}
    if content:
        content = content.strip()
        # 剥离 markdown 代码块标记（如 ```json 和 ```）
        if content.startswith("```"):
            import re
            content = re.sub(r'^```\w*\s*\n?|\n?```\s*$', '', content).strip()
        if content.startswith("{"):
            try:
                result_dict = json.loads(content)
            except json.JSONDecodeError:
                result_dict = {"result": content}
        else:
            result_dict = {"result": content}

    return result_dict, 200


async def handle_code(agent_hash: str, app_id: str, body: Dict, route_config: Dict) -> tuple:
    """处理 code 类型请求：bwrap 沙箱执行 Python 脚本"""
    script = route_config.get("script", "")
    if not script:
        return {"error": "No script configured"}, 400

    vfs = _get_vfs(agent_hash)
    script_path = f"{APPS_DIR}/{app_id}/{script}"
    script_content = vfs.cat(script_path)

    if script_content.startswith("Error:"):
        # 尝试从公共模板读取
        public_path = f"{PUBLIC_TEMPLATES_DIR}/{app_id}/{script}"
        script_content = vfs.cat(public_path)
        if script_content.startswith("Error:"):
            return {"error": f"Script not found: {script}"}, 404

    from services.sandbox_manager import SandboxManager
    sandbox = SandboxManager(vfs, f"app_{agent_hash}")

    # 注入 QUERY_JSON 环境变量到沙箱
    query_json_raw = json.dumps(body, ensure_ascii=False)
    code_with_env = (
        "import os, json\n"
        "os.environ['QUERY_JSON'] = " + repr(query_json_raw) + "\n\n"
    ) + script_content

    result = sandbox.exec_code(
        code=code_with_env,
        timeout=route_config.get("timeout", MAX_CODE_TIMEOUT),
    )

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    exit_code = result.exit_code

    if exit_code != 0:
        logger.error("[Apps] code execution failed: app=%s script=%s exit_code=%d stderr=%s",
                     app_id, script, exit_code, stderr[:200])
        return {"error": f"Script failed (exit {exit_code})", "stderr": stderr[:500]}, 500

    # 尝试解析 JSON 输出
    if stdout:
        try:
            return json.loads(stdout), 200
        except json.JSONDecodeError:
            return {"result": stdout}, 200

    return {"result": stdout}, 200


# ── Data 路由处理 ──────────────────────────────────────────

# Known query params (not treated as dynamic filters)
_KNOWN_PARAMS = {"search", "page", "limit", "sort", "order", "schema"}


async def handle_data(agent_hash: str, app_id: str, sub_path: str, route_config: Dict, request) -> tuple:
    """Handle data type routes: schema, single-item, search/filter/paginate.

    The endpoint in routes.json is a prefix; everything after it is sub_path.
    """
    from services.data_registry import get_config, QueryEngine
    from models.database import SessionLocal

    dataset = route_config.get("dataset", "")
    config = get_config(dataset)
    if config is None:
        return {"error": "dataset_not_found", "dataset": dataset}, 404

    sub_path = sub_path.strip("/")

    # ── _schema ──
    if sub_path == "_schema":
        db = SessionLocal()
        try:
            total = db.query(config.model).count()
        finally:
            db.close()
        return {
            "dataset": dataset,
            "label": config.label,
            "total": total,
            "schema": config.to_schema(),
        }, 200

    # ── Single item by identifier ──
    if sub_path:
        engine = QueryEngine(dataset, config)
        db = SessionLocal()
        try:
            item = engine.get_by_identifier(db, sub_path)
            if item is None:
                return {
                    "error": "item_not_found",
                    "dataset": dataset,
                    "identifier": sub_path,
                }, 404
            return item, 200
        finally:
            db.close()

    # ── Search / filter / paginate (sub_path is empty) ──
    query_params = dict(request.query_params)

    search_val = query_params.get("search")
    try:
        page = int(query_params.get("page", 1))
        limit = int(query_params.get("limit", 20))
        sort = query_params.get("sort")
        order = query_params.get("order", "asc")
    except (ValueError, TypeError):
        return {"error": "invalid_parameter", "message": "page, limit must be integers"}, 400
    include_schema = query_params.get("schema", "true").lower() != "false"

    # Parse dynamic filter params
    exact_filters = {}
    fuzzy_filters = {}
    for key, val in query_params.items():
        if ":" in key:
            field, _, filter_val = key.partition(":")
            if field and field not in _KNOWN_PARAMS:
                fuzzy_filters[field] = filter_val or ""
            continue
        if key in _KNOWN_PARAMS:
            continue
        if val:
            exact_filters[key] = val

    limit = min(limit, config.max_limit)

    engine = QueryEngine(dataset, config)
    db = SessionLocal()
    try:
        result = engine.search(
            db,
            search=search_val,
            page=page,
            limit=limit,
            sort=sort,
            order=order,
            exact_filters=exact_filters if exact_filters else None,
            fuzzy_filters=fuzzy_filters if fuzzy_filters else None,
        )
        if include_schema:
            result["schema"] = config.to_schema()
        return result, 200
    finally:
        db.close()
