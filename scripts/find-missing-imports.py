"""扫描项目中所有 import，找出不在 requirements.txt 中的"""
import ast, glob, sys

# 已安装的包
installed = set()
with open('requirements.txt') as f:
    for line in f:
        line = line.split('#')[0].strip()
        if line and not line.startswith('#'):
            pkg = line.split('>=')[0].split('~=')[0].split('==')[0].split('<')[0].strip().lower()
            installed.add(pkg)

# 标准库模块（简化列表）
stdlib = {
    'abc', 'ast', 'asyncio', 'base64', 'binascii', 'calendar', 'collections',
    'concurrent', 'contextlib', 'copy', 'csv', 'dataclasses', 'datetime', 'decimal',
    'difflib', 'dis', 'enum', 'errno', 'functools', 'glob', 'gzip', 'hashlib',
    'html', 'http', 'importlib', 'inspect', 'io', 'itertools', 'json', 'logging',
    'math', 'mimetypes', 'multiprocessing', 'operator', 'os', 'pathlib', 'pickle',
    'pkgutil', 'platform', 'pprint', 'queue', 'random', 're', 'secrets', 'selectors',
    'shlex', 'shutil', 'signal', 'socket', 'sqlite3', 'ssl', 'stat', 'string',
    'struct', 'subprocess', 'sys', 'tempfile', 'textwrap', 'threading', 'time',
    'traceback', 'typing', 'unicodedata', 'urllib', 'uuid', 'warnings', 'weakref',
    'xml', 'zipfile', 'zoneinfo',
}

# pip -> import name mapping
pip_to_import = {
    'pyjwt': 'jwt',
    'Pillow': 'PIL',
    'cos-python-sdk-v5': 'qcloud_cos',
    'python-multipart': 'multipart',
    'pyyaml': 'yaml',
    'pydantic-settings': 'pydantic_settings',
    'python-jose': 'jose',
    'pyotp': 'pyotp',
    'bcrypt': 'bcrypt',
    'pymysql': 'pymysql',
    'python-dotenv': 'dotenv',
    'python-docx': 'docx',
    'psutil': 'psutil',
    'redis': 'redis',
    'aiofiles': 'aiofiles',
    'certifi': 'certifi',
    'openpyxl': 'openpyxl',
    'apscheduler': 'apscheduler',
    'nest_asyncio': 'nest_asyncio',
    'jinja2': 'jinja2',
}

# Reverse mapping
import_to_pip = {v: k for k, v in pip_to_import.items()}

# Special known imports and their actual pip package names
known_imports = {
    'PIL': 'Pillow',
    'qcloud_cos': 'cos-python-sdk-v5',
    'multipart': 'python-multipart',
    'yaml': 'pyyaml',
    'pydantic_settings': 'pydantic-settings',
    'jose': 'python-jose',
    'dotenv': 'python-dotenv',
    'docx': 'python-docx',
    'jwt': 'PyJWT',
}

# Collect all imports
all_imports = set()
for f in glob.glob('**/*.py', recursive=True):
    if '/venv/' in f or '/.git/' in f or '/node_modules/' in f:
        continue
    try:
        with open(f) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split('.')[0]
                    all_imports.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split('.')[0]
                    all_imports.add(top)
    except:
        pass

# Check which imports are not in requirements
missing = []
for imp in sorted(all_imports):
    if imp == '__future__' or imp.startswith('_') or imp in stdlib:
        continue
    # Check if import name matches a pip package directly
    pip_name = import_to_pip.get(imp, imp.lower())
    if pip_name in installed:
        continue
    # Check known_imports
    if imp in known_imports:
        pkg = known_imports[imp].lower()
        if pkg in installed:
            continue
    # Special cases
    if imp in ['qcloud_cos', 'PyPDF2', 'Crypto', 'Cryptodome']:
        continue  # We know these are handled
    missing.append(imp)

print("=== 已安装的 pip 包 ===")
for p in sorted(installed):
    print(f"  {p}")
print(f"\n=== 项目中 import 但不在 requirements.txt 中的模块 ===")
for m in missing:
    print(f"  {m}")
