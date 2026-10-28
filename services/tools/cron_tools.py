"""
Agent 工具服务 - 定时任务/提醒工具
包含 create_cron_job（标准名）, list_reminders, cancel_reminder 等

T03a：提醒工具历史上被叫成两个名字：
  • registry 里注册的真实工具名 = `create_cron_job`（本文件里的**唯一**实现）
  • 默认工具配置 / 提示词 / 前端工具目录里写的是旧名 `schedule_reminder`

后果是「看起来能用、实际打不到」：模型被告知有旧名，但 TOOL_REGISTRY 里没有它
⇒ 未知工具；老配置把旧名写进 enabled 也永不生效。

修复原则（规格 §5 T03 第 3 条）：
  1. 标准名 = `create_cron_job`；默认配置/UI/提示词一律用标准名。
  2. 旧名只作为 **registry 别名**存在（`services/tool_registry.py` 的
     TOOL_NAME_ALIASES，唯一权威），不在这里再注册一个 @tool，也不复制业务逻辑。
  3. 名字归一化只有一个实现：`tool_registry.normalize_tool_name()`。
     本文件的 `reminder_tool_allowed()` 用它判断启用/禁用，因此
     「禁掉旧名」与「禁掉标准名」不能互相绕过。
"""

import json
import re
import logging
from datetime import datetime
from typing import Iterable, Optional

from services.tool_registry import tool, normalize_tool_name
from services.tools.base import AgentToolsServiceBase
from services.scheduler_service import (
    create_reminder,
    list_pending_tasks,
    cancel_task as scheduler_cancel_task,
)

logger = logging.getLogger(__name__)

# 提醒工具的标准名（旧名只在 tool_registry 的兼容映射里）
REMINDER_STANDARD_NAME = "create_cron_job"


def reminder_tool_allowed(enabled: Optional[Iterable[str]],
                          disabled: Optional[Iterable[str]] = None) -> bool:
    """提醒能力的启用/禁用判断 —— 名字统一经 registry 归一化。

    Args:
        enabled: AgentConfig `agents/{hash}/tools` 的 enabled 列表；
                 None 表示「无配置，不过滤」（与 AgentExecutor._get_enabled_tools 一致）。
        disabled: 同一配置里的 disabled 列表。显式 disabled 优先 ——
                 旧用户把旧名放进 disabled 的语义必须继续生效，
                 且不能靠改叫标准名绕过去。

    Returns:
        True 表示允许调用；False 表示被禁用。
    """
    canonical = normalize_tool_name(REMINDER_STANDARD_NAME)
    if disabled and any(normalize_tool_name(n) == canonical for n in disabled):
        return False
    if enabled is None:
        return True
    return any(normalize_tool_name(n) == canonical for n in enabled)


class CronToolsMixin(AgentToolsServiceBase):
    """定时任务/提醒工具 Mixin"""

    # ========== 提醒能力开关（name normalization 统一入口） ==========

    def _tool_config(self) -> dict:
        """读当前 Agent 的 tools 配置；无配置/读失败返回 {}（不过滤）。"""
        agent_hash = getattr(self, "agent_hash", None)
        if not agent_hash:
            return {}
        try:
            from models.database import AgentConfig, SessionLocal
            db = SessionLocal()
            try:
                row = db.query(AgentConfig).filter(
                    AgentConfig.agent_hash == agent_hash,
                    AgentConfig.key == f"agents/{agent_hash}/tools",
                ).first()
            finally:
                db.close()
            if not row or not row.value:
                return {}
            parsed = json.loads(row.value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception as e:  # noqa: BLE001
            # 读失败不在这里单方面放开/收紧（与 executor 的容错语义保持一致），
            # 但要留痕，避免「配置坏了却静默全开」无人知道。
            logger.warning(
                f"[cron_tools] 读取 tools 配置失败 agent={agent_hash}: {e}；"
                f"本次不额外过滤提醒工具"
            )
            return {}

    def _reminder_disabled_error(self) -> Optional[str]:
        """提醒能力被禁用时返回统一的拒绝消息（别名与标准名走同一条判断）。"""
        cfg = self._tool_config()
        if reminder_tool_allowed(cfg.get("enabled", None), cfg.get("disabled", None)):
            return None
        return (
            f"Error: 提醒/定时任务工具已被禁用（{REMINDER_STANDARD_NAME}）；"
            f"历史别名与标准名同样不可用，如需使用请先在设置中启用该工具。"
        )

    # ========== 标准名工具 ==========

    @tool(description="""创建定时/周期性任务（提醒的标准工具名）。
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
        disabled = self._reminder_disabled_error()
        if disabled:
            return disabled

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

    # 说明：这里**不再**定义旧名的第二个 @tool 或第二个方法。
    # 旧名由 services/tool_registry.py 的 TOOL_NAME_ALIASES 解析成
    # create_cron_job，registry / executor / 本文件的启用判断都走同一个
    # normalize_tool_name()。

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


__all__ = [
    "CronToolsMixin",
    "REMINDER_STANDARD_NAME",
    "reminder_tool_allowed",
]
