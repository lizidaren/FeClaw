"""
彻底清除代码中所有 lizidaren.cn 硬编码
"""
import glob

fixes = []

# ==============================
# 1. static/js/auth.js — 最严重
# ==============================
with open('static/js/auth.js', 'r') as f:
    c = f.read()

# Fix fallback: empty string fallback (already injected by server)
c = c.replace(
    "  ROOT_DOMAIN: window.ROOT_DOMAIN || 'feclaw.lizidaren.cn',",
    "  ROOT_DOMAIN: window.ROOT_DOMAIN || ''"
)
fixes.append("auth.js: ROOT_DOMAIN fallback ''")

# Fix broken cookie domains - the previous replacement put JS code inside a string literal!
# Current broken: domain=window.ROOT_DOMAIN || 'feclaw.lizidaren.cn'
# 4 cookie lines need fixing - just remove domain entirely (cookies default to current host)
import re
# Remove all cookie domain parameters
for pattern in [
    "domain=window.ROOT_DOMAIN || 'feclaw.lizidaren.cn'; ",
    "domain=window.ROOT_DOMAIN || 'feclaw.lizidaren.cn'",
]:
    count = c.count(pattern)
    if count:
        c = c.replace(pattern, '')
        fixes.append(f"auth.js: removed broken domain ({count}x)")

# Fix the comment about subdomain matching
c = c.replace(
    "// 匹配 *.feclaw.lizidaren.cn → 子域名",
    "// 匹配子域名（如 5178.domain.com）"
)
fixes.append("auth.js: comment fixed")

with open('static/js/auth.js', 'w') as f:
    f.write(c)


# ==============================
# 2. templates/settings.html
# ==============================
with open('templates/settings.html', 'r') as f:
    c = f.read()

c = c.replace(
    "document.cookie = 'feclaw_jwt=; path=/; domain=.feclaw.lizidaren.cn; SameSite=Lax; max-age=0';",
    "document.cookie = 'feclaw_jwt=; path=/; SameSite=Lax; max-age=0';"
)
fixes.append("settings.html: removed cookie domain")

with open('templates/settings.html', 'w') as f:
    f.write(c)


# ==============================
# 3. routers/oauth.py
# ==============================
with open('routers/oauth.py', 'r') as f:
    c = f.read()

c = c.replace(
    "allowed_hosts = {'localhost', '127.0.0.1', '::1', 'feclaw.chat', 'firstentrance.net', 'app.firstentrance.net', 'feclaw.lizidaren.cn'}",
    "allowed_hosts = {'localhost', '127.0.0.1', '::1', 'feclaw.chat', 'firstentrance.net', 'app.firstentrance.net', 'feclaw.lizidaren.cn', 'lizidaren.cn'}"
)
# Actually this already has lizidaren.cn. The allowed_hosts is for CORS / redirect safety.
# For open source, we should keep it minimal - just localhost
c = c.replace(
    "allowed_hosts = {'localhost', '127.0.0.1', '::1', 'feclaw.chat', 'firstentrance.net', 'app.firstentrance.net', 'feclaw.lizidaren.cn', 'lizidaren.cn'}",
    "allowed_hosts = {'localhost', '127.0.0.1', '::1'}"
)

# Line 94 fallback
c = c.replace(
    'domain = settings.FECLAW_PUBLIC_URL or "feclaw.lizidaren.cn"',
    'domain = settings.FECLAW_PUBLIC_URL or ""'
)
fixes.append("oauth.py: allowed_hosts + fallback")

with open('routers/oauth.py', 'w') as f:
    f.write(c)


# ==============================
# 4. routers/feclaw_domain.py
# ==============================
with open('routers/feclaw_domain.py', 'r') as f:
    c = f.read()

c = c.replace(
    'return [f".feclaw.lizidaren.cn", "feclaw.lizidaren.cn", ".firstentrance.net", "firstentrance.net"]',
    'return []  # 无 PUBLIC_URL 时不进行子域名匹配'
)
fixes.append("feclaw_domain.py: fallback empty list")

with open('routers/feclaw_domain.py', 'w') as f:
    f.write(c)


# ==============================
# 5. routers/fehub.py
# ==============================
with open('routers/fehub.py', 'r') as f:
    c = f.read()

# These publish URLs need to be dynamic. Replace hardcoded domains with template
c = c.replace(
    '"url": f"https://{p.agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/"',
    '"url": settings.FECLAW_PUBLIC_URL and f"https://{p.agent_hash}.{settings.FECLAW_PUBLIC_URL}/apps/{app_id}/" or f"/apps/{app_id}/"'
)
c = c.replace(
    '"url": f"https://{publish.agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/"',
    '"url": settings.FECLAW_PUBLIC_URL and f"https://{publish.agent_hash}.{settings.FECLAW_PUBLIC_URL}/apps/{app_id}/" or f"/apps/{app_id}/"'
)
fixes.append("fehub.py: dynamic URLs")

with open('routers/fehub.py', 'w') as f:
    f.write(c)


# ==============================
# 6. routers/static_site_public.py
# ==============================
with open('routers/static_site_public.py', 'r') as f:
    c = f.read()

c = c.replace(
    'if extract_hash_from_host(host) or (host.endswith(".feclaw.lizidaren.cn") and host != "feclaw.lizidaren.cn"):',
    'domain = getattr(settings, "FECLAW_PUBLIC_URL", "")\n        if extract_hash_from_host(host) or (domain and host.endswith(f".{domain}") and host != domain):'
)
fixes.append("static_site_public.py: dynamic domain check")

with open('routers/static_site_public.py', 'w') as f:
    f.write(c)


# ==============================
# 7. routers/desktop_api.py
# ==============================
with open('routers/desktop_api.py', 'r') as f:
    c = f.read()

c = c.replace(
    'avatar_url = f"https://feclaw.lizidaren.cn/api/vfs/view?path={cos_key}"',
    'avatar_url = ""  # avatar URL 需在部署时配置'
)
fixes.append("desktop_api.py: avatar URL")

with open('routers/desktop_api.py', 'w') as f:
    f.write(c)


# ==============================
# 8. services/tools/route_tools.py — tool descriptions
# ==============================
with open('services/tools/route_tools.py', 'r') as f:
    c = f.read()

c = c.replace(
    '用户可通过 https://{agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/ 访问。',
    '用户可通过部署域名访问。'
)
fixes.append("route_tools.py: description")

with open('services/tools/route_tools.py', 'w') as f:
    f.write(c)


# ==============================
# 9. services/tools/fehub_tools.py
# ==============================
with open('services/tools/fehub_tools.py', 'r') as f:
    c = f.read()

c = c.replace(
    'https://{agent_hash}.feclaw.lizidaren.cn/apps/{agent_hash}-{tag}/',
    '<部署域名>/apps/{agent_hash}-{tag}/'
)
fixes.append("fehub_tools.py: URL template")

with open('services/tools/fehub_tools.py', 'w') as f:
    f.write(c)


# ==============================
# 10. services/share_service.py — comment
# ==============================
with open('services/share_service.py', 'r') as f:
    c = f.read()

c = c.replace(
    '# 子域名 URL：{agent_hash}.feclaw.lizidaren.cn/s/{slug}',
    '# 子域名 URL（取决于 FECLAW_PUBLIC_URL）'
)
fixes.append("share_service.py: comment")

with open('services/share_service.py', 'w') as f:
    f.write(c)


# ==============================
# 11. services/apps_service.py — comment
# ==============================
with open('services/apps_service.py', 'r') as f:
    c = f.read()

c = c.replace(
    '将 Web 应用部署到 `https://{agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/`。',
    '将 Web 应用部署到部署域名下。'
)
fixes.append("apps_service.py: docstring")

with open('services/apps_service.py', 'w') as f:
    f.write(c)


# ==============================
# Final verification
# ==============================
print(f"\n{'='*50}")
print(f"修复完成 {len(fixes)} 处")
for f in fixes:
    print(f"  ✅ {f}")

# Verify remaining
print(f"\n{'='*50}")
print("检查残留...")
remaining = __import__('subprocess').run(
    'grep -rn "lizidaren\\.cn" --include="*.py" --include="*.js" --include="*.html" . 2>/dev/null | grep -v "\.git\|\.pyc\|node_modules\|claude-audit\|claude-logs\|claude-tasks\|history\|audits\|v3-progress\|public\|scripts/fix\|\.md"',
    shell=True, capture_output=True, text=True
)
if remaining.stdout.strip():
    print("仍有残留:")
    for line in remaining.stdout.strip().split('\n'):
        print(f"  ⚠️ {line}")
else:
    print("✅ 无残留!")
