"""
Agent 工具服务 - 定时任务/提醒工具
包含 create_cron_job, list_reminders, cancel_reminder 等
"""

import re
import logging
from datetime import datetime

from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase
from services.scheduler_service import (
    create_reminder,
    list_pending_tasks,
    cancel_task as scheduler_cancel_task,
)

logger = logging.getLogger(__name__)


class CronToolsMixin(AgentToolsServiceBase):
    """定时任务/提醒工具 Mixin"""

    @tool(description="""创建定时/周期性任务。
支持单次执行（如 "2025-06-01 08:00"、"in 30 minutes"、"07:30"）和循环执行（cron 表达式如 "0 8 * * *" 每天早上8点）。

参数说明：
- output: 执行结果去哪里。session=发回当前会话，push=直接推送消息（仅当前渠道），file=写入文件
- session_mode: current=在当前会话执行（Agent 看到上下文），new=开新会话干净执行
- pre_generate: 是否预生成回复。none=不预生成，1min/3min=提前预生成

注意：output=push 时仅限当前消息渠道（如飞书→飞书，不可跨渠道）
""", category="agent")
    def create_cron_job(
        self,
        prompt: str,
        schedule: str,
        output: str = "session",
        session_mode: str = "new",
        pre_generate: str = "none",
        file_path: str = None,
    ) -> str:
        """
        Args:
            prompt: 到时间执行的提示词
            schedule: 时间。单次: "YYYY-MM-DD HH:MM" / "in N minutes" / "HH:MM"，循环: cron
            output: "session" | "push" | "file"
            session_mode: "current" | "new"
            pre_generate: "none" | "1min" | "3min"
            file_path: output=file 时必填
        """
        if output == "file" and not file_path:
            return "Error: output=file 时必须提供 file_path"
        if output == "push" and session_mode != "new":
            return "Error: output=push 时 session_mode 必须为 new"

        channel = getattr(self, '_channel', 'web')
        session_id = getattr(self, '_session_id', None)
        if output == "session" and session_mode == "current" and not session_id:
            return "Error: session_mode=current 时需要当前有活跃会话"

        is_cron = bool(re.match(r'^[\d\s\*/,-\?LWC#]+$', schedule.strip())) and len(schedule.split()) >= 5

        # 收集当前会话上下文（给 task 类型用）
        context_messages = self._get_recent_messages() if hasattr(self, '_get_recent_messages') else []

        if is_cron:
            result = create_reminder(
                user_id=str(self.user_id),
                content=prompt,
                scheduled_at=datetime.now(),
                task_type="task",
                agent_hash=getattr(self, '_agent_hash', None),
                cron_expression=schedule,
                output=output,
                session_mode=session_mode,
                pre_generate=pre_generate,
                file_path=file_path,
                context_messages=context_messages or [],
            )
        else:
            result = create_reminder(
                user_id=str(self.user_id),
                content=prompt,
                scheduled_at=schedule,
                task_type="task",
                agent_hash=getattr(self, '_agent_hash', None),
                output=output,
                session_mode=session_mode,
                pre_generate=pre_generate,
                file_path=file_path,
                context_messages=context_messages or [],
            )

        if "error" in result:
            return f"Error: {result['error']}"

        return f"✅ 已创建定时任务，ID={result['id']}，时间={result['scheduled_at']}，output={output}"

    @tool(description="列出用户所有待执行的定时提醒", category="agent")
    def list_reminders(self) -> str:
        """
        列出当前用户所有待执行的提醒

        Returns:
            提醒列表
        """
        try:
            tasks = list_pending_tasks(self.user_id)
            if not tasks:
                return "（无待执行提醒）"

            lines = []
            for task in tasks:
                lines.append(f"[{task['id']}] {task['scheduled_at']} - {task['content']}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: 查询提醒失败: {e}"

    @tool(description="取消一个定时提醒", category="agent")
    def cancel_reminder(self, task_id: str) -> str:
        """
        取消提醒

        Args:
            task_id: 提醒ID

        Returns:
            取消结果
        """
        try:
            task_id_int = int(task_id)
            success = scheduler_cancel_task(task_id_int)
            if success:
                return f"OK: 已取消提醒 ID={task_id}"
            else:
                return f"Error: 提醒不存在或已取消: {task_id}"
        except ValueError:
            return f"Error: 无效的提醒ID: {task_id}"
        except Exception as e:
            return f"Error: 取消提醒失败: {e}"
