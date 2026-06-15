"""
定时任务服务
提供提醒的创建、查询、取消等功能
"""

import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

from models.database import SessionLocal, ScheduledTask

logger = logging.getLogger(__name__)


# 全局调度器引用（由 main.py 注入）
_scheduler = None


def set_scheduler(scheduler) -> None:
    """设置全局调度器引用"""
    global _scheduler
    _scheduler = scheduler


def schedule_task(task_id: int, run_at: datetime, job_type: str) -> None:
    """为单个任务安排精确的执行时间"""
    if _scheduler and _scheduler.running:
        job_id = f"{job_type}_{task_id}"
        # 移除已存在的job（如果存在）
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)
        _scheduler.add_job(
            _execute_scheduled_task,
            DateTrigger(run_date=run_at),
            id=job_id,
            args=[task_id, job_type],
            replace_existing=True
        )
        logger.info(f"[Scheduler] Added job {job_id} scheduled for {run_at}")
    else:
        logger.warning(f"[Scheduler] Cannot schedule task {task_id}: _scheduler={_scheduler}, running={_scheduler.running if _scheduler else 'N/A'}")


def reschedule_pending_tasks() -> None:
    """在启动时重新调度所有待执行的提醒任务"""
    from datetime import datetime
    db = SessionLocal()
    try:
        now = datetime.now()

        # 1. 重新调度所有未来的任务
        future_tasks = db.query(ScheduledTask).filter(
            ScheduledTask.status == "pending",
            ScheduledTask.scheduled_at > now
        ).all()

        for task in future_tasks:
            if task.task_type == "reminder":
                if task.pre_generate_at:
                    schedule_task(task.id, task.pre_generate_at, "pregenerate")
                schedule_task(task.id, task.scheduled_at, "send")
            elif task.task_type == "task":
                schedule_task(task.id, task.scheduled_at, "task")

        # 2. 检查过去被忽略的任务（极短时间窗口，可能漏执行）
        past_tasks = db.query(ScheduledTask).filter(
            ScheduledTask.status == "pending",
            ScheduledTask.scheduled_at <= now,
            ScheduledTask.scheduled_at > now - timedelta(minutes=30)  # 30分钟内的未完成任务
        ).all()

        for task in past_tasks:
            logger.info(f"[Scheduler] Rescheduling past task {task.id} (scheduled {task.scheduled_at})")
            if task.task_type == "reminder":
                # 立即执行发送（给30秒宽限）
                schedule_task(task.id, now + timedelta(seconds=30), "send")
            elif task.task_type == "task":
                schedule_task(task.id, now + timedelta(seconds=30), "task")

        logger.info(f"[Scheduler] Rescheduled {len(future_tasks)} future tasks and {len(past_tasks)} past tasks")
    finally:
        db.close()


def schedule_cron_task(task_id: int, cron_expression: str, job_type: str) -> None:
    """为周期性任务安排执行时间"""
    if _scheduler and _scheduler.running:
        job_id = f"{job_type}_{task_id}"
        # 移除已存在的job（如果存在）
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)

        # 解析 cron 表达式并添加任务
        try:
            # 解析简单 cron 格式: 分 时 日 月 周
            parts = cron_expression.split()
            if len(parts) >= 5:
                minute, hour, day, month, day_of_week = parts[:5]
                trigger = CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=day_of_week
                )
                _scheduler.add_job(
                    _execute_scheduled_task,
                    trigger,
                    id=job_id,
                    args=[task_id, job_type],
                    replace_existing=True
                )
                logger.info(f"[Scheduler] Scheduled cron task {task_id} with expression {cron_expression}")
        except Exception as e:
            logger.error(f"[Scheduler] Error scheduling cron task {task_id}: {e}")


def execute_scheduled_task(task_id: int, job_type: str) -> None:
    """执行调度的任务（供外部调用）"""
    _execute_scheduled_task(task_id, job_type)


def _execute_scheduled_task(task_id: int, job_type: str):
    """执行调度的任务"""
    if job_type == "pregenerate":
        _do_pregenerate(task_id)
    elif job_type == "send":
        _do_send(task_id)
    elif job_type == "task":
        _do_task(task_id)


def _do_pregenerate(task_id: int):
    """执行预生成任务"""
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return

        # 同步调用异步函数（需要新事件循环因为在 APScheduler 工作线程中）
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            content = loop.run_until_complete(
                generate_reminder_content_async(task.user_id, task.content, task.agent_hash)
            )
        finally:
            loop.close()

        if content:
            task.pre_status = "pre_generated"
            task.pre_generated_content = content
        else:
            task.pre_status = "failed"
        db.commit()
    except Exception as e:
        logger.error(f"[Scheduler] Error pre-generating task {task_id}: {e}")
        try:
            task.pre_status = "failed"
            db.commit()
        except Exception:
            logger.warning(f"[Scheduler] Failed to update pre_status for task {task_id}")
    finally:
        db.close()


def _do_send(task_id: int):
    """执行发送任务"""
    logger.debug(f"[Scheduler] _do_send called for task_id={task_id}")
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return

        # 使用预生成的内容（如果有）
        content = task.pre_generated_content or task.content

        # 获取用户微信绑定并发送（需要新事件循环因为在 APScheduler 工作线程中）
        from services.wechat_service import wechat_service
        binding = wechat_service.get_binding_by_user(int(task.user_id))
        if binding and binding.ilink_user_id:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                success = loop.run_until_complete(
                    wechat_service.send_message(
                        to_user_id=binding.ilink_user_id,
                        text=f"⏰ {content}"
                    )
                )
            finally:
                loop.close()
            if success:
                mark_task_done(task_id)
                logger.info(f"[Scheduler] Sent reminder {task_id} to user {task.user_id}")
            else:
                logger.warning(f"[Scheduler] Failed to send reminder {task_id}")
        else:
            # 无微信绑定，标记完成
            mark_task_done(task_id)
            logger.info(f"[Scheduler] No WeChat binding for user {task.user_id}, marking task {task_id} as done")
    except Exception as e:
        logger.error(f"[Scheduler] Error sending task {task_id}: {e}")
    finally:
        db.close()


def _do_task(task_id: int):
    """执行 task 类型的任务，调用 Agent 执行完整工具流"""
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return

        if task.task_type != "task":
            return

        # 根据 output_mode 和 session_mode 分支
        output_mode = getattr(task, 'output_mode', None) or "session"
        session_mode = getattr(task, 'session_mode', None) or "new"
        file_path = getattr(task, 'file_path', None)

        if output_mode == "file":
            _do_task_and_write_to_file(task, file_path)
        elif output_mode == "push":
            _do_task_and_push(task)
        else:
            # output=session: 发回 source_session
            _do_task_and_send_to_session(task)

    except Exception as e:
        logger.error(f"[Scheduler] Error executing task {task_id}: {e}")
    finally:
        db.close()


def _do_task_and_send_to_session(task):
    """output=session: 执行任务并发送到源会话"""
    task_id = task.id
    # 解析 context_messages（对话上下文）
    context_messages = []
    if task.context_messages:
        try:
            context_messages = json.loads(task.context_messages)
        except json.JSONDecodeError:
            logger.warning(f"[Scheduler] Failed to parse context_messages for task {task_id}")
            context_messages = []

    # 恢复 Agent 状态并执行工具流
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        success = loop.run_until_complete(
            execute_task_tool_flow(task.user_id, task.content, context_messages,
                                    task.agent_hash, task.channel)
        )
        if success:
            mark_task_done(task_id)
            logger.info(f"[Scheduler] Task {task_id} completed successfully (session)")
        else:
            logger.warning(f"[Scheduler] Task {task_id} failed (session)")
    finally:
        loop.close()


def _do_task_and_push(task):
    """output=push: 执行任务并通过渠道推送（仅限创建时的 channel）"""
    task_id = task.id
    # 解析 context_messages
    context_messages = []
    if task.context_messages:
        try:
            context_messages = json.loads(task.context_messages)
        except json.JSONDecodeError:
            pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        success = loop.run_until_complete(
            execute_task_tool_flow(task.user_id, task.content, context_messages,
                                    task.agent_hash, task.channel)
        )
        if success:
            mark_task_done(task_id)
            logger.info(f"[Scheduler] Task {task_id} completed successfully (push)")
        else:
            logger.warning(f"[Scheduler] Task {task_id} failed (push)")
    finally:
        loop.close()


def _do_task_and_write_to_file(task, file_path: str):
    """output=file: 执行任务并将结果写入文件"""
    task_id = task.id
    context_messages = []
    if task.context_messages:
        try:
            context_messages = json.loads(task.context_messages)
        except json.JSONDecodeError:
            pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        success = loop.run_until_complete(
            execute_task_tool_flow(task.user_id, task.content, context_messages,
                                    task.agent_hash, task.channel)
        )
        if success and file_path:
            logger.info(f"[Scheduler] Task {task_id} completed (file={file_path})")
        mark_task_done(task_id)
    finally:
        loop.close()


async def execute_task_tool_flow(user_id: str, task_content: str, context_messages: List[Dict],
                                    agent_hash: str = None, channel: str = "wechat") -> bool:
    """
    执行 task 类型的工具流

    Args:
        user_id: 用户ID
        task_content: 任务内容
        context_messages: 对话上下文列表
        agent_hash: Agent hash（必需）
        channel: 渠道标识

    Returns:
        是否执行成功
    """
    try:
        from services.chat_service import ChatService
        from models.chat_input import ChatInput

        if not agent_hash:
            logger.error(f"[Scheduler] execute_task_tool_flow: no agent_hash for user {user_id}")
            return False

        chat_service = ChatService(
            agent_hash=agent_hash,
            channel=channel,
            session_id="wechat_main",
        )

        # 注入历史上下文
        if context_messages:
            chat_service.context.history = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in context_messages
            ]
            chat_service._history_loaded_from_session = True

        response_text = ""
        async for event in chat_service.chat(input=ChatInput(text=task_content)):
            if event.type.name == "TEXT":
                response_text += event.content
            elif event.type.name == "DONE":
                break

        logger.info(f"[Scheduler] Task tool flow completed for user {user_id}: {response_text[:100] if response_text else '(empty)'}...")
        return bool(response_text)

    except Exception as e:
        logger.error(f"[Scheduler] Error in task tool flow for user {user_id}: {e}")
        return False


def parse_time_string(time_str: str) -> Optional[datetime]:
    """
    解析时间字符串为 datetime 对象

    支持格式：
    - "HH:MM" -> 今天或明天
    - "YYYY-MM-DD HH:MM" -> 指定日期时间
    - "in N minutes" -> N分钟后

    Args:
        time_str: 时间字符串

    Returns:
        datetime 对象，解析失败返回 None
    """
    time_str = time_str.strip()

    # 匹配 "in N minutes" 格式
    match_relative = re.match(r'^in\s+(\d+)\s+minutes?$', time_str, re.IGNORECASE)
    if match_relative:
        minutes = int(match_relative.group(1))
        return datetime.now() + timedelta(minutes=minutes)

    # 匹配 "YYYY-MM-DD HH:MM" 格式
    match_date = re.match(r'^(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})$', time_str)
    if match_date:
        date_str, hour_str, minute_str = match_date.groups()
        try:
            dt = datetime.strptime(f"{date_str} {hour_str}:{minute_str}", "%Y-%m-%d %H:%M")
            return dt
        except ValueError:
            pass

    # 匹配 "HH:MM" 格式（只有时间）
    match_time = re.match(r'^(\d{2}):(\d{2})$', time_str)
    if match_time:
        hour_str, minute_str = match_time.groups()
        hour = int(hour_str)
        minute = int(minute_str)

        now = datetime.now()
        # 尝试今天
        scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled_today > now:
            return scheduled_today
        # 如果已过今天时刻，设为明天
        return scheduled_today + timedelta(days=1)

    return None


def create_reminder(user_id: str, content: str, scheduled_at, task_type: str = "reminder",
                    agent_hash: str = None,
                    context_messages: Optional[List[Dict]] = None,
                    cron_expression: Optional[str] = None,
                    output: str = "session",
                    session_mode: str = "new",
                    pre_generate: str = "none",
                    file_path: str = None,
                    channel: str = None) -> Dict:
    """
    创建提醒任务或 task 类型任务

    Args:
        user_id: 用户ID
        content: 任务内容
        scheduled_at: 执行时间（datetime 对象或时间字符串）
        task_type: 任务类型 "reminder" 或 "task"
        context_messages: 对话上下文（task 类型需要）
        cron_expression: 周期表达式（可选）
        output: 输出模式 "session" | "push" | "file"
        session_mode: 会话模式 "current" | "new"
        pre_generate: 预生成模式 "none" | "1min" | "3min"
        file_path: output=file 时的目标路径
        channel: 创建时的渠道

    Returns:
        创建的任务信息字典
    """
    # 如果是字符串，解析为 datetime
    if isinstance(scheduled_at, str):
        parsed = parse_time_string(scheduled_at)
        if parsed is None:
            return {"error": f"无法解析时间: {scheduled_at}"}
        scheduled_at = parsed

    db = SessionLocal()
    try:
        # task 类型必须有 context_messages（可为空列表）
        if task_type == "task" and context_messages is None:
            return {"error": "task 类型必须提供 context_messages"}

        task = ScheduledTask(
            user_id=user_id,
            agent_hash=agent_hash or "",
            task_type=task_type,
            content=content,
            scheduled_at=scheduled_at,
            status="pending",
            context_messages=json.dumps(context_messages) if context_messages else None,
            cron_expression=cron_expression,
            pre_status="pending",
            output_mode=output,
            session_mode=session_mode,
            pre_generate=pre_generate,
            file_path=file_path,
            channel=channel,
        )

        # 如果是 reminder 类型，计算预生成时间
        if task_type == "reminder":
            pre_generate_at = scheduled_at - timedelta(minutes=1)
            now = datetime.now()
            if pre_generate_at < now:
                pre_generate_at = now + timedelta(seconds=30)
            task.pre_generate_at = pre_generate_at

        db.add(task)
        db.commit()
        db.refresh(task)

        # 调度任务
        if task_type == "reminder":
            schedule_task(task.id, task.pre_generate_at, "pregenerate")
            schedule_task(task.id, scheduled_at, "send")
        elif task_type == "task":
            if cron_expression:
                # 周期性任务
                schedule_cron_task(task.id, cron_expression, "task")
            else:
                # 单次任务
                schedule_task(task.id, scheduled_at, "task")

        return {
            "id": task.id,
            "user_id": task.user_id,
            "task_type": task.task_type,
            "content": task.content,
            "scheduled_at": task.scheduled_at.isoformat(),
            "pre_generate_at": task.pre_generate_at.isoformat() if task.pre_generate_at else None,
            "context_messages": context_messages,
            "cron_expression": cron_expression,
            "output_mode": output,
            "session_mode": session_mode,
            "pre_generate": pre_generate,
            "file_path": file_path,
            "channel": channel,
            "status": task.status,
            "pre_status": task.pre_status,
            "created_at": task.created_at.isoformat()
        }
    finally:
        db.close()


def list_pending_tasks(user_id: str) -> List[Dict]:
    """
    获取用户所有待执行的提醒任务

    Args:
        user_id: 用户ID

    Returns:
        任务列表
    """
    db = SessionLocal()
    try:
        tasks = db.query(ScheduledTask).filter(
            ScheduledTask.user_id == user_id,
            ScheduledTask.status == "pending"
        ).order_by(ScheduledTask.scheduled_at.asc()).all()

        return [
            {
                "id": task.id,
                "user_id": task.user_id,
                "task_type": task.task_type,
                "content": task.content,
                "scheduled_at": task.scheduled_at.isoformat(),
                "pre_generate_at": task.pre_generate_at.isoformat() if task.pre_generate_at else None,
                "pre_generated_content": task.pre_generated_content,
                "status": task.status,
                "pre_status": task.pre_status,
                "created_at": task.created_at.isoformat()
            }
            for task in tasks
        ]
    finally:
        db.close()


def cancel_task(task_id: int) -> bool:
    """
    取消任务

    Args:
        task_id: 任务ID

    Returns:
        是否成功取消
    """
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return False

        task.status = "cancelled"
        db.commit()

        # 移除已调度的jobs
        if _scheduler and _scheduler.running:
            pregenerate_job_id = f"pregenerate_{task_id}"
            send_job_id = f"send_{task_id}"
            if _scheduler.get_job(pregenerate_job_id):
                _scheduler.remove_job(pregenerate_job_id)
            if _scheduler.get_job(send_job_id):
                _scheduler.remove_job(send_job_id)

        return True
    finally:
        db.close()


def get_due_tasks() -> List[Dict]:
    """
    获取所有已到期但未执行的任务（fallback模式，排除已预生成的任务）

    Returns:
        到期任务列表
    """
    db = SessionLocal()
    try:
        now = datetime.now()
        tasks = db.query(ScheduledTask).filter(
            ScheduledTask.status == "pending",
            ScheduledTask.scheduled_at <= now,
            # 排除已成功预生成的任务（那些由 get_pregenerated_tasks 处理）
            (ScheduledTask.pre_status != "pre_generated") | (ScheduledTask.pre_generated_content.is_(None))
        ).all()

        return [
            {
                "id": task.id,
                "user_id": task.user_id,
                "task_type": task.task_type,
                "content": task.content,
                "scheduled_at": task.scheduled_at.isoformat(),
                "status": task.status,
                "pre_status": task.pre_status,
                "created_at": task.created_at.isoformat()
            }
            for task in tasks
        ]
    finally:
        db.close()


def mark_task_done(task_id: int) -> bool:
    """
    将任务标记为已完成

    Args:
        task_id: 任务ID

    Returns:
        是否成功标记
    """
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return False

        task.status = "done"
        db.commit()
        return True
    finally:
        db.close()


def get_tasks_for_pregeneration() -> List[Dict]:
    """
    获取需要预生成内容的任务
    条件：pre_generate_at <= now 且 status == pending 且 pre_status == pending

    Returns:
        需要预生成的任务列表
    """
    db = SessionLocal()
    try:
        now = datetime.now()
        tasks = db.query(ScheduledTask).filter(
            ScheduledTask.pre_status == "pending",
            ScheduledTask.status == "pending",
            ScheduledTask.pre_generate_at <= now
        ).all()

        return [
            {
                "id": task.id,
                "user_id": task.user_id,
                "task_type": task.task_type,
                "content": task.content,
                "scheduled_at": task.scheduled_at.isoformat(),
                "pre_generate_at": task.pre_generate_at.isoformat() if task.pre_generate_at else None,
                "status": task.status,
                "pre_status": task.pre_status,
                "created_at": task.created_at.isoformat(),
                "agent_hash": task.agent_hash or "",
            }
            for task in tasks
        ]
    finally:
        db.close()


def get_pregenerated_tasks() -> List[Dict]:
    """
    获取已预生成但未发送的任务
    条件：scheduled_at <= now 且 status == pending 且 pre_status == pre_generated 且有 pre_generated_content

    Returns:
        已预生成待发送的任务列表
    """
    db = SessionLocal()
    try:
        now = datetime.now()
        tasks = db.query(ScheduledTask).filter(
            ScheduledTask.pre_status == "pre_generated",
            ScheduledTask.status == "pending",
            ScheduledTask.scheduled_at <= now,
            ScheduledTask.pre_generated_content.isnot(None)
        ).all()

        return [
            {
                "id": task.id,
                "user_id": task.user_id,
                "task_type": task.task_type,
                "content": task.content,
                "pre_generated_content": task.pre_generated_content,
                "scheduled_at": task.scheduled_at.isoformat(),
                "status": task.status,
                "pre_status": task.pre_status,
                "created_at": task.created_at.isoformat()
            }
            for task in tasks
        ]
    finally:
        db.close()


def mark_task_pregenerating(task_id: int) -> bool:
    """标记任务正在预生成"""
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return False
        task.pre_status = "pre_generating"
        db.commit()
        return True
    finally:
        db.close()


def mark_task_pregenerated(task_id: int, content: str) -> bool:
    """标记任务预生成完成并存储生成的内容"""
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return False
        task.pre_status = "pre_generated"
        task.pre_generated_content = content
        db.commit()
        return True
    finally:
        db.close()


def mark_task_pregenerate_failed(task_id: int) -> bool:
    """标记任务预生成失败"""
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return False
        task.pre_status = "failed"
        db.commit()
        return True
    finally:
        db.close()


def mark_task_sent(task_id: int) -> bool:
    """标记任务已发送"""
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return False
        task.status = "sent"
        db.commit()
        return True
    finally:
        db.close()


async def generate_reminder_content_async(user_id: str, task_content: str, agent_hash: str = None) -> str:
    """
    调用 ChatService 生成提醒回复内容

    Args:
        user_id: 用户ID
        task_content: 原始提醒内容
        agent_hash: Agent hash（可选，通过 ChatService 生成更个性化的回复）

    Returns:
        生成的回复内容，如果失败返回 None
    """
    try:
        from services.chat_service import ChatService
        from models.chat_input import ChatInput

        if agent_hash:
            chat_service = ChatService(
                agent_hash=agent_hash,
                channel="wechat",
            )
            response_text = ""
            async for event in chat_service.chat(input=ChatInput(text=task_content)):
                if event.type.name == "TEXT":
                    response_text += event.content
                elif event.type.name == "DONE":
                    break
            if response_text:
                return response_text.strip()
            return None
        else:
            # Fallback: 直接使用 LLM（无 agent_hash）
            messages = [
                {
                    "role": "system",
                    "content": f"""你是一个友好的助手，正在为用户生成定时提醒的回复内容。
用户设置了一个提醒：「{task_content}」
请生成一个温馨、友好的提醒回复，要求：
1. 纯文本格式，不超过100字
2. 语言自然亲切
3. 直接给出提醒内容，不要有多余的解释
4. 如果提醒内容是问题，适当扩展但不啰嗦"""
                }
            ]
            from services.llm_service import llm_service
            response_text = ""
            async for chunk in llm_service.chat(messages=messages, stream=False):
                response_text += chunk
            if response_text:
                return response_text.strip()
            return None
    except Exception as e:
        logger.error(f"[Scheduler] Reminder generation failed for user {user_id}: {e}")
        return None


async def check_and_pregenerate_reminders() -> None:
    """
    检查需要预生成的任务并调用 Agent 生成内容
    由调度器每 60 秒调用
    """
    logger.info("[Scheduler] Checking tasks for pre-generation...")
    try:
        tasks = get_tasks_for_pregeneration()
        if not tasks:
            return

        logger.info(f"[Scheduler] Found {len(tasks)} tasks needing pre-generation")

        for task in tasks:
            try:
                task_id = task["id"]
                user_id = task["user_id"]
                content = task["content"]

                # 标记为正在预生成
                mark_task_pregenerating(task_id)
                logger.info(f"[Scheduler] Pre-generating content for task {task_id}")

                generated_content = await generate_reminder_content_async(user_id, content, task.get("agent_hash"))

                if generated_content:
                    mark_task_pregenerated(task_id, generated_content)
                    logger.info(f"[Scheduler] Pre-generated content for task {task_id}: {generated_content[:50]}...")
                else:
                    # 预生成失败，标记为 failed
                    mark_task_pregenerate_failed(task_id)
                    logger.warning(f"[Scheduler] Pre-generation failed for task {task_id}, will use fallback")

            except Exception as e:
                logger.error(f"[Scheduler] Error pre-generating task {task['id']}: {e}")
                mark_task_pregenerate_failed(task['id'])

        logger.info(f"[Scheduler] Pre-generation check completed for {len(tasks)} tasks")
    except Exception as e:
        logger.error(f"[Scheduler] Error in check_and_pregenerate_reminders: {e}")