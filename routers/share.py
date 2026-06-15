"""
分享链接路由 - 解析分享 token 并重定向/提供文件
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import RedirectResponse, FileResponse, Response
from sqlalchemy.orm import Session
from services.share_service import decode_share_token
from models.database import get_db
from config import settings
import os, logging
from urllib.parse import quote

logger = logging.getLogger(__name__)

# GeoGebra HTML 模板
GGB_TEMPLATE_2D = """<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GeoGebra 2D</title>
    <script src="https://www.geogebra.org/apps/deployggb.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { width: 100vw; height: 100vh; overflow: hidden; background: #f0f0f0; }
        #ggb-element { width: 100vw; height: 100vh; }
    </style>
</head>
<body>
<div id="ggb-element"></div>
<script>
    (function() {
        var params = {
            "appName": "classic",
            "width": window.innerWidth,
            "height": window.innerHeight,
            "showToolBar": true,
            "showAlgebraInput": true,
            "showMenuBar": true,
            "enableRightClick": true,
            "appletOnLoad": function(api) {
                var cmds = COMMANDS;
                for (var i = 0; i < cmds.length; i++) {
                    try { api.evalCommand(cmds[i]); } catch(e) { console.warn(cmds[i], e); }
                }
            }
        };
        var el = document.getElementById("ggb-element");
        el.style.width = window.innerWidth + "px";
        el.style.height = window.innerHeight + "px";
        var app = new GGBApplet(params, true);
        app.inject("ggb-element");
    })();
</script>
</body>
</html>"""

GGB_TEMPLATE_3D = """<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GeoGebra 3D</title>
    <script src="https://www.geogebra.org/apps/deployggb.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { width: 100vw; height: 100vh; overflow: hidden; background: #f0f0f0; }
        #ggb-element { width: 100vw; height: 100vh; }
    </style>
</head>
<body>
<div id="ggb-element"></div>
<script>
    (function() {
        var params = {
            "appName": "3d",
            "width": window.innerWidth,
            "height": window.innerHeight,
            "showToolBar": true,
            "showAlgebraInput": true,
            "showMenuBar": true,
            "enableRightClick": true,
            "appletOnLoad": function(api) {
                var cmds = COMMANDS;
                for (var i = 0; i < cmds.length; i++) {
                    try { api.evalCommand(cmds[i]); } catch(e) { console.warn(cmds[i], e); }
                }
            }
        };
        var el = document.getElementById("ggb-element");
        el.style.width = window.innerWidth + "px";
        el.style.height = window.innerHeight + "px";
        var app = new GGBApplet(params, true);
        app.inject("ggb-element");
    })();
</script>
</body>
</html>"""


def _render_ggb_file(content: bytes, is_3d: bool = False) -> str:
    """将 .2dggb/.3dggb 内容渲染为 GeoGebra HTML"""
    import json
    commands_text = content.decode("utf-8", errors="replace").strip()
    commands = [line.strip() for line in commands_text.split("\n")
                if line.strip() and not line.strip().startswith("#")]
    commands_json = json.dumps(commands)
    template = GGB_TEMPLATE_3D if is_3d else GGB_TEMPLATE_2D
    return template.replace("COMMANDS", commands_json)


router = APIRouter(tags=["share"])


@router.get("/s/{slug}")
async def resolve_share_by_slug(slug: str, request: Request, db: Session = Depends(get_db)):
    """通过友好短链 slug 解析分享链接（如 /s/sunset-oak-jupiter）
    
    支持子域名隔离：abcdefgh.feclaw.lizidaren.cn/s/xxx → 仅查该 Agent 的链接
    无子域名 ⚏ 回退全局查找
    """
    from services.share_service import resolve_slug

    # 从 Host 头提取 agent_hash（子域名前缀）
    host = request.headers.get("host", "")
    agent_hash = None
    if host and settings.FECLAW_DOMAIN in host:
        prefix = host.split(f".{settings.FECLAW_DOMAIN}")[0]
        # 4 字符的 agent hash 子域名
        if prefix and prefix != settings.FECLAW_DOMAIN and len(prefix) == 4:
            agent_hash = prefix

    mapping = resolve_slug(slug, agent_hash, db)
    if not mapping:
        raise HTTPException(status_code=404, detail="分享链接不存在或已过期")

    vfs_path = mapping.vfs_path

    # 通过 COS 获取文件（复用现有逻辑）
    from services.storage_service import StorageService
    from datetime import datetime

    if mapping.expires_at and datetime.utcnow() > mapping.expires_at:
        raise HTTPException(status_code=410, detail="分享链接已过期")

    storage = StorageService()
    cos_keys = []
    # vfs_path 可能以 /workspace/ 开头，拼接时避免重复 workspace 前缀
    _clean = vfs_path.removeprefix("/workspace/")
    if mapping.agent_hash:
        cos_keys.append(f"feclaw/agents/{mapping.agent_hash}/workspace/{_clean}")
        cos_keys.append(f"feclaw/agents/{mapping.agent_hash}{vfs_path}")
    cos_keys.append(f"feclaw/vfs{vfs_path}")
    cos_keys.append(f"feclaw/user_workspaces/{mapping.user_id}/workspace/{_clean}")
    # 也尝试无 /workspace/ 前缀的原始路径（兼容旧存储）
    cos_keys.append(f"feclaw/user_workspaces/{mapping.user_id}{vfs_path}")

    for cos_key in cos_keys:
        try:
            content = storage.get_file_content(cos_key)
            if content:
                ext = os.path.splitext(vfs_path)[1].lower()
                if ext == ".md":
                    md_content = content.decode("utf-8")
                    import json
                    safe_md = json.dumps(md_content)
                    html_page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{os.path.basename(vfs_path)}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5.5.1/github-markdown.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.1/marked.min.js"></script>
<style>body{{max-width:800px;margin:40px auto;padding:0 20px;}}</style>
</head><body><article class="markdown-body" id="c"></article>
<script>
var html = marked.parse({safe_md});
html = html.replace(/\\$\\$([\\s\\S]*?)\\$\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:true,throwOnError:false}}); }} catch(e) {{ return '$$'+eq+'$$'; }}
}});
html = html.replace(/\\$([^\\$\\n]+?)\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:false,throwOnError:false}}); }} catch(e) {{ return '$'+eq+'$'; }}
}});
document.getElementById('c').innerHTML = html;
</script>
</body></html>"""
                    return Response(content=html_page, media_type="text/html")
                elif ext == ".2dggb":
                    return Response(content=_render_ggb_file(content, is_3d=False), media_type="text/html")
                elif ext == ".3dggb":
                    return Response(content=_render_ggb_file(content, is_3d=True), media_type="text/html")

                mime_map = {".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                           ".png": "image/png", ".jpg": "image/jpeg",
                           ".json": "application/json", ".py": "text/plain; charset=utf-8"}
                ct = mime_map.get(ext, "application/octet-stream")
                _fname = os.path.basename(vfs_path)
                return Response(content=content, media_type=ct,
                              headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(_fname)}"})
        except Exception:
            continue

    raise HTTPException(status_code=404, detail="文件不存在或已删除")


@router.get("/share/{token}")
async def resolve_share(token: str, db: Session = Depends(get_db)):
    """
    解析分享链接 token，返回文件或重定向
    """
    vfs_path = decode_share_token(token, db=db)
    if not vfs_path:
        raise HTTPException(status_code=404, detail="分享链接无效或已过期")

    # 从 token 中提取 share_hash 查 agent_hash
    try:
        import base64
        raw_token = token
        padding = 4 - len(raw_token) % 4
        if padding != 4:
            raw_token += "=" * padding
        decoded = base64.urlsafe_b64decode(raw_token).decode()
        parts = decoded.rsplit("|", 2)
        share_hash = parts[1] if len(parts) >= 2 else None
    except Exception:
        share_hash = None

    agent_hash = None
    if share_hash:
        from models.database import ShareMapping
        mapping = db.query(ShareMapping).filter(
            ShareMapping.share_hash == share_hash
        ).first()
        if mapping:
            agent_hash = mapping.agent_hash

    # 通过 COS 获取文件
    try:
        from services.storage_service import StorageService
        storage = StorageService()

        cos_keys = []
        # vfs_path 可能以 /workspace/ 开头，拼接时避免重复 workspace 前缀
        _clean = vfs_path.removeprefix("/workspace/")
        if agent_hash:
            cos_keys.append(f"feclaw/agents/{agent_hash}/workspace/{_clean}")
            cos_keys.append(f"feclaw/agents/{agent_hash}{vfs_path}")  # 无 workspace 前缀
        # 也尝试 vfs 路径
        cos_keys.append(f"feclaw/vfs{vfs_path}")
        # 也尝试 user_workspaces 兜底
        cos_keys.append(f"feclaw/user_workspaces/2/workspace/{_clean}")
        # 也尝试无 /workspace/ 前缀的原始路径
        cos_keys.append(f"feclaw/user_workspaces/2{vfs_path}")

        for cos_key in cos_keys:
            content = storage.get_file_content(cos_key)
            if content:
                content_type = "application/octet-stream"
                ext = os.path.splitext(vfs_path)[1].lower()

                # Markdown 文件返回渲染后的 HTML 页面
                if ext == ".md":
                    md_content = content.decode("utf-8")
                    import json
                    safe_md = json.dumps(md_content)
                    html_page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{os.path.basename(vfs_path)}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5.5.1/github-markdown.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.1/marked.min.js"></script>
<style>body{{max-width:800px;margin:40px auto;padding:0 20px;}}</style>
</head><body><article class="markdown-body" id="c"></article>
<script>
// 渲染 Markdown 后替换公式
var html = marked.parse({safe_md});
html = html.replace(/\\$\\$([\\s\\S]*?)\\$\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:true,throwOnError:false}}); }} catch(e) {{ return '$$'+eq+'$$'; }}
}});
html = html.replace(/\\$([^\\$\\n]+?)\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:false,throwOnError:false}}); }} catch(e) {{ return '$'+eq+'$'; }}
}});
document.getElementById('c').innerHTML = html;
</script>
</body></html>"""
                    return Response(content=html_page, media_type="text/html")
                elif ext == ".2dggb":
                    return Response(content=_render_ggb_file(content, is_3d=False), media_type="text/html")
                elif ext == ".3dggb":
                    return Response(content=_render_ggb_file(content, is_3d=True), media_type="text/html")

                mime_map = {".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                           ".png": "image/png", ".jpg": "image/jpeg",
                           ".json": "application/json", ".py": "text/plain; charset=utf-8"}
                content_type = mime_map.get(ext, "application/octet-stream")
                _fname = os.path.basename(vfs_path)
                return Response(content=content, media_type=content_type,
                              headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(_fname)}"})
    except Exception as e:
        logger.warning(f"[Share] COS fetch failed: {e}")

    # 尝试通过 FUSE 本地路径
    fuse_path = f"/tmp/feclaw-fuse{vfs_path}"
    if os.path.isfile(fuse_path):
        return FileResponse(fuse_path, filename=os.path.basename(vfs_path))

    raise HTTPException(status_code=404, detail="文件不存在或已删除")
