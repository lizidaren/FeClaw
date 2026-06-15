"""
Agent 工具服务 - 分享/登录工具
包含 create_share_link, generate_totp
"""

import logging
from typing import Optional

from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase

logger = logging.getLogger(__name__)


class ShareToolsMixin(AgentToolsServiceBase):
    """分享/登录工具 Mixin"""

    @tool(description="创建文件分享链接", category="file")
    def create_share_link(
        self,
        path: str,
        mode: str = "share",
        password: Optional[str] = None
    ) -> str:
        """
        创建分享链接

        Args:
            path: VFS文件路径，如 "workspace/output/test.2dggb"
            mode: "share" 生成 hash 链接 或 "path" 路径映射
            password: 可选密码保护

        Returns:
            分享链接 URL
        """
        from services.share_service import create_share_link as _create_share_link

        vfs_path = path
        if not vfs_path.startswith("/"):
            vfs_path = f"/{vfs_path}"

        result = _create_share_link(
            vfs_path=vfs_path,
            mode=mode,
            password=password,
            user_id=self.user_id,
            expires_hours=24 * 7,  # 默认7天过期
            agent_hash=self.agent_hash,
        )

        if result is None:
            return "Error: 创建分享链接失败（可能是敏感文件或无效路径）"

        return result["url"]

    @tool(description="生成一次性登录码（分享 Agent 访问权限给他人）", category="agent")
    def generate_totp(self) -> str:
        """
        生成一次性登录码

        生成一个 6 位一次性验证码，他人可在 Agent 控制台输入此码登录。
        验证码基于 TOTP（RFC 6238），30 秒刷新，5 分钟内有效。

        Returns:
            包含登录码和使用说明的提示信息
        """
        from services.totp_service import totp_service
        from models.database import SessionLocal
        from models.agent_profile import AgentProfile
        from config import settings

        db = SessionLocal()
        try:
            agent = db.query(AgentProfile).filter(
                AgentProfile.hash == self.agent_hash
            ).first()

            if not agent:
                return "Error: Agent not found"

            code = totp_service.generate_code(agent.totp_secret)

            from datetime import datetime, timedelta
            expire = datetime.now() + timedelta(minutes=5)

            return f"""验证码: {code}
过期时间: {expire.strftime("%Y-%m-%d %H:%M:%S")}
登录地址: https://{agent.hash}.{settings.FECLAW_CDN_DOMAIN or settings.FECLAW_DOMAIN}/login
使用方法: 在 Agent 控制台登录页输入上方验证码即可

注意：验证码 5 分钟内有效，30 秒自动刷新。请告知接收方尽快使用。"""
        finally:
            db.close()
