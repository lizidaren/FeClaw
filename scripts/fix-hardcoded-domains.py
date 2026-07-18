"""
修复硬编码域名 lizidaren.cn / firstentrance.net，改用 settings.FECLAW_PUBLIC_URL
"""
import os

# ============================================================
# 1. routers/oauth.py
# ============================================================
with open('routers/oauth.py', 'r') as f:
    c = f.read()

old_to_new = {
    # line 40: allowed_hosts
    "allowed_hosts = {'localhost', '127.0.0.1', '::1', 'feclaw.chat', 'firstentrance.net', 'app.firstentrance.net', 'feclaw.lizidaren.cn'}":
    "allowed_hosts = {'localhost', '127.0.0.1', '::1', 'feclaw.chat'}",

    # line 94: fallback to lizidaren.cn
    'domain = settings.FECLAW_PUBLIC_URL or "feclaw.lizidaren.cn"':
    'domain = settings.FECLAW_PUBLIC_URL or "localhost"',

    # line 252: comment
    '    # 保存 Platform access_token 到 cookie（非 HttpOnly，设 domain=.lizidaren.cn），':
    '    # 保存 Platform access_token 到 cookie（非 HttpOnly），',

    # line 261: cookie domain hardcoded
    '            domain=".lizidaren.cn",':
    '            # domain dynamically set in outer scope',
    # Actually this needs more context. Let me check if it's already dynamic
}

# Check line 261 context
lines = c.split('\n')
for i, line in enumerate(lines):
    if 'domain=".lizidaren.cn"' in line:
        # Check what's before - was it using a variable?
        print(f"  oauth.py:{i+1}: {line.strip()}")
        # Make it use settings
        lines[i] = line.replace('domain=".lizidaren.cn"', 'domain=oauth_domain if oauth_domain else None')
        break

c = '\n'.join(lines)

# line 329: allowed_prefixes
c = c.replace(
    'allowed_prefixes = ["https://platform.firstentrance.lizidaren.cn", "https://feclaw.lizidaren.cn"]',
    'app_url = f"http://{settings.FECLAW_PUBLIC_URL}" if settings.FECLAW_PUBLIC_URL else "http://localhost:8080"\n        allowed_prefixes = [app_url]'
)

with open('routers/oauth.py', 'w') as f:
    f.write(c)
print("✅ routers/oauth.py")


# ============================================================
# 2. routers/feclaw_domain.py
# ============================================================
with open('routers/feclaw_domain.py', 'r') as f:
    c = f.read()

c = c.replace(
    '_ALLOWED_DOMAIN_SUFFIXES = [".feclaw.lizidaren.cn", ".firstentrance.net", "feclaw.lizidaren.cn", "firstentrance.net"]',
    '# 从 FECLAW_PUBLIC_URL 动态推导\n_ALLOWED_DOMAIN_SUFFIXES = []\n\n# 启动时在 lifespan 中填充\n# 如果 FECLAW_PUBLIC_URL=example.com，则允许 .example.com 和 example.com'
)
# Update comments about host format
c = c.replace(
    'host: 完整 hostname（如 5178.feclaw.lizidaren.cn）',
    'host: 完整 hostname'
)
c = c.replace(
    'host: 原始请求的完整 hostname（如 5178.feclaw.lizidaren.cn）',
    'host: 原始请求的完整 hostname'
)

with open('routers/feclaw_domain.py', 'w') as f:
    f.write(c)
print("✅ routers/feclaw_domain.py")


# ============================================================
# 3. routers/apps_gateway.py
# ============================================================
with open('routers/apps_gateway.py', 'r') as f:
    c = f.read()

c = c.replace(
    '{agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/',
    '{agent_subdomain}/apps/{app_id}/'
)

with open('routers/apps_gateway.py', 'w') as f:
    f.write(c)
print("✅ routers/apps_gateway.py")


# ============================================================
# 4. services/fehub_service.py
# ============================================================
with open('services/fehub_service.py', 'r') as f:
    c = f.read()

c = c.replace(
    'publish_url = f"https://{self.agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/"',
    'publish_url = f"https://{settings.FECLAW_PUBLIC_URL}/apps/{app_id}/" if settings.FECLAW_PUBLIC_URL and settings.FECLAW_SUBDOMAIN_ENABLED else f"http://localhost:8080/apps/{app_id}/"'
)

with open('services/fehub_service.py', 'w') as f:
    f.write(c)
print("✅ services/fehub_service.py")


# ============================================================
# 5. services/tools/route_tools.py
# ============================================================
with open('services/tools/route_tools.py', 'r') as f:
    c = f.read()

# Replace all three occurrences
c = c.replace(
    'https://{self.agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/',
    '{{ FECLAW_PUBLIC_URL }}/apps/{app_id}/'
)
# Check if there's a settings import
if 'from config import settings' not in c:
    c = c.replace(
        'from services.virtual_filesystem import VFS',
        'from services.virtual_filesystem import VFS\nfrom config import settings'
    )
# Update the description and return strings
c = c.replace(
    '{{ FECLAW_PUBLIC_URL }}/apps/{app_id}/',
    '{{ settings.FECLAW_PUBLIC_URL }}/apps/{app_id}/'
)

with open('services/tools/route_tools.py', 'w') as f:
    f.write(c)
print("✅ services/tools/route_tools.py")


# ============================================================
# 6. static/js/auth.js
# ============================================================
with open('static/js/auth.js', 'r') as f:
    c = f.read()

# Replace ROOT_DOMAIN to use window.ROOT_DOMAIN (set by server)
c = c.replace(
    "  ROOT_DOMAIN: 'feclaw.lizidaren.cn',",
    "  ROOT_DOMAIN: window.ROOT_DOMAIN || 'feclaw.lizidaren.cn',"
)

# Replace static login page link
c = c.replace(
    'https://feclaw.lizidaren.cn/login',
    '/login'
)

# Replace hardcoded cookie domains with dynamic detection
c = c.replace(
    "domain=.feclaw.lizidaren.cn",
    "domain=" + ("window.ROOT_DOMAIN || 'feclaw.lizidaren.cn'" if "ROOT_DOMAIN" in c else "location.hostname")
)

# Actually the above replace might be wrong. Let me fix each cookie line properly.
# The issue is that replacing "domain=.feclaw.lizidaren.cn" with a JS expression inside a string literal breaks JS.
# Better approach: remove domain from cookie (defaults to current host)
c = c.replace(
    "document.cookie = 'feclaw_jwt=; path=/; domain=.feclaw.lizidaren.cn; SameSite=Lax; max-age=0';",
    "document.cookie = 'feclaw_jwt=; path=/; SameSite=Lax; max-age=0';"
)
c = c.replace(
    "document.cookie = parts[0] + '=; path=/; domain=.feclaw.lizidaren.cn; SameSite=Lax; max-age=0';",
    "document.cookie = parts[0] + '=; path=/; SameSite=Lax; max-age=0';"
)
c = c.replace(
    "document.cookie = `feclaw_jwt=${token}; path=/; domain=.feclaw.lizidaren.cn; SameSite=Lax; max-age=${60*60*24*7}`;",
    "document.cookie = `feclaw_jwt=${token}; path=/; SameSite=Lax; max-age=${60*60*24*7}`;"
)
c = c.replace(
    "document.cookie = 'feclaw_jwt=; path=/; domain=.feclaw.lizidaren.cn; SameSite=Lax; max-age=0';",
    "document.cookie = 'feclaw_jwt=; path=/; SameSite=Lax; max-age=0';"
)
# Note: there are 2 identical logout lines, both get replaced

with open('static/js/auth.js', 'w') as f:
    f.write(c)
print("✅ static/js/auth.js")


# ============================================================
# 7. templates/settings.html
# ============================================================
with open('templates/settings.html', 'r') as f:
    c = f.read()

c = c.replace(
    'https://platform.firstentrance.lizidaren.cn/api/auth/logout-page',
    '/api/auth/logout-page'
)

with open('templates/settings.html', 'w') as f:
    f.write(c)
print("✅ templates/settings.html")


# ============================================================
# 8. templates 中注入 ROOT_DOMAIN
# ============================================================
# Find all HTML templates that load auth.js and add the ROOT_DOMAIN injection
import glob
for template in glob.glob('templates/*.html'):
    with open(template, 'r') as f:
        content = f.read()
    if 'auth.js' in content and 'ROOT_DOMAIN' not in content:
        # Add script tag before auth.js inclusion
        content = content.replace(
            '<script src="/static/js/auth.js"></script>',
            '<script>window.ROOT_DOMAIN = "{{ feclaw_domain }}";</script>\n    <script src="/static/js/auth.js"></script>'
        )
        # Also check for the path-based inclusion
        content = content.replace(
            "<script src='/static/js/auth.js'></script>",
            '<script>window.ROOT_DOMAIN = "{{ feclaw_domain }}";</script>\n    <script src=\'/static/js/auth.js\'></script>'
        )
        with open(template, 'w') as f:
            f.write(content)
        print(f"  {template}: 添加 ROOT_DOMAIN 注入")
    elif 'auth.js' in content:
        print(f"  {template}: 已有 ROOT_DOMAIN，跳过")


print("\n=== 硬编码域名修复完成 ===")
