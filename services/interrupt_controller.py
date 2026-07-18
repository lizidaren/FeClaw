"""
Interrupt Controller & WorkSession Manager - Agent V2

将 Agent 从「一问一答」升级为「自驱自主」的关键基础设施。

包含：
- InterruptType: 中断类型常量
- Interrupt: 中断消息
- WorkSession: 一次工作会话（WORKING 状态）
- WorkSessionManager: 全局会话管理
- InterruptController: 中断分发器（共享服务）
- CoprocessorService: per IM Agent 的协处理器（定时任务/文件监控）

设计原则：
- 单进程模式：直接函数调用 / 内存队列
- 多 Worker 模式（未来）：通过 Redis PubSub 推送（已留 hook）
- WorkSession 是运行时对象，不持久化（Worker 挂掉 → WorkSession 消失 → Agent 回 DORMANT）
- Coprocessor 是 per-Agent 独立 task，与 WorkSession 解耦，Agent 删除时销毁
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============== Interrupt Type 常量 ==============

class InterruptType:
    """中断类型常量（IRQ 向量表）"""
    MESSAGE = "irq_message"               # 群/单聊新消息
    CRON = "irq_cron"                     # 协处理器定时器到期
    WEBHOOK = "irq_webhook"               # 外部 HTTP 回调
    FILE_CHANGE = "irq_file_change"       # VFS 文件变化
    SEMANTIC_ALERT = "irq_semantic_alert" # 兴趣话题监测
    BG_TASK_DONE = "irq_bg_task_done"     # 后台任务完成
    WATCHDOG = "irq_watchdog"             # 系统监控


class Priority:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ============== WorkSession 子状态 ==============

class WorkSessionState:
    """WorkSession 工作循环的子状态（设计稿 §3.1）"""
    IDLE = "idle"               # 刚创建，未开始处理
    PROCESSING = "processing"   # 正在调 LLM API
    TOOL_CALL = "tool_call"     # LLM 返回 tool_call，执行中
    INTERRUPTED = "interrupted" # 有新的中断注入
    FLUSHING = "flushing"       # Agent 调了 flush()
    WATCHDOG = "watchdog"       # LLM 断了，正在分析


# ============== Interrupt 数据类 ==============

@dataclass
class Interrupt:
    """中断消息"""
    irq_type: str
    agent_hash: str
    priority: str = Priority.MEDIUM
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "irq_type": self.irq_type,
            "agent_hash": self.agent_hash,
            "priority": self.priority,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


# ============== WorkSession ==============

@dataclass
class WorkSession:
    """一次工作会话（Agent WORKING 状态的表现）"""
    id: str
    agent_hash: str
    channel: Optional[str] = None      # 触发渠道
    group_id: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    interrupted_count: int = 0
    interrupt_queue: List[Interrupt] = field(default_factory=list)
    state: str = WorkSessionState.IDLE
    running: bool = False              # 工作循环是否运行中
    interrupts_popped: int = 0         # 已消费的中断数，用于区分冷启动 vs 事件循环
    # Gen 2 IM Agent 灰度流：(session_id → 累积 draft 文本)，在 reply_buffer_flush 推 confirm 时清空
    draft_buffers: Dict[str, str] = field(default_factory=dict)

    def is_expired(self, max_hours: float = 12.0) -> bool:
        return (datetime.utcnow() - self.started_at) > timedelta(hours=max_hours)


class WorkSessionManager:
    """WorkSession 全局管理器（进程内单例）"""
    _instance: Optional["WorkSessionManager"] = None

    def __init__(self):
        self._sessions: Dict[str, WorkSession] = {}  # agent_hash → WorkSession

    @classmethod
    def instance(cls) -> "WorkSessionManager":
        if cls._instance is None:
            cls._instance = WorkSessionManager()
        return cls._instance

    def get_or_create(self, agent_hash: str, channel: Optional[str] = None,
                      group_id: Optional[str] = None) -> WorkSession:
        if agent_hash not in self._sessions:
            self._sessions[agent_hash] = WorkSession(
                id=uuid.uuid4().hex[:8],
                agent_hash=agent_hash,
                channel=channel,
                group_id=group_id,
            )
            logger.info(f"[WorkSession] 创建: id={self._sessions[agent_hash].id} agent={agent_hash}")
        else:
            self._sessions[agent_hash].last_activity = datetime.utcnow()
        return self._sessions[agent_hash]

    def get(self, agent_hash: str) -> Optional[WorkSession]:
        return self._sessions.get(agent_hash)

    def close(self, agent_hash: str) -> None:
        if agent_hash in self._sessions:
            ws = self._sessions.pop(agent_hash)
            logger.info(f"[WorkSession] 关闭: id={ws.id} agent={agent_hash}")

    def is_working(self, agent_hash: str) -> bool:
        return agent_hash in self._sessions

    def all_sessions(self) -> List[WorkSession]:
        return list(self._sessions.values())

    def close_expired(self, max_hours: float = 12.0) -> int:
        """清理过期 WorkSession，返回清理数量"""
        expired = [h for h, ws in self._sessions.items() if ws.is_expired(max_hours)]
        for h in expired:
            self.close(h)
        if expired:
            logger.info(f"[WorkSession] 清理过期会话 {len(expired)} 个")
        return len(expired)


# ============== InterruptController ==============

class InterruptController:
    """中断分发器（共享服务，进程内单例）

    单 Worker 模式：直接创建 WorkSession 或入队。
    多 Worker 模式（未来）：通过 Redis PubSub 推送。
    """
    _instance: Optional["InterruptController"] = None

    def __init__(self):
        self._ws_manager = WorkSessionManager.instance()
        self._on_dispatch_hooks: List[Callable[[Interrupt], None]] = []

    @classmethod
    def instance(cls) -> "InterruptController":
        if cls._instance is None:
            cls._instance = InterruptController()
        return cls._instance

    def add_hook(self, hook: Callable[[Interrupt], None]) -> None:
        """注册 dispatch 时的回调（用于日志/监控）。"""
        self._on_dispatch_hooks.append(hook)

    def dispatch(self, interrupt: Interrupt) -> None:
        """分发一个中断。

        - Agent 正在 WORKING → 追加到 interrupt_queue
        - Agent DORMANT → 创建 WorkSession，并自动启动 _work_loop 处理中断
        """
        ws = self._ws_manager.get(interrupt.agent_hash)
        if ws:
            ws.interrupt_queue.append(interrupt)
            ws.interrupted_count += 1
            logger.info(
                f"[Interrupt] 入队 agent={interrupt.agent_hash} "
                f"type={interrupt.irq_type} queue_len={len(ws.interrupt_queue)}"
            )
        else:
            ws = self._ws_manager.get_or_create(
                interrupt.agent_hash,
                channel=interrupt.payload.get("channel"),
                group_id=interrupt.payload.get("group_id"),
            )
            ws.interrupt_queue.append(interrupt)
            ws.running = True
            logger.info(
                f"[Interrupt] 创建 WorkSession agent={interrupt.agent_hash} "
                f"type={interrupt.irq_type}"
            )
            # 自动启动工作循环（关键：否则 WorkSession 永远不处理中断）
            asyncio.create_task(self._work_loop(ws))
            # 触发 hook（扩展用）
            for hook in self._on_dispatch_hooks:
                try:
                    hook(interrupt)
                except Exception as e:
                    logger.warning(f"[Interrupt] hook failed: {e}")

    def consume_pending(self, agent_hash: str) -> List[Interrupt]:
        """消费掉 agent 所有 pending 中断，返回消费列表。"""
        ws = self._ws_manager.get(agent_hash)
        if not ws:
            return []
        consumed = ws.interrupt_queue
        ws.interrupt_queue = []
        if consumed:
            logger.info(f"[Interrupt] 消费 {len(consumed)} 条中断 agent={agent_hash}")
        return consumed

    def consume_pending_messages(self, agent_hash: str) -> List[Interrupt]:
        """只消费 MESSAGE 类型的 pending 中断（供 TOCTOU 注入用）。

        保留 CRON / FILE_CHANGE 等其他类型给 work_loop 正常处理，
        避免 MESSAGE 中断被 work_loop 二次消费而产生重复回复。
        """
        ws = self._ws_manager.get(agent_hash)
        if not ws:
            return []
        consumed = [i for i in ws.interrupt_queue if i.irq_type == InterruptType.MESSAGE]
        if consumed:
            ws.interrupt_queue = [
                i for i in ws.interrupt_queue if i.irq_type != InterruptType.MESSAGE
            ]
            logger.info(
                f"[Interrupt] TOCTOU 消费 {len(consumed)} 条 MESSAGE 中断 agent={agent_hash}"
            )
        return consumed

    def close_session(self, agent_hash: str) -> None:
        self._ws_manager.close(agent_hash)

    async def _work_loop(self, ws: WorkSession) -> None:
        """按设计走状态机：消费 interrupt_queue，管理 WorkSession 子状态。"""
        try:
            while ws.running and not ws.is_expired():
                if not ws.interrupt_queue:
                    ws.state = WorkSessionState.IDLE
                    await asyncio.sleep(1)
                    continue

                interrupt = ws.interrupt_queue.pop(0)
                ws.interrupts_popped += 1
                ws.state = WorkSessionState.PROCESSING
                await self._handle_interrupt(ws, interrupt)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[WorkLoop] agent={ws.agent_hash} error: {e}")
        finally:
            ws.running = False
            ws.state = WorkSessionState.IDLE
            self._ws_manager.close(ws.agent_hash)

    async def _handle_interrupt(self, ws: WorkSession, interrupt: Interrupt) -> None:
        """处理一个中断——所有渠道（group/web/wechat/desktop）走同一套 buffer 路径。

        IM Agent 设计原则（统一）：
        - Agent 的直接输出是「私有的」思考过程，不会自动发给用户
        - 若要回复，必须依次调用 reply_buffer_write + reply_buffer_flush 工具
        - flush 时按 channel 参数路由到目标：
            * "group"   → 写 GroupMessage + 触发群内其他 IM Agent 回链
            * "web"     → 推 WebSocket 给前端
            * "wechat"  → 微信 API 推送
            * "desktop" → 推 WebSocket (chat_reply 事件)
        - ChatService 加载 ChatHistory 时按 channel 过滤（不同渠道互不混淆）

        「私聊也走 buffer」的理由：
        1. Agent 在私聊和群聊中的行为一致（避免两套心智模型）
        2. 统一 prompt 模板，上下文不混乱
        3. 用户的心智模型：IM Agent = 真实人类（思考 → 写 → 发）
        """
        from services.chat_service import ChatService, ChatInput
        logger.info(
            f"[WorkLoop] agent={ws.agent_hash} state={ws.state} "
            f"type={interrupt.irq_type}"
        )
        payload = interrupt.payload or {}
        channel = payload.get("channel", "group")
        group_id = payload.get("group_id")
        user_id = payload.get("user_id")
        to_user_id = payload.get("to_user_id")           # wechat only
        msg_id = payload.get("msg_id")                   # for tracking
        session_id = payload.get("session_id")           # web SSE correlation
        trigger_content = payload.get("trigger_content", "")
        trigger_sender = payload.get("trigger_sender", "")

        # 获取 Agent 自己的身份信息：soul.md 第一行（去除 # / 标题符号）
        # Bug fix：原代码读 persona config（默认 "# FeClaw 助手"），与 soul.md 矛盾
        agent_identity = ""
        try:
            from models.agent_profile import AgentProfile as _AP
            from models.database import SessionLocal as _SL
            _adb = _SL()
            _ap = _adb.query(_AP).filter(_AP.hash == ws.agent_hash).first()
            _ap_name = _ap.name if _ap and _ap.name else ""
            _adb.close()
            # 从 VFS 读 soul.md（与 chat_service.build_system_prompt 同源）
            _soul_first = ""
            try:
                from services.virtual_filesystem import VirtualFileSystem
                _vfs = VirtualFileSystem(agent_hash=ws.agent_hash)
                _raw = _vfs.cat("/workspace/agent/soul.md")
                if _raw and not _raw.startswith("Error"):
                    # 跳过空行/标题前缀，提取首条非空内容
                    for _ln in _raw.splitlines():
                        _ln = _ln.strip()
                        if not _ln:
                            continue
                        # 去掉 # 标题符号
                        _ln = _ln.lstrip("#").strip()
                        if _ln:
                            _soul_first = _ln[:80]
                            break
            except Exception:
                pass
            if _soul_first:
                agent_identity = f"（你是 {_ap_name or '?'}，人格：{_soul_first}）"
            elif _ap_name:
                agent_identity = f"（你是 {_ap_name}）"
        except Exception:
            pass

        # ====== 构造统一 prompt（不分私聊/群聊） ======
        if trigger_content:
            header = ("📩 你收到了来自 " + str(trigger_sender) + " 的消息")
            if agent_identity:
                header += " " + agent_identity
            header += "：\n" + trigger_content + "\n\n"
            prompt = header
            prompt += "📌 你输出的任何文字都只是思考过程，不会发给任何人。\n"
            prompt += "如果你要让对方看到你的回复，**必须**依次调用这两个工具：\n"
            prompt += "  1. reply_buffer_write(content=\"你的回复\") — 写入要发的消息\n"

            # 按渠道给具体的 flush 调用示例
            flush_parts = [f'channel="{channel}"']
            if channel == "group":
                if group_id:
                    flush_parts.append(f'group_id="{group_id}"')
            elif channel == "wechat":
                if to_user_id:
                    flush_parts.append(f'to_user_id="{to_user_id}"')
            elif channel == "web":
                if user_id is not None:
                    flush_parts.append(f'user_id={user_id!r}')
                if session_id:
                    flush_parts.append(f'session_id="{session_id}"')
            elif channel == "mobile":
                if user_id is not None:
                    flush_parts.append(f'user_id={user_id!r}')
                if session_id:
                    flush_parts.append(f'session_id="{session_id}"')
                if msg_id is not None:
                    flush_parts.append(f'msg_id="{msg_id}"')
            elif channel == "desktop":
                if user_id is not None:
                    flush_parts.append(f'user_id={user_id!r}')
                if msg_id:
                    flush_parts.append(f'msg_id="{msg_id}"')
            flush_call = ", ".join(flush_parts)
            destination = {
                "group": "群",
                "web": "Web 私聊用户",
                "wechat": "微信用户",
                "desktop": "Desktop 用户",
            }.get(channel, "对方")
            prompt += f"  2. reply_buffer_flush({flush_call}) — 推送到{destination}\n"

            prompt += "⚠️ 不调工具，没人看得到。\n\n"
            prompt += "你还可以选择：\n"
            # 从 AgentConfig 读 enabled 工具列表（最多展示 3 个），避免列出被禁用的工具
            _tool_examples = ["file_read", "file_write", "file_list"]
            try:
                from models.database import SessionLocal as _SL2, AgentConfig as _AC2
                _adb2 = _SL2()
                _tc = _adb2.query(_AC2).filter(
                    _AC2.agent_hash == ws.agent_hash,
                    _AC2.key == f"agents/{ws.agent_hash}/tools"
                ).first()
                _adb2.close()
                if _tc and _tc.value:
                    import json as _json
                    _tv = _json.loads(_tc.value)
                    _enabled = _tv.get("enabled") or []
                    if _enabled:
                        _tool_examples = _enabled[:3]
            except Exception:
                pass
            prompt += f"- 调用其他工具处理任务（如 {', '.join(_tool_examples)} 等）\n"
            prompt += "- 输出 **DORMANT** 关闭会话进入休眠（下次有新消息会唤醒你）"
        else:
            prompt = (
                f"[系统触发] 收到 {interrupt.irq_type} 中断\n\n"
                "📌 重要规则：你的直接输出是私有的（思考过程），不会被发给任何渠道。"
                "要发送消息，必须使用 reply_buffer_write + reply_buffer_flush 工具。"
            )

        try:
            ws.state = WorkSessionState.PROCESSING
            # ChatService 加载 channel 特定的 ChatHistory（每渠道独立的对话）
            # Fix: IRQ 路径必须把 group_id 透传给 ChatService，
            # 否则 AgentToolsService 会走「个人模式」分支，
            # 与群聊的 /mnt/group/{gid}/ 路径解析及 group 双写逻辑不一致。
            cs = ChatService(
                agent_hash=ws.agent_hash,
                channel=channel,
                group_id=group_id,
            )
            full = ""
            # Gen 2 IM Agent 灰度字流：web 渠道时把 LLM 中间 token + tool_call_arg 实时推 WS（draft）。
            # Classic Agent / group / wechat / desktop 渠道不受影响（仅 web 走 draft 通道）。
            _ws_manager = None
            if channel in ("web", "mobile"):
                try:
                    from routers.client_ws import manager as _ws_manager  # noqa: F401
                except Exception:
                    _ws_manager = None

            async def _push_draft(payload: Dict[str, Any]) -> None:
                """把 draft 推给前端（失败静默，不影响 LLM 流）。

                只推给当前连着的客户端：桌面端/移动端均可接收 draft 灰字流，客户端按 session_id 路由。
                """
                if not _ws_manager or not _ws_manager.is_connected:
                    return
                # draft 推给当前连着的客户端，客户端按 session_id 路由
                # 注：ClientConnectionManager 是单连接管理器，一个设备只连一个 WS
                try:
                    await _ws_manager.send(payload)
                except Exception as _e:
                    logger.debug(f"[WorkLoop] draft push failed: {_e}")

            async for event in cs.chat(input=ChatInput(text=prompt, skip_history=False)):
                t = str(event.type)
                if t == "ChatEventType.TEXT":
                    if hasattr(event, "content") and event.content:
                        _chunk = str(event.content)
                        full += _chunk
                        if channel in ("web", "mobile") and session_id:
                            ws.draft_buffers[session_id] = (
                                ws.draft_buffers.get(session_id, "") + _chunk
                            )
                            await _push_draft({
                                "type": "draft",
                                "event": "draft",
                                "channel": channel,
                                "agent_hash": ws.agent_hash,
                                "session_id": session_id,
                                "user_id": user_id,
                                "content": _chunk,
                            })
                elif t == "ChatEventType.TOOL_CALL_ARG":
                    # 工具参数也以 draft 形式推（前端可显示小灰字「调用 X… 参数：…」）
                    if channel in ("web", "mobile") and session_id:
                        _arg = getattr(event, "content", "") or ""
                        _tn = getattr(event, "tool_name", "") or ""
                        if _arg:
                            ws.draft_buffers[session_id] = (
                                ws.draft_buffers.get(session_id, "") + _arg
                            )
                            await _push_draft({
                                "type": "draft",
                                "event": "draft",
                                "channel": channel,
                                "agent_hash": ws.agent_hash,
                                "session_id": session_id,
                                "user_id": user_id,
                                "content": _arg,
                                "tool_name": _tn,
                            })
                elif t == "ChatEventType.DONE":
                    break

            full = (full or "").strip()
            # 任何渠道：不再"直接输出即回复"；只有在调用 flush 工具后才推送。
            # full 收集到的只是 LLM 流式文本（含工具调用），真正发送在 reply_buffer_flush 内完成。
            ws.state = WorkSessionState.FLUSHING if full and full.strip() not in ("NO_REPLY", "SILENT", "DORMANT") else WorkSessionState.IDLE
            if full and full.strip() in ("NO_REPLY", "SILENT"):
                logger.info(f"[WorkLoop] agent={ws.agent_hash} {full.strip()}, no flush")
            elif full and full.strip() == "DORMANT":
                logger.info(f"[WorkLoop] agent={ws.agent_hash} DORMANT, closing session")
                self._ws_manager.close(ws.agent_hash)
                ws.running = False
                return  # 跳过 finally 的 state 设置，WS 已销毁
            logger.info(
                f"[WorkLoop] agent={ws.agent_hash} channel={channel} "
                f"response_len={len(full)} type={interrupt.irq_type}"
            )
        except Exception as e:
            logger.warning(f"[_handle_interrupt] agent={ws.agent_hash} channel={channel} error: {e}")
        finally:
            ws.state = WorkSessionState.IDLE if not ws.interrupt_queue else WorkSessionState.PROCESSING


# ============== Coprocessor Service ==============

class CoprocessorService:
    """per IM Agent 的协处理器（独立 asyncio.Task）

    职责：
    - 定时器（Cron）轮询
    - 文件变化监测
    - 兴趣话题监测（留 hook，P3 实现）
    - Webhook 接收（留 hook，P3 实现）

    生命周期：
    - Agent 创建（agent_mode=im）→ start(agent_hash)
    - Agent 删除 → stop(agent_hash)
    - 服务器重启 → restart_all() 从 DB 加载所有 IM Agent 配置并启动
    """
    _agents: Dict[str, asyncio.Task] = {}  # agent_hash → asyncio.Task

    @classmethod
    async def start(cls, agent_hash: str) -> None:
        """启动单个 IM Agent 的协处理器（幂等）。"""
        if agent_hash in cls._agents:
            return
        task = asyncio.create_task(cls._coprocessor_loop(agent_hash))
        cls._agents[agent_hash] = task
        logger.info(f"[Coprocessor] 启动 agent={agent_hash}")

    @classmethod
    async def stop(cls, agent_hash: str) -> None:
        """停止协处理器（清理任务并从映射中移除）。"""
        task = cls._agents.pop(agent_hash, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[Coprocessor] stop error: {e}")
        logger.info(f"[Coprocessor] 停止 agent={agent_hash}")

    @classmethod
    def is_running(cls, agent_hash: str) -> bool:
        return agent_hash in cls._agents

    @classmethod
    async def restart_all(cls) -> int:
        """服务器重启时调用：从 DB 加载所有 agent_mode='im' 的 Agent 并启动协处理器。"""
        # 延迟导入避免循环依赖
        from models.database import SessionLocal
        from models.agent_profile import AgentProfile

        db = SessionLocal()
        try:
            im_agents = (
                db.query(AgentProfile)
                .filter(AgentProfile.agent_mode == "im")
                .all()
            )
            for agent in im_agents:
                await cls.start(agent.hash)
            logger.info(f"[Coprocessor] restart_all 启动了 {len(im_agents)} 个 IM Agent")
            return len(im_agents)
        except Exception as e:
            logger.error(f"[Coprocessor] restart_all 失败: {e}")
            return 0
        finally:
            db.close()

    @classmethod
    async def _coprocessor_loop(cls, agent_hash: str) -> None:
        """单 Agent 协处理器主循环。"""
        try:
            while True:
                try:
                    configs = await cls._load_config(agent_hash)

                    # 1. 执行到期 cron
                    for cron in configs.get("crons", []):
                        if cls._is_due(cron):
                            await cls._execute_cron(agent_hash, cron)

                    # 2. 文件变化监测
                    for watch in configs.get("file_watches", []):
                        if await cls._check_file_change(agent_hash, watch):
                            InterruptController.instance().dispatch(Interrupt(
                                irq_type=InterruptType.FILE_CHANGE,
                                agent_hash=agent_hash,
                                priority=Priority.LOW,
                                payload={"watch": watch},
                            ))

                except Exception as inner_e:
                    logger.warning(f"[Coprocessor] agent={agent_hash} 循环内错误: {inner_e}")

                # 3. 休眠 10s（每 10s 一轮）
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.info(f"[Coprocessor] 协处理器被取消 agent={agent_hash}")
            raise
        except Exception as e:
            logger.error(f"[Coprocessor] agent={agent_hash} 主循环异常退出: {e}")

    @classmethod
    async def _load_config(cls, agent_hash: str) -> Dict[str, Any]:
        """从 agent_config 表加载 coprocessor_config.json 内容。

        存储 key 约定：`agents/{hash}/system/coprocessor_config.json`
        """
        from models.database import AgentConfig, SessionLocal

        config_key = f"agents/{agent_hash}/system/coprocessor_config.json"
        db = SessionLocal()
        try:
            cfg = (
                db.query(AgentConfig)
                .filter(AgentConfig.key == config_key)
                .first()
            )
            if not cfg or not cfg.value:
                return {"crons": [], "file_watches": []}
            try:
                return json.loads(cfg.value)
            except json.JSONDecodeError:
                logger.warning(f"[Coprocessor] {config_key} 不是合法 JSON，重置为空")
                return {"crons": [], "file_watches": []}
        finally:
            db.close()

    @classmethod
    def _save_config(cls, agent_hash: str, config: Dict[str, Any]) -> None:
        """写回 agent_config 表。"""
        from models.database import AgentConfig, SessionLocal

        config_key = f"agents/{agent_hash}/system/coprocessor_config.json"
        db = SessionLocal()
        try:
            cfg = (
                db.query(AgentConfig)
                .filter(AgentConfig.key == config_key)
                .first()
            )
            if cfg:
                cfg.value = json.dumps(config, ensure_ascii=False)
                cfg.updated_at = datetime.utcnow()
            else:
                cfg = AgentConfig(
                    key=config_key,
                    value=json.dumps(config, ensure_ascii=False),
                    agent_hash=agent_hash,
                    description="Coprocessor configuration (cron/file_watches/topic_watches)",
                )
                db.add(cfg)
            db.commit()
        except Exception as e:
            logger.error(f"[Coprocessor] save_config 失败: {e}")
        finally:
            db.close()

    @classmethod
    def _is_due(cls, cron: Dict[str, Any]) -> bool:
        """简单判断 cron 是否到期（基于 last_run_at + interval_sec）。"""
        interval = cron.get("interval_sec", 0)
        if interval <= 0:
            return False
        last_run_str = cron.get("last_run_at")
        if not last_run_str:
            return True
        try:
            last_run = datetime.fromisoformat(last_run_str)
        except ValueError:
            return True
        return (datetime.utcnow() - last_run).total_seconds() >= interval

    @classmethod
    async def _execute_cron(cls, agent_hash: str, cron: Dict[str, Any]) -> None:
        """执行一个到期 cron：分发 IRQ_CRON 给 Agent。"""
        logger.info(f"[Coprocessor] cron 到期 agent={agent_hash} cron={cron.get('id')}")
        # 读完整配置（异步，避免阻塞事件循环）
        configs = await cls._load_config(agent_hash)
        crons = configs.get("crons", [])

        # 更新该 cron 的 last_run_at
        for c in crons:
            if c.get("id") == cron.get("id"):
                c["last_run_at"] = datetime.utcnow().isoformat()

        # 写回完整配置（保留 file_watches/topic_watches）
        configs["crons"] = crons
        await asyncio.to_thread(cls._save_config, agent_hash, configs)

        # 分发中断
        InterruptController.instance().dispatch(Interrupt(
            irq_type=InterruptType.CRON,
            agent_hash=agent_hash,
            priority=Priority.MEDIUM,
            payload={"cron_id": cron.get("id"), "task_desc": cron.get("task_desc", "")},
        ))

    @classmethod
    async def _check_file_change(cls, agent_hash: str, watch: Dict[str, Any]) -> bool:
        """检查文件是否发生变化（基于 mtime）。

        Returns:
            True 表示发生变化，应触发 IRQ_FILE_CHANGE
        """
        try:
            from services.storage_service import StorageService
            path = watch.get("path", "")
            if not path:
                return False
            storage = StorageService()
            # 简单实现：对比 VFS 中上次记录的 mtime
            last_mtime_str = watch.get("last_mtime")
            current = storage.get_file_meta(path)
            current_mtime = current.get("last_modified") if current else None
            if current_mtime and current_mtime != last_mtime_str:
                watch["last_mtime"] = current_mtime
                cls._save_config(agent_hash, {"file_watches": cls._load_config(agent_hash).get("file_watches", [])})
                return True
        except Exception as e:
            logger.debug(f"[Coprocessor] file_watch check 失败: {e}")
        return False

    # ========== 公开 API：coprocessor 配置 CRUD ==========

    @classmethod
    def add_cron(cls, agent_hash: str, schedule: str, task_desc: str,
                 created_by: str = "agent") -> Dict[str, Any]:
        """添加定时任务（被 coprocessor_add_cron 工具调用）。"""
        config = cls._load_config_sync(agent_hash)
        cron = {
            "id": uuid.uuid4().hex[:8],
            "schedule": schedule,       # 用户输入的 cron 表达式（保留供 UI 显示）
            "interval_sec": cls._parse_schedule_to_interval(schedule),
            "task_desc": task_desc,
            "created_by": created_by,    # "agent" | "user"
            "created_at": datetime.utcnow().isoformat(),
        }
        config.setdefault("crons", []).append(cron)
        cls._save_config(agent_hash, config)
        return cron

    @classmethod
    def remove_cron(cls, agent_hash: str, cron_id: str) -> bool:
        """删除定时任务。"""
        config = cls._load_config_sync(agent_hash)
        before = len(config.get("crons", []))
        config["crons"] = [c for c in config.get("crons", []) if c.get("id") != cron_id]
        if len(config["crons"]) < before:
            cls._save_config(agent_hash, config)
            return True
        return False

    @classmethod
    def list_crons(cls, agent_hash: str) -> List[Dict[str, Any]]:
        config = cls._load_config_sync(agent_hash)
        return config.get("crons", [])

    @classmethod
    def add_file_watch(cls, agent_hash: str, path: str, pattern: Optional[str] = None) -> Dict[str, Any]:
        config = cls._load_config_sync(agent_hash)
        watch = {
            "id": uuid.uuid4().hex[:8],
            "path": path,
            "pattern": pattern,
            "created_at": datetime.utcnow().isoformat(),
        }
        config.setdefault("file_watches", []).append(watch)
        cls._save_config(agent_hash, config)
        return watch

    @classmethod
    def list_file_watches(cls, agent_hash: str) -> List[Dict[str, Any]]:
        config = cls._load_config_sync(agent_hash)
        return config.get("file_watches", [])

    @classmethod
    def list_all(cls, agent_hash: str) -> Dict[str, Any]:
        """列出所有协处理器配置。"""
        return cls._load_config_sync(agent_hash)

    @classmethod
    def _load_config_sync(cls, agent_hash: str) -> Dict[str, Any]:
        """同步版本的 _load_config（供 CRUD 工具调用）。"""
        from models.database import AgentConfig, SessionLocal

        config_key = f"agents/{agent_hash}/system/coprocessor_config.json"
        db = SessionLocal()
        try:
            cfg = (
                db.query(AgentConfig)
                .filter(AgentConfig.key == config_key)
                .first()
            )
            if not cfg or not cfg.value:
                return {"crons": [], "file_watches": []}
            try:
                return json.loads(cfg.value)
            except json.JSONDecodeError:
                return {"crons": [], "file_watches": []}
        finally:
            db.close()

    @staticmethod
    def _parse_schedule_to_interval(schedule: str) -> int:
        """解析 schedule 字符串为 interval_sec。

        支持的语法：
        - "@every 30s" / "@every 5m" / "@every 1h"
        - 简单 cron 字符串："*/10 * * * *" → 600 秒（粗略估算）

        Returns:
            间隔秒数（>0），解析失败返回 60。
        """
        s = (schedule or "").strip()
        if not s:
            return 60
        if s.startswith("@every"):
            parts = s.split()
            if len(parts) >= 2:
                unit = parts[1][-1] if len(parts[1]) > 0 else "s"
                try:
                    n = int(parts[1][:-1])
                except ValueError:
                    return 60
                if unit == "s":
                    return max(1, n)
                if unit == "m":
                    return max(1, n * 60)
                if unit == "h":
                    return max(1, n * 3600)
        # 简单 cron 解析：返回默认 60s（复杂 cron 解析由 P3 引入 croniter 实现）
        return 60


# ============== 模块级单例访问 ==============

def get_work_session_manager() -> WorkSessionManager:
    return WorkSessionManager.instance()


def get_interrupt_controller() -> InterruptController:
    return InterruptController.instance()


__all__ = [
    "InterruptType",
    "Priority",
    "WorkSessionState",
    "Interrupt",
    "WorkSession",
    "WorkSessionManager",
    "InterruptController",
    "CoprocessorService",
    "get_work_session_manager",
    "get_interrupt_controller",
]