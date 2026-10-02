"""
Approval Service — Agent 文件修改审批（P3）

设计哲学：Agent 是参谋不是操作员。
- 群共享文件（.share/ 活文档）需要人审批才能改
- 参考库（.ref/）和附件（.attach/）一律只读，Agent 申请也不批
- 审批即执行：审批通过后系统直接调用 file_write / edit 执行被请求的操作
- TOCTOU 防护：审批前记录目标文件 checksum，审批后重算对比

IRQ 命名规约（动态 action）：
  irq.permission.{action}.granted
  irq.permission.{action}.denied
  irq.permission.{action}.conflict
  irq.permission.{action}.expired

注意：本服务与 services.permission_service.PermissionService（基于 DB 的文件权限管理）
是不同概念 —— 那个管"Agent 能否读写某路径"，这个管"Agent 写共享文件需不需要人批"。
"""
import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ========== IRQ 命名（动态 action） ==========

def permission_irq_type(action: str, outcome: str) -> str:
    """构造 IRQ 类型字符串。
    例：permission_irq_type('file_write', 'granted') → 'irq.permission.file_write.granted'
    """
    return f"irq.permission.{action}.{outcome}"


# ========== 审批动作枚举 ==========

class ApprovalAction(str, Enum):
    """P3 支持的审批动作（与现有工具名一致）"""
    FILE_WRITE = "file_write"
    EDIT = "edit"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    CONFLICT = "conflict"  # TOCTOU: 文件被改了，或执行失败
    EXPIRED = "expired"


# ========== 数据模型 ==========

@dataclass
class ApprovalRequest:
    """一次审批请求"""
    id: str
    agent_hash: str                          # 申请者
    action: str                              # 'file_write' | 'edit'
    target: str                              # 审批卡发到哪里：'group:{id}' | 'user:{id}'
    intent: str                              # 人类可读的解释
    arguments: Dict[str, Any]                # 待执行的参数
    file_path: str                           # 目标文件 VFS 路径（/mnt/group/1/.share/foo.md）
    file_cos_key: str                        # COS key（feclaw/groups/1/.share/foo.md）
    file_scope: str                          # '.share' | '.ref' | '.attach'
    checksum_at_request: str                 # 申请时的 sha256（hex）；空字符串表示文件不存在
    status: str = ApprovalStatus.PENDING
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 86400)  # 24h
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None         # user_id 或 'auto'
    error: Optional[str] = None              # 执行失败原因

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent_hash": self.agent_hash,
            "action": self.action,
            "target": self.target,
            "intent": self.intent,
            "arguments": self.arguments,
            "file_path": self.file_path,
            "file_cos_key": self.file_cos_key,
            "file_scope": self.file_scope,
            "checksum_at_request": self.checksum_at_request,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "error": self.error,
        }


# ========== ApprovalService 单例 ==========

class ApprovalService:
    """
    进程内单例。维护所有 pending 审批请求。
    未来多 Worker 部署时迁移到 Redis（todo）。
    """
    _instance: Optional["ApprovalService"] = None

    def __init__(self):
        self._requests: Dict[str, ApprovalRequest] = {}
        self._by_agent: Dict[str, List[str]] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    @classmethod
    def instance(cls) -> "ApprovalService":
        if cls._instance is None:
            cls._instance = ApprovalService()
        return cls._instance

    def start_cleanup_loop(self) -> None:
        """启动后台清理过期请求的循环。幂等。"""
        if self._cleanup_task is None or self._cleanup_task.done():
            try:
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
                logger.info("[ApprovalService] cleanup loop started")
            except RuntimeError as e:
                # 没有运行中的事件循环（导入阶段）——延后到第一次 create 时启动
                logger.debug(f"[ApprovalService] start_cleanup_loop deferred: {e}")

    async def _cleanup_loop(self) -> None:
        """每小时清理一次过期请求。"""
        try:
            while True:
                await asyncio.sleep(3600)
                try:
                    expired = await self.expire_due()
                    if expired:
                        logger.info(f"[ApprovalService] cleanup expired={len(expired)}")
                except Exception as e:
                    logger.warning(f"[ApprovalService] cleanup error: {e}")
        except asyncio.CancelledError:
            logger.info("[ApprovalService] cleanup loop cancelled")

    # ========== CRUD ==========

    async def create_request(
        self,
        agent_hash: str,
        action: str,
        target: str,
        intent: str,
        arguments: Dict[str, Any],
        file_path: str,
        file_cos_key: str,
        file_scope: str,
        checksum_at_request: str,
    ) -> ApprovalRequest:
        """创建一条审批请求。

        流程：
        1. 写 ApprovalRequest 内存记录
        2. **写一条审批卡到聊天流**（不是 IRQ —— IRQ 是 Agent 内部机制，
           人看不到；审批卡要给用户看 → 走 GroupMessage / ChatHistory）
        """
        import json as _json

        req = ApprovalRequest(
            id=uuid.uuid4().hex[:12],
            agent_hash=agent_hash,
            action=action,
            target=target,
            intent=intent,
            arguments=arguments,
            file_path=file_path,
            file_cos_key=file_cos_key,
            file_scope=file_scope,
            checksum_at_request=checksum_at_request,
        )
        async with self._lock:
            self._requests[req.id] = req
            self._by_agent.setdefault(agent_hash, []).append(req.id)
        logger.info(
            f"[ApprovalService] created id={req.id} agent={agent_hash} "
            f"action={action} path={file_path} scope={file_scope}"
        )

        # 第一次 create 时确保清理循环已启动
        self.start_cleanup_loop()

        # 写审批卡到聊天流（给人看）
        self._write_approval_card_to_chat(req)

        return req

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        return self._requests.get(request_id)

    def list_by_agent(self, agent_hash: str) -> List[ApprovalRequest]:
        ids = self._by_agent.get(agent_hash, [])
        return [self._requests[i] for i in ids if i in self._requests]

    def list_by_target(self, target: str) -> List[ApprovalRequest]:
        """列出发往某个 target 的所有 pending 请求。"""
        return [
            r for r in self._requests.values()
            if r.target == target and r.status == ApprovalStatus.PENDING
        ]

    async def grant(
        self,
        request_id: str,
        decided_by: str,
        executor: Optional[Callable[[ApprovalRequest], str]] = None,
    ) -> ApprovalRequest:
        """审批通过。

        Args:
            request_id: 请求 ID
            decided_by: 谁批的（user_id 字符串）
            executor: 可选回调，用于实际执行 file_write/edit。
                     返回 'OK: ...' 或 'Error: ...'。
                     若不传，仅更新状态（外部用 _execute_request 自行执行）。
        """
        req = self._requests.get(request_id)
        if req is None:
            raise KeyError(f"request {request_id} not found")
        if req.status != ApprovalStatus.PENDING:
            raise ValueError(f"request {request_id} status={req.status}, cannot grant")
        if req.is_expired():
            req.status = ApprovalStatus.EXPIRED
            req.decided_at = time.time()
            raise ValueError(f"request {request_id} expired")

        if executor is not None:
            try:
                result = executor(req)
                if result.startswith("Error:"):
                    req.error = result
                    req.status = ApprovalStatus.CONFLICT
                else:
                    req.status = ApprovalStatus.GRANTED
            except Exception as e:
                req.error = f"execute exception: {e}"
                req.status = ApprovalStatus.CONFLICT
        else:
            req.status = ApprovalStatus.GRANTED

        req.decided_at = time.time()
        req.decided_by = decided_by
        logger.info(
            f"[ApprovalService] grant id={request_id} by={decided_by} "
            f"status={req.status}"
        )
        # Phase 4：通知 Agent —— 走 IRQ
        outcome = "granted" if req.status == ApprovalStatus.GRANTED else "conflict"
        self._notify_agent(req, outcome)
        return req

    async def deny(
        self,
        request_id: str,
        decided_by: str,
        reason: Optional[str] = None,
    ) -> ApprovalRequest:
        """审批拒绝。"""
        req = self._requests.get(request_id)
        if req is None:
            raise KeyError(f"request {request_id} not found")
        if req.status != ApprovalStatus.PENDING:
            raise ValueError(f"request {request_id} status={req.status}, cannot deny")

        req.status = ApprovalStatus.DENIED
        req.decided_at = time.time()
        req.decided_by = decided_by
        req.error = reason
        logger.info(
            f"[ApprovalService] deny id={request_id} by={decided_by} "
            f"reason={reason!r}"
        )
        # Phase 4：通知 Agent
        self._notify_agent(req, "denied")
        return req

    async def expire_due(self) -> List[ApprovalRequest]:
        """把所有过期的 pending 请求标记为 EXPIRED，返回列表。"""
        now = time.time()
        expired: List[ApprovalRequest] = []
        async with self._lock:
            for r in list(self._requests.values()):
                if r.status == ApprovalStatus.PENDING and r.expires_at < now:
                    r.status = ApprovalStatus.EXPIRED
                    r.decided_at = now
                    expired.append(r)
        # Phase 4：通知所有刚到期的 Agent
        for r in expired:
            self._notify_agent(r, "expired")
        return expired

    @staticmethod
    def recompute_checksum(content: Optional[bytes]) -> str:
        """重算目标文件的 sha256。content=None 表示文件不存在。"""
        if content is None:
            return ""
        return hashlib.sha256(content).hexdigest()

    # ========== 聊天流审批卡（Phase 4）==========

    def _write_approval_card_to_chat(self, req: ApprovalRequest) -> Optional[str]:
        """把审批卡写到聊天流。

        - target='group:{id}'  → 写 GroupMessage (message_type=approval_card)
        - target='user:{id}'   → 写 ChatHistory (tool_name='approval_card')

        Returns:
            写入的消息 ID；None 表示没有可写的目标。
        """
        import json as _json
        from models.database import SessionLocal

        card_payload = {
            "type": "approval_card",
            "request_id": req.id,
            "action": req.action,
            "intent": req.intent,
            "agent_hash": req.agent_hash,
            "file_path": req.file_path,
            "checksum": req.checksum_at_request,
            "status": "pending",
            "created_at": req.created_at,
            "expires_at": req.expires_at,
        }

        db = SessionLocal()
        try:
            if req.target.startswith("group:"):
                gid = parse_group_target(req.target)
                if gid is None:
                    logger.warning(
                        f"[ApprovalService] invalid group target: {req.target!r}"
                    )
                    return None
                from models.group import GroupMessage
                msg_id = uuid.uuid4().hex
                msg = GroupMessage(
                    id=msg_id,
                    group_id=gid,
                    sender_type="system",
                    sender_hash=req.agent_hash,
                    content=_json.dumps(card_payload, ensure_ascii=False),
                    message_type="approval_card",
                    attachments=None,
                    mentions=[],
                    round=0,
                    created_at=__import__("datetime").datetime.utcnow(),
                )
                db.add(msg)
                db.commit()
                logger.info(
                    f"[ApprovalService] wrote approval_card GroupMessage "
                    f"msg_id={msg_id} group={gid} request_id={req.id}"
                )
                return msg_id
            elif req.target.startswith("user:"):
                uid = parse_user_target(req.target)
                if uid is None:
                    logger.warning(
                        f"[ApprovalService] invalid user target: {req.target!r}"
                    )
                    return None
                from models.database import ChatHistory
                chat = ChatHistory(
                    user_id=uid,
                    agent_hash=req.agent_hash,
                    role="assistant",  # 系统卡片用 assistant 角色（用户能识别为系统消息）
                    content=_json.dumps(card_payload, ensure_ascii=False),
                    tool_name="approval_card",
                    channel="permission",
                    session_id=f"approval_{req.id}",
                    attachments=None,
                    meta={"approval_request_id": req.id},
                    created_at=__import__("datetime").datetime.utcnow(),
                )
                db.add(chat)
                db.commit()
                logger.info(
                    f"[ApprovalService] wrote approval_card ChatHistory "
                    f"chat_id={chat.id} user={uid} request_id={req.id}"
                )
                return str(chat.id)
            else:
                logger.warning(
                    f"[ApprovalService] unknown target prefix: {req.target!r}"
                )
                return None
        except Exception as e:
            logger.error(
                f"[ApprovalService] failed to write approval card: {e}",
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass
            return None
        finally:
            db.close()

    def _notify_agent(self, req: ApprovalRequest, outcome: str) -> None:
        """审批结果通知 Agent —— 走 IRQ（这是 Agent 内部机制）。

        outcome ∈ {granted, denied, conflict, expired}
        """
        try:
            from services.interrupt_controller import (
                InterruptController,
                Interrupt,
                Priority,
            )
            irq_type = permission_irq_type(req.action, outcome)
            InterruptController.instance().dispatch(Interrupt(
                irq_type=irq_type,
                agent_hash=req.agent_hash,
                priority=Priority.MEDIUM,
                payload={
                    "request_id": req.id,
                    "action": req.action,
                    "file_path": req.file_path,
                    "outcome": outcome,
                    "decided_by": req.decided_by,
                    "error": req.error,
                    "channel": "group" if req.target.startswith("group:") else "user",
                    "group_id": parse_group_target(req.target),
                },
            ))
            logger.info(
                f"[ApprovalService] notified agent {req.agent_hash} "
                f"via IRQ {irq_type} for request_id={req.id}"
            )
        except Exception as e:
            logger.warning(f"[ApprovalService] notify_agent failed: {e}")


# ========== 工具函数 ==========

def parse_group_target(target: str) -> Optional[int]:
    """从 'group:123' 解析出 123（group_id）。"""
    if not target or not target.startswith("group:"):
        return None
    try:
        return int(target.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def parse_user_target(target: str) -> Optional[int]:
    """从 'user:123' 解析出 123（user_id）。"""
    if not target or not target.startswith("user:"):
        return None
    try:
        return int(target.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def is_group_shared_path(file_path: str) -> bool:
    """判断 VFS 路径是否落在群共享空间。"""
    return bool(file_path) and file_path.startswith("/mnt/group/")


def get_group_shared_scope(file_path: str) -> Optional[str]:
    """从 /mnt/group/{id}/{scope}/... 提取 scope（.share/.ref/.attach）。
    不在三层空间内 → 返回 None。
    """
    if not is_group_shared_path(file_path):
        return None
    parts = file_path.strip("/").split("/")
    # parts = ["mnt", "group", "{id}", "{scope}", ...]
    if len(parts) < 4:
        return None
    return parts[3]


# ========== 模块导出 ==========

__all__ = [
    "permission_irq_type",
    "ApprovalAction",
    "ApprovalStatus",
    "ApprovalRequest",
    "ApprovalService",
    "parse_group_target",
    "parse_user_target",
    "is_group_shared_path",
    "get_group_shared_scope",
]