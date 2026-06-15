"""
Agent JWT 服务
签发和验证 Agent 专属 JWT
"""

import secrets
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from jose import JWTError, jwt
import logging

from config import settings
from models.database import SessionLocal, AgentProfile

logger = logging.getLogger(__name__)


class AgentJWTService:
    """Agent JWT 服务类"""

    def __init__(self):
        self.jwt_secret = settings.JWT_SECRET
        self.jwt_algorithm = settings.JWT_ALGORITHM
        self.jwt_expire_hours = settings.JWT_EXPIRE_HOURS

    def issue_agent_jwt(
        self,
        user_id: int,
        agent_id: int,
        agent_hash: str,
        permissions: List[str] = None,
        expire_hours: int = None
    ) -> str:
        """
        签发 Agent 专属 JWT

        Args:
            user_id: 用户 ID
            agent_id: Agent ID
            agent_hash: Agent Hash (4 位)
            permissions: 权限列表
            expire_hours: 过期时间（小时）

        Returns:
            JWT token 字符串
        """
        if permissions is None:
            permissions = ["chat", "upload", "session"]

        expire_hours = expire_hours or self.jwt_expire_hours
        expire_time = datetime.utcnow() + timedelta(hours=expire_hours)

        payload = {
            "type": "agent_jwt",
            "user_id": user_id,
            "agent_id": agent_id,
            "agent_hash": agent_hash,
            "permissions": permissions,
            "iat": datetime.utcnow(),
            "exp": expire_time,
            "jti": secrets.token_hex(16)  # JWT ID，用于唯一标识
        }

        return jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)

    def verify_agent_jwt(self, token: str) -> Optional[Dict[str, Any]]:
        """
        验证 Agent JWT

        Args:
            token: JWT token 字符串

        Returns:
            解码后的 payload，验证失败返回 None
        """
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.jwt_algorithm])

            # 验证 token 类型
            if payload.get("type") != "agent_jwt":
                logger.warning("Token is not an agent JWT")
                return None

            return payload
        except JWTError as e:
            logger.error(f"Failed to verify agent JWT: {e}")
            return None

    def refresh_agent_jwt(self, token: str, expire_hours: int = None) -> Optional[str]:
        """
        刷新 Agent JWT

        Args:
            token: 当前有效的 JWT token
            expire_hours: 新 token 过期时间

        Returns:
            新的 JWT token，刷新失败返回 None
        """
        payload = self.verify_agent_jwt(token)
        if payload is None:
            return None

        # 签发新 token
        return self.issue_agent_jwt(
            user_id=payload["user_id"],
            agent_id=payload["agent_id"],
            agent_hash=payload["agent_hash"],
            permissions=payload.get("permissions", []),
            expire_hours=expire_hours
        )

    def check_permission(self, token: str, permission: str) -> bool:
        """
        检查 Agent JWT 是否具有特定权限

        Args:
            token: JWT token
            permission: 权限名称

        Returns:
            是否具有权限
        """
        payload = self.verify_agent_jwt(token)
        if payload is None:
            return False

        permissions = payload.get("permissions", [])
        return permission in permissions

    def get_agent_from_jwt(self, token: str) -> Optional[AgentProfile]:
        """
        从 JWT token 获取 AgentProfile 实例

        Args:
            token: JWT token

        Returns:
            AgentProfile 实例，验证失败返回 None
        """
        payload = self.verify_agent_jwt(token)
        if payload is None:
            return None

        db = SessionLocal()
        try:
            agent_id = payload.get("agent_id")
            agent = db.query(AgentProfile).filter(AgentProfile.id == agent_id).first()
            return agent
        finally:
            db.close()


# 全局服务实例
agent_jwt_service = AgentJWTService()