"""
工具函数
包含加密、认证、JWT等

认证相关的 FastAPI 依赖（`get_current_user` 等）已迁出到 `utils/auth_dependencies.py`。
本文件仅保留**底层原语**（密码哈希、JWT 编解码、admin 用户校验被依赖复用）。
为保持向后兼容，原依赖函数名在此 re-export。
"""

import hashlib
import uuid
import json
import calendar
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from config import settings
from models.database import get_db, User

# 保持向后兼容：所有 `from utils.auth import get_current_user*` 仍然可用
# 用 __getattr__ 懒加载避免 utils.auth <-> utils.auth_dependencies 循环导入
_LAZY_AUTH_DEPS = {
    "get_current_user",
    "get_current_user_id",
    "get_current_user_optional",
    "get_admin_user",
}


def __getattr__(name):
    if name in _LAZY_AUTH_DEPS:
        from utils import auth_dependencies
        return getattr(auth_dependencies, name)
    raise AttributeError(f"module 'utils.auth' has no attribute {name!r}")


security = HTTPBearer()


# ==========================================
# 密码处理
# ==========================================
#
# 哈希格式：
# - bcrypt（默认/新用户）：直接返回 bcrypt 字符串，形如 `$2b$12$<22字符salt>$<31字符hash>`
#   bcrypt 自带 salt，无需额外存储。
# - SHA-256 legacy（老用户）：自描述前缀 `$sha256v1$<salt>$<hash>`。
#   用于在 user.salt 字段变 NULL 后仍能 verify 历史 hash。
#
# 迁移策略：登录成功时若 `user.password_version == 1`，
# 自动用 bcrypt 重新 hash 并 `password_version = 2`。详见 P0.4 / ADR 0002。

import bcrypt as _bcrypt

_LEGACY_PREFIX = "$sha256v1$"
_BCRYPT_COST = 12  # ~250ms / hash（生产环境适当）


def generate_salt() -> str:
    """生成 SHA-256 legacy 用的盐值。新用户走 bcrypt，不需要调用此函数。"""
    return uuid.uuid4().hex


def hash_password(password: str, salt: str = "") -> str:
    """计算密码哈希。

    默认走 bcrypt（推荐）；若显式传入 `salt`，则返回 legacy SHA-256 自描述格式
    （仅用于迁移脚本主动重建历史 hash 的场景）。
    """
    if salt:
        # legacy 路径：自描述前缀，便于 verify 时反解 salt
        h = hashlib.sha256((password + salt).encode()).hexdigest()
        return f"{_LEGACY_PREFIX}{salt}${h}"
    # bcrypt 路径：salt 嵌入 hash，无需外部存储
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=_BCRYPT_COST)).decode()


def verify_password(password: str, password_hash: str, salt: str = "") -> bool:
    """验证密码。

    自动解析前缀走对应分支：
    - `$sha256v1$<salt>$<hash>` → 用 salt 重新算 SHA-256 比较
    - `$2b$...` / `$2a$...` → bcrypt.checkpw
    - 其他 → 视为非法格式返回 False
    """
    if not password_hash:
        return False

    if password_hash.startswith(_LEGACY_PREFIX):
        # legacy：从前缀里取 salt
        try:
            _, salt_legacy, stored = password_hash.split("$", 3)[1:]
        except ValueError:
            return False
        return hashlib.sha256((password + salt_legacy).encode()).hexdigest() == stored

    if password_hash.startswith(("$2b$", "$2a$", "$2y$")):
        try:
            return _bcrypt.checkpw(password.encode(), password_hash.encode())
        except (ValueError, TypeError):
            return False

    # 未知格式 —— 拒绝
    return False


def needs_rehash(password_hash: str) -> bool:
    """判断是否需要把 legacy hash 升级到 bcrypt。"""
    return password_hash.startswith(_LEGACY_PREFIX)


# ==========================================
# JWT处理
# ==========================================

def create_jwt_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """创建JWT Token"""
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=settings.JWT_EXPIRE_HOURS)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt


def decode_jwt_token(token: str) -> Optional[Dict[str, Any]]:
    """解码JWT Token"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


# ==========================================
# 其他工具函数
# ==========================================

def generate_qid(subject: str, question_id: int) -> str:
    """生成问题唯一ID"""
    # 科目缩写映射
    subject_map = {
        "语文": "YW", "数学": "SX", "英语": "YY", "物理": "WL",
        "化学": "HX", "生物": "SW", "历史": "LS", "政治": "ZZ",
        "地理": "DL", "其他": "QT"
    }
    prefix = subject_map.get(subject, "QT")
    return f"Q-{prefix}-{question_id}"


def extract_subject_from_filename(filename: str) -> str:
    """从文件名提取科目"""
    for keyword in getattr(settings, 'SUBJECT_KEYWORDS', []):
        if keyword in filename:
            return keyword
    return "其他"


def format_timestamp(dt: datetime) -> int:
    """将datetime转换为Unix时间戳（正确处理UTC时间）"""
    if dt is None:
        return 0
    # 如果datetime是UTC时间（使用utcnow创建），使用calendar.timegm
    # 如果datetime是本地时间，使用timestamp()
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        # naive datetime，假设是UTC时间（因为我们使用utcnow创建的）
        return calendar.timegm(dt.timetuple())
    else:
        # aware datetime，直接使用timestamp
        return int(dt.timestamp())


def json_dumps(obj: Any) -> str:
    """安全的JSON序列化"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def json_loads(json_str: str) -> Any:
    """安全的JSON反序列化"""
    return json.loads(json_str)