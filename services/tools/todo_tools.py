"""
Agent 工具服务 - TODO 管理工具
让 Agent 能在群聊/单聊中管理自己的待办事项。

存储：feclaw/agents/{agent_hash}/todos.json  (通过 FileStorage / COS)
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase

logger = logging.getLogger(__name__)


# TODO 存储路径（每个 Agent 一个 JSON 文件）
TODO_PATH_TEMPLATE = "feclaw/agents/{agent_hash}/todos.json"


# 优先级对应的 emoji
_PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _todo_path(agent_hash: str) -> str:
    return TODO_PATH_TEMPLATE.format(agent_hash=agent_hash)


def _load_todos(storage, agent_hash: str) -> List[Dict[str, Any]]:
    """从 COS 加载 TODO 列表，失败时返回空列表。"""
    path = _todo_path(agent_hash)
    try:
        raw = storage.get_file_content(path)
    except Exception as e:
        logger.warning(f"[TODO] 读取失败 {path}: {e}")
        return []
    if not raw:
        return []
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        data = json.loads(text)
        if isinstance(data, list):
            return data
        logger.warning(f"[TODO] {path} 内容不是 list，重置为空")
        return []
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"[TODO] 解析失败 {path}: {e}")
        return []


def _save_todos(storage, agent_hash: str, todos: List[Dict[str, Any]]) -> None:
    """将 TODO 列表写回 COS。"""
    path = _todo_path(agent_hash)
    payload = json.dumps(todos, ensure_ascii=False, indent=2).encode("utf-8")
    storage.put_object(path, payload)


def _priority_emoji(priority: str) -> str:
    return _PRIORITY_EMOJI.get(priority, "⚪")


class TodoToolsMixin(AgentToolsServiceBase):
    """TODO 管理工具 Mixin"""

    @tool(
        description=(
            "创建一条新的待办事项，保存到 Agent 的 VFS（todos.json）。"
            "Agent 可在群聊中通过 todo_list 跟踪自己的进度。"
            "若 depends_on 给出其它 Agent 的 hash 列表，本任务将被视为依赖前置任务（status=blocked）。"
        ),
        category="agent",
    )
    async def todo_create(
        self,
        title: str,
        description: str = "",
        priority: str = "medium",
        depends_on: Optional[List[str]] = None,
    ) -> str:
        """
        Args:
            title: 待办标题（如 "写技术篇初稿"）
            description: 详细描述
            priority: 优先级 high / medium / low
            depends_on: 前置依赖的 Agent hash 列表（如 ["18a2"]）
        """
        if priority not in _PRIORITY_EMOJI:
            return f"Error: priority 必须是 high/medium/low，收到: {priority!r}"

        deps = list(depends_on or [])
        todos = _load_todos(self.storage, self.agent_hash)

        todo = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "description": description,
            "priority": priority,
            "status": "blocked" if deps else "pending",
            "depends_on": deps,
            "progress": 0,
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "result": "",
        }
        todos.append(todo)
        _save_todos(self.storage, self.agent_hash, todos)

        emoji = _priority_emoji(priority)
        dep_note = f" ⏳依赖:{deps}" if deps else ""
        return f"✅ TODO 已创建: [{todo['id']}] {emoji} {title}{dep_note}"

    @tool(
        description="列出当前所有待办事项（支持按 status 过滤）。",
        category="agent",
    )
    async def todo_list(
        self,
        status: Optional[str] = None,
    ) -> str:
        """
        Args:
            status: 过滤状态 pending / in_progress / completed / blocked；不传则列出全部
        """
        valid_statuses = {"pending", "in_progress", "completed", "blocked"}
        if status and status not in valid_statuses:
            return f"Error: status 必须是 pending/in_progress/completed/blocked，收到: {status!r}"

        todos = _load_todos(self.storage, self.agent_hash)
        if status:
            todos = [t for t in todos if t.get("status") == status]

        if not todos:
            return "📭 没有待办事项"

        # 排序：按优先级（high > medium > low），其次按创建时间
        priority_rank = {"high": 0, "medium": 1, "low": 2}
        todos.sort(
            key=lambda t: (
                priority_rank.get(t.get("priority", "medium"), 1),
                t.get("created_at", ""),
            )
        )

        lines = ["📋 待办事项列表："]
        for t in todos:
            emoji = _priority_emoji(t.get("priority", "medium"))
            deps = t.get("depends_on") or []
            dep_note = f" ⏳依赖:{deps}" if deps else ""
            lines.append(
                f"  [{t['id']}] {emoji} {t['title']} "
                f"({t.get('status', 'pending')}, {t.get('progress', 0)}%){dep_note}"
            )
        return "\n".join(lines)

    @tool(
        description="更新待办事项的状态或进度。",
        category="agent",
    )
    async def todo_update(
        self,
        id: str,
        status: Optional[str] = None,
        progress: Optional[int] = None,
    ) -> str:
        """
        Args:
            id: 待办 ID
            status: 新状态 pending / in_progress / completed / blocked
            progress: 进度百分比 0-100
        """
        valid_statuses = {"pending", "in_progress", "completed", "blocked"}
        if status and status not in valid_statuses:
            return f"Error: status 必须是 pending/in_progress/completed/blocked，收到: {status!r}"
        if progress is not None and not (0 <= progress <= 100):
            return f"Error: progress 必须是 0-100，收到: {progress}"

        todos = _load_todos(self.storage, self.agent_hash)
        target = next((t for t in todos if t.get("id") == id), None)
        if not target:
            return f"⚠️ 未找到 TODO [{id}]"

        if status:
            target["status"] = status
        if progress is not None:
            target["progress"] = progress
        _save_todos(self.storage, self.agent_hash, todos)

        return (
            f"✅ TODO [{id}] 已更新: status={target['status']} "
            f"progress={target['progress']}%"
        )

    @tool(
        description="标记待办事项完成。",
        category="agent",
    )
    async def todo_complete(
        self,
        id: str,
        result_summary: str = "",
    ) -> str:
        """
        Args:
            id: 待办 ID
            result_summary: 完成结果摘要
        """
        todos = _load_todos(self.storage, self.agent_hash)
        target = next((t for t in todos if t.get("id") == id), None)
        if not target:
            return f"⚠️ 未找到 TODO [{id}]"

        target["status"] = "completed"
        target["progress"] = 100
        target["result"] = result_summary
        target["completed_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        _save_todos(self.storage, self.agent_hash, todos)

        suffix = f" — {result_summary}" if result_summary else ""
        return f"✅ TODO [{id}] 已完成: {target['title']}{suffix}"

    # ============================================
    # Coprocessor 工具（Agent V2 - IM Agent 自驱自主）
    # ============================================

    @tool(
        name="coprocessor_add_cron",
        description=(
            "给当前 Agent 的协处理器添加一个定时任务（cron）。"
            "定时器到期时会向 Agent 派发 IRQ_CRON 中断，触发 Agent 重新进入 WORKING 状态。"
            "schedule 语法：'@every 30s' / '@every 5m' / '@every 1h' 或简单 '*/10 * * * *'。"
        ),
        category="agent",
    )
    async def coprocessor_add_cron(
        self,
        schedule: str,
        task_desc: str,
    ) -> str:
        """
        Args:
            schedule: cron 表达式（推荐 @every 30s/5m/1h 格式）
            task_desc: 任务描述（让 Agent 自己知道该做什么）
        """
        from services.interrupt_controller import CoprocessorService
        cron = CoprocessorService.add_cron(self.agent_hash, schedule, task_desc, created_by="agent")
        return (
            f"✅ Cron 已添加 [{cron['id']}]: schedule={schedule}, "
            f"interval={cron['interval_sec']}s, task={task_desc!r}"
        )

    @tool(
        name="coprocessor_remove_cron",
        description="从协处理器删除一个定时任务（按 ID）。",
        category="agent",
    )
    async def coprocessor_remove_cron(self, cron_id: str) -> str:
        from services.interrupt_controller import CoprocessorService
        ok = CoprocessorService.remove_cron(self.agent_hash, cron_id)
        return f"✅ Cron [{cron_id}] 已删除" if ok else f"⚠️ 未找到 cron [{cron_id}]"

    @tool(
        name="coprocessor_add_file_watch",
        description="添加文件监控（基于 VFS 路径 mtime）。文件变化时会派发 IRQ_FILE_CHANGE。",
        category="agent",
    )
    async def coprocessor_add_file_watch(
        self,
        path: str,
        pattern: Optional[str] = None,
    ) -> str:
        """
        Args:
            path: VFS 路径（如 /workspace/foo.md 或 /mnt/group/{gid}/xxx）
            pattern: 可选的内容过滤 pattern（如 "TODO|FAIL"），匹配后才触发
        """
        from services.interrupt_controller import CoprocessorService
        watch = CoprocessorService.add_file_watch(self.agent_hash, path, pattern)
        return f"✅ File watch 已添加 [{watch['id']}]: path={path}"

    @tool(
        name="coprocessor_list",
        description="列出当前 Agent 协处理器的所有定时任务和文件监控。",
        category="agent",
    )
    async def coprocessor_list(self) -> str:
        from services.interrupt_controller import CoprocessorService
        config = CoprocessorService.list_all(self.agent_hash)
        crons = config.get("crons", [])
        watches = config.get("file_watches", [])

        lines = ["📋 协处理器配置："]
        if crons:
            lines.append("⏰ Cron:")
            for c in crons:
                lines.append(
                    f"  [{c['id']}] {c['schedule']} (interval={c.get('interval_sec')}s) "
                    f"by={c.get('created_by', 'agent')}: {c.get('task_desc', '')}"
                )
        else:
            lines.append("⏰ Cron: (无)")
        if watches:
            lines.append("👁️  File watches:")
            for w in watches:
                lines.append(f"  [{w['id']}] {w['path']} (pattern={w.get('pattern')})")
        else:
            lines.append("👁️  File watches: (无)")
        return "\n".join(lines)

    # ============================================
    # Group History Tool
    # ============================================

    @tool(
        name="get_group_history",
        description=(
            "获取群聊历史消息。在需要查看群内其他成员的发言或了解群聊上下文时使用。"
            "返回消息列表，按时间从旧到新排列，包含发送者标识。"
        ),
        category="agent",
    )
    async def get_group_history(
        self,
        group_id: str,
        limit: int = 20,
    ) -> str:
        """
        Args:
            group_id: 群组 ID。如果不确定，可以用 list_groups 工具查看所有群。
            limit: 返回最近 N 条消息（默认 20，最大 50）
        """
        from models.database import SessionLocal
        from models.agent_profile import AgentProfile
        from models.group import GroupMessage, GroupMember

        # Verify agent is member of this group
        db = SessionLocal()
        try:
            member = db.query(GroupMember).filter(
                GroupMember.group_id == group_id,
                GroupMember.agent_hash == self.agent_hash,
            ).first()
            if not member:
                return f"⚠️ 你不在群 {group_id} 中"

            limit = min(max(limit, 1), 50)
            msgs = db.query(GroupMessage).filter(
                GroupMessage.group_id == group_id
            ).order_by(GroupMessage.created_at.desc()).limit(limit).all()
            msgs.reverse()

            # Build sender name map
            hashes = {m.sender_hash for m in msgs if m.sender_hash}
            agents = db.query(AgentProfile.hash, AgentProfile.name).filter(
                AgentProfile.hash.in_(hashes)
            ).all() if hashes else []
            hash_to_name = {h: n for h, n in agents}

            lines = [f"📋 群聊历史（共 {len(msgs)} 条）:"]
            for m in msgs:
                if m.sender_type == "user":
                    sender = "👤 用户"
                elif m.sender_type == "agent":
                    sname = hash_to_name.get(m.sender_hash, m.sender_hash[:6])
                    sender = f"🤖 {sname}"
                else:
                    sender = m.sender_type
                content_short = (m.content or "")[:300]
                lines.append(f"  [{sender}] {content_short}")

            return "\n".join(lines)
        finally:
            db.close()


__all__ = ["TodoToolsMixin"]