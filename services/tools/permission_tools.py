"""
Permission Tools Mixin — request_permission 工具（P3）

Agent 想修改群共享文件（.share/ 活文档）时，不能直接调 file_write / edit。
必须通过本工具申请审批。

设计：
- action: 'file_write' | 'edit'（与现有工具同名，审批通过后直接路由执行）
- intent: 人类可读的解释
- target: 'group:{id}' | 'user:{id}' —— 审批卡发到哪里
- arguments: 待执行的参数（与对应工具一致）

IRQ 反馈（动态 action）：
  irq.permission.{action}.granted  → 文件已修改
  irq.permission.{action}.denied   → 人拒绝
  irq.permission.{action}.conflict → 文件被改 / 执行失败
  irq.permission.{action}.expired  → 24h 超时

Agent 收到这些 IRQ 后可在下一轮对话中得知结果。
"""

import asyncio
import logging
from typing import Optional

from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase
from services.approval_service import (
    ApprovalService,
    ApprovalAction,
    is_group_shared_path,
    get_group_shared_scope,
)
from services.vfs.paths import (
    GROUP_ATTACH_DIR,
    GROUP_REF_DIR,
    GROUP_SHARE_DIR,
)

logger = logging.getLogger(__name__)


class PermissionToolsMixin(AgentToolsServiceBase):
    """审批工具 Mixin"""

    @tool(
        description=(
            "申请修改群共享文件（.share/ 活文档）。Agent 在群里想改共享文件时，必须先调本工具申请，"
            "等群主审批后才能写入。审批通过后系统自动执行，无需 Agent 再调用 file_write / edit。"
            "对 .ref/ 和 .attach/ 的申请会直接被拒（这两类永远只读）。"
        ),
        category="file",
    )
    async def request_permission(
        self,
        action: str,
        intent: str,
        target: str,
        arguments: dict,
    ) -> str:
        """
        :param action: 要执行的操作名。'file_write' 或 'edit'（与现有工具一致）。
        :param intent: 人类可读的解释，如「想把活动方案日期从7.25改成7.26」。
        :param target: 审批卡发到哪里。'group:{group_id}'（群内审批）或 'user:{user_id}'（私信审批）。
        :param arguments: 待执行的参数。file_write 需 path/content；edit 需 path/old_string/new_string。
        """
        # 0. 基本校验
        if action not in (ApprovalAction.FILE_WRITE.value, ApprovalAction.EDIT.value):
            return (
                f"Error: 不支持的 action {action!r}。"
                f"目前仅支持 {ApprovalAction.FILE_WRITE.value} / {ApprovalAction.EDIT.value}"
            )
        if not intent or not intent.strip():
            return "Error: intent 不能为空，请告诉用户你为什么要改这个文件"
        if not target or ":" not in target:
            return "Error: target 格式错误，应为 'group:{id}' 或 'user:{id}'"

        # 1. 从 arguments 里取 path，校验它在群共享空间内
        path = arguments.get("path") if isinstance(arguments, dict) else None
        if not path:
            return "Error: arguments.path 必填"

        if not is_group_shared_path(path):
            return (
                f"Error: request_permission 只用于群共享文件路径（/mnt/group/...）。"
                f"路径 {path!r} 不在群共享空间，普通文件请直接用 file_write / edit。"
            )

        scope = get_group_shared_scope(path)
        if scope in (GROUP_REF_DIR, GROUP_ATTACH_DIR):
            return (
                f"Error: {scope}/ 是只读空间，Agent 申请也不批。"
                f"如需更新参考库/附件，请让用户手动操作。"
            )
        if scope != GROUP_SHARE_DIR:
            return (
                f"Error: {scope!r} 不在 .share/.ref/.attach 三层空间内。"
                f"request_permission 仅支持 .share/。"
            )

        # 2. 校验 action 与 arguments 的对应关系
        if action == ApprovalAction.FILE_WRITE.value:
            if "content" not in arguments:
                return "Error: file_write 模式需要 arguments.content"
        elif action == ApprovalAction.EDIT.value:
            if "old_string" not in arguments or "new_string" not in arguments:
                return "Error: edit 模式需要 arguments.old_string 和 arguments.new_string"

        # 3. 解析 COS key
        from services.tools.file_ops import _resolve_group_path  # 延迟导入避免循环
        cos_key, err = _resolve_group_path(path)
        if err:
            return err

        # 4. 读当前文件算 checksum（用于 TOCTOU 防护）
        try:
            current_bytes = await self.storage.get_file_content_async(cos_key.rstrip("/"))
        except Exception as e:
            logger.warning(f"[request_permission] read {path} failed: {e}")
            current_bytes = None
        checksum = ApprovalService.recompute_checksum(current_bytes)

        # 5. 创建审批请求（ApprovalService.create_request 内部已经写聊天流卡片，
        #    不再发 IRQ —— IRQ 是 Agent 内部机制，人看不到）
        req = await ApprovalService.instance().create_request(
            agent_hash=self.agent_hash,
            action=action,
            target=target,
            intent=intent.strip(),
            arguments=dict(arguments),
            file_path=path,
            file_cos_key=cos_key,
            file_scope=scope,
            checksum_at_request=checksum,
        )

        # 6. 返回给 Agent 的人类可读反馈
        return (
            f"OK: 审批请求已创建 (id={req.id})\n"
            f"目标文件: {path}\n"
            f"操作: {action}\n"
            f"理由: {intent}\n"
            f"审批卡已发往 {target}。\n"
            f"等待 24 小时内审批结果——审批通过后系统会自动执行修改，"
            f"你会在下一轮收到 IRQ(granted/denied/conflict/expired) 通知。"
        )

    @staticmethod
    def _execute_approved(req) -> str:
        """审批通过时由 ApprovalService.grant() 调用：执行被请求的操作。

        返回 'OK: ...' 或 'Error: ...'。
        """
        from services.tools.file_ops import _resolve_group_path
        import asyncio
        from services.file_storage import create_file_storage

        # 1. TOCTOU：重算 checksum，对比 request 时记录的
        storage = create_file_storage()
        cos_key, err = _resolve_group_path(req.file_path)
        if err:
            return f"Error: {err}"
        try:
            loop = asyncio.get_event_loop()
            current = loop.run_until_complete(
                storage.get_file_content_async(cos_key.rstrip("/"))
            )
        except Exception as e:
            return f"Error: 读文件失败: {e}"
        current_checksum = ApprovalService.recompute_checksum(current)
        if current_checksum != req.checksum_at_request:
            return (
                f"Error: 文件已被他人修改（checksum 不一致）。"
                f"请重新读取后再提议修改。"
            )

        # 2. 执行
        try:
            if req.action == ApprovalAction.FILE_WRITE.value:
                content = req.arguments.get("content", "")
                content_bytes = (
                    content.encode("utf-8") if isinstance(content, str) else content
                )
                loop.run_until_complete(
                    storage.put_object_async(cos_key.rstrip("/"), content_bytes)
                )
                return f"OK: 已写入 {req.file_path}"
            if req.action == ApprovalAction.EDIT.value:
                if current is None:
                    return f"Error: 文件不存在: {req.file_path}"
                old_string = req.arguments.get("old_string", "")
                new_string = req.arguments.get("new_string", "")
                text = current.decode("utf-8", errors="replace")
                if old_string not in text:
                    return "Error: 未找到 old_string"
                count = text.count(old_string)
                if count > 1:
                    return f"Error: old_string 出现 {count} 次"
                new_text = text.replace(old_string, new_string)
                loop.run_until_complete(
                    storage.put_object_async(
                        cos_key.rstrip("/"), new_text.encode("utf-8")
                    )
                )
                return f"OK: 已编辑 {req.file_path}"
        except Exception as e:
            return f"Error: 执行失败: {e}"
        return f"Error: 未知 action {req.action!r}"


# 给 ApprovalService.grant 提供一个便利的 executor 函数
async def grant_and_execute(request_id: str, decided_by: str):
    """审批通过 + 执行。返回 ApprovalRequest。"""
    req = ApprovalService.instance().get(request_id)
    if req is None:
        raise KeyError(f"request {request_id} not found")
    return await ApprovalService.instance().grant(
        request_id=request_id,
        decided_by=decided_by,
        executor=PermissionToolsMixin._execute_approved,
    )


__all__ = [
    "PermissionToolsMixin",
    "grant_and_execute",
]