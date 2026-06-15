"""
工具函数
包含加密、认证、JWT等
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

security = HTTPBearer()


# ==========================================
# 密码处理
# ==========================================

def generate_salt() -> str:
    """生成盐值"""
    return uuid.uuid4().hex


def hash_password(password: str, salt: str) -> str:
    """计算密码哈希"""
    # TODO: 迁移到 bcrypt，SHA-256 不适合密码存储
    return hashlib.sha256((password + salt).encode()).hexdigest()


def verify_password(password: str, salt: str, password_hash: str) -> bool:
    """验证密码"""
    return hash_password(password, salt) == password_hash


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
# 依赖注入：获取当前用户
# ==========================================

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db = Depends(get_db)
) -> User:
    """获取当前登录用户"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"status": "unauthorized"},
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    token = credentials.credentials
    payload = decode_jwt_token(token)
    
    if payload is None:
        raise credentials_exception
    
    user_id: int = payload.get("user_id")
    if user_id is None:
        raise credentials_exception
    
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    
    return user


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> int:
    """获取当前用户 ID（无 DB 查询，适合只需要 user_id 的路由）"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"status": "unauthorized"},
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = credentials.credentials
    payload = decode_jwt_token(token)

    if payload is None:
        raise credentials_exception

    user_id: int = payload.get("user_id")
    if user_id is None:
        raise credentials_exception

    return user_id


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    db = Depends(get_db)
) -> Optional[User]:
    """获取当前登录用户（可选）"""
    if credentials is None:
        return None
    
    token = credentials.credentials
    payload = decode_jwt_token(token)
    
    if payload is None:
        return None
    
    user_id: int = payload.get("user_id")
    if user_id is None:
        return None
    
    return db.query(User).filter(User.id == user_id).first()


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

async def get_admin_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db = Depends(get_db)
) -> User:
    """获取当前管理员用户（需要管理员权限）"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"status": "unauthorized"},
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = credentials.credentials
    payload = decode_jwt_token(token)

    if payload is None:
        raise credentials_exception

    user_id: int = payload.get("user_id")
    if user_id is None:
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception

    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"status": "forbidden", "message": "需要管理员权限"}
        )

    return user