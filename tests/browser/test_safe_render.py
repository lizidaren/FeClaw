"""T16「唯一安全渲染边界」验收：真实浏览器 + 静态守卫。

两层验证，都只指向本地文件（不访问生产、不启动服务器）：

1. 浏览器层（首选 Playwright ``channel="msedge"``）：用临时目录里的本地 harness
   加载仓库中**真实**的 ``static/marked.min.js`` / ``static/vendor/dompurify.min.js``
   / ``static/js/safe-render.js``，把不可信输入真正渲染进 DOM，断言：
   - ``<img onerror>``、``javascript:`` 链接、``</script>`` 逃逸、iframe/srcdoc、
     form/input、``data:`` 图片都不执行/不落地；
   - 代码块、表格、KaTeX 公式、图片、链接仍然正常渲染（链接被硬化成
     ``target=_blank`` + ``rel="noopener noreferrer"``）；
   - 无 DOMPurify 时 fail-closed（退化为纯文本，绝不返回未消毒 HTML）；
   - hook 只安装一次，重复加载 safe-render.js 不会重复 hook。

2. 静态层（无需浏览器）：T16 涉及的页面不再出现裸 ``marked.parse(...)``、
   脚本加载顺序为 marked → DOMPurify → safe-render、DOMPurify 自托管且哈希锁定。

运行：
    .venv/Scripts/python -m pytest tests/browser/test_safe_render.py -v

说明：
- 未安装 playwright 时浏览器用例 ``pytest.importorskip`` 跳过并给出原因；
  本机没有可用浏览器（先试 msedge，再试 Playwright 自带 chromium）时也明确跳过。
- 本文件**不**断言 ``routers/share.py``：share 页由 T17 线负责，改法写在 S2 报告的
  「跨界请求」里（见报告，不在本测试中制造跨线失败）。
"""

import ast
import hashlib
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC = os.path.join(ROOT, "static")
SAFE_RENDER = os.path.join(STATIC, "js", "safe-render.js")
DOMPURIFY = os.path.join(STATIC, "vendor", "dompurify.min.js")
MARKED = os.path.join(STATIC, "marked.min.js")
KATEX = os.path.join(STATIC, "katex.min.js")

# 自托管锁定的 DOMPurify 3.2.4（dist/purify.min.js，逐字节一致）
DOMPURIFY_SHA256 = "8eb41b658831fab175fad9bcd00fcb2d84e0ed3a25a55053d4ecd4444b8b43a0"

# T16 触及的页面：模板 3 个 + 内嵌页面 2 个
OWNED_RENDER_PAGES = [
    os.path.join(ROOT, "templates", "agent_chat.html"),
    os.path.join(ROOT, "templates", "agent_files.html"),
    os.path.join(ROOT, "templates", "agent_configure.html"),
    os.path.join(ROOT, "routers", "dashboard.py"),
    os.path.join(ROOT, "routers", "agent_config_ui.py"),
]

MARKED_TAG = "/static/marked.min.js"
PURIFY_TAG = "/static/vendor/dompurify.min.js"
SAFE_TAG = "/static/js/safe-render.js"

BARE_MARKED_PARSE = re.compile(r"marked\s*\.\s*parse\s*\(")


# --------------------------------------------------------------------- 静态守卫


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_owned_pages_do_not_call_marked_parse_directly():
    """所有标记渲染必须经过 safe-render，不允许页面自己 marked.parse + innerHTML。"""
    offenders = []
    for path in OWNED_RENDER_PAGES:
        content = _read(path)
        for match in BARE_MARKED_PARSE.finditer(content):
            line_no = content[: match.start()].count("\n") + 1
            offenders.append("%s:%d" % (os.path.basename(path), line_no))
    assert not offenders, "以下位置仍在裸调 marked.parse（应改为 FeClawSafeRender.renderMarkdownSafe）：\n" + "\n".join(offenders)


def test_script_load_order_is_marked_purify_saferender():
    """marked → DOMPurify → safe-render 的加载顺序不可调换（任何顺序错都可能未消毒）。"""
    problems = []
    for path in OWNED_RENDER_PAGES:
        content = _read(path)
        name = os.path.basename(path)
        idx_marked = content.find(MARKED_TAG)
        idx_purify = content.find(PURIFY_TAG)
        idx_safe = content.find(SAFE_TAG)
        if idx_marked < 0:
            problems.append("%s 缺少 %s" % (name, MARKED_TAG))
        if idx_purify < 0:
            problems.append("%s 缺少 %s" % (name, PURIFY_TAG))
        if idx_safe < 0:
            problems.append("%s 缺少 %s" % (name, SAFE_TAG))
        if idx_marked >= 0 and idx_purify >= 0 and idx_safe >= 0:
            if not (idx_marked < idx_purify < idx_safe):
                problems.append("%s 脚本顺序错误：marked(%d) / purify(%d) / safe-render(%d)" % (name, idx_marked, idx_purify, idx_safe))
    assert not problems, "脚本加载顺序不满足 T16：\n" + "\n".join(problems)


def test_python_embedded_pages_serve_expected_pipeline():
    """Python 内嵌页面：复核服务端**真正发出**的 HTML（ast 取 Python 解析后的字符串）。

    直接读 .py 源码会被 Python 转义误导（源码里的 ``\\\\S`` 在浏览器里是 ``\\S``，
    双写会变成“反斜杠+S”而非正则字符类）。这里用 ast 取出实际字符串，检查：
    脚本顺序、没有裸 marked.parse、没有被多转义一层的正则。
    """
    pages = []
    for path in (os.path.join(ROOT, "routers", "dashboard.py"),
                 os.path.join(ROOT, "routers", "agent_config_ui.py")):
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and "<!DOCTYPE html>" in node.value.value
            ):
                pages.append((os.path.basename(path), node.value.value))

    assert pages, "没有从 Python 文件里解析出任何内嵌页面（文件结构可能已变化）"

    problems = []
    with_pipeline = 0
    for name, html in pages:
        if MARKED_TAG not in html:
            continue  # 该页面不使用 markdown，不在 T16 范围
        with_pipeline += 1
        positions = [html.find(tag) for tag in (MARKED_TAG, PURIFY_TAG, SAFE_TAG)]
        if any(pos < 0 for pos in positions) or not (positions[0] < positions[1] < positions[2]):
            problems.append("%s：内嵌页面脚本顺序/缺失（%r）" % (name, positions))
        if BARE_MARKED_PARSE.search(html):
            problems.append("%s：内嵌页面仍直接调用 marked.parse" % name)
        if "\\\\S" in html or "\\\\$" in html:
            problems.append("%s：内嵌 JS 出现双反斜杠（Python 转义多一层）" % name)

    assert with_pipeline >= 2, "应至少有两个使用 markdown 的内嵌页面，实际 %d" % with_pipeline
    assert not problems, "Python 内嵌页面不符合 T16：\n" + "\n".join(problems)


def test_dompurify_is_self_hosted_and_pinned():
    """DOMPurify 必须自托管、锁定版本、哈希与上游 3.2.4 一致。"""
    assert os.path.isfile(DOMPURIFY), "缺少自托管 DOMPurify：%s" % DOMPURIFY
    raw = open(DOMPURIFY, "rb").read()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == DOMPURIFY_SHA256, (
        "DOMPurify 内容与锁定的 3.2.4 不一致（sha256=%s，期望 %s）；"
        "如确需升级，请同时更新 safe-render.js 注释与本测试常量" % (digest, DOMPURIFY_SHA256)
    )
    head = raw[:400].decode("utf-8", "ignore")
    assert "DOMPurify 3.2.4" in head, "文件头未保留版本注释"
    assert "Apache" in head and "Mozilla Public License" in head, "文件头未保留许可证注释"

    # 运行时不依赖 CDN：代码与页面只允许引用 /static/vendor/dompurify.min.js
    for path in [SAFE_RENDER] + OWNED_RENDER_PAGES:
        content = _read(path)
        lowered = content.lower()
        for bad in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com"):
            assert bad not in lowered, "%s 仍引用外部 CDN：%s" % (os.path.basename(path), bad)


def test_safe_render_module_syntax_and_api():
    """safe-render.js 语法正确且暴露约定 API（有 node 才做语法检查）。"""
    content = _read(SAFE_RENDER)
    for api in ("renderMarkdownSafe", "renderText", "window.FeClawSafeRender", "afterSanitizeAttributes"):
        assert api in content, "safe-render.js 缺少 %s" % api

    if not shutil.which("node"):
        pytest.skip("未安装 node，跳过 safe-render.js 语法检查（浏览器用例仍会真实加载该文件）")
    result = subprocess.run(["node", "--check", SAFE_RENDER], capture_output=True, text=True)
    assert result.returncode == 0, "safe-render.js 语法错误：%s" % (result.stderr or "").strip()[:800]


# --------------------------------------------------------------------- 浏览器层

HARNESS_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>FeClaw safe-render harness</title>
<script src="marked.min.js"></script>
<script src="dompurify.min.js"></script>
<script src="katex.min.js"></script>
<script src="safe-render.js"></script>
</head>
<body>
<div id="stage"></div>
<script>
  window.__pwn = undefined;
  window.__render = function (text, options) {
    return window.FeClawSafeRender.renderMarkdownSafe(text, options || undefined);
  };
  window.__mount = function (html) {
    var host = document.getElementById('stage');
    var box = document.createElement('div');
    box.innerHTML = html;
    host.appendChild(box);
    return box;
  };
  window.__probe = function () {
    var host = document.getElementById('stage');
    var nodes = Array.prototype.slice.call(host.querySelectorAll('*'));
    var attrNames = [];
    nodes.forEach(function (el) {
      Array.prototype.forEach.call(el.attributes, function (a) { attrNames.push(a.name); });
    });
    return {
      html: host.innerHTML,
      scripts: host.querySelectorAll('script').length,
      iframes: host.querySelectorAll('iframe, object, embed, frame, frameset').length,
      svgs: host.querySelectorAll('svg').length,
      forms: host.querySelectorAll('form, input, button, select, textarea').length,
      onattrs: attrNames.filter(function (n) { return /^on/i.test(n); }),
      hrefs: Array.prototype.map.call(host.querySelectorAll('a'), function (a) { return a.getAttribute('href'); }),
      imgSrcs: Array.prototype.map.call(host.querySelectorAll('img'), function (i) { return i.getAttribute('src'); }),
      pwn: window.__pwn === undefined ? null : window.__pwn
    };
  };
</script>
</body>
</html>
"""

# 故意不加载 DOMPurify：验证 fail-closed
HARNESS_NO_PURIFY_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>FeClaw safe-render harness (no DOMPurify)</title>
<script src="marked.min.js"></script>
<script src="safe-render.js"></script>
</head>
<body>
<div id="stage"></div>
<script>
  window.__pwn = undefined;
</script>
</body>
</html>
"""

# 恶意输入：每一条都必须“不执行、不落地为主动内容”
XSS_PAYLOADS = [
    ("img_onerror", '<img src="x" onerror="window.__pwn = 1">'),
    ("svg_onload", '<svg onload="window.__pwn = 1"></svg>'),
    ("js_link", "[点我](javascript:window.__pwn = 1)"),
    ("js_link_no_space", "[点我](javascript:window.__pwn=1)"),
    ("data_link", "[点我](data:text/html;base64,PHNjcmlwdD53aW5kb3cuX19wd249MTwvc2NyaXB0Pg==)"),
    ("script_tag", "<script>window.__pwn = 1</script>"),
    ("script_breakout", "</script><script>window.__pwn = 1</script>"),
    ("html_comment_breakout", "<!-- </script> --><script>window.__pwn = 1</script>"),
    ("iframe_srcdoc", '<iframe srcdoc="<script>window.__pwn = 1</script>"></iframe>'),
    ("form_input", '<form action="https://evil.test"><input name="q"><button>go</button></form>'),
    ("img_js_src", '<img src="javascript:window.__pwn = 1">'),
    ("img_data_src", '<img src="data:image/svg+xml,<svg onload=window.__pwn=1>">'),
    ("onclick_attr", '<div onclick="window.__pwn = 1">x</div>'),
    ("mermaid_fence", '```mermaid\n<img src="x" onerror="window.__pwn = 1">\n```'),
    ("md_link_title_breakout", '[x](https://example.test "a\\" onerror=\\"window.__pwn=1")'),
]

BENIGN_MARKDOWN = "\n\n".join([
    "## 标题二",
    "```js\nconst a = 1 < 2;\n```",
    "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |",
    "勾股定理 $a^2+b^2=c^2$",
    "![示意图](https://example.test/fig.png)",
    "[官网](https://example.test/page)",
])


def _copy_assets(dest):
    for src in (MARKED, DOMPURIFY, SAFE_RENDER, KATEX):
        assert os.path.isfile(src), "缺少静态资源：%s" % src
        shutil.copy2(src, os.path.join(dest, os.path.basename(src)))


@pytest.fixture(scope="module")
def harness_dir(tmp_path_factory):
    target = tmp_path_factory.mktemp("safe_render_harness")
    _copy_assets(str(target))
    (target / "harness.html").write_text(HARNESS_HTML, encoding="utf-8")
    (target / "harness_no_purify.html").write_text(HARNESS_NO_PURIFY_HTML, encoding="utf-8")
    return target


def _launch(playwright):
    """优先本机 msedge（channel="msedge"），其次 Playwright 自带 chromium。"""
    errors = []
    for kwargs in ({"channel": "msedge"}, {}):
        try:
            return playwright.chromium.launch(headless=True, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 汇总后统一 skip
            errors.append("%s: %s" % (kwargs or "bundled chromium", exc))
    pytest.skip("没有可用的浏览器内核（本测试刻意不下载浏览器）：" + " | ".join(errors))


@pytest.fixture(scope="module")
def browser(harness_dir):
    pytest.importorskip(
        "playwright.sync_api",
        reason="未安装 playwright：这些用例需要真实浏览器才能证明 XSS 不执行（pip install playwright，本测试复用本机 msedge，不下载浏览器）",
    )
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            yield browser
        finally:
            browser.close()


class _Session(object):
    def __init__(self, page):
        self.page = page
        self.errors = []
        self.dialogs = []

    def render(self, text):
        return self.page.evaluate("(t) => window.__render(t)", text)

    def mount(self, html):
        self.page.evaluate("(h) => window.__mount(h)", html)

    def probe(self):
        return self.page.evaluate("() => window.__probe()")


@pytest.fixture()
def session(browser, harness_dir):
    from playwright.sync_api import Error as PlaywrightError

    page = browser.new_page()
    s = _Session(page)
    page.on("pageerror", lambda exc: s.errors.append(str(exc)))
    page.on("dialog", lambda d: (s.dialogs.append(d.message), d.dismiss()))
    page.goto((harness_dir / "harness.html").as_uri())
    page.wait_for_function("() => !!(window.FeClawSafeRender && window.FeClawSafeRender.isAvailable())")
    try:
        yield s
    finally:
        try:
            page.close()
        except PlaywrightError:
            pass


def test_dangerous_payloads_do_not_execute(session):
    """<img onerror> / javascript: / </script> 逃逸 / iframe / form / data: 都不执行。"""
    for name, payload in XSS_PAYLOADS:
        session.page.evaluate("() => { window.__pwn = undefined; document.getElementById('stage').innerHTML = ''; }")
        html = session.render(payload)
        session.mount(html)
        probe = session.probe()

        assert probe["pwn"] is None, "[%s] 恶意载荷真的执行了（window.__pwn 被置位）" % name
        assert not session.dialogs, "[%s] 触发了弹窗：%s" % (name, session.dialogs)
        assert not session.errors, "[%s] 页面报错：%s" % (name, session.errors)
        assert probe["scripts"] == 0, "[%s] 输出里仍有 <script>" % name
        assert probe["iframes"] == 0, "[%s] 输出里仍有 iframe/object/embed" % name
        assert probe["svgs"] == 0, "[%s] 输出里仍有 <svg>" % name
        assert probe["forms"] == 0, "[%s] 输出里仍有 form/input/button" % name
        assert not probe["onattrs"], "[%s] 输出里仍有 on* 属性：%s" % (name, probe["onattrs"])

        for href in probe["hrefs"]:
            if href is None:
                continue
            assert not re.match(r"^\s*(?:javascript|data|vbscript):", href, re.I), \
                "[%s] 链接协议未白名单过滤：%s" % (name, href)
        for src in probe["imgSrcs"]:
            if src is None:
                continue
            assert not re.match(r"^\s*(?:javascript|data|vbscript):", src, re.I), \
                "[%s] 图片协议未过滤：%s" % (name, src)

        lowered = (probe["html"] or "").lower()
        assert "<script" not in lowered, "[%s] 输出字符串里仍有 <script" % name
        # 注意：不在此断言 "onerror=" 子串 —— 转义后它可能作为 title 文本出现，
        # 是否属主动属性由上方的 DOM 属性检查（onattrs）判定。


def test_benign_markdown_still_renders(session):
    """代码块 / 表格 / 公式 / 图片 / 链接仍然可读（安全边界不能把正常内容吃掉）。"""
    html = session.render(BENIGN_MARKDOWN)
    session.mount(html)
    probe = session.probe()

    assert not session.errors, "正常内容渲染报错：%s" % session.errors
    assert probe["pwn"] is None

    code = session.page.locator("#stage pre code").first.inner_text()
    assert "const a = 1 < 2;" in code, "代码块内容被破坏：%r" % code

    assert session.page.locator("#stage table").count() == 1, "表格没有渲染"
    assert session.page.locator("#stage table td").count() >= 2, "表格单元格缺失"

    assert session.page.locator("#stage .katex").count() >= 1, "KaTeX 公式没有渲染"

    img = session.page.locator("#stage img[src='https://example.test/fig.png']")
    assert img.count() == 1, "正常图片没有渲染"
    assert img.first.get_attribute("alt") == "示意图", "图片 alt 丢失"

    link = session.page.locator("#stage a[href='https://example.test/page']")
    assert link.count() == 1, "正常链接没有渲染"
    assert link.first.get_attribute("target") == "_blank", "链接未强制 target=_blank"
    rel = (link.first.get_attribute("rel") or "").lower()
    assert "noopener" in rel and "noreferrer" in rel, "链接缺少 noopener noreferrer：%r" % rel

    assert session.page.locator("#stage h2").count() == 1, "标题没有渲染"
    assert "javascript:" not in probe["html"].lower()


def test_math_inside_code_fence_stays_literal(session):
    """代码块里的 $...$ 不能被当成公式渲染（否则代码会被改写）。"""
    html = session.render("```\n$x^2$\n```\n\n行内 $y^2$")
    session.mount(html)
    assert session.page.locator("#stage pre .katex").count() == 0, "代码块内被错误渲染成公式"
    assert "$x^2$" in session.page.locator("#stage pre").first.inner_text(), "代码块内容被篡改"
    assert session.page.locator("#stage .katex").count() == 1, "行内公式没有渲染"


def test_render_text_escapes_markup(browser, harness_dir):
    """纯文本入口只做转义，不解释标签。"""
    page = browser.new_page()
    try:
        page.goto((harness_dir / "harness.html").as_uri())
        page.wait_for_function("() => !!(window.FeClawSafeRender && window.FeClawSafeRender.isAvailable())")
        out = page.evaluate("() => window.FeClawSafeRender.renderText('<img src=x onerror=alert(1)>')")
    finally:
        page.close()
    assert out == "&lt;img src=x onerror=alert(1)&gt;"
    assert "<img" not in out


def test_missing_dompurify_fails_closed(browser, harness_dir):
    """DOMPurify 未加载时只能退化为纯文本，绝不返回未消毒 HTML。"""
    page = browser.new_page()
    try:
        page.goto((harness_dir / "harness_no_purify.html").as_uri())
        page.wait_for_function("() => !!window.FeClawSafeRender")
        result = page.evaluate(
            """() => {
                var sr = window.FeClawSafeRender;
                return {
                    available: sr.isAvailable(),
                    dangerous: sr.renderMarkdownSafe('<img src="x" onerror="window.__pwn=1">'),
                    markdown: sr.renderMarkdownSafe('# 标题')
                };
            }"""
        )
    finally:
        page.close()
    assert result["available"] is False
    dangerous = result["dangerous"].lower()
    assert "<img" not in dangerous, "缺少消毒器时仍然输出可执行标签：%r" % result["dangerous"]
    assert "&lt;img" in dangerous, "缺少消毒器时应转义为纯文本：%r" % result["dangerous"]
    assert "<h1>" not in result["markdown"].lower(), "缺少消毒器时不应渲染 markdown 结构"


def test_hooks_installed_once_and_module_reload_is_idempotent(session):
    """hook 一次性安装：重复调用 installHooks / 重复加载脚本都不叠加 hook。"""
    first = session.page.evaluate("() => window.FeClawSafeRender.installHooks()")
    assert first is False, "installHooks() 应幂等（第二次返回 false）"

    marker_before = session.page.evaluate("() => window.DOMPurify.__feclawSafeRenderHooks")
    version_before = session.page.evaluate("() => window.FeClawSafeRender.version")
    assert marker_before == version_before, "hook 标记未写入 DOMPurify 实例"

    session.page.evaluate(
        """() => new Promise(function (resolve) {
            var s = document.createElement('script');
            s.src = 'safe-render.js';
            s.onload = function () { resolve(true); };
            s.onerror = function () { resolve(false); };
            document.head.appendChild(s);
        })"""
    )
    after = session.page.evaluate(
        """() => ({
            version: window.FeClawSafeRender.version,
            marker: window.DOMPurify.__feclawSafeRenderHooks,
            installAgain: window.FeClawSafeRender.installHooks()
        })"""
    )
    assert after["version"] == version_before, "重复加载改写了模块版本"
    assert after["marker"] == marker_before, "重复加载叠加了 hook 状态"
    assert after["installAgain"] is False

    # 重复加载后渲染仍然安全
    html = session.render('<img src="x" onerror="window.__pwn = 1">')
    session.mount(html)
    probe = session.probe()
    assert probe["pwn"] is None
    assert not probe["onattrs"]
