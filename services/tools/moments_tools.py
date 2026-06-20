"""
Agent 工具服务 - GroupMoments 工具和自动发布钩子

提供 create_post 工具，以及 file_write / spawn_subagent 的自动发布钩子。
"""

import logging
from typing import Optional, List, Dict

from services.tool_registry import tool
from models.database import SessionLocal

logger = logging.getLogger(__name__)


class MomentsToolsMixin:
    """
    Mixin providing GroupMoments create_post tool and auto-publish hooks.

    Auto-publish hooks:
    - file_write → "file_changed" moment after successful write
    - spawn_subagent → "analysis" moment after subagent completes

    The _group_id must be set on the tools service instance when running in a group context.
    """

    # ========== create_post tool ==========

    @tool(
        description="将内容发布到当前群组的朋友圈/动态中。必须提供标题和内容。发布后会推送到群组成员。",
        category="general",
    )
    def create_post(
        self,
        title: str,
        content: str,
        attachments: Optional[List[dict]] = None,
    ) -> dict:
        """
        Agent 工具：将内容发布到当前群组的朋友圈/动态。

        :param title: 动态标题
        :param content: 动态正文内容
        :param attachments: 附件列表（可选）
        """
        from services.moments_service import moments_service

        group_id = getattr(self, "_group_id", None)
        if not group_id:
            return {"status": "error", "message": "create_post 只能在群组上下文中使用"}

        db = SessionLocal()
        try:
            moment = moments_service.create_moment(
                db=db,
                group_id=group_id,
                agent_hash=self.agent_hash,
                kind="manual",
                title=title,
                content=content,
                attachments=attachments,
            )

            # WS push (fire-and-forget)
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(moments_service.push_moments_event(group_id, moment))
                else:
                    loop.run_until_complete(moments_service.push_moments_event(group_id, moment))
            except Exception as e:
                logger.debug(f"[create_post] WS push failed: {e}")

            return {
                "status": "ok",
                "moment_id": moment.id,
                "message": f"已发布到群组动态",
            }
        finally:
            db.close()

    # ========== Auto-publish: file_write ==========

    async def file_write(self, path: str, content: str) -> str:
        """
        Override file_write to auto-publish a 'file_changed' moment after successful write.
        """
        from services.moments_service import moments_service

        # Call original file_write (super() routes to FileOpsMixin via MRO)
        result = await super().file_write(path, content)

        # Auto-publish if successful and in group context
        group_id = getattr(self, "_group_id", None)
        if group_id and (result.startswith("OK") or "已写入" in result or "written" in result.lower()):
            # Extract filename from path
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            db = SessionLocal()
            try:
                moments_service.auto_publish(
                    db=db,
                    group_id=group_id,
                    agent_hash=self.agent_hash,
                    kind="file_changed",
                    title=f"修改了文件 {filename}",
                    content=f"在群组中更新了文件 {path}",
                )
            except Exception as e:
                logger.debug(f"[file_write] auto-publish failed: {e}")
            finally:
                db.close()

        return result

    # ========== Auto-publish: spawn_subagent ==========

    def spawn_subagent(
        self,
        model: str,
        reasoning_effort: str,
        task: str,
        image_base64: Optional[str] = None,
        image_path: Optional[str] = None,
        custom_system_prompt: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        max_retries: int = 0,
        include_stats: bool = False,
        summarize_output: bool = False,
        preset_role: Optional[str] = None,
    ) -> str:
        """
        Override spawn_subagent to auto-publish an 'analysis' moment after completion.
        """
        from services.moments_service import moments_service

        # Call original spawn_subagent (sync, runs in thread pool)
        result = super().spawn_subagent(
            model=model,
            reasoning_effort=reasoning_effort,
            task=task,
            image_base64=image_base64,
            image_path=image_path,
            custom_system_prompt=custom_system_prompt,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            include_stats=include_stats,
            summarize_output=summarize_output,
            preset_role=preset_role,
        )

        # Auto-publish if in group context and not an error
        group_id = getattr(self, "_group_id", None)
        if group_id and not result.startswith("Error:"):
            db = SessionLocal()
            try:
                # Truncate task description for title
                title = task[:80] + "..." if len(task) > 80 else task
                moments_service.auto_publish(
                    db=db,
                    group_id=group_id,
                    agent_hash=self.agent_hash,
                    kind="analysis",
                    title=f"子任务完成：{title}",
                    content=result[:500] if len(result) > 500 else result,
                )
            except Exception as e:
                logger.debug(f"[spawn_subagent] auto-publish failed: {e}")
            finally:
                db.close()

        return result
