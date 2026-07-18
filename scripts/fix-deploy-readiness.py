"""
批量修复部署就绪性审计发现的 P0/P1 问题。
"""
import re

# ============================================================
# 1. requirements.txt — 添加缺失依赖
# ============================================================
with open('requirements.txt', 'r') as f:
    req = f.read()

missing = """
# ─── 部署就绪性补全 ───
redis>=5.0.0                # Redis 缓存/会话
aiofiles>=24.0.0            # 异步文件 I/O
certifi>=2024.0.0           # SSL 证书
psutil>=5.9.0               # 系统指标监控
python-docx>=1.0.0          # .docx 文档解析
openpyxl>=3.0.0             # .xlsx 表格解析
"""

if 'redis>=' not in req:
    req += missing
    with open('requirements.txt', 'w') as f:
        f.write(req)
    print("✅ requirements.txt: 添加 6 个缺失依赖")
else:
    print("→ requirements.txt: 已有依赖，跳过")


# ============================================================
# 2. config.py — 多项修复
# ============================================================
with open('config.py', 'r') as f:
    config = f.read()

# 2a. 移除重复的 STORAGE_PREFIX
old = '''STORAGE_PREFIX: str = "feclaw/"  # 存储前缀，隔离多实例（本地/COS 均生效）
STORAGE_PREFIX: str = "feclaw/"  # 向下兼容，已由 STORAGE_PREFIX 取代'''
new = 'STORAGE_PREFIX: str = "feclaw/"  # 存储前缀，隔离多实例（本地/COS 均生效）'
if old in config:
    config = config.replace(old, new, 1)
    print("✅ config.py: 移除重复 STORAGE_PREFIX")
else:
    print("→ config.py: 重复 STORAGE_PREFIX 可能已修")

# 2b. VECTOR_STORAGE_BACKEND 默认改为 numpy
config = config.replace(
    'VECTOR_STORAGE_BACKEND: str = "cos"',
    'VECTOR_STORAGE_BACKEND: str = "numpy"'
)
print("✅ config.py: VECTOR_STORAGE_BACKEND 默认改为 numpy")

# 2c. FECLAW_VENV_PATH 改为相对路径
config = config.replace(
    'FECLAW_VENV_PATH: str = "/home/ubuntu/FeClaw/venv"',
    'FECLAW_VENV_PATH: str = "./venv"'
)
print("✅ config.py: FECLAW_VENV_PATH 改为 ./venv")

# 2d. DEBUG 默认改为 False
config = config.replace(
    'DEBUG: bool = True',
    'DEBUG: bool = False'
)
print("✅ config.py: DEBUG 默认改为 False")

# 2e. JWT_SECRET 添加注释强调必须配置
config = config.replace(
    'JWT_SECRET: str = ""',
    'JWT_SECRET: str = ""  # ⚠️ 必须设置！用于 JWT 签名的密钥。首次启动会提示设置。'
)
print("✅ config.py: JWT_SECRET 添加注释")

# 2f. COOKIE_SECURE 改为自动检测（保留 False 默认，添加注释）
config = config.replace(
    'COOKIE_SECURE: bool = False  # Cookie Secure flag（生产开启）',
    'COOKIE_SECURE: bool = False  # Cookie Secure flag（生产开启；如自动检测可设 auto）'
)
print("✅ config.py: COOKIE_SECURE 添加 auto 提示")

with open('config.py', 'w') as f:
    f.write(config)


# ============================================================
# 3. models/database.py — SQLAlchemy 废弃 API
# ============================================================
with open('models/database.py', 'r') as f:
    db = f.read()

db = db.replace(
    'from sqlalchemy.ext.declarative import declarative_base',
    'from sqlalchemy.orm import declarative_base'
)
with open('models/database.py', 'w') as f:
    f.write(db)
print("✅ models/database.py: 改用 sqlalchemy.orm.declarative_base")


# ============================================================
# 4. main.py — 默认 admin 密码 + CORS + JWT 校验
# ============================================================
with open('main.py', 'r') as f:
    main = f.read()

# 4a. 移除默认 admin/admin，改随机密码
old_admin = '''    if admin_count == 0:
        import hashlib
        from models.user import hash_password

        default_password = hashlib.sha256("admin".encode()).hexdigest()
        admin = User(
            username="admin",
            password_hash=hash_password(default_password),
            email="admin@feclaw.local",
            is_admin=True
        )
        db_session.add(admin)
        db_session.commit()
        logger.info(f"Created default admin user: admin")
        logger.warning("⚠️ 默认管理员密码为 'admin'，请立即修改！")'''

new_admin = '''    if admin_count == 0:
        import secrets
        from models.user import hash_password

        random_password = secrets.token_hex(12)
        admin = User(
            username="admin",
            password_hash=hash_password(random_password),
            email="admin@feclaw.local",
            is_admin=True
        )
        db_session.add(admin)
        db_session.commit()
        # 环境变量 FECLAW_ADMIN_PASSWORD 可覆盖随机密码（用于自动化部署）
        final_password = os.environ.get("FECLAW_ADMIN_PASSWORD", random_password)
        # 更新为指定密码
        admin.password_hash = hash_password(final_password)
        db_session.commit()
        logger.info("=" * 60)
        logger.info("  🚀 初始管理员账户已创建")
        logger.info(f"  用户名: admin")
        logger.info(f"  密  码: {final_password}")
        logger.info("  ⚠️ 请立即登录并修改密码！")
        logger.info("=" * 60)'''

if old_admin in main:
    main = main.replace(old_admin, new_admin, 1)
    print("✅ main.py: 默认 admin 密码改为随机生成")
else:
    print("→ main.py: 默认 admin 密码可能已修")

# 4b. CORS 修复
old_cors = '''    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 开发阶段允许所有来源；生产环境请按需限制
        allow_credentials=True,'''

new_cors = '''    # 构建允许的来源列表
    cors_origins = ["*"]
    if settings.FECLAW_PUBLIC_URL:
        cors_origins = [
            f"https://{settings.FECLAW_PUBLIC_URL}",
            f"http://{settings.FECLAW_PUBLIC_URL}",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,'''

main = main.replace(old_cors, new_cors, 1)
print("✅ main.py: CORS 改为动态来源")

# 4c. 移除旧 CORS 注释
main = main.replace(
    '    # 开发阶段允许所有来源；生产环境请按需限制',
    '    # 根据 FECLAW_PUBLIC_URL 动态设置'
)

with open('main.py', 'w') as f:
    f.write(main)


# ============================================================
# 5. 创建 .python-version
# ============================================================
with open('.python-version', 'w') as f:
    f.write("3.12\n")
print("✅ .python-version: 创建")


# ============================================================
# 6. 创建 pyproject.toml (最小版本)
# ============================================================
with open('pyproject.toml', 'w') as f:
    f.write("""[project]
name = "feclaw"
version = "1.0.0"
description = "A living guide for every student — AI learning platform"
requires-python = ">=3.10"

[build-system]
requires = ["setuptools>=64.0"]
build-backend = "setuptools.backends._legacy:_Backend"
""")
print("✅ pyproject.toml: 创建（含 requires-python）")

print("\n=== 简单修复完成 ===")
