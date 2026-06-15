"""
文件权限服务
提供 Agent 对文件的读写权限控制
"""

from typing import List, Optional
from datetime import datetime

from sqlalchemy.orm import Session
from models.database import FilePermission, SessionLocal, AgentProfile


# 权限级别
class Permission:
    READ = "read"
    WRITE = "write"
    READWRITE = "readwrite"
    NONE = "none"

    # 权限层级（数字越大权限越高）
    LEVELS = {
        NONE: 0,
        WRITE: 1,
        READ: 2,
        READWRITE: 3,
    }

    @classmethod
    def is_valid(cls, perm: str) -> bool:
        return perm in [cls.READ, cls.WRITE, cls.READWRITE, cls.NONE]

    @classmethod
    def has_read(cls, perm: str) -> bool:
        return perm in [cls.READ, cls.READWRITE]

    @classmethod
    def has_write(cls, perm: str) -> bool:
        return perm in [cls.WRITE, cls.READWRITE]


class PermissionService:
    """文件权限服务"""

    def __init__(self, user_id: str = None, db: Optional[Session] = None, agent_hash: str = None):
        """
        初始化权限服务
        
        Args:
            user_id: 用户 ID（向后兼容）
            db: 数据库会话
            agent_hash: Agent 的 4 位 hash（推荐）
        """
        self._user_id = user_id
        self._agent_hash = agent_hash
        self._db = db
        
        # 如果只有 agent_hash，从数据库获取 user_id
        if user_id is None and agent_hash:
            db_sess = db or SessionLocal()
            try:
                agent = db_sess.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
                if agent:
                    self._user_id = str(agent.user_id)
            finally:
                if db is None:
                    db_sess.close()
    
    @property
    def user_id(self) -> str:
        return str(self._user_id) if self._user_id else ""
    
    @property
    def agent_hash(self) -> str:
        return self._agent_hash or ""

    @property
    def db(self) -> Session:
        """获取数据库会话"""
        if self._db is None:
            self._db = SessionLocal()
        return self._db

    def close(self) -> None:
        """关闭数据库会话"""
        if self._db is not None:
            self._db.close()
            self._db = None

    def get_default_permission(self, file_path: str) -> str:
        """
        获取文件的默认权限
        默认所有文件都是 "readwrite"（Agent 可以读写）

        特殊路径：
        - /public/* 目录默认只读（平台公共信息，任何人可读）
        """
        # /public/ 路径默认只读（平台公共信息，不可写入）
        import fnmatch
        normalized = file_path.strip("/")
        if normalized == "public" or normalized.startswith("public/") or fnmatch.fnmatch(normalized, "public/*"):
            return Permission.READ

        # 敏感文件可以在这里设置更严格的默认权限
        sensitive_patterns = [
            "*.env",
            ".env",
            "*/.env",
            "*/*.env",
            "IDENTITY.md",
            "*/IDENTITY.md",
            "*/*/IDENTITY.md",
            "SOUL.md",
            "*/SOUL.md",
            "*/*/SOUL.md",
            "SECRET*",
            "*/SECRET*",
            "PASSWORD*",
            "*/PASSWORD*",
        ]

        import fnmatch
        for pattern in sensitive_patterns:
            if fnmatch.fnmatch(file_path, pattern):
                return Permission.READ  # 敏感文件默认只读

        return Permission.READWRITE

    def check_permission(self, file_path: str, required_permission: str) -> bool:
        """
        检查 Agent 对文件是否有指定权限

        Args:
            file_path: 相对于 Agent 空间的路径，如 "workspace/USER.md"
            required_permission: 需要的权限 ("read" | "write")

        Returns:
            True 如果有权限，False 否则
        """
        # 标准化路径
        file_path = file_path.strip("/")

        # 构建查询条件
        query = self.db.query(FilePermission)
        if self._agent_hash:
            query = query.filter(FilePermission.agent_hash == self._agent_hash)
        else:
            query = query.filter(FilePermission.user_id == self.user_id)
        
        perm_record = query.filter(FilePermission.file_path == file_path).first()

        if perm_record:
            permission = perm_record.permission
        else:
            # 无记录，使用默认权限
            permission = self.get_default_permission(file_path)

        # 检查权限
        if required_permission == "read":
            return Permission.has_read(permission)
        elif required_permission == "write":
            return Permission.has_write(permission)
        else:
            return False

    def grant_permission(self, file_path: str, permission: str) -> bool:
        """
        授予或更新文件权限

        Args:
            file_path: 相对于 Agent 空间的路径
            permission: 权限 ("read" | "write" | "readwrite" | "none")

        Returns:
            True 如果成功，False 如果权限无效
        """
        if not Permission.is_valid(permission):
            return False

        # 标准化路径
        file_path = file_path.strip("/")

        # 构建查询条件
        query = self.db.query(FilePermission)
        if self._agent_hash:
            query = query.filter(FilePermission.agent_hash == self._agent_hash)
        else:
            query = query.filter(FilePermission.user_id == self.user_id)
        
        existing = query.filter(FilePermission.file_path == file_path).first()

        if existing:
            # 更新
            existing.permission = permission
            existing.updated_at = datetime.utcnow()
        else:
            # 创建
            new_perm = FilePermission(
                user_id=self.user_id,
                agent_hash=self._agent_hash or "",
                file_path=file_path,
                permission=permission
            )
            self.db.add(new_perm)

        self.db.commit()
        return True

    def revoke_permission(self, file_path: str) -> bool:
        """
        撤销文件权限（删除权限记录）

        Args:
            file_path: 相对于 Agent 空间的路径

        Returns:
            True 如果成功
        """
        # 标准化路径
        file_path = file_path.strip("/")

        # 构建查询条件
        query = self.db.query(FilePermission)
        if self._agent_hash:
            query = query.filter(FilePermission.agent_hash == self._agent_hash)
        else:
            query = query.filter(FilePermission.user_id == self.user_id)
        
        deleted = query.filter(FilePermission.file_path == file_path).delete()

        self.db.commit()
        return deleted > 0

    def list_permissions(self) -> List[FilePermission]:
        """
        列出 Agent 的所有文件权限

        Returns:
            权限记录列表
        """
        query = self.db.query(FilePermission)
        if self._agent_hash:
            query = query.filter(FilePermission.agent_hash == self._agent_hash)
        else:
            query = query.filter(FilePermission.user_id == self.user_id)
        return query.order_by(FilePermission.file_path).all()

    def get_permission(self, file_path: str) -> str:
        """
        获取文件的具体权限设置

        Args:
            file_path: 相对于 Agent 空间的路径

        Returns:
            权限字符串，如果没有设置则返回默认权限
        """
        # 标准化路径
        file_path = file_path.strip("/")

        query = self.db.query(FilePermission)
        if self._agent_hash:
            query = query.filter(FilePermission.agent_hash == self._agent_hash)
        else:
            query = query.filter(FilePermission.user_id == self.user_id)
        
        perm_record = query.filter(FilePermission.file_path == file_path).first()

        if perm_record:
            return perm_record.permission

        return self.get_default_permission(file_path)


def check_permission(user_id: str, file_path: str, required_permission: str, db: Session = None, agent_hash: str = None) -> bool:
    """
    便捷函数：检查权限

    Args:
        user_id: 用户ID（向后兼容）
        file_path: 文件路径
        required_permission: 需要的权限
        db: 可选的数据库会话
        agent_hash: Agent 的 4 位 hash（推荐）

    Returns:
        True 如果有权限
    """
    service = PermissionService(user_id=user_id, db=db, agent_hash=agent_hash)
    try:
        return service.check_permission(file_path, required_permission)
    finally:
        if db is None:
            service.close()


def grant_permission(user_id: str, file_path: str, permission: str, db: Session = None, agent_hash: str = None) -> bool:
    """
    便捷函数：授予权限

    Args:
        user_id: 用户ID（向后兼容）
        file_path: 文件路径
        permission: 权限
        db: 可选的数据库会话
        agent_hash: Agent 的 4 位 hash（推荐）

    Returns:
        True 如果成功
    """
    service = PermissionService(user_id=user_id, db=db, agent_hash=agent_hash)
    try:
        return service.grant_permission(file_path, permission)
    finally:
        if db is None:
            service.close()


def revoke_permission(user_id: str, file_path: str, db: Session = None, agent_hash: str = None) -> bool:
    """
    便捷函数：撤销权限

    Args:
        user_id: 用户ID（向后兼容）
        file_path: 文件路径
        db: 可选的数据库会话
        agent_hash: Agent 的 4 位 hash（推荐）

    Returns:
        True 如果成功
    """
    service = PermissionService(user_id=user_id, db=db, agent_hash=agent_hash)
    try:
        return service.revoke_permission(file_path)
    finally:
        if db is None:
            service.close()
