"""
FeClaw 首次启动配置向导 — 后端服务

提供：
- 配置检测（is_setup_complete）
- .env 安全读写（update_env / read_env）
- Provider 列表（get_provider_list）
- 管理员随机密码生成 + Banner 打印
- admin 用户创建 / 重置
- /setup API 的辅助函数
- 冷启动：generate_setup_token / test_db_connection / init_database

所有 .env 写操作通过 _env_lock（threading.Lock）+ .env 文件权限 600 保证线程与权限安全。
"""
from __future__ import annotations

import logging
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────
# 路径常量
# ───────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE_FILE = BASE_DIR / ".env.example"

# .env 写入锁 —— 防止并发写入撕裂文件
_env_lock = threading.Lock()


# ───────────────────────────────────────────────────────────
# Provider 列表（前端 Step 2 用）
# ───────────────────────────────────────────────────────────

# 注意：与 services/model_registry.PROVIDER_META 保持一致。
# 但本表为面向用户的元数据（名称、描述、能力、推荐模型），更丰富。
#
# capability_models: 把每个能力映射到该 provider 中能用于该能力的具体模型列表。
# 缺失的 capability 键表示该 provider 不提供该能力。
# Step 4 模型下拉框直接消费此字段；Step 1-3 不读此字段（向后兼容）。
PROVIDER_LIST: List[Dict[str, Any]] = [
    {
        "id": "qwen",
        "name": "阿里云百炼",
        "description": "推荐，一个 Key 覆盖文本/视觉/嵌入/搜索",
        "badge": "推荐",
        "api_key_name": "QWEN_API_KEY",
        "covers": ["text", "vision", "embedding", "search"],
        "models": ["qwen3.6-flash", "qwen3.6-plus", "qwen3.7-plus", "qwen3.7-max", "qwen3.6-35b-a3b", "qwen3-vl-flash", "qwen3-vl-plus", "text-embedding-v4"],
        "capability_models": {
            "text": ["qwen3.6-flash", "qwen3.6-plus", "qwen3.7-plus", "qwen3.7-max"],
            "vision": ["qwen3.6-35b-a3b", "qwen3-vl-flash", "qwen3-vl-plus"],
            "embedding": ["text-embedding-v4"],
        },
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "description": "中文更自然，有深度思考",
        "badge": None,
        "api_key_name": "DEEPSEEK_API_KEY",
        "covers": ["text"],
        "models": ["deepseek-v4-flash"],
        "capability_models": {
            "text": ["deepseek-v4-flash"],
        },
    },
    {
        "id": "zhipuai",
        "name": "智谱 GLM",
        "description": "flash 模型免费，GLM-4.6V 支持视觉",
        "badge": None,
        "api_key_name": "ZHIPU_API_KEY",
        "covers": ["text", "vision"],
        "models": ["glm-4.7", "glm-4.7-flash", "glm-4.6v", "glm-4.5-air", "glm-5-turbo", "glm-5"],
        "capability_models": {
            "text": ["glm-4.7", "glm-4.7-flash", "glm-4.5-air", "glm-5-turbo", "glm-5"],
            "vision": ["glm-4.6v"],
        },
    },
    {
        "id": "kimi",
        "name": "Kimi (月之暗面)",
        "description": "搜索能力强，长上下文",
        "badge": None,
        "api_key_name": "KIMI_API_KEY",
        "covers": ["search", "text"],
        "models": ["kimi-k2.5", "kimi-k2.6"],
        "capability_models": {
            "text": ["kimi-k2.5", "kimi-k2.6"],
        },
    },
    {
        "id": "mimo",
        "name": "小米 MiMo",
        "description": "速度快",
        "badge": None,
        "api_key_name": "MIMO_API_KEY",
        "covers": ["text"],
        "models": ["mimo-v2.5", "mimo-v2.5-pro", "mimo-v2.5-pro-ultraspeed"],
        "capability_models": {
            "text": ["mimo-v2.5", "mimo-v2.5-pro", "mimo-v2.5-pro-ultraspeed"],
        },
    },
    {
        "id": "doubao",
        "name": "火山引擎 (豆包)",
        "description": "图片理解 / 文生图",
        "badge": None,
        "api_key_name": "DOUBAO_API_KEY",
        "covers": ["vision", "image_generation"],
        "models": ["doubao-seed-2-0-lite-260215", "doubao-seed-2-1-turbo-260628", "doubao-seed-2-1-pro-260628", "doubao-seedream-5-0-260128"],
        "capability_models": {
            "vision": ["doubao-seed-2-0-lite-260215"],
        },
    },
]

# LLM API key 名称集合（用于配置检测）
LLM_API_KEY_NAMES: List[str] = [p["api_key_name"] for p in PROVIDER_LIST]


# ───────────────────────────────────────────────────────────
# .env 读取 / 写入
# ───────────────────────────────────────────────────────────

def _parse_env_file(path: Path) -> Dict[str, str]:
    """解析 .env 文件为 dict（保留注释 / 空行的顺序由调用方维护）。"""
    result: Dict[str, str] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 去掉包裹的引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def _set_env_value(env_text: str, key: str, value: str) -> str:
    """在不丢失注释和顺序的情况下更新/插入 env 项。

    若 key 已存在（行首匹配），则替换该行；
    否则追加到文件末尾。
    """
    if not value:
        return env_text
    lines = env_text.splitlines()
    new_line = f"{key}={value}"
    found = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k = stripped.split("=", 1)[0].strip()
        if k == key:
            lines[i] = new_line
            found = True
            break
    if not found:
        # 若文件末尾无换行，补一个
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(new_line)
    return "\n".join(lines) + "\n"


def update_env(updates: Dict[str, str]) -> None:
    """线程安全地更新 .env 文件。

    - 仅在 value 非空时写入
    - 写入后 chmod 600（仅 owner 可读写）
    - 文件不存在则自动创建
    """
    if not updates:
        return
    with _env_lock:
        if ENV_FILE.exists():
            try:
                text = ENV_FILE.read_text(encoding="utf-8")
            except Exception as e:
                logger.error(f"[Setup] 读取 .env 失败: {e}")
                text = ""
        else:
            text = ""
        for k, v in updates.items():
            if v:
                text = _set_env_value(text, k, v)
        # 写入
        ENV_FILE.write_text(text, encoding="utf-8")
        # 权限 600
        try:
            os.chmod(ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except Exception as e:
            # Windows / WSL 等可能不支持；只记录不抛
            logger.debug(f"[Setup] chmod 600 失败（非致命）: {e}")


def get_partial_config() -> Dict[str, Any]:
    """返回当前 .env 中已有的"非敏感占位"信息（用于前端预填）。"""
    env = _parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    # 避免把真实密钥回显给前端 —— 只标记"是否已设置"
    api_keys = {name: bool(env.get(name, "").strip()) for name in LLM_API_KEY_NAMES}
    return {
        "jwt_secret_set": bool(env.get("JWT_SECRET", "").strip()),
        "database_url": env.get("DATABASE_URL", settings.DATABASE_URL or ""),
        "storage_mode": env.get("STORAGE_MODE", settings.STORAGE_MODE or "auto"),
        "api_keys_present": api_keys,
        "setup_complete": (env.get("SETUP_COMPLETE", "").lower() == "true"),
    }


def get_current_admin(db) -> Optional[Any]:
    """获取当前 admin 用户的 email（如果有）。"""
    try:
        from models.database import User
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            return None
        return {"username": admin.username, "email": admin.email}
    except Exception as e:
        logger.warning(f"[Setup] get_current_admin 失败: {e}")
        return None


# ───────────────────────────────────────────────────────────
# 配置检测
# ───────────────────────────────────────────────────────────

def _has_any_llm_key() -> bool:
    env = _parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    for name in LLM_API_KEY_NAMES:
        if env.get(name, "").strip():
            return True
    # 也兼容 settings 中已加载的值
    for name in LLM_API_KEY_NAMES:
        if getattr(settings, name, ""):
            return True
    return False


def _has_admin_user(db) -> bool:
    try:
        from models.database import User
        return db.query(User).filter(User.username == "admin").first() is not None
    except Exception:
        return False


def is_setup_complete(db=None) -> bool:
    """检查 .env 是否满足最低运行要求。

    最低运行要求：
    1. SETUP_COMPLETE == "true" 显式标记
    2. JWT_SECRET 非空
    3. DATABASE_URL 能连上（仅在 db 提供时检测；无 db 仅做字段校验）
    4. 至少有一个 LLM API Key
    5. admin 用户存在（仅在 db 提供时检测）
    """
    env = _parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    # 优先以显式标记为准
    if env.get("SETUP_COMPLETE", "").lower() == "true":
        # 但即便标记为 true，仍做最基本校验：JWT + 至少一个 LLM key
        if not env.get("JWT_SECRET", "").strip() and not settings.JWT_SECRET:
            return False
        if not _has_any_llm_key():
            return False
        if db is not None and not _has_admin_user(db):
            return False
        return True

    # 未标记 —— 走全量检查
    if not (env.get("JWT_SECRET", "").strip() or settings.JWT_SECRET):
        return False
    if not _has_any_llm_key():
        return False
    if db is not None and not _has_admin_user(db):
        return False
    return True


# ───────────────────────────────────────────────────────────
# 管理员密码生成 + Banner
# ───────────────────────────────────────────────────────────

def generate_admin_password(length: int = 12) -> str:
    """生成强随机密码（16 字符 url-safe base64）"""
    return secrets.token_urlsafe(12)[:length] if length <= 16 else secrets.token_urlsafe(length)


def print_admin_banner(password: str, host: str = "localhost", port: int = 8080) -> None:
    """打印首次启动 banner 到控制台。"""
    banner = f"""
  ╔══════════════════════════════════════════════╗
  ║                                              ║
  ║   FeClaw 首次启动                             ║
  ║                                              ║
  ║   管理面板: http://{host}:{port}              ║
  ║   用户名:   admin                             ║
  ║   密码:     {password:<30s}║
  ║                                              ║
  ║   ⚠️ 此密码仅显示一次                         ║
  ║   忘记后运行 --reset-admin 重置                ║
  ╚══════════════════════════════════════════════╝
"""
    # 用 print 而非 logger：logger 可能被配置成只入文件 / 不会显示在终端
    print(banner)


def create_or_reset_admin(db, password: str) -> Any:
    """创建或重置 admin 用户的密码。返回 User 对象。

    - 若已存在 admin：更新 password_hash（bcrypt），password_version=2
    - 若不存在：创建新用户，bcrypt 哈希，is_admin=True
    """
    from models.database import User
    from utils.auth import hash_password

    admin = db.query(User).filter(User.username == "admin").first()
    if admin:
        admin.password_hash = hash_password(password)
        admin.salt = None
        admin.password_version = 2
        admin.is_admin = True
        db.commit()
        db.refresh(admin)
        logger.info("[Setup] 已重置 admin 用户密码")
        return admin
    # 创建
    admin = User(
        username="admin",
        password_hash=hash_password(password),
        salt=None,
        password_version=2,
        is_admin=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    logger.info("[Setup] 已创建 admin 用户（随机密码）")
    return admin


# ───────────────────────────────────────────────────────────
# Provider 列表（API 用）
# ───────────────────────────────────────────────────────────

def get_provider_list() -> Dict[str, Any]:
    """返回前端 Step 2 需要的 provider 元数据 + 当前 key 状态。"""
    env = _parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    api_keys_present: Dict[str, bool] = {}
    for p in PROVIDER_LIST:
        name = p["api_key_name"]
        api_keys_present[name] = bool(env.get(name, "").strip() or getattr(settings, name, ""))
    return {
        "providers": PROVIDER_LIST,
        "current_api_keys": api_keys_present,
    }


# ───────────────────────────────────────────────────────────
# 连接测试（最小实现：检查 key 是否设置 + 形态合法）
# ───────────────────────────────────────────────────────────

def _looks_like_valid_key(value: str) -> bool:
    if not value or len(value) < 8:
        return False
    return True


async def verify_provider(provider_id: str) -> Dict[str, Any]:
    """测试某个 provider 的 API key 是否可用。

    实际是只做格式校验 + 标记 key 存在，不发起真实网络请求
    （避免启动时阻塞 / 速率限制）。详细校验在 /setup/verify 中可选执行。
    """
    provider = next((p for p in PROVIDER_LIST if p["id"] == provider_id), None)
    if not provider:
        return {"ok": False, "provider": provider_id, "error": "unknown provider"}
    key_name = provider["api_key_name"]
    env = _parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    value = env.get(key_name, "") or getattr(settings, key_name, "")
    if not _looks_like_valid_key(value):
        return {"ok": False, "provider": provider_id, "error": f"{key_name} 未设置或格式异常"}
    return {"ok": True, "provider": provider_id, "key_name": key_name}


async def verify_config(db=None) -> Dict[str, Any]:
    """测试当前配置（异步）—— 给出每项的状态。"""
    results: List[Dict[str, Any]] = []

    # 1. JWT_SECRET
    env = _parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    jwt_set = bool(env.get("JWT_SECRET", "").strip() or settings.JWT_SECRET)
    results.append({"name": "JWT_SECRET", "ok": jwt_set, "message": "已设置" if jwt_set else "缺失"})

    # 2. DATABASE_URL
    db_url = env.get("DATABASE_URL", "") or settings.DATABASE_URL or ""
    db_ok = bool(db_url)
    results.append({"name": "DATABASE_URL", "ok": db_ok, "message": "已设置" if db_ok else "缺失"})

    # 3. LLM Keys
    for p in PROVIDER_LIST:
        r = await verify_provider(p["id"])
        results.append({
            "name": p["api_key_name"],
            "ok": r["ok"],
            "message": "已设置" if r["ok"] else r.get("error", "未设置"),
            "provider": p["id"],
        })

    # 4. admin 用户
    if db is not None:
        admin_ok = _has_admin_user(db)
        results.append({"name": "admin_user", "ok": admin_ok, "message": "已存在" if admin_ok else "缺失"})

    overall = all(r["ok"] for r in results)
    return {"status": "ok" if overall else "partial", "results": results, "overall_ok": overall}


# ───────────────────────────────────────────────────────────
# 冷启动：token 生成、数据库连接测试、数据库初始化
# ───────────────────────────────────────────────────────────

def generate_setup_token() -> str:
    """生成冷启动临时鉴权 token（16 字节 hex = 32 字符）。

    用途：首次启动时 main.py 生成并打印到控制台 + 写入 .env。
    用户访问 /setup* 必须带 ?token=<SETUP_TOKEN> 才能通过。
    完成配置后此 token 被清空，setup 路由降级为 JWT 鉴权。
    """
    return secrets.token_hex(16)


def build_database_url(host: str, port: int, user: str, password: str, database: str) -> str:
    """组装 SQLAlchemy 格式的 MySQL URL。

    pymysql 驱动；密码特殊字符走 urllib.parse.quote。
    """
    from urllib.parse import quote_plus
    pwd = quote_plus(password or "")
    return f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{database}?charset=utf8mb4"


def test_db_connection(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> Tuple[bool, str]:
    """测试 MySQL 连接 + 目标数据库是否存在（不存在尝试创建）。

    返回 (ok, message)：
    - ok=True  → message 友好提示（"连接成功" / "数据库 X 已自动创建"）
    - ok=False → message 错误描述（直接展示给用户）

    此函数不修改任何 .env —— 仅做连接测试。
    """
    try:
        import pymysql
    except ImportError:
        return False, "缺少 pymysql 依赖，请先 `pip install pymysql`"

    # 1. 先尝试直接连目标数据库（不创建）
    db_url = build_database_url(host, port, user, password, database)
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True, "连接成功"
    except Exception as e:
        err = str(e).lower()
        # 数据库不存在 → 尝试创建
        if "unknown database" in err or "1049" in err:
            try:
                conn = pymysql.connect(
                    host=host,
                    port=int(port),
                    user=user,
                    password=password,
                    charset="utf8mb4",
                )
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"CREATE DATABASE IF NOT EXISTS `{database}` "
                            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                        )
                    conn.commit()
                finally:
                    conn.close()
                # 重新尝试连接目标库
                from sqlalchemy import create_engine, text as _text
                engine2 = create_engine(db_url, pool_pre_ping=True)
                with engine2.connect() as conn2:
                    conn2.execute(_text("SELECT 1"))
                engine2.dispose()
                return True, f"数据库 {database} 已自动创建，连接成功"
            except Exception as e2:
                return False, f"无法创建数据库 {database}：{e2}"
        # 鉴权失败 / 网络不通 / 主机不存在
        return False, f"连接失败：{e}"


def init_database(
    db_url: str,
    admin_username: str,
    admin_password: str,
    jwt_secret: Optional[str] = None,
) -> Tuple[bool, str]:
    """用给定 db_url 初始化数据库（建表 + 创建 admin 用户）+ 写 .env。

    步骤：
    1. 用传入的 db_url 创建 engine
    2. Base.metadata.create_all() 建表（需导入所有模型）
    3. 创建 admin 用户（bcrypt 加密）
    4. 写入 DATABASE_URL + JWT_SECRET 到 .env
       - JWT_SECRET 缺省时自动生成（32 字节 hex）

    返回 (ok, message)。
    """
    if not admin_username or not admin_password:
        return False, "管理员用户名和密码不能为空"
    if len(admin_password) < 8:
        return False, "密码至少 8 位"

    try:
        from sqlalchemy import create_engine, text
        from models.database import Base, SessionLocal, User
        from utils.auth import hash_password
    except ImportError as e:
        return False, f"导入数据库模块失败：{e}"

    try:
        engine = create_engine(db_url, pool_pre_ping=True)
        # 先确认可连
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        return False, f"无法连接数据库：{e}"

    # 1. 建表（先 import 所有模型以确保 metadata 完整）
    try:
        # 主 models.database 已包含大部分表
        from models import database as _db_models  # noqa: F401
        from models.agent_profile import AgentProfile  # noqa: F401
        from models.database import ChatHistory  # noqa: F401
        from models.agent_buffer import AgentBuffer  # noqa: F401
        from models.fehub import FePublish, AppData  # noqa: F401
        from models.zentrim import (  # noqa: F401
            ZentrimEntry, ZentrimTimeline, ZentrimTimelineEntry, ZentrimReference,
        )
        from models.group import (  # noqa: F401
            Group, GroupMember, GroupMessage, GroupMoments,
        )
        from models.kaoxiang_models import KaoxiangKaodian  # noqa: F401
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        return False, f"建表失败：{e}"

    # 2. 创建 admin 用户
    try:
        # 用临时 SessionLocal 绑定新 engine
        from sqlalchemy.orm import sessionmaker as _sessionmaker
        TempSessionLocal = _sessionmaker(autocommit=False, autoflush=False, bind=engine)
        db = TempSessionLocal()
        try:
            existing = db.query(User).filter(User.username == admin_username).first()
            if existing:
                # 已存在 → 重置密码
                existing.password_hash = hash_password(admin_password)
                existing.salt = None
                existing.password_version = 2
                existing.is_admin = True
            else:
                admin = User(
                    username=admin_username,
                    password_hash=hash_password(admin_password),
                    salt=None,
                    password_version=2,
                    is_admin=True,
                )
                db.add(admin)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        return False, f"创建管理员失败：{e}"

    # 3. 写入 .env（DATABASE_URL + JWT_SECRET）
    try:
        updates: Dict[str, str] = {"DATABASE_URL": db_url}
        if jwt_secret and jwt_secret.strip():
            updates["JWT_SECRET"] = jwt_secret.strip()
        else:
            updates["JWT_SECRET"] = secrets.token_hex(32)
        update_env(updates)
    except Exception as e:
        return False, f"写入 .env 失败：{e}"

    return True, "数据库已初始化，管理员已创建"
