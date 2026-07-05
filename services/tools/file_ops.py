"""
Agent 工具服务 - 文件操作工具
包含 file_read/write/list/delete/edit

支持群共享空间路径 /mnt/group/{group_id}/xxx → feclaw/groups/{gid}/xxx
"""

import asyncio
import logging
from typing import Optional, Tuple

from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase
from models.group import GroupMember
from models.database import SessionLocal

logger = logging.getLogger(__name__)


# 群组别名映射（测试用，每次重测前更新）
# key = 友好名称, value = 真实 UUID
GROUP_ALIASES = {
    "interview": "b20440ba-93f3-4390-864d-78912a607d3b",
}


class FileOpsMixin(AgentToolsServiceBase):
    """文件操作工具 Mixin"""

    # ========== 群共享空间路径解析 ==========

    @staticmethod
    def _is_group_path(path: str) -> bool:
        """检查路径是否为群共享空间路径 /mnt/group/{gid}/..."""
        return bool(path) and path.startswith("/mnt/group/")

    @staticmethod
    def _resolve_group_path(path: str) -> Tuple[Optional[str], Optional[str]]:
        """
        解析群共享空间路径到 COS key（工具层解析，VFS 不感知）

        /mnt/group/{gid}/xxx → feclaw/groups/{gid}/xxx
        /mnt/group/{gid}/    → feclaw/groups/{gid}/         (目录前缀)
        /mnt/group/{alias}/xxx → 自动按 GROUP_ALIASES 替换为真实 UUID

        Returns:
            (cos_key, error_msg)
        """
        if not path.startswith("/mnt/group/"):
            return (None, f"Error: 不是群共享空间路径: {path}")
        parts = path.strip("/").split("/")
        # parts = ["mnt", "group", "{gid}", ...]
        if len(parts) < 3 or not parts[2]:
            return (None, "Error: /mnt/group/ 路径必须包含 group_id，格式为 /mnt/group/{group_id}/...")
        if parts[2] in GROUP_ALIASES:
            parts[2] = GROUP_ALIASES[parts[2]]
        gid = parts[2]
        if ".." in gid or "/" in gid or not gid.strip():
            return (None, "Error: group_id 非法")
        rest = "/".join(parts[3:])
        if ".." in rest:
            return (None, "Error: 路径不允许 .. 穿越")
        if rest:
            return (f"feclaw/groups/{gid}/{rest}", None)
        return (f"feclaw/groups/{gid}/", None)

    def _resolve_path(self, path: str) -> Tuple[Optional[str], Optional[str]]:
        """
        工具层统一路径解析（V2 群共享空间支持）

        /mnt/group/{gid}/... → feclaw/groups/{gid}/...
        其他路径走 VFS _resolve_path
        """
        if self._is_group_path(path):
            return self._resolve_group_path(path)
        return self._vfs._resolve_path(path)

    def _check_group_access(self, group_id: str) -> Optional[str]:
        """检查当前 Agent 是否是指定群的成员

        Returns:
            None if allowed, error string if denied
        """
        if not self.agent_hash:
            return "Error: 无法获取 Agent 身份"
        try:
            db = SessionLocal()
            try:
                member = db.query(GroupMember).filter(
                    GroupMember.group_id == group_id,
                    GroupMember.agent_hash == self.agent_hash,
                ).first()
                if not member:
                    return f"Error: 你不是群 {group_id} 的成员，无权访问群共享空间"
                return None
            finally:
                db.close()
        except Exception as e:
            return f"Error: 检查群成员失败: {e}"

    # ========== 文件工具 ==========

    @tool(description="读取文件。path 用绝对路径（以 / 开头），如 /workspace/soul.md 或 /mnt/group/{group_id}/shared.md。⚠️ 必须使用用户消息中「图片路径:」标注的路径，禁止自己构造路径。", category="file")
    async def file_read(self, path: str) -> str:
        """从 COS 读取文件"""
        # 群共享空间：跳过 agent 权限检查（group 内共享）
        if not self._is_group_path(path):
            if not self._check_read(path):
                current_perm = self._perm_service.get_permission(path)
                return f"Error: 无读权限 (当前权限: {current_perm}): {path}"

        # 群共享空间路径
        if self._is_group_path(path):
            parts = path.strip("/").split("/")
            gid = parts[2]
            access_err = self._check_group_access(gid)
            if access_err:
                return access_err
            cos_key, err = self._resolve_group_path(path)
            if err:
                return err
            try:
                content_bytes = await self.storage.get_file_content_async(cos_key.rstrip("/"))
                if content_bytes is None:
                    return f"Error: 文件不存在或无法读取: {path}"
                return content_bytes.decode("utf-8", errors="replace")
            except Exception as e:
                return f"Error: 读取群共享文件失败: {e}"

        result = await self._vfs.async_cat(path)
        if result.startswith("Error:"):
            # 提示模型检查用户消息中的图片路径标注
            result += (
                f"\n\n💡 提示：如果这是图片文件，请检查用户消息中「图片路径:」后面标注的路径。"
                f"不要自己构造或猜测路径（如 current_image.png），使用用户消息中标注的路径重新读取。"
            )
        return result

    @tool(description="写入内容到文件（覆盖写）。path 用绝对路径（以 / 开头），如 /workspace/notes.txt 或 /mnt/group/{group_id}/shared.md。注意：会完全覆盖文件原有内容，不确定文件是否存在时请先用 file_read 查看。如果是修改性的编辑操作，建议使用 Edit 工具，更加方便且精确。", category="file")
    async def file_write(self, path: str, content: str) -> str:
        """写入文件到 COS"""
        # 群共享空间：跳过 agent 权限检查
        if not self._is_group_path(path):
            if not self._check_write(path):
                current_perm = self._perm_service.get_permission(path)
                return f"Error: 无写权限 (当前权限: {current_perm}): {path}"

        # 群共享空间路径：直写存储（不走 VFS echo，避免触发 agent 索引）
        if self._is_group_path(path):
            parts = path.strip("/").split("/")
            gid = parts[2]
            access_err = self._check_group_access(gid)
            if access_err:
                return access_err
            cos_key, err = self._resolve_group_path(path)
            if err:
                return err
            try:
                content_bytes = content.encode("utf-8") if isinstance(content, str) else content
                await self.storage.put_object_async(cos_key.rstrip("/"), content_bytes)
                return f"OK: 已写入 {path}"
            except Exception as e:
                return f"Error: 写入群共享文件失败: {e}"

        # 走 VFS echo 写入，确保版本控制、自动索引等钩子生效
        return await self._vfs.async_write(path, content)

    @tool(description="列出目录下的文件。dir 用绝对路径（以 / 开头），如 /workspace 或 /mnt/group/{group_id}/。返回的路径也是绝对路径。", category="file")
    async def file_list(self, dir: str = "") -> str:
        """列出目录下的文件"""
        if ".." in dir:
            return "Error: 路径不允许 .."

        # 群共享空间路径
        if self._is_group_path(dir):
            parts = dir.strip("/").split("/")
            gid = parts[2]
            access_err = self._check_group_access(gid)
            if access_err:
                return access_err
            cos_prefix, err = self._resolve_group_path(dir)
            if err:
                return err
            # 确保目录前缀以 / 结尾
            if not cos_prefix.endswith("/"):
                cos_prefix += "/"
        else:
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

    @tool(description="删除文件（禁止删除 agent/ 目录）。path 用绝对路径（以 / 开头），如 /workspace/x.txt 或 /mnt/group/{group_id}/x.txt。", category="file")
    async def file_delete(self, path: str) -> str:
        """从 COS 删除文件，禁止删除 agent/ 目录"""
        # 群共享空间：跳过 agent 权限检查
        if not self._is_group_path(path):
            if not self._check_write(path):
                current_perm = self._perm_service.get_permission(path)
                return f"Error: 无写权限 (当前权限: {current_perm}): {path}"

        # 群共享空间：检查当前 Agent 是否是群成员
        if self._is_group_path(path):
            parts = path.strip("/").split("/")
            gid = parts[2]
            access_err = self._check_group_access(gid)
            if access_err:
                return access_err

        key, err = self._resolve_path(path)
        if err:
            return err

        if path.startswith("agent/") or "/agent/" in path:
            return "Error: 禁止删除 agent/ 目录"

        try:
            await self.storage.delete_file_by_key_async(key)
            return f"OK: 已删除 {path}"
        except Exception as e:
            return f"Error: 删除失败: {e}"

    @tool(description="对文件进行精确的字符串替换修改。将 old_string 替换为 new_string。注意：old_string 在文件中必须唯一出现一次，否则拒绝替换。适用于修改参数、常量、函数调用等。对于简单修改比 file_write 更安全。支持 /workspace/ 和 /mnt/group/{group_id}/ 路径。", category="file")
    async def edit(self, path: str, old_string: str, new_string: str) -> str:
        """
        对文件进行精确的字符串替换修改。
        找到第一个匹配的 old_string 并替换为 new_string。
        """
        try:
            # 群共享空间路径：直读直写
            if self._is_group_path(path):
                cos_key, err = self._resolve_group_path(path)
                if err:
                    return err
                content_bytes = await self.storage.get_file_content_async(cos_key.rstrip("/"))
                if content_bytes is None:
                    return f"Error: 文件不存在: {path}"
                content = content_bytes.decode("utf-8", errors="replace")
            else:
                content = await self._vfs.async_read_file(path)
                if content.startswith("Error:"):
                    return content

            count = content.count(old_string)
            if count == 0:
                return f"Error: 在文件中未找到匹配的字符串"

            if count > 1:
                return (
                    f"Error: 目标字符串在文件中出现了 {count} 次，"
                    f"无法确定替换哪一个。请先用旧字符串的更多上下文来唯一匹配，"
                    f"或者使用 file_write 覆盖整个文件。"
                )

            new_content = content.replace(old_string, new_string)

            # 群共享空间路径：直写存储
            if self._is_group_path(path):
                cos_key, _ = self._resolve_group_path(path)
                await self.storage.put_object_async(cos_key.rstrip("/"), new_content.encode("utf-8"))
                return f"OK: 已编辑 {path}"

            # 走 VFS echo 写入，触发版本控制 + 自动索引
            return await self._vfs.async_write(path, new_content)
        except Exception as e:
            return f"Error: 修改失败: {e}"
