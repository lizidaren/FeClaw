import urllib.request, json, textwrap

# Devicon SVG URLs (MIT licensed) for common file types
ICONS = {
    'py': ('python', 'python-original'),
    'js': ('javascript', 'javascript-original'),
    'ts': ('typescript', 'typescript-original'),
    'jsx': ('react', 'react-original'),
    'tsx': ('react', 'react-original'),
    'html': ('html5', 'html5-original'),
    'css': ('css3', 'css3-original'),
    'go': ('go', 'go-original'),
    'rust': ('rust', 'rust-original'),
    'ruby': ('ruby', 'ruby-original'),
    'swift': ('swift', 'swift-original'),
    'kt': ('kotlin', 'kotlin-original'),
    'java': ('java', 'java-original'),
    'php': ('php', 'php-original'),
    'r': ('r', 'r-original'),
    'lua': ('lua', 'lua-original'),
    'dart': ('dart', 'dart-original'),
}

BASE = 'https://cdn.jsdelivr.net/gh/devicons/devicon/icons/{}/{}.svg'

svgs = {}
for ext, (name, file) in ICONS.items():
    url = BASE.format(name, file)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        data = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')
        svgs[ext] = data.replace("'", "\\'").replace('\n', ' ')
        print(f"✅ {ext} ({name}) - {len(data)} bytes")
    except Exception as e:
        print(f"❌ {ext} ({name}): {e}")

# Output as JS
print("\n=== JS Object ===")
for ext, svg in svgs.items():
    print(f"'{ext}': '{svg}',")
