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
import json
from html import escape
from urllib.parse import quote

logger = logging.getLogger(__name__)

# GeoGebra HTML 模板
GGB_TEMPLATE_2D = """<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GeoGebra 2D</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { width: 100vw; height: 100vh; overflow: hidden; background: #f0f0f0; }
        #ggb-element { width: 100vw; height: 100vh; }
        #ggb-fallback { display: none; padding: 40px; font-family: sans-serif; color: #333; text-align: center; }
    </style>
</head>
<body>
<div id="ggb-element"></div>
<div id="ggb-fallback">
    <h2>⚠️ 图形加载失败</h2>
    <p>GeoGebra 组件未能加载（可能因当前网络无法访问 geogebra.org）。<br>请稍后重试，或切换网络环境。</p>
</div>
<script>
    (function() {
        var shown = false;
        function showFallback() {
            if (shown) return;
            shown = true;
            document.getElementById('ggb-element').style.display = 'none';
            document.getElementById('ggb-fallback').style.display = 'block';
        }
        function boot() {
            // FIX-D：deployggb.js 按需加载 + 失败降级（国内不可达时不白屏，给出提示）
            if (typeof GGBApplet === 'undefined') { showFallback(); return; }
            if (shown) return;
            try {
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
            } catch (e) { showFallback(); }
        }
        var s = document.createElement('script');
        s.src = 'https://www.geogebra.org/apps/deployggb.js';
        s.onload = boot;
        s.onerror = showFallback;
        setTimeout(function() { if (typeof GGBApplet === 'undefined') showFallback(); }, 15000);
        document.head.appendChild(s);
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
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { width: 100vw; height: 100vh; overflow: hidden; background: #0f0f1a; }
        #ggb-element { width: 100vw; height: 100vh; }
        #ggb-fallback { display: none; padding: 40px; font-family: sans-serif; color: #e0e0e0; text-align: center; }
    </style>
</head>
<body>
<div id="ggb-element"></div>
<div id="ggb-fallback">
    <h2>⚠️ 图形加载失败</h2>
    <p>GeoGebra 组件未能加载（可能因当前网络无法访问 geogebra.org）。<br>请稍后重试，或切换网络环境。</p>
</div>
<script>
    (function() {
        var shown = false;
        function showFallback() {
            if (shown) return;
            shown = true;
            document.getElementById('ggb-element').style.display = 'none';
            document.getElementById('ggb-fallback').style.display = 'block';
        }
        function boot() {
            // FIX-D：deployggb.js 按需加载 + 失败降级（国内不可达时不白屏，给出提示）
            if (typeof GGBApplet === 'undefined') { showFallback(); return; }
            if (shown) return;
            try {
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
            } catch (e) { showFallback(); }
        }
        var s = document.createElement('script');
        s.src = 'https://www.geogebra.org/apps/deployggb.js';
        s.onload = boot;
        s.onerror = showFallback;
        setTimeout(function() { if (typeof GGBApplet === 'undefined') showFallback(); }, 15000);
        document.head.appendChild(s);
    })();
</script>
</body>
</html>"""

JSXGRAPH_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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


def _js_safe(value) -> str:
    """JSON 序列化并转义 < > &（Q21/M7）。

    `json.dumps` 不转义 `<`/`/`/`&`，含 `</script><script>…` 的 Markdown 会被
    原样塞进 `<script>` 块，令攻击者逃出脚本上下文。把 `<`/`>`/`&` 换成
    `<`/`>`/`&` 后，JS 字符串字面量在源码层不再包含可被 HTML
    解析器识别的 `</script>`，而 JS 运行时仍还原出原字符。
    """
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


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


# github-markdown-css 的本地化替代（避免 CDN 被墙导致分享页只剩外壳）
_MARKDOWN_BASE_CSS = """
.markdown-body{color:#24292f;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;font-size:16px;line-height:1.7;word-wrap:break-word;}
.markdown-body h1,.markdown-body h2,.markdown-body h3,.markdown-body h4,.markdown-body h5,.markdown-body h6{margin-top:24px;margin-bottom:16px;font-weight:600;line-height:1.25;}
.markdown-body h1{font-size:2em;padding-bottom:.3em;border-bottom:1px solid #eaecef;}
.markdown-body h2{font-size:1.5em;padding-bottom:.3em;border-bottom:1px solid #eaecef;}
.markdown-body h3{font-size:1.25em;}
.markdown-body p{margin:0 0 16px;}
.markdown-body ul,.markdown-body ol{padding-left:2em;margin-bottom:16px;}
.markdown-body li{margin-bottom:4px;}
.markdown-body blockquote{margin:0 0 16px;padding:0 1em;color:#6a737d;border-left:.25em solid #dfe2e5;}
.markdown-body code{padding:.2em .4em;margin:0;font-size:85%;background:rgba(27,31,35,.05);border-radius:3px;font-family:SFMono-Regular,Consolas,"Liberation Mono",Menlo,monospace;}
.markdown-body pre{padding:16px;overflow:auto;font-size:85%;line-height:1.45;background:#f6f8fa;border-radius:6px;margin-bottom:16px;}
.markdown-body pre code{display:inline;padding:0;margin:0;background:transparent;}
.markdown-body table{border-spacing:0;border-collapse:collapse;margin-bottom:16px;display:block;overflow-x:auto;}
.markdown-body table th,.markdown-body table td{padding:6px 13px;border:1px solid #dfe2e5;}
.markdown-body table tr{background:#fff;border-top:1px solid #c6cbd1;}
.markdown-body table tr:nth-child(2n){background:#f6f8fa;}
.markdown-body a{color:#0969da;text-decoration:none;}
.markdown-body hr{height:.25em;padding:0;margin:24px 0;background-color:#d0d7de;border:0;}
.markdown-body img{max-width:100%;height:auto;}
"""


def _markdown_share_page(md_content: str, vfs_path: str, share_hash_value) -> str:
    """渲染 markdown 分享页：全部资源本地托管，Mermaid 按需加载（无 mermaid 图不加载 3.24MB）。"""
    safe_md = _js_safe(md_content)
    has_mermaid = "```mermaid" in md_content
    mermaid_tag = '<script src="/static/mermaid.min.js"></script>' if has_mermaid else ''
    has_mermaid_js = "true" if has_mermaid else "false"
    html_page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(os.path.basename(vfs_path))}</title>
<link rel="stylesheet" href="/static/katex.min.css">
<script src="/static/marked.min.js"></script>
<script src="/static/katex.min.js"></script>
{mermaid_tag}
<style>
body{{max-width:800px;margin:40px auto;padding:0 20px;-webkit-touch-callout:none;}}
@media (max-width:640px){{body{{font-size:16px;line-height:1.8;padding:0 16px;margin:24px auto;}}}}
@media (max-width:480px){{body{{font-size:17px;line-height:1.9;margin:16px auto;}}}}
{_MARKDOWN_BASE_CSS}
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
marked.use({{renderer:{{code:function(code,lang){{if(lang==='mermaid')return'<pre class="mermaid">'+code+'</pre>';if(lang)return'<pre><code class="language-'+lang+'">'+code+'</code></pre>';return'<pre><code>'+code+'</code></pre>';}}}}}});
var html = marked.parse({safe_md});
if (typeof katex !== 'undefined') {{
  html = html.replace(/\\$\\$([\\s\\S]*?)\\$\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:true,throwOnError:false}}); }} catch(e) {{ return '$$'+eq+'$$'; }}
  }});
  html = html.replace(/\\$([^\\$\\n]+?)\\$/g, function(_, eq) {{
    try {{ return katex.renderToString(eq, {{displayMode:false,throwOnError:false}}); }} catch(e) {{ return '$'+eq+'$'; }}
  }});
}}
document.getElementById('c').innerHTML = html;
window._RAW_MD = {safe_md};
if ({has_mermaid_js} && typeof mermaid !== 'undefined') {{
  mermaid.initialize({{startOnLoad:false,theme:'default'}});
  mermaid.run({{nodes:document.querySelectorAll('.mermaid')}});
}}
</script>
<script>var SHARE_HASH = {_js_safe(share_hash_value or "")}; var VFS_PATH = {_js_safe(vfs_path)};</script>
<script src="/static/js/share-reference.js"></script>
</body></html>"""
    return html_page


router = APIRouter(tags=["share"])


@router.get("/s/{slug}")
async def resolve_share_by_slug(slug: str, request: Request, db: Session = Depends(get_db)):
    """通过友好短链 slug 解析分享链接（如 /s/sunset-oak-jupiter）
    
    支持子域名隔离：子域名下的 /s/xxx → 仅查该 Agent 的分享链接
    无子域名 ⚏ 回退全局查找
    """
    from services.share_service import resolve_slug

    # 从 Host 头提取 agent_hash（子域名前缀）。
    # M2：收敛到 utils.agent_access.extract_hash_from_host —— 同时接受 4 位（老）与
    # 8 位（新）hex 子域名；此前这里手写 `len(prefix) == 4`，8 位 hash 的 Agent 分享
    # 子域名永远解析不出隔离作用域。
    from utils.agent_access import extract_hash_from_host
    host = request.headers.get("host", "")
    agent_hash = None
    if host and settings.FECLAW_SUBDOMAIN_ENABLED and settings.FECLAW_PUBLIC_URL in host:
        agent_hash = extract_hash_from_host(host)

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
                    return Response(content=_markdown_share_page(md_content, vfs_path, mapping.share_hash), media_type="text/html")
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
                    return Response(content=_markdown_share_page(md_content, vfs_path, share_hash or ""), media_type="text/html")
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
