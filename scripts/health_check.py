#!/usr/bin/env python3
"""FeClaw 前端资源 / 语法 / 页面 / 接口 健康检查（一条命令跑完）。

背景：2026-10-02 一晚连撞三次「静默地成功地做错事」——
  ① auth.js 少一个逗号 ⇒ 整文件 SyntaxError ⇒ Auth 未定义 ⇒ 页面只报「服务不可用」
  ② settings.html 登出链指向不存在的端点
  ③ 缺依赖 ⇒ /api/file/sts-credential 500 ⇒ 文件页静默回落「本地存储」
  （Q9 又补一例：dashboard JS 静默坏）
本脚本把这些「静态坏」变成可检测、可重复、可回归的体检。

四个阶段
  ① JS 语法体检      node --check 全部 static/js/*.js（script + module 两种模式）
                     并抽取 templates/*.html 的内联 <script> 一并检查（Jinja 变量已中性化）
  ② 静态资源真实性   扫描模板里的 src=/href=/url()，本站资源真请求，必须 200；外链单列
  ③ 页面加载体检     真 Chromium 逐页打开，收集 console.error / pageerror / 失败请求
  ④ 接口健康矩阵     按 /openapi.json 穷举所有路由，真实请求，输出「接口/方法/期望码/实测码」

用法
  python scripts/health_check.py                          # 跑在 http://127.0.0.1:8080
  python scripts/health_check.py --base-url http://127.0.0.1:8091
  python scripts/health_check.py --json /tmp/hc.json --skip-browser
  python scripts/health_check.py --only 1,2               # 只跑指定阶段

依赖：node（①）；playwright + 本机已装的 chromium（③，不会联网下载浏览器）。
退出码：0 = 无发现；1 = 有发现；2 = 环境准备失败（如服务没起、缺 node）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ─────────────────────────── 配置 ───────────────────────────

# 浏览器访客身份：用测试账号的 JWT 注入 cookie（不含真实用户）
TEST_USER = os.environ.get("FECLAW_HC_USER", "q4_health")
TEST_PASS = os.environ.get("FECLAW_HC_PASS", "Q4Health#2026")

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

# ④ 接口探活：变更类方法一律用「不存在的」资源 id，避免破坏数据
BOGUS_HASH = "ffffffff"
BOGUS_ID = "99999999"

# 自毁类：会吊销本轮 token，放到最后并用独立 token 跑
SELF_DESTRUCTIVE = (
    "/api/auth/logout", "/api/oauth/logout",
    "/api/user/change-password", "/api/user/password",
    "/setup/", "/api/auth/revoke-all",
)

# 这些 POST/PUT 用 {} 请求体会「真的创建资源」（而不是校验失败）⇒ 跳过，
# 以免每跑一次体检就往库里塞数据。
MUTATING_SKIP = {
    "/api/user/agents": "空 body 会真创建一个 Agent",
    "/api/chat/sessions": "空 body 可能建会话",
    "/api/groups": "空 body 可能建群",
    "/api/organizations": "空 body 可能建组织",
    "/api/console/agents": "空 body 可能建 Agent",
    "/api/zentrim/entries": "空 body 可能建条目",
    "/api/share/reference": "空 body 可能建分享",
    "/api/static-sites": "空 body 可能建站",
    "/api/user/agents/{hash}/vfs/mkdir": "会在 VFS 建目录",
}


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
    hr("② 模板静态资源真实性")
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
                sub_host: str | None) -> dict:
    hr("③ 页面加载即报错体检（Playwright / 真 Chromium）")
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

    results = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=exe, headless=True,
                              args=[f"--host-resolver-rules={','.join(hmap)}", "--no-sandbox"])
        for label, role, path_tpl in PAGES:
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
                else:
                    unknown.append(s)
            for f in fails:
                (known if _benign(f) else unknown).append(f)
            for st, u in bad:
                if _benign(u) or (label in ADMIN_ONLY_PAGES and st == 403):
                    known.append((f"{st} {u}", "见上（favicon / 非管理员 403）"))
                else:
                    unknown.append(f"{st} {u}")

            results.append({"label": label, "url": url, "status": status,
                            "final": None, "errors": errs, "reqfail": fails,
                            "http4xx5xx": bad, "unknown": unknown,
                            "known": [{"item": i, "why": w} for i, w in known]})
            n_err = len([e for e in errs if e.startswith("pageerror")])
            print(f"  {label:20s} status={status} pageerror={n_err} "
                  f"console.err={len(errs) - n_err} reqfail={len(fails)} http>=400={len(bad)}"
                  f"  未知={len(unknown)}")
            for e in unknown:
                print(f"      ✗ {e[:220]}")
            for i, w in known:
                print(f"      · 已知：{i[:120]}  —— {w}")
        b.close()

    return {"pages": results}


# ─────────────────── ④ 接口健康矩阵 ───────────────────

def _expected(method: str, path: str) -> str:
    if method in ("PUT", "PATCH", "DELETE"):
        return "4xx（不存在资源）"
    if "{" in path:
        return "4xx"
    return "200/4xx"


def phase_api(base_url: str, token: str, agent_hash: str, agent_id: str, user_id: str) -> dict:
    hr("④ 接口健康矩阵（按 /openapi.json 穷举）")
    code, raw = http(base_url + "/openapi.json", timeout=60, maxbytes=None)
    if code != 200:
        print(f"  ✗ 取不到 /openapi.json（{code}）")
        return {"rows": [], "error": f"openapi {code}: {raw[:160]}"}
    spec = json.loads(raw)
    read_tbl = _param_table(agent_hash, agent_id, user_id)

    rows = []
    for path in sorted(spec["paths"]):
        for method in spec["paths"][path]:
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            mut = method in ("put", "patch", "delete")
            sd = path.startswith(SELF_DESTRUCTIVE)
            skip = path in MUTATING_SKIP and method in ("post", "put", "patch")
            real = re.sub(r"\{([^}]+)\}", lambda m: read_tbl.get(m.group(1), "1"), path)
            body = {} if method in ("post", "put", "patch") else None
            if sd:
                rows.append({"method": method.upper(), "path": path, "real": real,
                             "code": None, "expected": "自毁接口", "note": "延后单独跑",
                             "self_destructive": True})
                continue
            if skip:
                rows.append({"method": method.upper(), "path": path, "real": real,
                             "code": None, "expected": "跳过", "note": MUTATING_SKIP[path],
                             "self_destructive": False})
                continue
            c, payload = http(base_url + real, method.upper(), token=token, body=body)
            rows.append({"method": method.upper(), "path": path, "real": real,
                         "code": c, "expected": _expected(method.upper(), path),
                         "note": "", "body": payload[:200], "self_destructive": False})

    # 自毁接口最后跑（用一次性 token，跑完即失效）
    tmp_token = _login_temp(base_url)
    for r in rows:
        if r["self_destructive"]:
            c, payload = http(base_url + r["real"], r["method"],
                              token=tmp_token, body={} if r["method"] != "GET" else None)
            r["code"] = c
            r["body"] = payload[:200]

    hist = Counter(r["code"] for r in rows)
    print(f"  ops={len(rows)}  histogram={hist.most_common()}")
    bad = [r for r in rows if r["code"] is not None and r["code"] >= 500]
    skipped = [r for r in rows if r["code"] is None and r.get("note", "").startswith(("跳过", "延后"))]
    print(f"  5xx = {len(bad)}   跳过/自毁 = {len(skipped)}")
    for r in bad:
        print(f"    ✗ {r['code']} {r['method']:6s} {r['real']}\n        {(r.get('body') or '')[:160]}")
    return {"rows": rows, "histogram": dict(hist)}


def _login_temp(base_url: str) -> str | None:
    c, _ = http(base_url + "/api/user/login", "POST",
                body={"username": TEST_USER, "password": TEST_PASS})
    if c != 200:
        return None
    try:
        return json.loads(http(base_url + "/api/user/login", "POST",
                               body={"username": TEST_USER, "password": TEST_PASS})[1]).get("token")
    except Exception:  # noqa: BLE001
        return None


# ─────────────────── 准备：测试账号 ───────────────────

def ensure_account(base_url: str) -> tuple[str | None, str | None, str | None]:
    """确保测试账号存在并返回 (token, agent_hash, agent_id)。"""
    c, raw = http(base_url + "/api/user/login", "POST",
                  body={"username": TEST_USER, "password": TEST_PASS})
    if c != 200:
        c, raw = http(base_url + "/api/user/register", "POST",
                      body={"username": TEST_USER, "password": TEST_PASS,
                            "email": f"{TEST_USER}@example.invalid"})
    if c != 200:
        print(f"  ⚠️ 无法登录/注册测试账号 {TEST_USER}（{c}）: {raw[:200]}")
        return None, None, None
    token = json.loads(raw).get("token")
    # 复用已有 agent；没有才建（GET 用 /api/console/agents —— /api/user/agents 只有 POST）
    agent_hash = agent_id = None
    c, raw = http(base_url + "/api/console/agents", token=token)
    if c == 200:
        try:
            items = json.loads(raw).get("agents", [])
            if items:
                agent_hash = items[0].get("hash")
                agent_id = str(items[0].get("id", "1"))
        except Exception:  # noqa: BLE001
            pass
    if not agent_hash:
        c, raw = http(base_url + "/api/user/agents", "POST", token=token,
                      body={"name": "健康检查助手", "agent_type": "classic"})
        if c == 200:
            agent_hash = json.loads(raw).get("hash")
    return token, agent_hash, agent_id


# ─────────────────────────── main ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="FeClaw 健康检查")
    ap.add_argument("--base-url", default=os.environ.get("FECLAW_HC_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--root-host", default=os.environ.get("FECLAW_HC_HOST", "feclaw.lizidaren.cn"))
    ap.add_argument("--sub-host", default=os.environ.get("FECLAW_HC_SUBHOST", ""),
                    help="Agent 子域名主机（留空则按 <hash>.<root-host> 推导）")
    ap.add_argument("--json", default=None, help="把结果写到该 JSON 文件")
    ap.add_argument("--only", default="1,2,3,4", help="只跑指定阶段，如 1,2")
    ap.add_argument("--skip-browser", action="store_true")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",")}
    root_host = args.root_host

    # 服务探活
    c, _ = http(args.base_url + "/login", timeout=10)
    if c is None:
        print(f"✗ 服务未起：{args.base_url}（先启动 uvicorn --lifespan off）", file=sys.stderr)
        return 2

    token, agent_hash, agent_id = ensure_account(args.base_url)
    if not (token and agent_hash):
        print("⚠️ 测试账号/Agent 准备失败，③④ 可能大量 4xx")
    sub_host = args.sub_host or (f"{agent_hash}.{root_host}" if agent_hash else "")
    print(f"base_url={args.base_url}  root_host={root_host}  sub_host={sub_host}  "
          f"agent={agent_hash}  token={'ok' if token else 'MISSING'}")

    out = {"meta": {"base_url": args.base_url, "agent_hash": agent_hash}}

    if "1" in only:
        out["js"] = phase_js(REPO)
    if "2" in only:
        out["assets"] = phase_assets(args.base_url, REPO)
    if "3" in only and not args.skip_browser:
        out["pages"] = phase_pages(args.base_url, root_host, token, agent_hash, sub_host)
    if "4" in only:
        out["api"] = phase_api(args.base_url, token, agent_hash or "ffffffff",
                               agent_id or "1", "1")

    # ── 结论 ──
    hr("结论")
    findings = 0
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
    print(f"\n  合计硬失败项：{findings}")

    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"  JSON → {args.json}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
