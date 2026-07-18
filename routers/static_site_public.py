"""
静态网站公开访问路由

处理 {subdomain}.site.firstentrance.net 的公开访问请求。
用户无需认证即可访问已发布的静态网站文件。

路由优先级：
1. 精确匹配文件路径
2. 目录下的 index.html
3. 404 页面

支持的功能：
- 自动 Content-Type 检测
- 默认首页（index.html）
- 目录浏览（可选）
"""

import re
import os
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response, HTMLResponse
from urllib.parse import unquote

from models.database import SessionLocal, StaticSite
from services.storage_service import StorageService
from services.static_site_service import StaticSiteService
from config import settings
from routers.feclaw_domain import _get_domain, extract_hash_from_host


router = APIRouter(tags=["静态网站公开访问"])


# 支持的 MIME 类型映射
MIME_TYPES = {
    # 文本类型
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    
    # 图片类型
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    
    # 字体类型
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    
    # 音视频类型
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".ogg": "audio/ogg",
    
    # 文档类型
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    
    # 其他
    ".wasm": "application/wasm",
}

# 默认首页文件
DEFAULT_INDEX_FILES = ["index.html", "index.htm"]

# 404 页面模板
NOT_FOUND_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>404 - 页面未找到</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }}
        .container {{
            text-align: center;
            padding: 40px;
        }}
        h1 {{
            font-size: 120px;
            margin: 0;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.2);
        }}
        p {{
            font-size: 24px;
            margin: 20px 0;
        }}
        a {{
            color: white;
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>404</h1>
        <p>页面未找到</p>
        <p><a href="/">返回首页</a></p>
    </div>
</body>
</html>
"""

# FeClaw 品牌 404 页面模板（当访问 feclaw 域名的未知路径时使用）
FECLAW_NOT_FOUND_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>404 - 页面未找到 - FeClaw</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            background: linear-gradient(135deg, #0f0f23 0%, #1a1a2e 100%);
            color: #e0e0e0;
        }}
        .container {{
            text-align: center;
            padding: 40px;
        }}
        .logo-icon {{
            width: 80px; height: 80px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 20px;
            display: inline-flex; align-items: center; justify-content: center;
            font-size: 36px;
            margin-bottom: 24px;
        }}
        h1 {{
            font-size: 72px;
            margin: 0;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}
        p {{
            font-size: 18px;
            margin: 16px 0 32px;
            color: #888;
        }}
        a {{
            display: inline-block;
            padding: 12px 32px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-size: 15px;
            transition: opacity 0.2s;
        }}
        a:hover {{ opacity: 0.9; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="logo-icon">🌊</div>
        <h1>404</h1>
        <p>页面未找到 — 您访问的页面不存在或已被移除</p>
        <a href="/">返回首页</a>
    </div>
</body>
</html>
"""

# 站点暂停页面模板
SUSPENDED_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>站点已暂停</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            background: linear-gradient(135deg, #f5af19 0%, #f12711 100%);
            color: white;
        }}
        .container {{
            text-align: center;
            padding: 40px;
        }}
        h1 {{
            font-size: 48px;
            margin: 0;
        }}
        p {{
            font-size: 20px;
            margin: 20px 0;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>⚠️ 站点已暂停</h1>
        <p>此站点当前不可访问</p>
        <p>请联系站点管理员</p>
    </div>
</body>
</html>
"""


def get_content_type(file_path: str) -> str:
    """
    根据文件扩展名获取 Content-Type
    
    Args:
        file_path: 文件路径
        
    Returns:
        MIME 类型字符串
    """
    ext = "." + file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    return MIME_TYPES.get(ext, "application/octet-stream")


def parse_subdomain(host: str) -> Optional[str]:
    """
    从 Host 头解析子域名
    
    支持格式:
    - {subdomain}.site.firstentrance.net
    - {subdomain}.site.firstentrance.net:8080
    - localhost（开发模式，返回 None）
    
    Args:
        host: HTTP Host 头
        
    Returns:
        子域名或 None
    """
    # 移除端口号
    host = host.split(":")[0]
    
    # 开发环境
    if host in ["localhost", "127.0.0.1"]:
        return None
    
    # 匹配 *.site.firstentrance.net
    pattern = r'^([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.site\.firstentrance\.net$'
    match = re.match(pattern, host, re.IGNORECASE)
    
    if match:
        return match.group(1).lower()
    
    return None


def get_site_by_subdomain(subdomain: str) -> Optional[StaticSite]:
    """
    根据子域名查询站点
    
    Args:
        subdomain: 子域名
        
    Returns:
        StaticSite 模型实例或 None
    """
    db = SessionLocal()
    try:
        site = db.query(StaticSite).filter(
            StaticSite.subdomain == subdomain,
            StaticSite.status == "active"
        ).first()
        return site
    finally:
        db.close()


def get_cos_key(site: StaticSite, file_path: str) -> str:
    """
    生成 COS 文件 key
    
    Args:
        site: StaticSite 实例
        file_path: 文件路径（相对于站点根目录）
        
    Returns:
        COS key
    """
    # COS 路径格式: firstentrance/static-sites/{user_id}/{file_path}
    prefix = "firstentrance/static-sites/"
    return f"{prefix}{site.user_id}/{file_path.lstrip('/')}"


@router.api_route("/{file_path:path}", methods=["GET", "HEAD"])
async def serve_static_file(request: Request, file_path: str = ""):
    """
    静态文件服务路由
    
    处理 {subdomain}.site.firstentrance.net/{file_path} 的请求
    
    Args:
        request: FastAPI 请求对象
        file_path: 请求的文件路径
        
    Returns:
        文件内容或错误页面
    """
    # 排除应用自身的静态文件路径（由 StaticFiles mount 处理）
    if file_path.startswith("static/") or file_path == "static":
        raise HTTPException(status_code=404, detail="Use app static files")

    # 排除 API 路由（由其他 router 处理）
    if file_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API routes handled by other routers")
    
    # 解析子域名
    host = _get_domain(request)
    subdomain = parse_subdomain(host)
    
    if not subdomain:
        # 检查是否为 FeClaw 子域名（返回 FeClaw 品牌 404）
        if extract_hash_from_host(host) or (settings.FECLAW_PUBLIC_URL and host.endswith(f".{settings.FECLAW_PUBLIC_URL}") and host != settings.FECLAW_PUBLIC_URL):
            return HTMLResponse(
                content=FECLAW_NOT_FOUND_TEMPLATE,
                status_code=404
            )
        # 不是静态站点域名，返回 404
        raise HTTPException(status_code=404, detail="Not a static site domain")
    
    # 查询站点
    site = get_site_by_subdomain(subdomain)
    
    if not site:
        # 站点不存在
        return HTMLResponse(
            content=NOT_FOUND_TEMPLATE,
            status_code=404
        )
    
    # 检查站点状态
    if site.status == "suspended":
        return HTMLResponse(
            content=SUSPENDED_TEMPLATE,
            status_code=503
        )
    
    # URL 解码文件路径
    file_path = unquote(file_path)
    
    # 安全检查：防止路径遍历攻击
    normalized = os.path.normpath(file_path)
    if normalized.startswith("..") or os.path.isabs(normalized):
        raise HTTPException(status_code=400, detail="Invalid path")
    
    # 初始化存储服务
    storage = StorageService()
    
    # 尝试加载文件
    content = None
    actual_path = file_path
    
    # 1. 尝试精确匹配
    if file_path:
        cos_key = get_cos_key(site, file_path)
        content = storage.get_file_content(cos_key)
    
    # 2. 如果是目录，尝试默认首页
    if content is None and (not file_path or file_path.endswith("/")):
        for index_file in DEFAULT_INDEX_FILES:
            index_path = f"{file_path.rstrip('/')}/{index_file}" if file_path else index_file
            cos_key = get_cos_key(site, index_path)
            content = storage.get_file_content(cos_key)
            if content is not None:
                actual_path = index_path
                break
    
    # 3. 尝试添加 .html 后缀（SPA 支持）
    if content is None and file_path and not "." in file_path.split("/")[-1]:
        html_path = f"{file_path}.html"
        cos_key = get_cos_key(site, html_path)
        content = storage.get_file_content(cos_key)
        if content is not None:
            actual_path = html_path
    
    # 文件不存在
    if content is None:
        # 尝试返回根目录的 404.html
        cos_key = get_cos_key(site, "404.html")
        content_404 = storage.get_file_content(cos_key)
        if content_404:
            return HTMLResponse(
                content=content_404,
                status_code=404
            )
        
        # 返回默认 404 页面
        return HTMLResponse(
            content=NOT_FOUND_TEMPLATE,
            status_code=404
        )
    
    # HEAD 请求只返回头部
    if request.method == "HEAD":
        return Response(
            headers={
                "Content-Type": get_content_type(actual_path),
                "Content-Length": str(len(content))
            }
        )
    
    # 记录访问统计（异步，不影响响应）
    try:
        service = StaticSiteService(StorageService())
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent", "")[:512]  # 截断防止过长
        referer = request.headers.get("referer", "")[:512]
        
        service.record_visit(
            site_id=site.id,
            file_path=actual_path,
            client_ip=client_ip,
            user_agent=user_agent,
            referer=referer,
            response_size=len(content),
            response_status=200
        )
    except Exception as e:
        # 统计记录失败不影响正常访问
        logging.getLogger(__name__).warning(f"record_visit error: {e}")
    
    # 返回文件内容
    return Response(
        content=content,
        media_type=get_content_type(actual_path)
    )


@router.api_route("/", methods=["GET", "HEAD"])
async def serve_index(request: Request):
    """
    首页路由
    
    处理 {subdomain}.site.firstentrance.net/ 的请求
    """
    return await serve_static_file(request, "")
