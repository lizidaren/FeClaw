"""
TOTP 服务（标准 RFC 6238 实现）

使用 pyotp 库实现基于时间的一次性密码
- 每个 Agent 有独立的 TOTP secret
- 30 秒窗口
- 允许向前追溯多个窗口（宽限时间）
"""
import pyotp
import secrets
from datetime import datetime
from typing import Optional, Tuple

from models.database import SessionLocal
from models.agent_profile import AgentProfile
from config import settings


class TOTPService:
    """TOTP 服务"""
    
    # 时间窗口配置
    INTERVAL = 30  # 30 秒一个窗口
    # Q21/M17：原 VALID_WINDOWS=10（±300 秒）—— 同一时刻存在 21 个合法码，把
    # 6 位码爆破成功率从 1/10^6 抬到 21/10^6。降到 1（±30 秒），配合 /api/totp/verify
    # 的 IP+agent_hash 限流（Q19/C2）进一步收窄在线爆破面。
    VALID_WINDOWS = 1
    JWT_EXPIRE_DAYS = 14  # JWT 有效期 14 天
    
    @staticmethod
    def generate_secret() -> str:
        """
        生成 Base32 编码的 TOTP secret
        
        用于创建新 Agent 时生成
        """
        return pyotp.random_base32()
    
    @staticmethod
    def generate_code(secret: str) -> str:
        """
        根据当前时间和 secret 生成 TOTP 码
        
        Args:
            secret: Base32 编码的 TOTP secret
            
        Returns:
            6 位数字验证码
        """
        totp = pyotp.TOTP(secret)
        return totp.now()
    
    @staticmethod
    def verify_code(secret: str, code: str) -> bool:
        """
        验证 TOTP 码（允许多个时间窗口）
        
        Args:
            secret: Base32 编码的 TOTP secret
            code: 用户输入的 6 位验证码
            
        Returns:
            验证是否成功
        """
        totp = pyotp.TOTP(secret)
        # valid_window 参数允许向前追溯 N 个窗口
        return totp.verify(code, valid_window=TOTPService.VALID_WINDOWS)
    
    @staticmethod
    def generate_for_agent(agent_hash: str) -> Tuple[str, str]:
        """
        为 Agent 生成当前 TOTP 码
        
        Args:
            agent_hash: Agent 的 4 位 hash
            
        Returns:
            (code, agent_hash) - 6位验证码和 agent hash
        """
        db = SessionLocal()
        try:
            agent = db.query(AgentProfile).filter(
                AgentProfile.hash == agent_hash
            ).first()
            
            if not agent:
                raise ValueError(f"Agent {agent_hash} not found")
            
            code = TOTPService.generate_code(agent.totp_secret)
            return code, agent_hash
        finally:
            db.close()
    
    @staticmethod
    def verify_agent_totp(agent_hash: str, code: str) -> Optional[dict]:
        """
        验证 Agent 的 TOTP 并签发 JWT
        
        Args:
            agent_hash: Agent 的 4 位 hash
            code: 6 位验证码
            
        Returns:
            验证成功返回 {"token": jwt, "agent_hash": xxx, "user_id": xxx}
            失败返回 None
        """
        db = SessionLocal()
        try:
            agent = db.query(AgentProfile).filter(
                AgentProfile.hash == agent_hash
            ).first()
            
            if not agent:
                return None
            
            # 验证 TOTP
            if not TOTPService.verify_code(agent.totp_secret, code):
                return None
            
            # 签发 JWT
            import jwt
            from datetime import timedelta

            expires_at = datetime.utcnow() + timedelta(days=TOTPService.JWT_EXPIRE_DAYS)
            payload = {
                "user_id": agent.user_id,
                "agent_hash": agent.hash,
                "auth_method": "totp",   # 保留：审计 + 页面级子域校验用
                "exp": expires_at,
                "iat": datetime.utcnow()
            }
            # FIX-A（审计 §1.2 / §6 N2）：TOTP 登录签发的令牌是 **Agent 作用域**的
            # 受限凭证（typ=totp），而非全站会话凭证。此前误签成 typ=session，导致
            # 「拿到任一 Agent 的码 = 拿到该账号全部权限」（/api/user、/api/console、
            # 其他 Agent 的文件等），与 README「只分享某一个 Agent」不符。
            #   - typ=totp ⇒ owner/全局接口（require typ=session）一律 403；
            #   - 携带 agent_hash ⇒ Agent 作用域路由校验 token.agent_hash == 目标 agent；
            #   - 必须写 jwt_version，否则登出过的用户新签发的 token 会被误判为版本 0 而已失效。
            from utils.auth import (
                attach_jwt_version, TYP_CLAIM,
                TOKEN_TYPE_TOTP,
            )
            payload[TYP_CLAIM] = TOKEN_TYPE_TOTP
            attach_jwt_version(payload)
            token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
            
            return {
                "token": token,
                "agent_hash": agent.hash,
                "user_id": agent.user_id,
                "expires_at": expires_at.isoformat()
            }
        finally:
            db.close()
    
    @staticmethod
    def verify_jwt(token: str) -> Optional[dict]:
        """
        验证 JWT token
        
        Returns:
            成功返回 {"user_id": xxx, "agent_hash": xxx}
            失败返回 None
        """
        import jwt
        
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None

        # Q22 令牌分级：本入口是「页面/Agent 上下文」校验（SSO 会话 + TOTP 会话都合法），
        # 只放行 session / totp，拒绝 agent / refresh 令牌。
        from utils.auth import token_type, TOKEN_TYPE_SESSION, TOKEN_TYPE_TOTP
        if token_type(payload) not in (TOKEN_TYPE_SESSION, TOKEN_TYPE_TOTP):
            return None

        # FIX-E（复审残余 L3）：补登出吊销（jwt_version）校验 —— 与页面路由侧
        # （_resolve_local_user_id）和 session 依赖侧（_decode_or_none）口径一致。
        # is_token_revoked 在「无法判定」（用户不存在/查不到版本）时返回 False（放行），
        # 仅在能确定版本不一致时才拒绝，不会误伤老 token。
        from utils.auth import is_token_revoked
        if is_token_revoked(payload):
            return None

        return {
            "user_id": payload["user_id"],
            "agent_hash": payload.get("agent_hash"),
        }
    
    @staticmethod
    def create_agent(user_id: int, name: str = "") -> AgentProfile:
        """
        创建新 Agent
        
        Args:
            user_id: 所属用户 ID
            name: Agent 名称
            
        Returns:
            新创建的 AgentProfile
        """
        db = SessionLocal()
        try:
            # 生成唯一的 4 位 hash
            while True:
                hash_value = secrets.token_hex(2)  # 4 位十六进制
                existing = db.query(AgentProfile).filter(
                    AgentProfile.hash == hash_value
                ).first()
                if not existing:
                    break
            
            # 生成 TOTP secret
            totp_secret = TOTPService.generate_secret()
            
            # 创建 Agent
            agent = AgentProfile(
                user_id=user_id,
                hash=hash_value,
                totp_secret=totp_secret,
                name=name,
                status="pending",
                created_at=datetime.utcnow()
            )
            
            db.add(agent)
            db.commit()
            db.refresh(agent)
            
            return agent
        finally:
            db.close()


# 单例
totp_service = TOTPService()
