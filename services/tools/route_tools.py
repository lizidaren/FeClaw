"""
Route Tools — Agent 管理 App 路由的工具

Agent 通过这些工具在 /workspace/apps/ 下注册/注销 Web 应用路由。
"""

import json
import logging
from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase
from services.virtual_filesystem import VirtualFileSystem

logger = logging.getLogger(__name__)


class RouteToolsMixin(AgentToolsServiceBase):
    """路由管理工具集"""

    @tool(description="注册一个 App。在 /workspace/apps/{app_id}/routes.json 已存在的情况下将其上线。用户可通过 https://{agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/ 访问。", category="agent")
    def route_register(self, app_id: str) -> str:
        """
        注册一个 App，使其可以通过浏览器访问。

        Args:
            app_id: App 的唯一标识（如 "my-vocab-app"）

        Returns:
            注册结果的文字描述
        """
        from services.apps_service import register_app_sync
        result = register_app_sync(self.agent_hash, app_id)
        if result:
            return f"✅ App '{app_id}' 已注册。可访问 {{ settings.FECLAW_PUBLIC_URL }}/apps/{app_id}/"
        return f"❌ 注册失败：{app_id}（未找到 routes.json 或已达上限）"

    @tool(description="注销一个已注册的 App。App 文件不会被删除，可随时重新注册。", category="agent")
    def route_unregister(self, app_id: str) -> str:
        """
        注销一个 App，下线网站。

        Args:
            app_id: 要注销的 App ID

        Returns:
            注销结果的文字描述
        """
        from services.apps_service import unregister_app_sync
        if unregister_app_sync(self.agent_hash, app_id):
            return f"✅ App '{app_id}' 已注销。文件保留在 /workspace/apps/{app_id}/"
        return f"❌ 未找到 App '{app_id}'"

    @tool(description="列出当前 Agent 已注册的所有 App。", category="agent")
    def route_list(self) -> str:
        """
        列出所有已注册的 App。

        Returns:
            已注册 App 的列表
        """
        from services.apps_service import list_registered_apps
        apps = list_registered_apps(self.agent_hash)
        if not apps:
            return "暂无已注册的 App。"
        
        lines = [f"📱 已注册 {len(apps)} 个 App："]
        for app_id in apps:
            url = f"{{ settings.FECLAW_PUBLIC_URL }}/apps/{app_id}/"
            lines.append(f"  - {app_id}: {url}")
        return "\n".join(lines)
