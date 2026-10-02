"""
分享链接路由 - 解析分享 token 并重定向/提供文件
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import RedirectResponse, FileResponse, Response
from sqlalchemy.orm import Session
from services.share_service import decode_share_token, verify_share_password
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
    #c img{{max-width:100%;height:auto;}}
.markdown-body pre{{overflow-x:auto;}}
.katex-display{{overflow-x:auto;overflow-y:hidden;max-width:100%;}}
@keyframes fadeIn{{from{{opacity:0;}}to{{opacity:1;}}}}
.feclaw-ref-markdown strong{{color:#f0c040;}}
.feclaw-ref-markdown code{{background:#333;color:#7ecfff;padding:1px 5px;border-radius:3px;font-size:13px;}}
.feclaw-ref-markdown a{{color:#5b7cfa;}}
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

GGB_TEMPLATE_3D = """..."""  # 保持原样，占位

JSXGRAPH_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>JSXGraph - 交互式几何</title>
    <link rel="stylesheet" href="/static/jsxgraph.css" />
    <script src="/static/jsxgraphcore.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { width: 100vw; height: 100vh; overflow: hidden; background: #fafafa; }
        #jxgbox { width: 100vw; height: 100vh; }
    </style>
</head>
<body>
<div id="jxgbox" class="jxgbox"></div>
<script>
(function() {
    try {
        JSXGRAPH_CODE
    } catch(e) {
        document.body.innerHTML = '<div style="padding:40px;font-family:sans-serif;">'
            + '<h2>JSXGraph 渲染错误</h2>'
            + '<pre style="background:#fee;padding:16px;border-radius:8px;margin-top:16px;overflow:auto;">'
            + e.toString() + '</pre></div>';
    }
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


def _render_jsxgraph_file(content: bytes) -> str:
    """将 .jsxgraph 内容渲染为 JSXGraph HTML"""
    code = content.decode("utf-8", errors="replace").strip()
    return JSXGRAPH_TEMPLATE.replace("JSXGRAPH_CODE", code)


def _share_error_page(status_code: int, title: str, message: str) -> Response:
    """分享页友好错误页（替代 FastAPI 默认的裸 500 / JSON 错误）。"""
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f5f6f8;color:#333;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
.card{{background:#fff;border-radius:12px;padding:40px 48px;max-width:420px;
box-shadow:0 2px 12px rgba(0,0,0,.06);text-align:center;}}
h1{{font-size:20px;margin:0 0 12px;}}
p{{font-size:15px;line-height:1.6;color:#666;margin:0;}}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{message}</p></div></body></html>"""
    return Response(content=html, status_code=status_code, media_type="text/html")


router = APIRouter(tags=["share"])


@router.get("/s/{slug}")
async def resolve_share_by_slug(slug: str, request: Request, db: Session = Depends(get_db)):
    """通过友好短链 slug 解析分享链接（如 /s/sunset-oak-jupiter）
    
    支持子域名隔离：子域名下的 /s/xxx → 仅查该 Agent 的分享链接
    无子域名 ⚏ 回退全局查找
    """
    from services.share_service import resolve_slug

    # 从 Host 头提取 agent_hash（子域名前缀）
    host = request.headers.get("host", "")
    agent_hash = None
    if host and settings.FECLAW_SUBDOMAIN_ENABLED and settings.FECLAW_PUBLIC_URL in host:
        prefix = host.split(f".{settings.FECLAW_PUBLIC_URL}")[0]
        # 4 字符的 agent hash 子域名
        if prefix and prefix != settings.FECLAW_PUBLIC_URL and len(prefix) == 4:
            agent_hash = prefix

    mapping = resolve_slug(slug, agent_hash, db)
    if not mapping:
        raise HTTPException(status_code=404, detail="分享链接不存在或已过期")

    vfs_path = mapping.vfs_path

    # 通过存储后端获取文件（感知 STORAGE_MODE，构造失败不再裸 500）
    from datetime import datetime

    if mapping.expires_at and datetime.utcnow() > mapping.expires_at:
        raise HTTPException(status_code=410, detail="分享链接已过期")

    # Q20/H18：密码保护必须真生效 —— 缺失/错误密码一律拒绝
    if mapping.password:
        supplied = request.query_params.get("password") or ""
        if not supplied or not verify_share_password(supplied, mapping.password):
            return _share_error_page(
                401,
                "需要密码",
                "此分享链接受密码保护，请在链接后追加 ?password=访问密码。",
            )

    try:
        from services.file_storage import create_file_storage
        storage = create_file_storage(mode=getattr(settings, "STORAGE_MODE", "auto"))
    except Exception as e:
        logger.error(f"[Share] storage unavailable for /s/{slug}: {e}")
        return _share_error_page(
            503,
            "存储服务不可用",
            "文件存储服务当前不可用，暂时无法打开这个分享链接，请稍后再试。",
        )
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
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{os.path.basename(vfs_path)}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5.5.1/github-markdown.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.1/marked.min.js"></script>
<script src="/static/mermaid.min.js"></script>
<style>
body{{max-width:800px;margin:40px auto;padding:0 20px;-webkit-touch-callout:none;}}
@media (max-width:640px){{body{{font-size:16px;line-height:1.8;padding:0 16px;margin:24px auto;}}}}
@media (max-width:480px){{body{{font-size:17px;line-height:1.9;margin:16px auto;}}}}
#c img{{max-width:100%;height:auto;}}
.markdown-body pre{{overflow-x:auto;}}
.katex-display{{overflow-x:auto;overflow-y:hidden;max-width:100%;}}
@keyframes fadeIn{{from{{opacity:0;}}to{{opacity:1;}}}}
.feclaw-ref-markdown strong{{color:#f0c040;}}
.feclaw-ref-markdown code{{background:#333;color:#7ecfff;padding:1px 5px;border-radius:3px;font-size:13px;}}
.feclaw-ref-markdown a{{color:#5b7cfa;}}
</style>
</head><body><article class="markdown-body" id="c"></article>
<script>
mermaid.initialize({{startOnLoad:false,theme:'default'}});

marked.use({{renderer:{{code:function(code,lang){{if(lang==='mermaid')return'<pre class="mermaid">'+code+'</pre>';if(lang)return'<pre><code class="language-'+lang+'">'+code+'</code></pre>';return'<pre><code>'+code+'</code></pre>';}}}}}});

var html = marked.parse({safe_md});
html = html.replace(/\\$\\$([\\s\\S]*?)\\$\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:true,throwOnError:false}}); }} catch(e) {{ return '$$'+eq+'$$'; }}
}});
html = html.replace(/\\$([^\\$\\n]+?)\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:false,throwOnError:false}}); }} catch(e) {{ return '$'+eq+'$'; }}
}});
document.getElementById('c').innerHTML = html;
window._RAW_MD = {safe_md};
mermaid.run({{nodes:document.querySelectorAll('.mermaid')}});
</script>
<script>var SHARE_HASH = {json.dumps(mapping.share_hash)}; var VFS_PATH = {json.dumps(vfs_path)};</script>
<script src="/static/js/share-reference.js"></script>
</body></html>"""
                    return Response(content=html_page, media_type="text/html")
                elif ext == ".2dggb":
                    return Response(content=_render_ggb_file(content, is_3d=False), media_type="text/html")
                elif ext == ".3dggb":
                    return Response(content=_render_ggb_file(content, is_3d=True), media_type="text/html")
                elif ext == ".jsxgraph":
                    return Response(content=_render_jsxgraph_file(content), media_type="text/html")

                mime_map = {".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                           ".png": "image/png", ".jpg": "image/jpeg",
                           ".json": "application/json", ".py": "text/plain; charset=utf-8",
                           ".mp3": "audio/mpeg", ".wav": "audio/wav"}
                ct = mime_map.get(ext, "application/octet-stream")
                _fname = os.path.basename(vfs_path)
                return Response(content=content, media_type=ct,
                              headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(_fname)}"})
        except Exception:
            continue

    raise HTTPException(status_code=404, detail="文件不存在或已删除")


@router.get("/share/{token}")
async def resolve_share(token: str, request: Request, db: Session = Depends(get_db)):
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
    mapping = None
    if share_hash:
        from models.database import ShareMapping
        mapping = db.query(ShareMapping).filter(
            ShareMapping.share_hash == share_hash
        ).first()
        if mapping:
            agent_hash = mapping.agent_hash

    # Q20/H18：token 路径同样校验 DB 过期时间 + 密码保护（此前只验 token 内嵌过期）
    if mapping:
        from datetime import datetime as _dt
        if mapping.expires_at and _dt.utcnow() > mapping.expires_at:
            raise HTTPException(status_code=410, detail="分享链接已过期")
        if mapping.password:
            supplied = request.query_params.get("password") or ""
            if not supplied or not verify_share_password(supplied, mapping.password):
                return _share_error_page(
                    401,
                    "需要密码",
                    "此分享链接受密码保护，请在链接后追加 ?password=访问密码。",
                )

    # 通过存储后端获取文件（感知 STORAGE_MODE，构造失败给可读错误页，不再裸 500）
    try:
        from services.file_storage import create_file_storage
        storage = create_file_storage(mode=getattr(settings, "STORAGE_MODE", "auto"))
    except Exception as e:
        logger.error(f"[Share] storage unavailable for /share/{token}: {e}")
        return _share_error_page(
            503,
            "存储服务不可用",
            "文件存储服务当前不可用，暂时无法打开这个分享链接，请稍后再试。",
        )

    try:
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
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{os.path.basename(vfs_path)}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5.5.1/github-markdown.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.1/marked.min.js"></script>
<script src="/static/mermaid.min.js"></script>
<style>
body{{max-width:800px;margin:40px auto;padding:0 20px;-webkit-touch-callout:none;}}
@media (max-width:640px){{body{{font-size:16px;line-height:1.8;padding:0 16px;margin:24px auto;}}}}
@media (max-width:480px){{body{{font-size:17px;line-height:1.9;margin:16px auto;}}}}
#c img{{max-width:100%;height:auto;}}
.markdown-body pre{{overflow-x:auto;}}
.katex-display{{overflow-x:auto;overflow-y:hidden;max-width:100%;}}
@keyframes fadeIn{{from{{opacity:0;}}to{{opacity:1;}}}}
.feclaw-ref-markdown strong{{color:#f0c040;}}
.feclaw-ref-markdown code{{background:#333;color:#7ecfff;padding:1px 5px;border-radius:3px;font-size:13px;}}
.feclaw-ref-markdown a{{color:#5b7cfa;}}
</style>
</head><body><article class="markdown-body" id="c"></article>
<script>
mermaid.initialize({{startOnLoad:false,theme:'default'}});
marked.use({{renderer:{{code:function(code,lang){{if(lang==='mermaid')return'<pre class="mermaid">'+code+'</pre>';if(lang)return'<pre><code class="language-'+lang+'">'+code+'</code></pre>';return'<pre><code>'+code+'</code></pre>';}}}}}});
var html = marked.parse({safe_md});
html = html.replace(/\\$\\$([\\s\\S]*?)\\$\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:true,throwOnError:false}}); }} catch(e) {{ return '$$'+eq+'$$'; }}
}});
html = html.replace(/\\$([^\\$\\n]+?)\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:false,throwOnError:false}}); }} catch(e) {{ return '$'+eq+'$'; }}
}});
document.getElementById('c').innerHTML = html;
window._RAW_MD = {safe_md};
mermaid.run({{nodes:document.querySelectorAll('.mermaid')}});
</script>
<script>var SHARE_HASH = {json.dumps(share_hash or '')}; var VFS_PATH = {json.dumps(vfs_path)};</script>
<script src="/static/js/share-reference.js"></script>
</body></html>"""
                    return Response(content=html_page, media_type="text/html")
                elif ext == ".2dggb":
                    return Response(content=_render_ggb_file(content, is_3d=False), media_type="text/html")
                elif ext == ".3dggb":
                    return Response(content=_render_ggb_file(content, is_3d=True), media_type="text/html")
                elif ext == ".jsxgraph":
                    return Response(content=_render_jsxgraph_file(content), media_type="text/html")

                mime_map = {".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                           ".png": "image/png", ".jpg": "image/jpeg",
                           ".json": "application/json", ".py": "text/plain; charset=utf-8",
                           ".mp3": "audio/mpeg", ".wav": "audio/wav"}
                content_type = mime_map.get(ext, "application/octet-stream")
                _fname = os.path.basename(vfs_path)
                return Response(content=content, media_type=content_type,
                              headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(_fname)}"})
    except Exception as e:
        logger.warning(f"[Share] storage fetch failed: {e}")

    # 尝试通过 FUSE 本地路径
    fuse_path = f"{settings.FUSE_MOUNT_DIR}{vfs_path}"
    if os.path.isfile(fuse_path):
        return FileResponse(fuse_path, filename=os.path.basename(vfs_path))

    raise HTTPException(status_code=404, detail="文件不存在或已删除")
