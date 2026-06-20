"""
FeHub Tools — fe init / fe vcs / fe publish commands

Mixes into AgentToolsService via the tools/__init__.py composite class.
"""

import logging

from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase
from services.fehub_service import FeHubService

logger = logging.getLogger(__name__)


class FeHubToolsMixin(AgentToolsServiceBase):
    """FeHub VCS + Publish tools Mixin."""

    def _get_fehub_service(self) -> FeHubService:
        """Lazily create FeHubService for this agent."""
        return FeHubService(agent_hash=self.agent_hash)

    @tool(description="初始化小程序项目。创建 manifest.json + index.html 骨架。fe init [--template=/path/to/template]", category="file")
    async def fe_init(self, path: str = "/workspace", template_path: str = "") -> str:
        """
        fe init [--template=/path/to/template]

        在指定路径初始化一个新的小程序项目。
        - 如果指定了 --template，会从已有模板目录复制内容
        - 否则生成最小化项目骨架（manifest.json + index.html）
        """
        svc = self._get_fehub_service()
        return await svc.init_project(path=path, template_path=template_path)

    @tool(description="记录版本。fe vcs commit <message>", category="file")
    async def fe_vcs_commit(self, message: str, file_path: str = "") -> str:
        """
        fe vcs commit <message>

        将当前工作区的变更保存为新版本。
        - message: 提交消息（必填）
        """
        if not message or not message.strip():
            return "Error: 请提供提交消息: fe vcs commit <message>"
        svc = self._get_fehub_service()
        return await svc.commit(path="/workspace", message=message)

    @tool(description="查看版本历史。fe vcs log [file_path]", category="file")
    async def fe_vcs_log(self, file_path: str = "") -> str:
        """
        fe vcs log [file_path]

        查看版本历史记录。
        - 如果指定 file_path，只显示涉及该文件的提交
        - 同时显示已发布版本标签
        """
        svc = self._get_fehub_service()
        return await svc.log(path="/workspace", file_path=file_path)

    @tool(description="对比两个版本。fe vcs diff <file> <ref_a> <ref_b>", category="file")
    async def fe_vcs_diff(self, file_path: str, ref_a: str, ref_b: str) -> str:
        """
        fe vcs diff <file> <ref_a> <ref_b>

        对比文件在两个版本之间的差异（unified diff 格式）。
        - ref_a / ref_b 可以是：提交时间戳前缀 或 发布标签名
        """
        if not file_path:
            return "Error: 请指定文件路径: fe vcs diff <file> <ref_a> <ref_b>"
        if not ref_a or not ref_b:
            return "Error: 请指定两个版本引用: fe vcs diff <file> <ref_a> <ref_b>"
        svc = self._get_fehub_service()
        return await svc.diff(file_path=file_path, ref_a=ref_a, ref_b=ref_b)

    @tool(description="恢复旧版本。fe vcs restore <file> <ref>", category="file")
    async def fe_vcs_restore(self, file_path: str, ref: str) -> str:
        """
        fe vcs restore <file> <ref>

        将指定文件恢复到之前的某个版本。
        - ref 可以是提交时间戳前缀或发布标签名
        """
        if not file_path:
            return "Error: 请指定文件路径: fe vcs restore <file> <ref>"
        if not ref:
            return "Error: 请指定版本引用: fe vcs restore <file> <ref>"
        svc = self._get_fehub_service()
        return await svc.restore(file_path=file_path, ref=ref)

    @tool(description="发布小程序。fe publish <tag> [--public]", category="file")
    async def fe_publish(self, tag: str, is_public: bool = False) -> str:
        """
        fe publish <tag> [--public]

        将当前工作区发布为指定版本的快照。
        - 自动生成访问地址（https://{agent_hash}.feclaw.lizidaren.cn/apps/{agent_hash}-{tag}/）
        - 如果指定 --public，则发布为公开应用
        - 发布后可在 Agent 个人主页看到
        """
        if not tag or not tag.strip():
            return "Error: 请指定发布标签: fe publish <tag>"
        svc = self._get_fehub_service()
        return await svc.publish(path="/workspace", tag=tag.strip(), is_public=is_public)
