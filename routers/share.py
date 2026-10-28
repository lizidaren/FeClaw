"""
分享链接路由 - 解析分享 token 并重定向/提供文件

T17a「分享边界」（本线程 S3）：
- 路径只从 `ShareMapping` 已确认身份推导（见 services.share_service.resolve_mapping_storage_key），
  删除 `feclaw/vfs{path}` / `feclaw/user_workspaces/2/...` / 全局 FUSE 回退。
- 主动内容（html/svg/js/jsxgraph/ggb…）未配置 `SHARE_ACTIVE_ORIGIN` 时按附件 + nosniff 返回；
  配置独立域时只 302 过去，绝不在主应用源内联执行。
- 密码不走 query：新增 `POST /s/{slug}/unlock` 与 `POST /share/{token}/unlock`，
  下发短时 HMAC 签名 cookie；`?password=` 仅兼容并标 deprecated。
- `_markdown_share_page` 的渲染 JS 属于 T16/S2 边界，本次未改动。
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from services import share_service as share_svc
from models.database import get_db
from config import settings
import os, logging
import json
from html import escape
from urllib.parse import quote, parse_qs, urlparse

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
    """将 .2dggb/.3dggb 内容渲染为 GeoGebra HTML

    T17a：这两个扩展名属于**主动内容**。当前只在配置了独立分享域时才会被
    调用（见 `_share_content_response`），主应用源内一律按附件下载。
    """
    commands_text = content.decode("utf-8", errors="replace").strip()
    commands = [line.strip() for line in commands_text.split("\n")
                if line.strip() and not line.strip().startswith("#")]
    commands_json = json.dumps(commands)
    template = GGB_TEMPLATE_3D if is_3d else GGB_TEMPLATE_2D
    return template.replace("COMMANDS", commands_json)


def _render_jsxgraph_file(content: bytes) -> str:
    """将 .jsxgraph 内容渲染为 JSXGraph HTML（同样只在独立分享域下使用）。"""
    code = content.decode("utf-8", errors="replace").strip()
    return JSXGRAPH_TEMPLATE.replace("JSXGRAPH_CODE", code)


def _js_safe(value) -> str:
    """JSON 序列化并转义 < > &（Q21/M7）。

    `json.dumps` 不转义 `<`/`/`/`&`，含 `</script><script>…` 的 Markdown 会被
    原样塞进 `<script>` 块，令攻击者逃出脚本上下文。把 `<`/`>`/`&` 换成
    `\u003c`/`\u003e`/`\u0026` 后，JS 字符串字面量在源码层不再包含可被 HTML
    解析器识别的 `</script>`，而 JS 运行时仍还原出原字符。
    """
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _share_error_page(status_code: int, title: str, message: str, headers: dict = None) -> Response:
    """分享页友好错误页（替代 FastAPI 默认的裸 500 / JSON 错误）。"""
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
<style>
body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f5f6f8;color:#333;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
.card{{background:#fff;border-radius:12px;padding:40px 48px;max-width:420px;
box-shadow:0 2px 12px rgba(0,0,0,.06);text-align:center;}}
h1{{font-size:20px;margin:0 0 12px;}}
p{{font-size:15px;line-height:1.6;color:#666;margin:0;}}
</style></head>
<body><div class="card"><h1>{escape(title)}</h1><p>{escape(message)}</p></div></body></html>"""
    return Response(
        content=html, status_code=status_code, media_type="text/html; charset=utf-8",
        headers={**_share_headers(), **(headers or {})},
    )


def _password_page(status_code: int, *, slug: str = None, token: str = None,
                   error: str = "") -> Response:
    """密码表单页（无内联脚本：直接 POST 到 unlock，服务端 303 回分享页）。

    T17a-4：密码不再拼进 query；表单 POST 目标按路由类型构造，全部转义。
    """
    if slug:
        action = f"/s/{quote(slug, safe='')}/unlock"
        back = f"/s/{quote(slug, safe='')}"
    else:
        action = f"/share/{quote(token or '', safe='')}/unlock"
        back = f"/share/{quote(token or '', safe='')}"
    err_html = (f'<p class="err">{escape(error)}</p>' if error else "")
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>需要密码</title>
<style>
body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f5f6f8;color:#333;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
.card{{background:#fff;border-radius:12px;padding:36px 40px;max-width:380px;
box-shadow:0 2px 12px rgba(0,0,0,.06);}}
h1{{font-size:20px;margin:0 0 8px;}}
p{{font-size:14px;line-height:1.6;color:#666;margin:0 0 16px;}}
p.err{{color:#c0392b;}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;font-size:15px;border:1px solid #d0d7de;
border-radius:8px;margin-bottom:14px;}}
button{{width:100%;padding:11px 12px;font-size:15px;border:0;border-radius:8px;
background:#2f6fed;color:#fff;cursor:pointer;}}
form{{margin:0;}}
</style></head>
<body><div class="card">
<h1>需要密码</h1>
<p>此分享链接受密码保护。输入访问密码后即可查看，密码只用于本次访问，不会出现在链接里。</p>
{err_html}
<form method="post" action="{escape(action)}">
<input type="password" name="password" autocomplete="current-password" required autofocus
 aria-label="访问密码" placeholder="访问密码">
<button type="submit">打开分享</button>
</form>
<noscript><p>当前浏览器禁用了脚本，也可直接向上述地址提交表单。</p></noscript>
</div></body></html>"""
    return Response(
        content=html, status_code=status_code, media_type="text/html; charset=utf-8",
        headers=_share_headers(),
    )


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


def _share_markdown_script(safe_md: str, has_mermaid_js: str) -> str:
    """分享页的渲染脚本（T16/S2 唯一安全渲染入口）。

    规则（S2 交接）：
    - `marked` → `DOMPurify` → `FeClawSafeRender` 三段加载，渲染只走
      `FeClawSafeRender.renderMarkdownSafe`，页面内**不再出现裸 `marked.parse`**；
    - 代码块（含 mermaid）文本先经 `FeClawSafeRender.renderText` 转义再拼 HTML；
    - 依赖脚本加载失败时只输出转义纯文本，绝不回退到未消毒的 marked 输出；
    - 数学由 safe-render 的 `postProcess` 用 KaTeX 处理（不再手工对消毒后的 HTML
      做 `renderToString` 替换，避免绕过消毒器）。
    """
    script = """<script>
(function () {
  function esc(t) {
    return String(t === null || t === undefined ? '' : t)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function codeText(code) {
    if (typeof FeClawSafeRender !== 'undefined' && FeClawSafeRender.renderText) {
      return FeClawSafeRender.renderText(code);
    }
    return esc(code);
  }
  if (typeof marked !== 'undefined' && marked.use) {
    marked.use({renderer: {code: function (code, lang) {
      var safeCode = codeText(code);
      if (lang === 'mermaid') { return '<pre class="mermaid">' + safeCode + '</pre>'; }
      if (lang) { return '<pre><code class="language-' + esc(lang) + '">' + safeCode + '</code></pre>'; }
      return '<pre><code>' + safeCode + '</code></pre>';
    }}});
  }
  var RAW = __RAW_MD__;
  var SAFE;
  if (typeof FeClawSafeRender !== 'undefined' && FeClawSafeRender.renderMarkdownSafe) {
    SAFE = FeClawSafeRender.renderMarkdownSafe(RAW, {breaks: false});
  } else {
    SAFE = esc(RAW);  // 依赖加载失败：只给转义纯文本，绝不退回未消毒 HTML
  }
  document.getElementById('c').innerHTML = SAFE;
  window._RAW_MD = RAW;
  if (__HAS_MERMAID__ && typeof mermaid !== 'undefined') {
    mermaid.initialize({startOnLoad: false, theme: 'default'});
    mermaid.run({nodes: document.querySelectorAll('.mermaid')});
  }
})();
</script>"""
    return script.replace("__RAW_MD__", safe_md).replace("__HAS_MERMAID__", has_mermaid_js)


def _markdown_share_page(md_content: str, vfs_path: str, share_hash_value) -> str:
    """渲染 markdown 分享页：全部资源本地托管，Mermaid 按需加载（无 mermaid 图不加载 3.24MB）。

    T16/S2：渲染只经 `FeClawSafeRender.renderMarkdownSafe`（见 `_share_markdown_script`）。
    T17a：响应头由 `_share_headers()` 统一加 nosniff / no-referrer / no-store。
    """
    safe_md = _js_safe(md_content)
    has_mermaid = "```mermaid" in md_content
    mermaid_tag = '<script src="/static/mermaid.min.js"></script>' if has_mermaid else ''
    has_mermaid_js = "true" if has_mermaid else "false"
    script_block = _share_markdown_script(safe_md, has_mermaid_js)
    html_page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(os.path.basename(vfs_path))}</title>
<link rel="stylesheet" href="/static/katex.min.css">
<script src="/static/marked.min.js"></script>
<script src="/static/katex.min.js"></script>
<script src="/static/vendor/dompurify.min.js"></script>
<script src="/static/js/safe-render.js"></script>
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
{script_block}
<script>var SHARE_HASH = {_js_safe(share_hash_value or "")}; var VFS_PATH = {_js_safe(vfs_path)};</script>
<script src="/static/js/share-reference.js"></script>
</body></html>"""
    return html_page


router = APIRouter(tags=["share"])


# ── 通用响应头（T17a-2：nosniff / 不缓存 / 不泄漏 referrer） ────────────────────

def _share_headers(extra: dict = None) -> dict:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, no-store",
        # 防止分享页被任意站点嵌入/嗅探
        "X-Frame-Options": "SAMEORIGIN",
    }
    if extra:
        headers.update(extra)
    return headers


def _deprecated_query_headers(kind: str, ident: str) -> dict:
    """`?password=` 兼容通道的弃用标记（真实证明应走 POST unlock + cookie）。"""
    if kind == "slug":
        target = f"/s/{quote(ident or '', safe='')}/unlock"
    else:
        target = f"/share/{quote(ident or '', safe='')}/unlock"
    return {
        "Deprecation": "true",
        "Warning": f'299 - "password in query is deprecated; use POST {target}"',
    }


def _request_is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return forwarded == "https"


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1"):
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return peer or "unknown"


# 解锁尝试限流（T18a：统一走 services/rate_limiter.py）。
# 单进程用内存滑窗；`RATE_LIMIT_BACKEND=redis` 时多 worker 共享计数；
# Redis 不可用且 `RATE_LIMIT_FAIL_MODE=closed` 时拒绝解锁（503），不退回“无限猜”。
_UNLOCK_WINDOW = 60
_UNLOCK_MAX = 10


def _unlock_rate_verdict(request: Request, ident: str) -> str:
    """密码解锁限流判定。

    返回 `"ok"` / `"limited"` / `"unavailable"`（Redis 后端不可用且 fail-closed）。
    """
    key = f"{_client_ip(request)}:{ident or ''}"
    try:
        from services.rate_limiter import RateLimitUnavailable, share_unlock_limiter
    except Exception as exc:  # pragma: no cover - 限流模块不可用时不放大风险
        logger.warning("[Share] unlock limiter unavailable, rejecting: %s", exc)
        return "unavailable"
    try:
        return "limited" if share_unlock_limiter.is_limited(key) else "ok"
    except RateLimitUnavailable:
        return "unavailable"
    except Exception as exc:  # pragma: no cover - 后端异常同样按 fail-closed 处理
        logger.warning("[Share] unlock limiter error, rejecting: %s", exc)
        return "unavailable"


def _host_agent_hash(request: Request):
    """子域名 → agent_hash（与既有 M2 收敛逻辑一致）。"""
    from utils.agent_access import extract_hash_from_host
    host = request.headers.get("host", "")
    if host and settings.FECLAW_SUBDOMAIN_ENABLED and settings.FECLAW_PUBLIC_URL in host:
        return extract_hash_from_host(host)
    return None


def _safe_resolve_slug(slug: str, request: Request, db: Session):
    """按 slug 查映射；数据库不可用 → 503（不能让分享页裸 500）。"""
    try:
        return share_svc.resolve_slug(slug, _host_agent_hash(request), db)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Share] slug lookup failed for /s/%s: %s", slug, exc, exc_info=True)
        raise HTTPException(status_code=503, detail="分享服务当前不可用，请稍后再试")


def _safe_decode_token(token: str, db: Session):
    """按 token 查映射；解码失败/数据库不可用都按不可用处理（不猜、不 500）。"""
    try:
        return share_svc.decode_share_token_mapping(token, db=db)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Share] token lookup failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail="分享服务当前不可用，请稍后再试")


def _storage_or_503(kind: str, ident: str):
    try:
        from services.file_storage import create_file_storage
        return create_file_storage(mode=getattr(settings, "STORAGE_MODE", "auto"))
    except Exception as exc:
        logger.error("[Share] storage unavailable for %s/%s: %s", kind, ident, exc)
        raise HTTPException(
            status_code=503,
            detail="文件存储服务当前不可用，暂时无法打开这个分享链接，请稍后再试。",
        )


# 按扩展名返回的安全 Content-Type（不含任何可执行 HTML）
_MIME_MAP = {
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".pdf": "application/pdf",
}


def _content_disposition(disposition: str, filename: str) -> str:
    return f"{disposition}; filename*=UTF-8''{quote(filename or 'download')}"


def _share_origin_host() -> str:
    """独立分享域的主机名（未配置返回空串）。"""
    origin = share_svc.share_active_origin()
    if not origin:
        return ""
    try:
        return urlparse(origin).netloc.split("@")[-1].split(":")[0].strip().lower()
    except Exception:
        return ""


def _is_on_share_origin(request: Request) -> bool:
    """当前请求是否已经落在独立分享域上（避免 302 自环）。"""
    origin_host = _share_origin_host()
    if not origin_host:
        return False
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    return bool(host) and host == origin_host


def _active_inline_response(content: bytes, rel_path: str, headers: dict) -> Response:
    """只在独立分享域内联主动内容（该域必须不承载应用 Cookie）。"""
    ext = share_svc.file_extension(rel_path)
    filename = os.path.basename(rel_path or "")
    if ext == ".2dggb":
        return Response(content=_render_ggb_file(content, is_3d=False),
                        media_type="text/html; charset=utf-8", headers=headers)
    if ext == ".3dggb":
        return Response(content=_render_ggb_file(content, is_3d=True),
                        media_type="text/html; charset=utf-8", headers=headers)
    if ext == ".jsxgraph":
        return Response(content=_render_jsxgraph_file(content),
                        media_type="text/html; charset=utf-8", headers=headers)
    inline_types = {
        ".html": "text/html; charset=utf-8",
        ".htm": "text/html; charset=utf-8",
        ".xhtml": "application/xhtml+xml; charset=utf-8",
        ".svg": "image/svg+xml",
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".ggb": "application/octet-stream",
    }
    media_type = inline_types.get(ext, "application/octet-stream")
    extra = {}
    if ext == ".ggb":
        # .ggb 是 GeoGebra 压缩包，不是网页；独立域也只给下载
        extra["Content-Disposition"] = _content_disposition("attachment", filename)
    return Response(content=content, media_type=media_type, headers={**headers, **extra})


def _share_content_response(content: bytes, rel_path: str, mapping, *, request: Request,
                            slug: str = None, token: str = None,
                            extra_headers: dict = None) -> Response:
    """按内容类型返回分享响应（T17a-2 主动内容隔离的唯一出口）。"""
    headers = _share_headers(extra_headers)
    filename = os.path.basename(rel_path or "")

    # Markdown：主应用源渲染页（渲染安全由 T16/S2 负责）
    if share_svc.file_extension(rel_path) == ".md":
        try:
            md_content = content.decode("utf-8")
        except Exception:
            md_content = content.decode("utf-8", errors="replace")
        return Response(
            content=_markdown_share_page(md_content, rel_path, getattr(mapping, "share_hash", "")),
            media_type="text/html; charset=utf-8",
            headers=headers,
        )

    # 主动内容：只允许在独立分享域内联；主应用源一律 302 过去或按附件下载
    if share_svc.is_active_content(rel_path):
        origin = share_svc.share_active_origin()
        if origin and not _is_on_share_origin(request):
            location = (
                f"{origin}/s/{quote(slug, safe='')}" if slug
                else f"{origin}/share/{quote(token or '', safe='')}"
            )
            logger.info("[Share] active content redirected to isolated origin: %s", location)
            return RedirectResponse(url=location, status_code=302, headers=headers)
        if origin and _is_on_share_origin(request):
            # 已在独立域（cookie-free origin）上：允许其按页面渲染
            return _active_inline_response(content, rel_path, headers)
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                **headers,
                "Content-Disposition": _content_disposition("attachment", filename),
                # 双保险：即使某些客户端忽略 Content-Type，也不允许脚本/插件执行
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    # 图片/纯文本：可内联展示，但类型固定 + nosniff
    if share_svc.is_inline_safe(rel_path):
        media_type = _MIME_MAP.get(share_svc.file_extension(rel_path), "application/octet-stream")
        return Response(
            content=content,
            media_type=media_type,
            headers={**headers, "Content-Disposition": _content_disposition("inline", filename)},
        )

    # 其余一律下载，且不给可执行类型
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={**headers, "Content-Disposition": _content_disposition("attachment", filename)},
    )


def _serve_share(mapping, request: Request, db: Session, *, slug: str = None,
                 token: str = None) -> Response:
    """统一解析出口：过期/撤销/密码/身份/路径全部 fail closed。"""
    try:
        share_svc.assert_mapping_live(mapping)

        cookie_value = request.cookies.get(share_svc.unlock_cookie_name())
        query_password = request.query_params.get("password")
        proof = share_svc.password_proof_status(
            mapping, cookie_value=cookie_value, query_password=query_password
        )
        if not proof["ok"]:
            return _password_page(401, slug=slug, token=token)

        key_info = share_svc.resolve_mapping_storage_key(mapping, db=db)
    except share_svc.ShareBoundaryError as exc:
        logger.warning(
            "[Share] boundary reject slug=%s token=%s reason=%s status=%s",
            slug, (token or "")[:12], exc.reason, exc.status_code,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except HTTPException:
        raise
    except Exception as exc:
        # 数据库不可用等基础设施故障：返回明确 503，既不 500 也不放行
        logger.error("[Share] identity/path resolution failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail="分享服务当前不可用，请稍后再试")

    if proof.get("proof") == "query":
        logger.warning(
            "[Share] deprecated query password accepted slug=%s (use POST unlock)", slug
        )
        extra_headers = _deprecated_query_headers("slug" if slug else "token", slug or token)
    else:
        extra_headers = None

    ident = slug or (token or "")[:12]
    storage = _storage_or_503("s", ident)
    try:
        content = storage.get_file_content(key_info["key"])
    except Exception as exc:
        logger.error("[Share] storage read failed key=%s: %s", key_info["key"], exc, exc_info=True)
        content = None

    if not content:
        raise HTTPException(status_code=404, detail="文件不存在或已删除")

    return _share_content_response(
        content, key_info["rel_path"], mapping, request=request,
        slug=slug, token=token, extra_headers=extra_headers,
    )


def _wants_json(request: Request) -> bool:
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        return True
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


async def _read_unlock_password(request: Request):
    """读取解锁请求里的密码（JSON 为主；urlencoded 表单兼容无 JS 页面）。"""
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in ctype:
            body = await request.body()
            if not body:
                return None
            data = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                value = data.get("password")
                if isinstance(value, str):
                    return value
            return None
        raw = (await request.body()).decode("utf-8", errors="replace")
        values = parse_qs(raw).get("password") or []
        return values[0] if values else None
    except Exception:
        return None


async def _unlock_share(mapping, request: Request, *, slug: str = None,
                        token: str = None) -> Response:
    """POST unlock：校验密码 → 下发短时 HMAC cookie（绑定 slug + share_hash）。"""
    ident = slug or (token or "")
    wants_json = _wants_json(request)

    def _deny(status_code: int, message: str) -> Response:
        if wants_json:
            return JSONResponse({"ok": False, "detail": message}, status_code=status_code,
                                headers=_share_headers())
        if status_code == 429:
            return _share_error_page(429, "请稍后再试", message)
        return _password_page(status_code, slug=slug, token=token, error=message)

    verdict = _unlock_rate_verdict(request, ident)
    if verdict == "unavailable":
        # fail-closed：限流后端不可用时宁可拒绝解锁，也不放开无限次猜密码
        return _deny(503, "限流服务暂不可用，请稍后再试")
    if verdict == "limited":
        return _deny(429, "尝试次数过多，请稍后再试")

    try:
        share_svc.assert_mapping_live(mapping)
    except share_svc.ShareBoundaryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    password = await _read_unlock_password(request)

    # 无密码分享：unlock 幂等成功（不签发 cookie）
    if not getattr(mapping, "password", None):
        if wants_json:
            return JSONResponse({"ok": True, "password_required": False}, headers=_share_headers())
        return RedirectResponse(
            url=(f"/s/{quote(slug, safe='')}" if slug else f"/share/{quote(token or '', safe='')}"),
            status_code=303, headers=_share_headers(),
        )

    if not password or not share_svc.verify_share_password(password, mapping.password):
        logger.warning("[Share] unlock failed slug=%s ip=%s", slug, _client_ip(request))
        return _deny(401, "密码错误，请重试")

    cookie_slug = slug if slug else (getattr(mapping, "slug", "") or "")
    value, ttl = share_svc.encode_unlock_cookie(cookie_slug, mapping.share_hash)
    if wants_json:
        response = JSONResponse(
            {"ok": True, "password_required": True, "expires_in": ttl,
             "slug": getattr(mapping, "slug", "") or ""},
            headers=_share_headers(),
        )
    else:
        response = RedirectResponse(
            url=(f"/s/{quote(slug, safe='')}" if slug else f"/share/{quote(token or '', safe='')}"),
            status_code=303, headers=_share_headers(),
        )
    response.set_cookie(
        key=share_svc.unlock_cookie_name(),
        value=value,
        max_age=ttl,
        path="/",
        httponly=True,
        samesite="lax",       # 点击分享链接属于同站顶层导航，Strict 会丢掉 cookie
        secure=_request_is_https(request),
    )
    logger.info("[Share] unlock granted slug=%s ttl=%ss", slug, ttl)
    return response


@router.get("/s/{slug}")
async def resolve_share_by_slug(slug: str, request: Request, db: Session = Depends(get_db)):
    """通过友好短链 slug 解析分享链接（如 /s/sunset-oak-jupiter）

    支持子域名隔离：子域名下的 /s/xxx → 仅查该 Agent 的分享链接。
    T17a：解析只用 mapping 已确认身份推导路径，不再有全局/跨用户回退。
    """
    mapping = _safe_resolve_slug(slug, request, db)
    if not mapping:
        raise HTTPException(status_code=404, detail="分享链接不存在或已过期")
    return _serve_share(mapping, request, db, slug=slug)


@router.post("/s/{slug}/unlock")
async def unlock_share_by_slug(slug: str, request: Request, db: Session = Depends(get_db)):
    """用密码换取短时解锁 cookie（密码不进 query）。"""
    mapping = _safe_resolve_slug(slug, request, db)
    if not mapping:
        raise HTTPException(status_code=404, detail="分享链接不存在或已过期")
    return await _unlock_share(mapping, request, slug=slug)


@router.get("/share/{token}")
async def resolve_share(token: str, request: Request, db: Session = Depends(get_db)):
    """解析分享链接 token，返回文件（规则与 /s/{slug} 一致）。"""
    mapping = _safe_decode_token(token, db)
    if not mapping:
        raise HTTPException(status_code=404, detail="分享链接无效或已过期")
    return _serve_share(mapping, request, db, token=token)


@router.post("/share/{token}/unlock")
async def unlock_share_by_token(token: str, request: Request, db: Session = Depends(get_db)):
    """token 路由的解锁入口（与 /s/{slug}/unlock 同步规则）。"""
    mapping = _safe_decode_token(token, db)
    if not mapping:
        raise HTTPException(status_code=404, detail="分享链接无效或已过期")
    return await _unlock_share(mapping, request, token=token)
