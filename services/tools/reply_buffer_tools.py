"""
Agent 工具服务 - ReplyBuffer 工具
Agent V2: 缓冲-刷新模式发送消息。

核心目的：
1. 提供注入点（每次工具调用 = 一次中断注入机会）
2. 防 TOCTOU：Flush 前检查自写入后是否有新消息
3. 多媒体附件：消息和附件一并提交
4. 渠道统一（IM Agent 设计）：群聊 / Web / WeChat / Desktop 共用
   reply_buffer_write + reply_buffer_flush 路径，不区分"私聊直接输出"模式。

6 个工具：
- reply_buffer_write(content, attachments)            写入缓冲区
- reply_buffer_flush(channel, group_id, to_user_id,
                    user_id, session_id, msg_id, to)  确认发送（执行 TOCTOU 检查 + 路由）
- reply_buffer_cancel()                                取消
- reply_buffer_stash()                                 暂存到 stash
- reply_buffer_pop()                                   从 stash 恢复
- reply_buffer_edit(old_string, new_string)            修改缓冲区内已有内容
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.tool_registry import tool
from services.tools.base import AgentToolsServiceBase
from models.agent_buffer import AgentBuffer
from models.database import SessionLocal

logger = logging.getLogger(__name__)


# ============== 内部辅助函数 ==============

def _get_buffer(agent_hash: str) -> Optional[AgentBuffer]:
    """读取当前 Agent 的 buffer 行（不存在返回 None）"""
    db = SessionLocal()
    try:
        return (
            db.query(AgentBuffer)
            .filter(AgentBuffer.agent_hash == agent_hash)
            .first()
        )
    finally:
        db.close()


def _create_buffer_row(agent_hash: str) -> AgentBuffer:
    """创建初始空 buffer 行"""
    db = SessionLocal()
    try:
        buf = AgentBuffer(
            agent_hash=agent_hash,
            content="",
            attachments=[],
            version=0,
        )
        db.add(buf)
        db.commit()
        db.refresh(buf)
        return buf
    finally:
        db.close()


def _save_buffer(
    agent_hash: str,
    content: str,
    attachments: List[Dict[str, Any]],
    bump_version: bool = True,
) -> AgentBuffer:
    """写入或更新 buffer 行。bump_version=True 时 version 自增。"""
    db = SessionLocal()
    try:
        buf = (
            db.query(AgentBuffer)
            .filter(AgentBuffer.agent_hash == agent_hash)
            .with_for_update()
            .first()
        )
        if not buf:
            buf = AgentBuffer(
                agent_hash=agent_hash,
                content="",
                attachments=[],
                version=0,
            )
            db.add(buf)
            db.flush()
        buf.content = content
        buf.attachments = attachments or []
        if bump_version:
            buf.version = (buf.version or 0) + 1
        buf.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(buf)
        return buf
    finally:
        db.close()


def _clear_buffer(agent_hash: str) -> None:
    """清空 buffer 内容（保留行）"""
    db = SessionLocal()
    try:
        buf = (
            db.query(AgentBuffer)
            .filter(AgentBuffer.agent_hash == agent_hash)
            .first()
        )
        if buf:
            buf.content = ""
            buf.attachments = []
            buf.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def _update_buffer_content(agent_hash: str, new_content: str, new_version: int) -> None:
    """更新 buffer 内容（不重置 attachments）。供 reply_buffer_edit 使用。"""
    db = SessionLocal()
    try:
        buf = (
            db.query(AgentBuffer)
            .filter(AgentBuffer.agent_hash == agent_hash)
            .first()
        )
        if buf:
            buf.content = new_content
            buf.version = new_version
            buf.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def _check_new_messages(
    agent_hash: str,
    channel: str,
    group_id: Optional[str],
    since: datetime,
) -> List[Dict[str, Any]]:
    """
    TOCTOU 检查：自 since 之后目标渠道是否有新消息。

    Returns:
        新消息列表（含 sender / content / created_at），可能为空列表
    """
    if channel == "group" and group_id:
        from models.group import GroupMessage
        db = SessionLocal()
        try:
            rows = (
                db.query(GroupMessage)
                .filter(
                    GroupMessage.group_id == group_id,
                    GroupMessage.created_at > since,
                )
                .order_by(GroupMessage.created_at.asc())
                .limit(20)
                .all()
            )
            return [
                {
                    "sender_type": r.sender_type,
                    "sender_hash": r.sender_hash,
                    "content": (r.content or "")[:500],
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()
    # direct 渠道暂不查 DB；返回空，由 Agent 自行处理
    return []


async def _send_to_group(
    group_id: str,
    sender_hash: str,
    content: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """走 group_service.on_message 把消息入库（async 版本，可在 async 上下文被 await）。"""
    from services.group_service import GroupDispatchService
    svc = GroupDispatchService()
    msg_id = await svc.on_message(
        group_id=group_id,
        sender_type="agent",
        sender_hash=sender_hash,
        content=content,
        attachments=attachments,
    )
    return msg_id


def _save_group_message_direct(
    group_id: str,
    sender_hash: str,
    content: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """直接 INSERT 一条 GroupMessage（用于同步上下文，如 coprocessor cron）。"""
    import uuid as _uuid
    from models.group import GroupMessage
    db = SessionLocal()
    try:
        msg_id = str(_uuid.uuid4())
        msg = GroupMessage(
            id=msg_id,
            group_id=group_id,
            sender_type="agent",
            sender_hash=sender_hash,
            content=content,
            message_type="text",
            attachments=attachments,
            mentions=[],
            round=0,
            created_at=datetime.utcnow(),
        )
        db.add(msg)
        db.commit()
        # ⚠️ 不在这里调 dispatch_to_members（它会通过 ChatService 双写 GroupMessage 泄漏）
        # IM Agent 的回链由 _trigger_re_dispatch 通过 InterruptController IRQ 路径处理
        return msg_id
    finally:
        db.close()


# ============== 非群渠道推送（Web / WeChat / Desktop 私聊） ==============

async def _push_direct_response(
    channel: str,
    agent_hash: str,
    content: str,
    user_id: Optional[int] = None,
    to_user_id: Optional[str] = None,
    msg_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> bool:
    """将 Agent 私聊响应推回原渠道（IM Agent 统一 buffer 路径的终端）。

    - wechat: 通过 wechat_service.send_message 推送给微信用户
    - web/desktop: 通过 ClientConnectionManager (WebSocket) 推送给前端
                   推完后 ChatHistory 仍由 ChatService 在 chat() 末尾写入。

    静默降级契约（graceful degradation contract）：
        本函数**绝不抛异常**给 LLM。任何推送失败（WS 未连接 / 发送过程中断 /
        wechat API 报错 / manager 加载异常）都会被捕获并返回 False。
        调用方据此向 LLM 返回"已写入 ChatHistory"的成功消息（不是 Error），
        用户下次打开对话时仍可看到完整历史。

    Returns:
        True 表示已发送；False 表示通道空闲或推送失败（用户离线/离开）。
        调用方应将 False 视为"消息已持久化但未实时推送"，无需重试。
    """
    try:
        if channel == "wechat":
            if not to_user_id:
                logger.warning(f"[PushResponse] wechat 渠道缺少 to_user_id, agent={agent_hash}")
                return False
            try:
                from services.wechat_service import wechat_service as _wxs
                ok = await _wxs.send_message(
                    to_user_id=to_user_id,
                    text=content,
                )
                logger.info(
                    f"[PushResponse] wechat ok={ok} agent={agent_hash} to={to_user_id[:20]} "
                    f"content_len={len(content)}"
                )
                return ok
            except Exception as e:
                logger.warning(f"[PushResponse] wechat 发送失败: {e}")
                return False

        # web / desktop：通过 WS 推送
        try:
            from routers.client_ws import manager as _ws_manager
        except Exception as e:
            logger.warning(f"[PushResponse] 加载 WS manager 失败: {e}")
            return False

        if not _ws_manager.is_connected:
            logger.warning(
                f"[PushResponse] WS 未连接 agent={agent_hash} channel={channel}, "
                f"响应仅保存到 ChatHistory（用户可下次进入会话时查看）"
            )
            return False

        from datetime import datetime as _dt
        ts = _dt.utcnow().isoformat() + "Z"

        if channel == "desktop":
            # desktop: chat_reply + chat_event(done) 事件序列
            await _ws_manager.send({
                "type": "chat_reply",
                "id": msg_id or "",
                "text": content,
                "agent": agent_hash,
                "timestamp": ts,
            })
            await _ws_manager.send({
                "type": "chat_event",
                "id": msg_id or "",
                "kind": "done",
                "data": {"session_id": session_id or ""},
                "timestamp": ts,
            })
        else:
            # web: direct_message_reply 事件
            await _ws_manager.send({
                "type": "direct_message_reply",
                "agent_hash": agent_hash,
                "user_id": user_id,
                "msg_id": msg_id or "",
                "session_id": session_id or "",
                "channel": channel,
                "content": content,
                "timestamp": ts,
            })
            # Gen 2 IM Agent 灰度字流：flush 成功后推 confirm，让前端把同 session 的灰字变成黑字。
            # 不影响 Classic Agent / desktop 路径。
            if channel in ("web", "mobile") and session_id:
                try:
                    from services.interrupt_controller import WorkSessionManager
                    _ws = WorkSessionManager.instance().get(agent_hash)
                except Exception:
                    _ws = None
                await _ws_manager.send({
                    "type": "confirm",
                    "event": "confirm",
                    "channel": channel,
                    "agent_hash": agent_hash,
                    "user_id": user_id,
                    "session_id": session_id or "",
                    "msg_id": msg_id or "",
                    "content": content,
                    "timestamp": ts,
                })
                if _ws is not None:
                    _ws.draft_buffers.pop(session_id, None)
        logger.info(
            f"[PushResponse] {channel} ok agent={agent_hash} "
            f"response_len={len(content)}"
        )
        return True
    except Exception as e:
        logger.warning(f"[PushResponse] {channel} push failed: {e}")
        return False


# ============== 回链触发（flush 到群后异步分发到其他 IM Agent） ==============

def _trigger_re_dispatch(agent_hash: str, group_id: str, content: str) -> None:
    """flush 成功后异步触发群内其他 IM Agent 的回链（不阻塞 flush 返回）。"""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_re_dispatch(agent_hash, group_id, content))
    except RuntimeError:
        # 无运行中的事件循环（极少数同步上下文）——降级为新线程跑
        import threading
        threading.Thread(
            target=lambda: asyncio.run(_do_re_dispatch(agent_hash, group_id, content)),
            daemon=True,
        ).start()


async def _do_re_dispatch(agent_hash: str, group_id: str, content: str) -> None:
    """异步回链分发：找群内其他 IM Agent，触发一个（轮流）。"""
    # 局部导入避免循环依赖（interrupt_controller 可能被 router 反向 import）
    from models.agent_profile import AgentProfile
    from models.database import SessionLocal
    from models.group import GroupMember
    from services.interrupt_controller import (
        Interrupt,
        InterruptController,
        InterruptType,
        Priority,
    )

    try:
        db = SessionLocal()
        try:
            members = db.query(GroupMember).filter(
                GroupMember.group_id == group_id,
                GroupMember.agent_hash != "",
                GroupMember.agent_hash != agent_hash,
            ).all()
            im_hashes = [
                m.agent_hash for m in members
                if db.query(AgentProfile)
                .filter(
                    AgentProfile.hash == m.agent_hash,
                    AgentProfile.agent_mode == "im",
                ).first()
            ]
            if not im_hashes:
                return

            # 用「哈希（昵称）」称呼发送者
            _sender_profile = db.query(AgentProfile).filter(
                AgentProfile.hash == agent_hash
            ).first()
            _sender_name = (
                f"{agent_hash}（{_sender_profile.name}）"
                if _sender_profile and _sender_profile.name else agent_hash
            )

            # 按 hash 排序轮流触发第一个（避免同时回复导致双倍消息）
            im_hashes_sorted = sorted(im_hashes)
            # 广播给所有其他 IM Agent（TOCTOU 注入 + 原子 flush 保证时序）
            ic = InterruptController.instance()
            for target in im_hashes_sorted:
                ic.dispatch(Interrupt(
                    irq_type=InterruptType.MESSAGE,
                    agent_hash=target,
                    priority=Priority.HIGH,
                    payload={
                        "group_id": group_id,
                        "channel": "group",
                        "irq_round": 1,
                        "trigger_content": content[:500],
                        "trigger_sender": _sender_name,
                    },
                ))
            logger.info(
                f"[BufferFlush] 回链广播 agent={agent_hash} → {im_hashes_sorted}"
            )
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[BufferFlush] 回链触发失败: {e}")


# ============== Mixin ==============

class ReplyBufferToolsMixin(AgentToolsServiceBase):
    """ReplyBuffer 工具 Mixin"""

    @tool(
        name="reply_buffer_write",
        description="在群聊的输入框中输入你想要发送的消息。写入后必须调用 reply_buffer_flush 才会正式发出。这是群聊发言的第一步。",
        category="agent",
    )
    async def reply_buffer_write(
        self,
        content: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Args:
            content: 消息内容
            attachments: 附件列表，每项包含 type(file/image), path(VFS路径), caption(可选说明)
        """
        if not isinstance(content, str):
            return "Error: content 必须是字符串"

        # 验证 attachments 结构（宽松校验）
        atts = attachments or []
        for i, a in enumerate(atts):
            if not isinstance(a, dict):
                return f"Error: attachments[{i}] 不是 dict"
            if a.get("type") not in ("file", "image", None):
                return f"Error: attachments[{i}].type 必须是 file/image"

        # 检查是否覆盖
        existing = _get_buffer(self.agent_hash)
        overwritten = bool(existing and (existing.content or existing.attachments))

        _save_buffer(self.agent_hash, content, atts, bump_version=True)
        suffix = "\n⚠️ 已覆盖上一条 buffer 内容" if overwritten else ""
        att_str = f"\n📎 附件数: {len(atts)}" if atts else ""
        return f"✅ Buffer 已写入{att_str}（待 flush）{suffix}"

    @tool(
        name="reply_buffer_flush",
        description=(
            "敲下回车，将输入框中的内容正式发送出去（群聊/私聊/web/wechat/desktop 通用）。"
            "调用前必须先调用 reply_buffer_write。"
            "不调这个，没人能看到你说的话——无论是群里还是私聊。"
        ),
        category="agent",
    )
    async def reply_buffer_flush(
        self,
        channel: str,
        group_id: Optional[str] = None,
        to_user_id: Optional[str] = None,
        user_id: Optional[int] = None,
        session_id: Optional[str] = None,
        msg_id: Optional[str] = None,
        to: Optional[str] = None,
    ) -> str:
        """
        Args:
            channel: 发送渠道
                - "group"   : 群聊，写 GroupMessage + 触发群内其他 IM Agent 回链
                - "web"     : Web 私聊，通过 WebSocket 推回前端
                - "wechat"  : 微信私聊，通过微信 API 发送给用户
                - "desktop" : Desktop 客户端私聊，通过 WebSocket 推送 chat_reply 事件
            group_id:   channel="group" 时必填，群 ID（UUID）
            to_user_id: channel="wechat" 时必填，微信用户 ID（open_id 等）
            user_id:    channel="web" / "desktop" 时使用，平台用户 ID
            session_id: channel="web" 时可选，SSE 会话标识
            msg_id:     channel="desktop" 时使用，跟踪原始消息 ID
            to:         人类可读目标描述（仅供日志/调试，不会真正用于寻址）
        """
        valid_channels = ("group", "web", "wechat", "desktop")
        if channel not in valid_channels:
            return f"Error: channel 必须是 {valid_channels}"

        buf = _get_buffer(self.agent_hash)
        if not buf or not buf.content:
            return "⚠️ 缓冲区为空，没什么可发的"

        # TOCTOU 检查：仅 group 渠道有意义（消息在 GroupMessage 中可查）
        new_msgs: List[Dict[str, Any]] = []
        if channel == "group" and group_id:
            try:
                new_msgs = _check_new_messages(
                    self.agent_hash, channel, group_id, buf.updated_at
                )
            except Exception as e:
                logger.warning(f"[ReplyBuffer] TOCTOU 检查失败 {e}")

        if new_msgs:
            # 把新消息注入给 LLM —— 提示先修改再 flush
            lines = [f"⚠️ 自写入 buffer 后群内新增 {len(new_msgs)} 条消息，未发送。请先评估是否需要修改 buffer 内容。", ""]
            for m in new_msgs[:10]:
                sender = f"{m['sender_type']}:{m['sender_hash'] or '?'}"
                content_preview = (m.get("content") or "").replace("\n", " ")[:120]
                lines.append(f"  - [{sender}] {content_preview}")
            lines.append("")
            lines.append("👉 你可以：")
            lines.append("  1. reply_buffer_write(...) 修改内容")
            lines.append("  2. reply_buffer_cancel() 取消发送")
            lines.append("  3. 直接再次 reply_buffer_flush() 忽略新消息强行发送（第二次不再检查）")
            return "\n".join(lines)

        # 执行发送：按渠道路由
        try:
            if channel == "group":
                if not group_id:
                    return "Error: channel=group 时必须提供 group_id"
                msg_id_out = await _send_to_group(
                    group_id=group_id,
                    sender_hash=self.agent_hash,
                    content=buf.content,
                    attachments=buf.attachments,
                )
                _clear_buffer(self.agent_hash)
                # ✅ flush 成功后触发回链（异步，不阻塞 flush 返回）
                _trigger_re_dispatch(self.agent_hash, group_id, buf.content)
                target = f"群 {group_id[:8]}"
                return f"✅ 消息已发送到 {target}（msg_id={msg_id_out}）"

            # channel ∈ {web, wechat, desktop}：推回原渠道
            # ChatHistory 由 ChatService 在 chat() 末尾写入，无需在此重复保存
            ok = await _push_direct_response(
                channel=channel,
                agent_hash=self.agent_hash,
                content=buf.content,
                user_id=user_id,
                to_user_id=to_user_id,
                msg_id=msg_id,
                session_id=session_id,
            )
            _clear_buffer(self.agent_hash)
            preview = buf.content[:80] + ("..." if len(buf.content) > 80 else "")
            if ok:
                target_desc = to or (
                    f"微信用户 {to_user_id[:12]}..." if to_user_id
                    else f"用户 #{user_id}" if user_id
                    else "用户"
                )
                return f"✅ 消息已通过 {channel} 推送 | 目标：{target_desc} | 内容：{preview}"
            # WS 离线 / wechat 失败等：消息已写 ChatHistory，用户下次可见
            return (
                f"⚠️ {channel} 推送失败（用户可能离线），消息已写入个人 ChatHistory\n"
                f"内容预览：{preview}"
            )
        except Exception as e:
            logger.exception(f"[ReplyBuffer] flush 失败: {e}")
            return f"Error: 发送失败: {e}"

    @tool(
        name="reply_buffer_cancel",
        description="取消缓冲区的消息。清空当前 buffer 内容（不影响 stash）。",
        category="agent",
    )
    async def reply_buffer_cancel(self) -> str:
        buf = _get_buffer(self.agent_hash)
        if not buf or not buf.content:
            return "📭 缓冲区为空，无需取消"
        _clear_buffer(self.agent_hash)
        return "🗑️ Buffer 已清空，消息取消发送"

    @tool(
        name="reply_buffer_stash",
        description=(
            "暂存当前 buffer 到 stash（覆盖旧 stash），并清空当前 buffer。"
            "用于临时切换话题（先存好手头的回复，回头再 pop 回来再发）。"
        ),
        category="agent",
    )
    async def reply_buffer_stash(self) -> str:
        buf = _get_buffer(self.agent_hash)
        if not buf or not buf.content:
            return "📭 缓冲区为空，没东西可暂存"
        db = SessionLocal()
        try:
            buf = (
                db.query(AgentBuffer)
                .filter(AgentBuffer.agent_hash == self.agent_hash)
                .first()
            )
            buf.stash_content = buf.content
            buf.stash_attachments = buf.attachments
            buf.stash_version = buf.version
            buf.stashed_at = datetime.utcnow()
            buf.content = ""
            buf.attachments = []
            buf.updated_at = datetime.utcnow()
            db.commit()
            return "📦 Buffer 已暂存到 stash，当前 buffer 已清空"
        finally:
            db.close()

    @tool(
        name="reply_buffer_pop",
        description=(
            "从 stash 恢复最近一次暂存的 buffer，覆盖当前 buffer。"
            "如果当前 buffer 有内容，会被 stash 恢复的内容覆盖（结果会提示）。"
        ),
        category="agent",
    )
    async def reply_buffer_pop(self) -> str:
        buf = _get_buffer(self.agent_hash)
        if not buf or not buf.stash_content:
            return "📭 Stash 为空，没有可恢复的 buffer"
        db = SessionLocal()
        try:
            buf = (
                db.query(AgentBuffer)
                .filter(AgentBuffer.agent_hash == self.agent_hash)
                .first()
            )
            overwritten = bool(buf.content or buf.attachments)
            buf.content = buf.stash_content
            buf.attachments = buf.stash_attachments or []
            buf.version = (buf.version or 0) + 1
            buf.updated_at = datetime.utcnow()
            # stash 仍保留，允许再次 pop（设计选择：保留 stash 副本）
            db.commit()
            suffix = "\n⚠️ 已覆盖当前 buffer" if overwritten else ""
            return f"📤 Stash 已恢复到 buffer（version={buf.version}）{suffix}"
        finally:
            db.close()

    @tool(
        name="reply_buffer_edit",
        description=(
            "修改缓冲区中的已有内容。用 old_string 唯一匹配后替换为 new_string。"
            "适用于写完后发现有个别字词需要修改的场景（不重写全文）。"
            "如果匹配不到或匹配到多处则拒绝修改。"
        ),
        category="agent",
    )
    async def reply_buffer_edit(self, old_string: str, new_string: str) -> str:
        """
        Args:
            old_string: 要被替换的旧文本（必须唯一匹配）
            new_string: 替换后的新文本
        """
        buf = _get_buffer(self.agent_hash)
        if not buf:
            return "⚠️ 缓冲区为空，无法编辑"

        if not buf.content:
            return "⚠️ 缓冲区为空，无法编辑"

        count = buf.content.count(old_string)
        if count == 0:
            return "⚠️ 未找到匹配的文本"
        elif count > 1:
            return f"⚠️ 匹配到 {count} 处，文本不唯一，无法精确替换"

        new_content = buf.content.replace(old_string, new_string, 1)
        _update_buffer_content(self.agent_hash, new_content, buf.version + 1)
        return f"✅ 已编辑缓冲区：替换了 1 处"


__all__ = ["ReplyBufferToolsMixin", "AgentBuffer"]