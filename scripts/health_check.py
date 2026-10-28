#!/usr/bin/env python3
"""FeClaw 前端资源 / 语法 / 页面 / 接口 健康检查（一条命令跑完）。

背景：2026-10-02 一晚连撞三次「静默地成功地做错事」——
  ① auth.js 少一个逗号 ⇒ 整文件 SyntaxError ⇒ Auth 未定义 ⇒ 页面只报「服务不可用」
  ② settings.html 登出链指向不存在的端点
  ③ 缺依赖 ⇒ /api/file/sts-credential 500 ⇒ 文件页静默回落「本地存储」
  （Q9 又补一例：dashboard JS 静默坏）
本脚本把这些「静态坏」变成可检测、可重复、可回归的体检。

⚠️ 2026-10-25（T02）：默认路径**必须真的只读**。
   旧版默认会 login/register、创建 Agent、跑 POST/PUT/PATCH/DELETE、撞自毁接口，
   即「体检本身会改数据」。现在分三级：

   L0 默认（只读巡检，CI/日常/生产可安全跑）
      ① JS 语法体检（本地 node --check）
      ② 模板静态资源真实性（仅 GET）
      ③ 页面加载体检（真 Chromium，仅 GET/导航）
      ④ 接口矩阵（仅 GET；POST/PUT/PATCH/DELETE 一律记为「只读模式跳过」）
      • 不登录、不注册、不创建、不删除、不调用模型
      • 身份只来自运维显式提供的只读令牌 FECLAW_HC_TOKEN（可选，脚本不代登录）

   L1 --integration（仍只读）
      允许带 FECLAW_HC_TOKEN 做已鉴权 GET 探活；仍然不 login/register、不写。

   L2 --integration --allow-mutations（破坏性，人已批准的一次性空库/测试环境）
      • 必须显式传 --base-url，且只接受回环/私网地址，生产域名一律拒绝（fail-closed）
      • 不复用任何已有账号或它的首个 Agent：本次运行注册自己的随机账号、自己建 Agent
      • 每条写操作的目标都必须能追溯到本次 ResourceLedger 记录（清理白名单）
      • 清理只删 ledger 里记录的资源；未记录的 id 一律拒绝，账号（无删除端点）如实报保留

用法
  python scripts/health_check.py                          # L0 只读巡检 http://127.0.0.1:8080
  python scripts/health_check.py --base-url http://127.0.0.1:8091 --only 1,2
  python scripts/health_check.py --json /tmp/hc.json --skip-browser
  python scripts/health_check.py --ready-check            # 只做 /ready 门禁（deploy.sh 同一契约）
  python scripts/health_check.py --integration --allow-mutations \
        --base-url http://127.0.0.1:8091                # L2，仅一次性测试环境

依赖：node（①）；playwright + 本机已装的 chromium（③，不会联网下载浏览器）。
退出码：0 = 无发现；1 = 有发现；2 = 环境准备失败 / 参数不安全（如 mutation 目标不合规）。
"""
from __future__ import annotations

import argparse
import glob
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 输出统一 UTF-8：CI / Windows 控制台的 locale 可能是 GBK，中文与 ✓/✗ 会直接
# UnicodeEncodeError 把体检脚本自己搞崩（静默失败的反面：脚本挂了却像没问题）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

# ─────────────────────────── 配置 ───────────────────────────

DEFAULT_BASE_URL = "http://127.0.0.1:8080"

# 生产域名：mutation 模式必须拒绝（即使有人显式传了这些 host 也拒绝）
PRODUCTION_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get(
        "FECLAW_HC_PROD_HOSTS", "feclaw.lizidaren.cn,lizidaren.cn"
    ).split(",")
    if h.strip()
)

# 只读巡检可用的令牌：由运维显式提供，脚本自己**不**登录拿 token
ENV_READONLY_TOKEN = "FECLAW_HC_TOKEN"

# 页面清单（label, host 角色 root|sub, path）
PAGES = [
    ("index",           "root", "/"),
    ("dashboard",       "root", "/dashboard"),
    ("settings",        "root", "/settings"),
    ("login",           "root", "/login"),
    ("admin-settings",  "root", "/admin/settings"),
    ("agent-flat",      "root", "/agent/{hash}"),
    ("agent-configure", "root", "/agent/{hash}/configure"),
    ("agent-files-flat", "root", "/agent/{hash}/files"),
    ("agent-chat-flat", "root", "/agent/{hash}/chat"),
    ("agent-root-sub",  "sub",  "/"),
    ("agent-files-sub", "sub",  "/files"),
    ("agent-chat-sub",  "sub",  "/chat"),
    ("agent-settings-sub", "sub", "/settings"),
]

# 已知 / 已记录的良性噪声（不判 FAIL，但会在报告里带理由单列）。
BENIGN_SUBSTR = {
    "favicon.ico": "无模板声明 favicon ⇒ 浏览器默认探测 /favicon.ico 404（纯装饰）",
    "net::ERR_ABORTED": "导航把在途请求中断（如 /api/oauth/me），非服务端错误",
}
# 测试账号非管理员 ⇒ 管理页 403 属预期（换成 admin 账号可消除）
ADMIN_ONLY_PAGES = {"admin-settings"}

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# ④ 接口探活：变更类方法一律用「不存在的」资源 id，避免破坏数据
BOGUS_HASH = "ffffffff"
BOGUS_ID = "99999999"

# 自毁类：会吊销本轮 token，放到最后并用独立 token 跑（仅 L2）
SELF_DESTRUCTIVE = (
    "/api/auth/logout", "/api/oauth/logout",
    "/api/user/change-password", "/api/user/password",
    "/setup/", "/api/auth/revoke-all",
)

# 这些 POST/PUT 用 {} 请求体会「真的创建资源」（而不是校验失败）⇒ 跳过。
# L2 也不跑：那会创建 ledger 之外、无法追溯清理的资源。
MUTATING_SKIP = {
    "/api/user/agents": "空 body 会真创建一个 Agent（改用本次 ledger 显式创建）",
    "/api/chat/sessions": "空 body 可能建会话",
    "/api/groups": "空 body 可能建群",
    "/api/organizations": "空 body 可能建组织",
    "/api/console/agents": "空 body 可能建 Agent",
    "/api/zentrim/entries": "空 body 可能建条目",
    "/api/share/reference": "空 body 可能建分享",
    "/api/static-sites": "空 body 可能建站",
    "/api/user/agents/{hash}/vfs/mkdir": "会在 VFS 建目录",
}

# 只读巡检也绝不触碰的路径：可能触发计费模型 / 自毁。模型可用性应独立低频观察，
# 不能让每次探活都调用收费模型。
READONLY_SKIP_SUBSTR = (
    "/chat/stream",      # SSE 对话 = 模型调用
    "/chat/ws",          # WebSocket 对话
    "/generate",         # 生成类
    "/auto_suggest",     # 自动建议会话（模型）
    "/summarize",        # 摘要（模型）
    "/tts",              # 语音合成（计费）
    "/llm",              # 直连模型
    "/completion",
    "/oauth/authorize",  # 会 302 到第三方 OAuth 提供方（只读巡检不出网）
    "/oauth/callback",
    "/oauth/login",
)

READY_OK_STATUS = "ready"


def _param_table(agent_hash: str, agent_id: str, user_id: str) -> dict:
    return {
        "agent_hash": agent_hash, "hash": agent_hash,
        "agent_id": agent_id, "user_id": user_id,
        "session_id": "hc-nonexistent-session",
        "group_id": "999999", "site_id": "999999", "app_id": "999999",
        "entry_id": "999999", "request_id": "999999", "timeline_id": "999999",
        "ref_id": "999999", "ref_hash": "00000000", "moment_id": "999999",
        "organization_id": "999999", "sandbox_id": "999999", "provider_id": "999999",
        "slug": "hc-no-such-slug", "token": "hc-no-such-token",
        "path": "hc_no_such_file.txt", "file_path": "hc_no_such_file.txt",
        "provider": "hc-nonexistent", "model": "hc-nonexistent",
    }


# ─────────────────────── 资源账本（清理白名单） ───────────────────────

class ResourceLedger:
    """记录本次 run 创建的资源。

    清理阶段只能删除这里的记录（或运维用 --mutation-resource-allowlist 显式追加的
    kind:id）。未记录的 id 一律拒绝 —— 这样「体检脚本」不会删掉别人的数据。
    """

    def __init__(self) -> None:
        self._created: dict[str, list[str]] = {}
        self._allowlist: set[tuple[str, str]] = set()

    def record(self, kind: str, ident) -> None:
        if ident is None or str(ident) == "":
            raise ValueError(f"拒绝记录空资源 {kind}")
        bucket = self._created.setdefault(kind, [])
        ident = str(ident)
        if ident not in bucket:
            bucket.append(ident)

    def ids(self, kind: str) -> list[str]:
        return list(self._created.get(kind, []))

    def add_allowlist(self, entries: str) -> None:
        """--mutation-resource-allowlist 形如 "agent:12,agent:13"。"""
        for raw in (entries or "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            if ":" not in raw:
                raise ValueError(f"allowlist 条目格式应为 kind:id，收到 {raw!r}")
            kind, ident = raw.split(":", 1)
            self._allowlist.add((kind.strip(), ident.strip()))

    def may_delete(self, kind: str, ident) -> bool:
        ident = str(ident)
        return ident in self._created.get(kind, []) or (kind, ident) in self._allowlist

    def summary(self) -> dict:
        return {k: list(v) for k, v in self._created.items()}


# ─────────────────────────── 工具 ───────────────────────────

def hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def http(url: str, method: str = "GET", token: str | None = None,
         body: dict | None = None, timeout: int = 25, retries: int = 2,
         maxbytes: int | None = 4000):
    data = json.dumps(body).encode() if body is not None else None
    last = (None, "unreachable")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read() if maxbytes is None else r.read(maxbytes)
                return r.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read() if (e.fp and maxbytes is None) else (e.read(maxbytes) if e.fp else b"")
            return e.code, raw.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = (None, f"{type(e).__name__}: {e}")
    return last


# ─────────────────── /ready 门禁（deploy.sh 同一契约） ───────────────────

def probe_ready(base_url: str, timeout: float = 5.0) -> tuple[bool, dict]:
    """检查 /ready：退出码语义 + HTTP status + JSON status == "ready" 三者都要过。

    返回 (ok, report)。report 里只有可公开字段：不写凭证、不回显内部异常文本。
    """
    url = base_url.rstrip("/") + "/ready"
    report: dict = {"url": url, "ok": False, "http_status": None, "reason": "",
                    "status": None, "checks": None}
    code, raw = http(url, "GET", timeout=timeout, retries=0, maxbytes=None)
    report["http_status"] = code
    if code is None:
        report["reason"] = "unreachable"
        return False, report
    if not (raw or "").strip():
        report["reason"] = "empty-body"
        return False, report
    if code != 200:
        report["reason"] = f"http-{code}"
        # 失败时仍然尝试解析 body 便于排障（只取 status/checks，不落原始文本）
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            return False, report
        if isinstance(payload, dict):
            report["status"] = payload.get("status")
            report["checks"] = payload.get("checks")
        return False, report
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        report["reason"] = "invalid-json"
        return False, report
    if not isinstance(payload, dict):
        report["reason"] = "json-not-object"
        return False, report
    status = payload.get("status")
    report["status"] = status
    report["checks"] = payload.get("checks")
    if status != READY_OK_STATUS:
        report["reason"] = f"status={status!r}"
        return False, report
    report["ok"] = True
    return True, report


# ─────────────────────── ① JS 语法体检 ───────────────────────

def _node_check(src: str, module: bool) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".mjs" if module else ".js",
                                     delete=False, encoding="utf-8") as f:
        f.write(src)
        tmp = f.name
    try:
        cmd = ["node", "--check", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True, ""
        # node 的报错首行是 "file:line"，转成可读
        lines = [l for l in r.stderr.splitlines() if l.strip()]
        loc = lines[0] if lines else "?"
        msg = next((l for l in lines if "Error" in l), lines[-1] if lines else "")
        return False, f"{loc}  {msg}"
    finally:
        os.unlink(tmp)


def _neutralize_jinja(js: str) -> str:
    js = re.sub(r"\{\{.*?\}\}", "0", js, flags=re.S)
    js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)
    js = re.sub(r"\{#.*?#\}", "", js, flags=re.S)
    return js


def phase_js(repo: Path) -> dict:
    hr("① JS 语法体检")
    if subprocess.run(["node", "--version"], capture_output=True).returncode != 0:
        print("  ⚠️ 未找到 node，跳过阶段①")
        return {"skipped": "node not found", "findings": []}

    findings, checked = [], []
    for f in sorted(glob.glob(str(repo / "static/js/*.js"))):
        src = Path(f).read_text(encoding="utf-8", errors="replace")
        ok_s, err_s = _node_check(src, module=False)
        ok_m, err_m = _node_check(src, module=True)
        name = os.path.basename(f)
        checked.append(name)
        if not (ok_s or ok_m):
            findings.append({"file": name, "script": err_s, "module": err_m})
            print(f"  ✗ {name}\n      script: {err_s}\n      module: {err_m}")
        else:
            tag = "script" if ok_s else "module-only"
            print(f"  ✓ {name}  ({tag})")
    print(f"  —— static/js：{len(checked)} 个文件")

    # 模板内联脚本（Q9 dashboard 静默坏就发生在这里）
    inline_n = 0
    for path in sorted(glob.glob(str(repo / "templates/*.html"))):
        html = Path(path).read_text(encoding="utf-8", errors="replace")
        for i, (attrs, body) in enumerate(
                re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I), start=1):
            if re.search(r"\bsrc\s*=", attrs, re.I) or not body.strip():
                continue
            inline_n += 1
            js = _neutralize_jinja(body)
            ok_w, err_w = _node_check(f"(async function(){{\n{js}\n}})();", module=False)
            ok_b, err_b = _node_check(js, module=False)
            if not (ok_w or ok_b):
                name = os.path.basename(path)
                findings.append({"file": f"{name}#inline{i}", "script": err_b, "module": err_w})
                print(f"  ✗ {name} inline#{i}: {err_b}")
    print(f"  —— 模板内联脚本：{inline_n} 段，问题 {len(findings)} 处")

    return {"checked": checked, "inline": inline_n, "findings": findings}


# ─────────────────── ② 模板静态资源真实性 ───────────────────

_ASSET_RE = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']|url\(\s*['"]?([^)'"]+)""", re.I)


def phase_assets(base_url: str, repo: Path) -> dict:
    hr("② 模板静态资源真实性（仅 GET）")
    local, external, dynamic = {}, {}, {}
    for path in sorted(glob.glob(str(repo / "templates/*.html"))):
        html = Path(path).read_text(encoding="utf-8", errors="replace")
        name = os.path.basename(path)
        for m in _ASSET_RE.finditer(html):
            u = (m.group(1) or m.group(2) or "").strip()
            if not u or u.startswith(("javascript:", "mailto:", "#", "data:")):
                continue
            # JS 模板插值（`/chat?agent=${agent.id}`）与 Jinja（`{{ ... }}`）都是动态值，
            # 不是静态资源 —— 跳过，否则会误报 404/422。
            if "${" in u or "{{" in u:
                dynamic.setdefault(u, set()).add(name)
                continue
            if u.startswith("//") or re.match(r"https?://", u):
                external.setdefault(u, set()).add(name)
            elif u.startswith("/"):
                local.setdefault(u, set()).add(name)

    print(f"  —— 本站资源 {len(local)} 个（真请求，必须 200）")
    bad = []
    for u, srcs in sorted(local.items()):
        code, _ = http(base_url + u)
        ok = code == 200
        print(f"  {'✓' if ok else '✗'} {code}  {u}  <- {','.join(sorted(srcs))}")
        if not ok:
            bad.append((u, code, sorted(srcs)))

    print(f"\n  —— 动态引用 {len(dynamic)} 个（JS 插值 / Jinja，非静态资源）")
    for u, srcs in sorted(dynamic.items()):
        print(f"    ~ {u}  <- {','.join(sorted(srcs))}")

    print(f"\n  —— 外部 CDN 依赖 {len(external)} 个（不请求；Q9 🟡13 存疑项单列）")
    ext_rows = []
    for u, srcs in sorted(external.items()):
        print(f"    · {u}  <- {','.join(sorted(srcs))}")
        ext_rows.append({"url": u, "templates": sorted(srcs)})

    return {"local": {u: sorted(s) for u, s in local.items()},
            "missing": bad, "external": ext_rows,
            "dynamic": {u: sorted(s) for u, s in dynamic.items()}}


# ─────────────────── ③ 页面加载体检 ───────────────────

def _find_chromium() -> str | None:
    """优先用本机已装的 playwright chromium —— 绝不联网下载。"""
    cands = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    cands += ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def phase_pages(base_url: str, root_host: str, token: str, agent_hash: str,
                sub_host: str | None, guard_mutations: bool = True) -> dict:
    """页面加载体检。

    guard_mutations=True（默认只读巡检）：在浏览器层拦截并 abort 所有
    POST/PUT/PATCH/DELETE —— 页面 JS 悄悄发写请求也不会落到服务端。
    """
    hr("③ 页面加载即报错体检（Playwright / 真 Chromium，仅导航 GET）")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠️ 未安装 playwright，跳过阶段③")
        return {"skipped": "playwright not installed", "pages": []}

    exe = _find_chromium()
    if not exe:
        print("  ⚠️ 未找到本机 chromium，跳过阶段③（不联网下载）")
        return {"skipped": "no chromium", "pages": []}
    print(f"  chromium: {exe}")

    hmap = [f"MAP {root_host} 127.0.0.1"]
    if sub_host:
        hmap.append(f"MAP {sub_host} 127.0.0.1")

    anonymous = not token
    if anonymous:
        print("  ℹ️ 未提供只读令牌（FECLAW_HC_TOKEN）：按匿名身份加载；"
              "需要登录的 401/403 记为已知预期，不判失败")
    # 只读巡检不代登录 ⇒ 可能不知道真实 Agent hash。此时带 {hash} 的页面没有意义
    # （会打出不存在的 Agent）。显式 --agent-hash / --sub-host 时才会跑。
    skip_hash_pages = not agent_hash

    results = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=exe, headless=True,
                              args=[f"--host-resolver-rules={','.join(hmap)}", "--no-sandbox"])
        for label, role, path_tpl in PAGES:
            if "{hash}" in path_tpl and skip_hash_pages:
                print(f"  ~ {label:20s} 跳过（未提供 --agent-hash：只读巡检不代登录取 hash）")
                continue
            path = path_tpl.format(hash=agent_hash)
            host = sub_host if role == "sub" else root_host
            if role == "sub" and not sub_host:
                continue
            ctx = b.new_context(ignore_https_errors=True)
            if token:
                ctx.add_cookies([{"name": "feclaw_jwt", "value": token, "domain": root_host, "path": "/"}])
                if sub_host:
                    ctx.add_cookies([{"name": "feclaw_jwt", "value": token, "domain": sub_host, "path": "/"}])
            page = ctx.new_page()
            errs, fails, bad = [], [], []
            blocked_mutations: list[str] = []
            if guard_mutations:
                # 只读兜底：即使页面 JS 想 POST/PUT/PATCH/DELETE，也在浏览器层 abort，
                # 保证「默认巡检」对服务端零写入。
                def _guard(route, request, L=blocked_mutations):
                    if request.method.upper() in MUTATING_METHODS:
                        L.append(f"{request.method.upper()} {request.url}")
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", _guard)
            page.on("pageerror", lambda e, L=errs: L.append(f"pageerror: {e}"))
            page.on("console", lambda m, L=errs: L.append(f"console.error: {m.text}")
                    if m.type == "error" else None)
            page.on("requestfailed", lambda r, L=fails: L.append(f"{r.url} :: {r.failure}"))
            page.on("response", lambda r, L=bad: L.append((r.status, r.url)) if r.status >= 400 else None)
            url = f"{base_url.replace('127.0.0.1', host).replace('localhost', host)}{path}"
            status = None
            try:
                resp = page.goto(url, wait_until="networkidle", timeout=30000)
                status = resp.status if resp else None
            except Exception as e:  # noqa: BLE001
                errs.append(f"goto-error: {e}")
                status = None
            page.wait_for_timeout(1500)
            ctx.close()

            def _benign(s: str) -> str | None:
                for k, why in BENIGN_SUBSTR.items():
                    if k in s:
                        return why
                return None

            known, unknown = [], []
            for s in errs:
                why = _benign(s)
                if why:
                    known.append((s, why))
                elif label in ADMIN_ONLY_PAGES and "403" in s:
                    known.append((s, "测试账号非管理员 ⇒ 管理页 403 预期"))
                elif anonymous and ("401" in s or "403" in s):
                    known.append((s, "匿名加载：需要登录的请求 401/403 属预期（提供 FECLAW_HC_TOKEN 可覆盖）"))
                else:
                    unknown.append(s)
            for f in fails:
                (known if _benign(f) else unknown).append(f)
            for st, u in bad:
                if _benign(u) or (label in ADMIN_ONLY_PAGES and st == 403):
                    known.append((f"{st} {u}", "见上（favicon / 非管理员 403）"))
                elif anonymous and st in (401, 403):
                    known.append((f"{st} {u}", "匿名加载：401/403 属预期"))
                else:
                    unknown.append(f"{st} {u}")

            results.append({"label": label, "url": url, "status": status,
                            "final": None, "errors": errs, "reqfail": fails,
                            "http4xx5xx": bad, "unknown": unknown,
                            "blocked_mutations": blocked_mutations,
                            "known": [{"item": i, "why": w} for i, w in known]})
            n_err = len([e for e in errs if e.startswith("pageerror")])
            print(f"  {label:20s} status={status} pageerror={n_err} "
                  f"console.err={len(errs) - n_err} reqfail={len(fails)} http>=400={len(bad)}"
                  f"  未知={len(unknown)}  已拦截写请求={len(blocked_mutations)}")
            for e in unknown:
                print(f"      ✗ {e[:220]}")
            for bm in blocked_mutations:
                print(f"      ⛔ 只读拦截：{bm[:160]}")
            for i, w in known:
                print(f"      · 已知：{i[:120]}  —— {w}")
        b.close()

    return {"pages": results}


# ─────────────────── ④ 接口健康矩阵 ───────────────────

def _expected_readonly(method: str, path: str) -> str:
    if "{" in path:
        return "2xx/4xx"
    return "2xx/4xx"


def _skip_readonly(path: str) -> str | None:
    low = path.lower()
    for token in READONLY_SKIP_SUBSTR:
        if token in low:
            return f"只读巡检跳过（可能触发模型/计费或长连接）：{token}"
    if path.startswith(SELF_DESTRUCTIVE):
        return "自毁接口（会吊销 token）—— 仅 L2 用独立一次性账号跑"
    return None


def _write_json(path: str, payload) -> None:
    """JSON 一律 UTF-8 落盘（默认 locale 在 Windows 是 GBK，会写出坏文件）。"""
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_openapi(base_url: str) -> tuple[dict | None, str]:
    code, raw = http(base_url + "/openapi.json", timeout=60, maxbytes=None)
    if code != 200:
        return None, f"openapi {code}: {(raw or '')[:160]}"
    try:
        return json.loads(raw), ""
    except Exception as e:  # noqa: BLE001
        return None, f"openapi parse error: {type(e).__name__}"


def phase_api_readonly(base_url: str, token: str | None) -> dict:
    """只读接口矩阵：只发 GET；写方法仅登记为「跳过」，绝不发出。"""
    hr("④ 接口健康矩阵（只读：仅 GET）")
    spec, err = _load_openapi(base_url)
    if spec is None:
        print(f"  ✗ 取不到 /openapi.json（{err}）")
        return {"rows": [], "error": err}

    read_tbl = _param_table(BOGUS_HASH, BOGUS_ID, "1")
    rows = []
    mutating_skipped = 0
    for path in sorted(spec.get("paths", {})):
        for method in sorted(spec["paths"][path]):
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            if method.upper() in MUTATING_METHODS:
                mutating_skipped += 1
                rows.append({"method": method.upper(), "path": path, "real": path,
                             "code": None, "expected": "只读模式跳过",
                             "note": "只读巡检不发写/变更请求（--integration --allow-mutations 才跑）",
                             "self_destructive": False})
                continue
            skip = _skip_readonly(path)
            real = re.sub(r"\{([^}]+)\}", lambda m: read_tbl.get(m.group(1), "1"), path)
            if skip:
                rows.append({"method": method.upper(), "path": path, "real": real,
                             "code": None, "expected": "跳过", "note": skip,
                             "self_destructive": False})
                continue
            c, payload = http(base_url + real, "GET", token=token)
            rows.append({"method": "GET", "path": path, "real": real,
                         "code": c, "expected": _expected_readonly("GET", path),
                         "note": "", "body": (payload or "")[:200], "self_destructive": False})

    hist = Counter(r["code"] for r in rows)
    print(f"  ops={len(rows)}  写方法已跳过={mutating_skipped}  histogram={hist.most_common()}")
    bad = [r for r in rows if r["code"] is not None and r["code"] >= 500]
    print(f"  5xx = {len(bad)}   跳过/只读省略 = {len([r for r in rows if r['code'] is None])}")
    for r in bad:
        print(f"    ✗ {r['code']} {r['method']:6s} {r['real']}\n        {(r.get('body') or '')[:160]}")
    return {"rows": rows, "histogram": dict(hist),
            "mutating_skipped": mutating_skipped, "readonly": True}


# ─────────────────── L2：一次性资源 + 变更探活 ───────────────────

def _random_tag() -> str:
    import secrets
    return f"{int(time.time())}{secrets.token_hex(3)}"


def _register_disposable_account(base_url: str, tag: str, ledger: ResourceLedger) -> dict:
    """注册本次运行专属账号（绝不复用已有账号 / 首个 agent）。"""
    import secrets
    username = os.environ.get("FECLAW_HC_MUTATION_USER") or f"q4_hc_{tag}"
    password = os.environ.get("FECLAW_HC_MUTATION_PASS") or f"Hc#{secrets.token_hex(8)}"
    code, raw = http(base_url + "/api/user/register", "POST",
                     body={"username": username, "password": password,
                           "email": f"{username}@example.invalid"})
    if code != 200:
        raise RuntimeError(f"一次性账号注册失败（HTTP {code}）：拒绝复用已有账号")
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        payload = {}
    token = payload.get("token")
    if not token:
        code, raw = http(base_url + "/api/user/login", "POST",
                         body={"username": username, "password": password})
        if code != 200:
            raise RuntimeError(f"一次性账号登录失败（HTTP {code}）")
        token = (json.loads(raw) or {}).get("token")
    if not token:
        raise RuntimeError("一次性账号拿不到 token")
    ledger.record("account", username)
    print(f"  + 本次一次性账号：{username}（已记入 ledger，只用于本 run）")
    return {"username": username, "token": token}


def _create_ledger_agent(base_url: str, token: str, tag: str, ledger: ResourceLedger) -> dict:
    name = f"hc-probe-{tag}"
    code, raw = http(base_url + "/api/user/agents", "POST", token=token,
                     body={"name": name, "agent_type": "classic"})
    if code != 200:
        raise RuntimeError(f"本次测试 Agent 创建失败（HTTP {code}）")
    payload = json.loads(raw or "{}")
    agent_hash = payload.get("hash")
    agent_id = payload.get("id")
    if agent_hash:
        ledger.record("agent_hash", agent_hash)
    if agent_id is not None:
        ledger.record("agent", agent_id)
    print(f"  + 本次测试 Agent：name={name} hash={agent_hash} id={agent_id}（已记入 ledger）")
    return payload


def phase_api_mutations(base_url: str, account: dict, ledger: ResourceLedger,
                        tag: str) -> dict:
    """L2 破坏性探活：只用本次一次性账号的 token，且不创建 ledger 外的资源。"""
    hr("④' 接口变更矩阵（--integration --allow-mutations，一次性账号）")
    token = account["token"]
    spec, err = _load_openapi(base_url)
    if spec is None:
        print(f"  ✗ 取不到 /openapi.json（{err}）")
        return {"rows": [], "error": err}

    read_tbl = _param_table(BOGUS_HASH, BOGUS_ID, "1")
    rows = []
    for path in sorted(spec.get("paths", {})):
        for method in sorted(spec["paths"][path]):
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            real = re.sub(r"\{([^}]+)\}", lambda m: read_tbl.get(m.group(1), "1"), path)
            if path.startswith(SELF_DESTRUCTIVE):
                rows.append({"method": method.upper(), "path": path, "real": real,
                             "code": None, "expected": "自毁接口", "note": "延后单独跑",
                             "self_destructive": True})
                continue
            mut = method in ("post", "put", "patch", "delete")
            if mut and path in MUTATING_SKIP:
                rows.append({"method": method.upper(), "path": path, "real": real,
                             "code": None, "expected": "跳过",
                             "note": MUTATING_SKIP[path] + "（ledger 外资源，不创建）",
                             "self_destructive": False})
                continue
            body = {} if mut else None
            c, payload = http(base_url + real, method.upper(), token=token, body=body)
            rows.append({"method": method.upper(), "path": path, "real": real,
                         "code": c, "expected": _expected_readonly(method.upper(), path),
                         "note": "", "body": (payload or "")[:200], "self_destructive": False})

    # 自毁接口：再用一个独立的一次性账号，避免吊销主 token
    tmp_account = _register_disposable_account(base_url, tag + "d", ledger)
    for r in rows:
        if r["self_destructive"]:
            c, payload = http(base_url + r["real"], r["method"],
                              token=tmp_account["token"],
                              body={} if r["method"] != "GET" else None)
            r["code"] = c
            r["body"] = (payload or "")[:200]

    hist = Counter(r["code"] for r in rows)
    print(f"  ops={len(rows)}  histogram={hist.most_common()}")
    bad = [r for r in rows if r["code"] is not None and r["code"] >= 500]
    print(f"  5xx = {len(bad)}")
    for r in bad:
        print(f"    ✗ {r['code']} {r['method']:6s} {r['real']}\n        {(r.get('body') or '')[:160]}")
    return {"rows": rows, "histogram": dict(hist), "readonly": False}


def cleanup_created_resources(base_url: str, ledger: ResourceLedger,
                              token: str | None) -> dict:
    """只清理 ledger（+ 显式 allowlist）里的资源；其它 id 一律拒绝。"""
    hr("⑤ 清理本次创建的资源（白名单）")
    deleted, refused, failed = [], [], []
    for agent_id in ledger.ids("agent"):
        if not ledger.may_delete("agent", agent_id):
            refused.append(agent_id)
            print(f"  ⛔ 拒绝删除未记录/未授权的 agent {agent_id}")
            continue
        code, payload = http(base_url + f"/api/console/agents/{agent_id}", "DELETE",
                             token=token)
        if code in (200, 204):
            deleted.append(agent_id)
            print(f"  - 已删除本次测试 Agent id={agent_id}")
        elif code == 404:
            print(f"  ~ 本次测试 Agent id={agent_id} 已不存在（视为已清理）")
            deleted.append(agent_id)
        else:
            failed.append({"agent": agent_id, "code": code})
            print(f"  ✗ 删除 Agent id={agent_id} 失败 HTTP {code}: {(payload or '')[:120]}")
    for username in ledger.ids("account"):
        print(f"  ℹ️ 一次性账号 {username} 无删除端点：保留并如实报告（不伪装已清理）")
    return {"deleted_agents": deleted, "refused": refused, "failed": failed,
            "retained_accounts": ledger.ids("account")}


# ─────────────────── mutation 目标安全校验 ───────────────────

def _is_private_host(host: str) -> bool:
    host = (host or "").strip().lower().strip("[]")
    if not host:
        return False
    if host in ("localhost",):
        return True
    if host.endswith((".local", ".localhost", ".test", ".invalid", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def validate_mutation_target(base_url_explicit: bool, base_url: str,
                             integration: bool) -> str | None:
    """L2 目标校验；返回错误串（非 None ⇒ 拒绝执行且不发任何请求）。"""
    if not integration:
        return "--allow-mutations 必须与 --integration 同时使用"
    if not base_url_explicit:
        return ("mutation 模式必须显式传 --base-url（不接受默认值/环境变量默认），"
                "并指向一次性测试环境")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        return f"--base-url 必须是 http(s)：{base_url!r}"
    host = (parsed.hostname or "").lower()
    if not host:
        return f"--base-url 缺少主机名：{base_url!r}"
    for prod in PRODUCTION_HOSTS:
        if host == prod or host.endswith("." + prod):
            return f"拒绝在生产域名上执行 mutation：{host}"
    if _is_private_host(host):
        return None
    allowed_extra = {
        h.strip().lower() for h in os.environ.get("FECLAW_HC_MUTATION_HOSTS", "").split(",")
        if h.strip()
    }
    if host in allowed_extra:
        return None
    return (f"mutation 模式只允许回环/私网 base-url（当前 {host}）；"
            f"公网 staging 必须显式列入 FECLAW_HC_MUTATION_HOSTS 后再评估")


# ─────────────────────────── main ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="FeClaw 健康检查（默认只读）")
    ap.add_argument("--base-url", default=None,
                    help="服务地址；默认 http://127.0.0.1:8080（mutation 模式必须显式给）")
    ap.add_argument("--root-host", default=os.environ.get("FECLAW_HC_HOST", "feclaw.lizidaren.cn"))
    ap.add_argument("--sub-host", default=os.environ.get("FECLAW_HC_SUBHOST", ""),
                    help="Agent 子域名主机（留空则按 <hash>.<root-host> 推导）")
    ap.add_argument("--json", default=None, help="把结果写到该 JSON 文件")
    ap.add_argument("--only", default="1,2,3,4", help="只跑指定阶段，如 1,2")
    ap.add_argument("--skip-browser", action="store_true")
    ap.add_argument("--integration", action="store_true",
                    help="启用集成检查（仍只读；可用 FECLAW_HC_TOKEN 做已鉴权 GET）")
    ap.add_argument("--allow-mutations", action="store_true",
                    help="允许写/删除探活；必须同时 --integration，且显式指定非生产 --base-url")
    ap.add_argument("--mutation-resource-allowlist", default="",
                    help='允许清理的资源白名单，形如 "agent:12,agent:13"（默认只清本次 ledger）')
    ap.add_argument("--ready-check", action="store_true",
                    help="只检查 /ready（退出码 + HTTP status + JSON status==ready）")
    ap.add_argument("--ready-timeout", type=float, default=5.0)
    ap.add_argument("--agent-hash", default=os.environ.get("FECLAW_HC_AGENT_HASH", ""),
                    help="只读巡检时已知的 Agent hash（用于 agent 页面/子域检查；不代登录）")
    args = ap.parse_args()

    base_url = args.base_url
    base_url_explicit = base_url is not None
    if not base_url:
        base_url = os.environ.get("FECLAW_HC_URL", DEFAULT_BASE_URL)
    base_url = base_url.rstrip("/")

    # ── /ready 门禁模式（deploy.sh 同一契约）──
    if args.ready_check:
        ok, report = probe_ready(base_url, timeout=args.ready_timeout)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.json:
            _write_json(args.json, report)
        if not ok:
            print(f"✗ /ready 未通过：{report.get('reason')}", file=sys.stderr)
            return 1
        print("✓ /ready → status=ready")
        return 0

    ledger = ResourceLedger()
    mutation_mode = bool(args.allow_mutations)

    if mutation_mode:
        problem = validate_mutation_target(base_url_explicit, base_url, args.integration)
        if problem:
            print(f"✗ 拒绝执行 mutation 检查：{problem}", file=sys.stderr)
            return 2
        try:
            ledger.add_allowlist(args.mutation_resource_allowlist)
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2
    elif args.mutation_resource_allowlist:
        print("✗ --mutation-resource-allowlist 只在 --allow-mutations 下有意义",
              file=sys.stderr)
        return 2

    token = None
    if args.integration and not mutation_mode:
        token = os.environ.get(ENV_READONLY_TOKEN) or None
        if not token:
            print(f"  ℹ️ --integration 但未设置 {ENV_READONLY_TOKEN}：只做匿名只读探活")
        else:
            print(f"  ℹ️ 使用运维提供的只读令牌（{ENV_READONLY_TOKEN}），脚本不代为登录")

    only = {s.strip() for s in args.only.split(",")}
    root_host = args.root_host

    # 服务探活（只读 GET）
    c, _ = http(base_url + "/login", timeout=10)
    if c is None:
        print(f"✗ 服务未起：{base_url}（先启动 uvicorn --lifespan off）", file=sys.stderr)
        return 2

    tag = _random_tag()
    agent_hash = (args.agent_hash or "").strip()
    account: dict | None = None
    out: dict = {"meta": {"base_url": base_url, "mode": "read-only",
                          "allow_mutations": mutation_mode, "integration": args.integration,
                          "agent_hash": agent_hash or None}}

    if mutation_mode:
        out["meta"]["mode"] = "integration-mutations"
        try:
            account = _register_disposable_account(base_url, tag, ledger)
            created = _create_ledger_agent(base_url, account["token"], tag, ledger)
            agent_hash = created.get("hash") or agent_hash
            out["meta"]["agent_hash"] = agent_hash or None
            token = account["token"]
        except RuntimeError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2

    sub_host = args.sub_host or (f"{agent_hash}.{root_host}" if agent_hash else "")
    print(f"base_url={base_url}  mode={out['meta']['mode']}  root_host={root_host}  "
          f"sub_host={sub_host or '-'}  token={'ok' if token else 'none'}")

    findings = 0
    try:
        if "1" in only:
            out["js"] = phase_js(REPO)
        if "2" in only:
            out["assets"] = phase_assets(base_url, REPO)
        if "3" in only and not args.skip_browser:
            out["pages"] = phase_pages(base_url, root_host, token, agent_hash, sub_host,
                                       guard_mutations=not mutation_mode)
        if "4" in only:
            if mutation_mode and account:
                out["api"] = phase_api_mutations(base_url, account, ledger, tag)
            else:
                out["api"] = phase_api_readonly(base_url, token)
    except RuntimeError as e:
        # 一次性资源准备/清理失败等：如实记为失败，不让 traceback 变成「看起来没跑」
        print(f"✗ 检查中断：{e}", file=sys.stderr)
        out["interrupted"] = str(e)
        findings += 1
    finally:
        if mutation_mode:
            out["cleanup"] = cleanup_created_resources(base_url, ledger, token)
            out["meta"]["created_resources"] = ledger.summary()

    # ── 结论 ──
    hr("结论")
    if "js" in out:
        findings += len(out["js"].get("findings", []))
        print(f"  ① JS 语法问题：{len(out['js'].get('findings', []))}")
    if "assets" in out:
        n = len(out["assets"]["missing"])
        findings += n
        print(f"  ② 本站资源 404/500：{n}")
    if "pages" in out:
        pgs = out["pages"]["pages"]
        pe = sum(len([e for e in p["errors"] if e.startswith("pageerror")]) for p in pgs)
        unk = sum(len(p["unknown"]) for p in pgs)
        kn = sum(len(p["known"]) for p in pgs)
        print(f"  ③ pageerror={pe}  未知异常/请求={unk}  已知噪声={kn}")
        findings += pe + unk
    if "api" in out:
        if out["api"].get("error"):
            print(f"  ④ ✗ 无法枚举接口：{out['api']['error']}")
            findings += 1
        n5 = sum(1 for r in out["api"]["rows"] if r["code"] is not None and r["code"] >= 500)
        print(f"  ④ 接口 5xx：{n5}")
        findings += n5
        if out["api"].get("readonly"):
            print(f"  ④ 只读模式：跳过写方法 {out['api'].get('mutating_skipped', 0)} 个（未发出任何写请求）")
    if "cleanup" in out:
        cl = out["cleanup"]
        print(f"  ⑤ 清理：deleted={len(cl['deleted_agents'])} refused={len(cl['refused'])} "
              f"failed={len(cl['failed'])} retained_accounts={len(cl['retained_accounts'])}")
        findings += len(cl["failed"])
    print(f"\n  合计硬失败项：{findings}")

    if args.json:
        _write_json(args.json, out)
        print(f"  JSON → {args.json}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
