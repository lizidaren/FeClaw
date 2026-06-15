"""
Agent 工具服务 - 文件操作工具
包含 file_read/write/list/delete/edit
"""

import asyncio
import logging

from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase

logger = logging.getLogger(__name__)


class FileOpsMixin(AgentToolsServiceBase):
    """文件操作工具 Mixin"""

    # ========== 文件工具 ==========

    @tool(description="读取文件。path 用绝对路径（以 / 开头），如 /workspace/soul.md。⚠️ 必须使用用户消息中「图片路径:」标注的路径，禁止自己构造路径。", category="file")
    async def file_read(self, path: str) -> str:
        """从 COS 读取文件"""
        if not self._check_read(path):
            current_perm = self._perm_service.get_permission(path)
            return f"Error: 无读权限 (当前权限: {current_perm}): {path}"

        result = await self._vfs.async_cat(path)
        if result.startswith("Error:"):
            # 提示模型检查用户消息中的图片路径标注
            result += (
                f"\n\n💡 提示：如果这是图片文件，请检查用户消息中「图片路径:」后面标注的路径。"
                f"不要自己构造或猜测路径（如 current_image.png），使用用户消息中标注的路径重新读取。"
            )
        return result

    @tool(description="写入内容到文件。path 用绝对路径（以 / 开头），如 /workspace/notes.txt", category="file")
    async def file_write(self, path: str, content: str) -> str:
        """写入文件到 COS"""
        if not self._check_write(path):
            current_perm = self._perm_service.get_permission(path)
            return f"Error: 无写权限 (当前权限: {current_perm}): {path}"

        # 走 VFS echo 写入，确保版本控制、自动索引等钩子生效
        return await self._vfs.async_write(path, content)

    @tool(description="列出目录下的文件。dir 用绝对路径（以 / 开头），如 /workspace。返回的路径也是绝对路径。", category="file")
    async def file_list(self, dir: str = "") -> str:
        """列出目录下的文件"""
        if ".." in dir:
            return "Error: 路径不允许 .."

        cos_prefix, err = self._vfs._resolve_to_dir_prefix(dir)
        if err:
            return err

        try:
            # 在线程池执行 _parse_dir_contents，避免 COS list_objects 阻塞事件循环
            entries = await asyncio.to_thread(self._vfs._parse_dir_contents, cos_prefix)
            if not entries:
                return "（空目录）"
            names = []
            for e in entries:
                if e.name == ".directory":
                    continue
                name = e.name + "/" if e.type == "directory" else e.name
                # 返回绝对路径
                if dir and dir != "/":
                    base = dir.rstrip("/")
                else:
                    base = ""
                names.append(f"{base}/{name}" if base else f"/{name}")

            # 在结果前提示正在列出的绝对路径
            cwd = self._vfs._cwd or ""
            if dir and not dir.startswith("/"):
                actual_path = f"/{cwd}/{dir}".rstrip("/") if cwd else f"/{dir}".rstrip("/")
            elif dir:
                actual_path = dir.rstrip("/")
            else:
                actual_path = f"/{cwd}" if cwd else "/"
            return f"【{actual_path}/】\n" + "\n".join(names)
        except Exception as e:
            return f"Error: 列出失败: {e}"

    @tool(description="删除文件（禁止删除 agent/ 目录）。path 用绝对路径（以 / 开头）。", category="file")
    async def file_delete(self, path: str) -> str:
        """从 COS 删除文件，禁止删除 agent/ 目录"""
        if not self._check_write(path):
            current_perm = self._perm_service.get_permission(path)
            return f"Error: 无写权限 (当前权限: {current_perm}): {path}"

        key, err = self._vfs._resolve_path(path)
        if err:
            return err

        if path.startswith("agent/") or "/agent/" in path:
            return "Error: 禁止删除 agent/ 目录"

        try:
            await self.storage.delete_file_by_key_async(key)
            return f"OK: 已删除 {path}"
        except Exception as e:
            return f"Error: 删除失败: {e}"

    @tool(description="对文件进行精确的字符串替换修改。用 new_string 替换文件中第一个出现的 old_string。适用于修改参数、常量、函数调用等。对于简单修改比 file_write 更安全。", category="file")
    async def edit(self, path: str, old_string: str, new_string: str) -> str:
        """
        对文件进行精确的字符串替换修改。
        找到第一个匹配的 old_string 并替换为 new_string。
        """
        try:
            content = await self._vfs.async_read_file(path)
            if content.startswith("Error:"):
                return content

            if old_string not in content:
                return f"Error: 在文件中未找到匹配的字符串"

            if content.count(old_string) > 1:
                new_content = content.replace(old_string, new_string, 1)
            else:
                new_content = content.replace(old_string, new_string)

            # 走 VFS echo 写入，触发版本控制 + 自动索引
            return await self._vfs.async_write(path, new_content)
        except Exception as e:
            return f"Error: 修改失败: {e}"
