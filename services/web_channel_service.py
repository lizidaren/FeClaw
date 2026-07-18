"""
Web 渠道服务 - ChatService 的 Web 适配层

职责：
- 会话管理（数据库 CRUD）
- 将 ChatService 事件流转换为 SSE 格式
- 图片处理（保存到 VFS，通知 Agent）
- 不重复实现聊天逻辑，调用统一的 ChatService
"""
import asyncio
import json
import base64
import time
import logging
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union
from sqlalchemy.orm import Session

import httpx

from models.database import ConversationSession
from models.chat import ChatEventType
from config import settings
from services.chat_service import ChatService
from models.chat_input import ChatInput, Attachment
from services.storage_service import StorageService
from services.vfs_image_dedup import VFSImageDeduplicationService

# 渠道定义
CHANNEL_WECHAT = "wechat"
CHANNEL_WEB = "web"
# 移动端渠道：与 Web 共享同一份会话存储，但 topic 前缀独立
# （隔离：mobile 和 web 的会话不会互相出现在对方的列表里）
CHANNEL_MOBILE = "mobile"

logger = logging.getLogger(__name__)


class AgentNotSpecifiedError(ValueError):
    """请求没有显式指定 Agent。"""


class AgentOwnershipError(PermissionError):
    """请求的 Agent 不属于当前用户（不存在时也使用此错误，避免信息泄露）。"""


class SessionNotFoundError(LookupError):
    """会话不存在，或会话的 Agent / channel 与当前请求不匹配。"""


async def _download_and_save_image_to_vfs(image_url: str, user_id: int, agent_hash: str = None) -> Tuple[Optional[str], Optional[bytes]]:
    """
    保存图片到 Agent VFS 工作区（与微信端共用相同逻辑）

    Args:
        image_url: 图片 URL 或 data:image/...;base64,... URI
        user_id: 用户 ID（用于去重回退）
        agent_hash: Agent hash（用于确定存储路径）

    Returns:
        VFS 文件路径，失败返回 None
    """
    if not image_url:
        return None, None

    try:
        # 处理 data URI 或直接 URL
        if image_url.startswith('data:'):
            # data:image/png;base64,... 格式
            header, data = image_url.split(',', 1)
            image_bytes = base64.b64decode(data)
        else:
            # 下载 URL
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(image_url)
                response.raise_for_status()
                image_bytes = response.content

        # 🔄 图片去重检查
        dedup = VFSImageDeduplicationService(user_id=str(user_id), agent_hash=agent_hash)
        existing_path = dedup.find_duplicate(image_bytes)
        if existing_path:
            # 图片已存在，直接复用
            logger.info(f"[FeClaw] Image deduplicated: reusing {existing_path}")
            return existing_path, image_bytes

        # 生成文件名
        timestamp = int(time.time() * 1000)
        filename = f"temp_{timestamp}.png"
        vfs_path = f"/workspace/images/{filename}"

        # 写入文件
        storage = StorageService()
        if agent_hash:
            abs_key = f"feclaw/agents/{agent_hash}/workspace/images/{filename}"
        else:
            abs_key = f"feclaw/user_workspaces/{user_id}/workspace/images/{filename}"
        storage.upload_file(
            file_bytes=image_bytes,
            key=abs_key
        )

        # 注册图片到去重清单
        dedup.register_image(vfs_path, image_bytes)

        logger.info(f"[FeClaw] Saved image to VFS: {vfs_path}")
        return vfs_path, image_bytes

    except Exception as e:
        logger.warning(f"[FeClaw] Failed to save image to VFS: {e}")
        return None, None


class WebChannelService:
    """Web / Mobile 渠道服务 - ChatService 的 HTTP/SSE 适配层。"""

    def __init__(
        self,
        db: Session,
        user_id: int,
        agent_hash: Optional[str] = None,
        channel: str = CHANNEL_WEB,
    ):
        self.db = db
        self.user_id = user_id
        self._agent_hash = agent_hash.strip() if agent_hash else None
        self.channel = channel
        self._agent = None

    def _get_owned_agent(self):
        """解析并缓存当前用户拥有的 Agent；绝不回退到默认 Agent。"""
        if not self._agent_hash:
            raise AgentNotSpecifiedError("agent_hash required")
        if self._agent is None:
            from models.database import AgentProfile

            self._agent = self.db.query(AgentProfile).filter(
                AgentProfile.hash == self._agent_hash,
                AgentProfile.user_id == self.user_id,
            ).first()
            if self._agent is None:
                raise AgentOwnershipError("agent not owned by user")
        return self._agent

    async def resolve_agent(
        self,
        query_agent_hash: Optional[str] = None,
        body_agent_hash: Optional[str] = None,
        channel: Optional[str] = None,
    ):
        """按 body > query 的优先级解析 Agent，并校验归属。"""
        selected_hash = (body_agent_hash or query_agent_hash or "").strip()
        if not selected_hash:
            raise AgentNotSpecifiedError("agent_hash required")
        self._agent_hash = selected_hash
        self._agent = None
        if channel:
            self.channel = channel
        return self._get_owned_agent()

    @property
    def agent_hash(self) -> str:
        """返回已通过 ownership 校验的 Agent hash。"""
        return self._get_owned_agent().hash
    
    async def chat_stream(
        self,
        user_input: str,
        session_id: Optional[str] = None,
        image_url: Optional[str] = None,
        file_path: Optional[str] = None,
        file_name: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        流式聊天 - 调用统一的 ChatService
        
        Args:
            user_input: 用户输入
            session_id: 会话 ID（可选）
            image_url: 图片 URL（可选，支持 base64 data URL）
            file_path: 文件 VFS 路径（可选，前端上传后）
            file_name: 原始文件名（可选）
            channel: 请求渠道（web / mobile）；必须与已有会话的 topic 前缀一致

        Yields:
            SSE 格式的字符串: "event: token\ndata: {...}\n\n"
        """
        effective_channel = channel or self.channel
        if not effective_channel:
            raise ValueError("channel required")
        self.channel = effective_channel

        # 获取或创建会话；已有 session 必须同时匹配 user / agent / channel。
        session = self.get_or_create_session(session_id, channel=effective_channel)
        
        # 图片处理（与微信端相同的逻辑）
        # 如果有图片，保存到 VFS，然后告诉 Agent 图片路径和快速描述
        actual_user_input = user_input
        vfs_path = None
        if image_url:
            # 下载图片并保存到用户VFS工作区
            vfs_path, image_bytes = await _download_and_save_image_to_vfs(image_url, self.user_id, agent_hash=self.agent_hash)
            if vfs_path:
                # Pre-LLM: 用 Qwen3 VL Flash 快速描述图片
                image_desc = None
                try:
                    # 根据 sr_enabled 决定预识别模式
                    from models.database import AgentProfile
                    _ap = self.db.query(AgentProfile).filter(
                        AgentProfile.hash == self.agent_hash
                    ).first()
                    _use_4d = _ap and not _ap.sr_enabled

                    if image_bytes:
                        if _use_4d:
                            from services.image_describer import describe_image_4d
                            image_desc = await describe_image_4d(image_bytes, timeout=15.0)
                        else:
                            from services.image_describer import describe_image_3d
                            image_desc = await describe_image_3d(image_bytes, timeout=15.0)
                except Exception as e:
                    logger.warning(f"[FeClaw] Pre-LLM image description failed: {e}")

                # 构建带描述的提示词
                prefix_parts = [
                    "\n【用户上传图片】",
                    f"图片路径: {vfs_path}",
                    "⚠️ 后续任何工具调用中如需引用此图片，必须使用上述「图片路径」的值，不得使用其他路径或自己构造路径（如 current_image.png 等）。",
                ]
                if image_desc:
                    prefix_parts.append(f"图片概述: {image_desc}")
                    prefix_parts.append("")
                    prefix_parts.append("（以上为预识别描述，供参考。）")

                # 发送预识别结果为主管 SSE 事件（可点击展开）
                if image_desc:
                    yield f"event: pipeline\ndata: {json.dumps({
                        "content": "📷 图片预识别完成",
                        "result_preview": image_desc[:2000],
                        "done": True,
                        "tool": "image_describer",
                        "query": "图片分析"
                    }, ensure_ascii=False)}\n\n"

                prefix = "\n".join(prefix_parts)
                
                if user_input:
                    actual_user_input = prefix + "\n\n" + user_input
                else:
                    actual_user_input = prefix
                logger.info(f"[FeClaw] Image saved to VFS: {vfs_path}" + (f", described ({len(image_desc)} chars)" if image_desc else ""))
            else:
                # 保存失败，提示用户
                actual_user_input = f"【图片上传失败】\n\n{user_input}" if user_input else "【图片上传失败】"
                logger.warning(f"[FeClaw] Failed to save image to VFS")
        
        # 文件/压缩包处理：前端已上传到 VFS，注入路径信息
        if file_path:
            import os as _os
            if file_path.lower().endswith('.zip'):
                # ── Zip 解包 ──
                zip_prefix = f"\n【用户上传压缩包{('（' + file_name + '）') if file_name else ''}】\n"
                try:
                    import zipfile, tempfile, os as zip_os
                    from services.storage_service import StorageService
                    
                    # 从 VFS 下载 zip
                    agent_hash = self.agent_hash
                    abs_key = f"feclaw/agents/{agent_hash}/{file_path.lstrip('/')}"
                    storage = StorageService()
                    zip_bytes = storage.get_file_content(abs_key)
                    
                    if zip_bytes:
                        # 解压到临时目录
                        zip_tmp = tempfile.mktemp(suffix='.zip')
                        with open(zip_tmp, 'wb') as f:
                            f.write(zip_bytes)
                        
                        extract_dir = tempfile.mkdtemp()
                        zip_name_no_ext = _os.path.splitext(_os.path.basename(file_path))[0]
                        extracted_files = []
                        
                        with zipfile.ZipFile(zip_tmp, 'r') as zf:
                            for info in zf.infolist():
                                # 安全校验：防 zip bomb / 路径穿越
                                if info.file_size > 50 * 1024 * 1024:
                                    continue  # 单文件 > 50MB 跳过
                                if info.is_dir():
                                    continue
                                # 防止路径穿越
                                safe_path = _os.path.normpath(info.filename)
                                if safe_path.startswith('..') or _os.path.isabs(safe_path):
                                    continue
                                
                                # 读取到临时文件
                                data = zf.read(info.filename)
                                # 上传到 VFS: uploads/{zipname}/{original_path}
                                vfs_target = f"uploads/{zip_name_no_ext}/{safe_path}"
                                vfs_key = f"feclaw/agents/{agent_hash}/{vfs_target}"
                                storage.upload_file(data, vfs_key)
                                extracted_files.append({
                                    "name": safe_path,
                                    "size": info.file_size,
                                    "vfs_path": vfs_target
                                })
                        
                        # 清理临时文件
                        zip_os.unlink(zip_tmp)
                        zip_os.rmdir(extract_dir)
                        
                        # 构建文件列表上下文
                        file_lines = [f"解压到目录: uploads/{zip_name_no_ext}/", "包含以下文件:"]
                        for ef in sorted(extracted_files, key=lambda x: x['name']):
                            sz = ef['size']
                            sz_str = f"{sz/1024:.0f} KB" if sz < 1024*1024 else f"{sz/1024/1024:.1f} MB"
                            file_lines.append(f"  📄 {ef['name']}  ({sz_str})")
                        file_lines.append("提示：Agent 可使用 VFS 或 parse_file 工具处理各文件。")
                        
                        zip_prefix = "\n".join([zip_prefix] + file_lines) + "\n"
                        
                        if actual_user_input:
                            actual_user_input = zip_prefix + "\n" + actual_user_input
                        else:
                            actual_user_input = zip_prefix
                        logger.info(f"[FeClaw] Zip extracted: {file_name or file_path} -> {len(extracted_files)} files")
                    else:
                        # 下载失败
                        fallback = f"\n【注意】压缩包{file_name or file_path}下载失败，请尝试重新上传。\n"
                        actual_user_input = (fallback + "\n" + actual_user_input) if actual_user_input else fallback
                except Exception as e:
                    logger.warning(f"[FeClaw] Zip extraction failed: {e}")
                    err_msg = f"\n【注意】压缩包{file_name or file_path}解压失败，可直接使用原始文件路径。\n"
                    actual_user_input = (err_msg + "\n" + actual_user_input) if actual_user_input else err_msg
            else:
                # 非 zip 文件：自动提取文档内容（类似图片预识别）
                file_suffix = f"（{file_name}）" if file_name else ""
                file_prefix_parts = [
                    f"\n【用户上传文件{file_suffix}】",
                    f"文件路径: {file_path}",
                ]
                
                # 自动提取文档内容
                parsed_content = None
                _ext = (file_path or "").lower()
                _doc_exts = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".json", ".csv", ".yaml", ".xml"}
                if any(_ext.endswith(e) for e in _doc_exts):
                    try:
                        import tempfile
                        from services.tools.universal_parser import _extract_text
                        agent_hash = self.agent_hash
                        abs_key = f"feclaw/agents/{agent_hash}/{file_path.lstrip('/')}"
                        from services.storage_service import StorageService
                        _storage = StorageService()
                        _data = _storage.get_file_content(abs_key)
                        if _data:
                            _tmp = tempfile.mktemp(suffix=_ext)
                            with open(_tmp, "wb") as f:
                                f.write(_data)
                            parsed_content = _extract_text(_tmp, max_chars=5000)
                            import os as _os2
                            _os2.unlink(_tmp)
                            # 乱码太多 → 快速提取不可用
                            if parsed_content and parsed_content.count("\ufffd") > max(len(parsed_content) * 0.10, 50):
                                parsed_content = None
                    except Exception as e:
                        logger.warning(f"[FeClaw] Auto-parse failed for {file_path}: {e}")
                
                if parsed_content:
                    file_prefix_parts.append(f"文档内容:\n{parsed_content[:4000]}")
                    file_prefix_parts.append("（以上为自动提取的文档内容，Agent 无需再尝试读取文件，可直接基于以上内容回答。如需完整内容或进一步分析，可使用 parse_file 工具。）")
                    yield f"event: pipeline\ndata: {json.dumps({
                        "content": "📄 文档自动解析完成",
                        "result_preview": parsed_content[:2000],
                        "done": True,
                        "tool": "document_extractor",
                        "query": "文档分析"
                    }, ensure_ascii=False)}\n\n"
                    
                    # 后台异步：VFS 向量索引（fire-and-forget）
                    try:
                        from services.vfs_indexer import VfsIndexer
                        asyncio.create_task(
                            VfsIndexer(
                                agent_hash=self.agent_hash,
                                file_path=file_path,
                            ).run()
                        )
                    except Exception as _idx_e:
                        logger.warning(f"[FeClaw] 后台索引失败: {_idx_e}")
                else:
                    # 乱码/扫描 PDF → 自动 VLM OCR（后台处理）
                    file_prefix_parts.append("（正在对文档进行 OCR 识别...）")
                    yield f"event: pipeline\ndata: {json.dumps({
                        "content": "🔍 正在 OCR 识别文档...",
                        "done": False,
                        "tool": "document_extractor",
                    }, ensure_ascii=False)}\n\n"
                    
                    try:
                        from services.storage_service import StorageService as _SS
                        from services.tools.universal_parser import _vlm_chat
                        import fitz as _fitz
                        _data = _SS().get_file_content(abs_key)
                        if _data:
                            import tempfile as _tf
                            _tmp = _tf.mktemp(suffix=_ext)
                            with open(_tmp, "wb") as _fp:
                                _fp.write(_data)
                            _imgs = []
                            with _fitz.open(_tmp) as _doc:
                                for _i in range(min(len(_doc), 15)):
                                    _pix = _doc[_i].get_pixmap(dpi=200)
                                    _p = _tf.mktemp(suffix=".png")
                                    _pix.save(_p)
                                    _imgs.append(_p)
                            if _imgs:
                                _vlm_text = await _vlm_chat(
                                    _imgs,
                                    "识别全部内容，保留题号、题干、数学公式($...$)、表格数据。",
                                    settings.QWEN_API_KEY
                                )
                                import os as _os4
                                for _p in _imgs:
                                    try: _os4.unlink(_p)
                                    except: pass
                                try: _os4.unlink(_tmp)
                                except: pass
                                
                                if _vlm_text and len(_vlm_text) > 100:
                                    file_prefix_parts = [
                                        f"\n【用户上传文件{file_suffix}】",
                                        f"文件路径: {file_path}",
                                        f"【文档 OCR 识别结果】\n{_vlm_text[:6000]}",
                                        "（以上为自动识别结果）",
                                    ]
                                    yield f"event: pipeline\ndata: {json.dumps({
                                        "content": "📄 文档 OCR 识别完成",
                                        "result_preview": _vlm_text[:2000],
                                        "done": True,
                                    }, ensure_ascii=False)}\n\n"
                                else:
                                    yield f"event: pipeline\ndata: {json.dumps({
                                        "content": "❌ OCR 识别失败",
                                        "done": True,
                                    }, ensure_ascii=False)}\n\n"
                    except Exception as e:
                        logger.warning(f"[FeClaw] VLM OCR failed: {e}")
                        yield f"event: pipeline\ndata: {json.dumps({
                            "content": "❌ OCR 处理异常",
                            "done": True,
                        }, ensure_ascii=False)}\n\n"
                    yield f"event: pipeline\ndata: {json.dumps({
                        "content": "📄 文档需专用工具解析",
                        "done": True,
                        "tool": "document_extractor",
                        "query": "文档分析"
                    }, ensure_ascii=False)}\n\n"
                
                file_prefix = "\n".join(file_prefix_parts) + "\n"
                if actual_user_input:
                    actual_user_input = file_prefix + "\n" + actual_user_input
                else:
                    actual_user_input = file_prefix
                logger.info(f"[FeClaw] File attached: {file_path}" + (f" (parsed {len(parsed_content)} chars)" if parsed_content else ""))
        
        # 在持久化本轮用户消息之前截取历史，避免 ChatService 再次收到当前输入。
        session_messages = self._parse_messages(session.messages)
        persisted_images = (
            [{"url": vfs_path or image_url}]
            if image_url
            else None
        )
        persisted_files = (
            [{"path": file_path, "name": file_name or file_path.rsplit("/", 1)[-1]}]
            if file_path
            else None
        )

        # ── IM Agent 路由：直接走 IRQ → WorkLoop（异步处理）──
        # IM Agent 收到消息后应自主决策（不再一问一答），所以 web/mobile 私聊也走 IRQ。
        if self._is_im_agent():
            self.add_message(
                session,
                "user",
                user_input,
                images=persisted_images,
                files=persisted_files,
            )
            yield self._build_queued_sse(actual_user_input, session, effective_channel)
            return

        # 调用统一的 ChatService，并把请求 channel/session 原样透传。
        chat_service = ChatService(
            agent_hash=self.agent_hash,
            channel=effective_channel,
            session_id=session.session_id,
        )

        # 从 ConversationSession 加载“本轮之前”的历史消息并注入 ChatContext。
        if session_messages:
            chat_service.context.history = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in session_messages
                if m.get("role") in {"user", "assistant", "tool"}
            ]
            logger.info(f"[FeClaw] Loaded {len(session_messages)} messages from session {session.session_id}")
        # ConversationSession 已提供当前会话历史，禁止 ChatService 再加载跨会话历史。
        chat_service._history_loaded_from_session = True
        logger.info(f"[FeClaw] history_loaded_from_session=True (session={session.session_id}, messages={len(session_messages)})")

        # 历史装载完成后才持久化当前用户消息，确保模型只接收一次本轮输入。
        self.add_message(
            session,
            "user",
            user_input,
            images=persisted_images,
            files=persisted_files,
        )

        full_response = ""
        usage = {"input_tokens": 0, "output_tokens": 0}
        tool_calls: List[Dict[str, Any]] = []

        # 构建 ChatInput（新签名，含附件信息）
        chat_attachments = []
        if image_url and vfs_path:
            chat_attachments.append(Attachment(type="image", url=vfs_path))

        async for event in chat_service.chat(input=ChatInput(text=actual_user_input, attachments=chat_attachments)):
            if event.type == ChatEventType.TEXT:
                # 真正的流式：直接输出每个 token
                full_response += event.content
                yield f"event: token\ndata: {json.dumps({'content': event.content}, ensure_ascii=False)}\n\n"
            
            elif event.type == ChatEventType.PRE_TOOL:
                # 工具调用前的思考（可选显示）
                yield f"event: thinking\ndata: {json.dumps({'content': event.content}, ensure_ascii=False)}\n\n"
            
            elif event.type == ChatEventType.TOOL_CALL:
                # Web 协议要求每次工具调用都有稳定 ID，供 tool_result 和历史记录关联。
                tool_call_id = uuid.uuid4().hex
                tool_call = {
                    "id": tool_call_id,
                    "name": event.tool_name or "",
                    "args": event.tool_args if event.tool_args is not None else event.content,
                    "status": "pending",
                }
                tool_calls.append(tool_call)
                yield f"event: tool\ndata: {json.dumps({
                    'content': event.content,
                    'tool_name': event.tool_name,
                    'tool_args': event.tool_args,
                    'tool_call_id': tool_call_id,
                }, ensure_ascii=False)}\n\n"
                # 工具调用后可能长时间无数据（工具执行中），加填充强制 CDN flush 已输出的内容
                yield f": flush {' ' * 2048}\n\n"

            elif event.type == ChatEventType.TOOL_RESULT:
                # 优先关联最近一个同名且尚未完成的调用。
                matched_call = next(
                    (
                        call for call in reversed(tool_calls)
                        if call["status"] == "pending"
                        and (not event.tool_name or call["name"] == event.tool_name)
                    ),
                    None,
                )
                if matched_call is None:
                    matched_call = {
                        "id": uuid.uuid4().hex,
                        "name": event.tool_name or "",
                        "args": None,
                        "status": "pending",
                    }
                    tool_calls.append(matched_call)
                matched_call["result"] = event.tool_result or event.content
                matched_call["status"] = "done"
                yield f"event: tool_result\ndata: {json.dumps({
                    'content': event.tool_result or event.content,
                    'tool_name': event.tool_name,
                    'tool_call_id': matched_call['id'],
                }, ensure_ascii=False)}\n\n"
            
            elif event.type == ChatEventType.DONE:
                usage = {
                    "input_tokens": event.metadata.get("input_tokens", 0),
                    "output_tokens": event.metadata.get("output_tokens", 0)
                }
            
            elif event.type == ChatEventType.KEEPALIVE:
                # 心跳注释（加填充字节强制触发 CDN flush）
                yield ": keepalive " + ("x" * 1024) + "\n\n"

            elif event.type == ChatEventType.PIPELINE:
                # 流水线状态更新
                payload = {'content': event.content}
                if event.metadata:
                    payload['result_preview'] = event.metadata.get('result_preview', '')
                    payload['query'] = event.metadata.get('query', '')
                    payload['tool'] = event.metadata.get('tool', '')
                    payload['done'] = event.metadata.get('done', False)
                    payload['error'] = event.metadata.get('error', False)
                yield f"event: pipeline\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

            elif event.type == ChatEventType.SEARCH_PROGRESS:
                # 搜索结果的流式内容
                payload = {'content': event.content}
                if event.metadata:
                    payload['query'] = event.metadata.get('query', '')
                    payload['tool'] = event.metadata.get('tool', '')
                yield f"event: search_progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

            elif event.type == ChatEventType.REASONING:
                # 深度思考推理过程
                yield f"event: reasoning\ndata: {json.dumps({'content': event.content}, ensure_ascii=False)}\n\n"
            
            elif event.type == ChatEventType.ERROR:
                yield f"event: error\ndata: {json.dumps({'code': 'LLM_ERROR', 'message': event.error_message}, ensure_ascii=False)}\n\n"
                return
        
        # 保存 AI 回复及结构化工具调用，供 session detail 完整恢复。
        self.add_message(
            session,
            "assistant",
            full_response,
            tool_calls=tool_calls or None,
        )

        # 返回完成事件
        yield f"event: done\ndata: {json.dumps({'session_id': session.session_id, 'usage': usage}, ensure_ascii=False)}\n\n"

    def get_or_create_session(
        self,
        session_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> ConversationSession:
        """获取或创建会话，并严格校验 user / agent / channel 绑定。"""
        effective_channel = channel or self.channel
        expected_topic_prefix = f"[{effective_channel}]"

        if session_id:
            session = self.db.query(ConversationSession).filter(
                ConversationSession.session_id == session_id,
                ConversationSession.user_id == self.user_id,
            ).first()

            if (
                session is None
                or session.agent_hash != self.agent_hash
                or not (session.topic or "").startswith(expected_topic_prefix)
            ):
                raise SessionNotFoundError("Session not found")
            return session

        # 创建新会话
        new_session_id = f"sess_{uuid.uuid4().hex[:16]}"
        now = datetime.utcnow()

        session = ConversationSession(
            session_id=new_session_id,
            agent_hash=self.agent_hash,
            user_id=self.user_id,
            messages="[]",
            created_at=now,
            updated_at=now,
            message_count=0,
        )

        # classic 会话不写 channel 列（该列受 IM Agent 唯一约束），渠道存在 topic 前缀。
        session.topic = expected_topic_prefix

        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)

        return session
    
    def _parse_messages(self, messages_json: str) -> list:
        """安全解析消息 JSON"""
        try:
            return json.loads(messages_json)
        except json.JSONDecodeError:
            logger.warning(f"[FeClaw] Failed to parse messages JSON")
            return []

    def get_session_list(self, limit: int = 20, agent_hash: Optional[str] = None) -> list:
        """获取会话列表，可选按 agent_hash 筛选"""
        q = self.db.query(ConversationSession).filter(
            ConversationSession.user_id == self.user_id,
            ConversationSession.is_archived == False
        ).filter(
            ConversationSession.topic.like(f"[{CHANNEL_WEB}]%")
        )
        if agent_hash:
            q = q.filter(ConversationSession.agent_hash == agent_hash)
        sessions = q.order_by(
            ConversationSession.updated_at.desc()
        ).limit(limit).all()

        result = []
        for session in sessions:
            messages = self._parse_messages(session.messages)
            first_user_msg = ""
            for msg in messages:
                if msg.get("role") == "user":
                    first_user_msg = msg.get("content", "")[:50]
                    break
            
            result.append({
                "session_id": session.session_id,
                "message_count": session.message_count,
                "created_at": session.created_at.strftime("%Y-%m-%d %H:%M") if session.created_at else "",
                "updated_at": session.updated_at.strftime("%Y-%m-%d %H:%M") if session.updated_at else "",
                "first_message": first_user_msg
            })
        
        return result
    
    def get_session(self, session_id: str) -> Optional[dict]:
        """获取会话详情"""
        session = self.db.query(ConversationSession).filter(
            ConversationSession.session_id == session_id,
            ConversationSession.user_id == self.user_id
        ).first()
        
        if not session:
            return None

        messages = self._parse_messages(session.messages)

        return {
            "session_id": session.session_id,
            "messages": messages,
            "message_count": session.message_count,
            "created_at": session.created_at.strftime("%Y-%m-%d %H:%M") if session.created_at else "",
            "updated_at": session.updated_at.strftime("%Y-%m-%d %H:%M") if session.updated_at else ""
        }
    
    def archive_session(self, session_id: str) -> bool:
        """归档会话"""
        session = self.db.query(ConversationSession).filter(
            ConversationSession.session_id == session_id,
            ConversationSession.user_id == self.user_id
        ).first()
        
        if not session:
            return False
        
        session.is_archived = True
        self.db.commit()
        return True
    
    def add_message(
        self,
        session: ConversationSession,
        message_or_role: Union[Dict[str, Any], str],
        content: Optional[str] = None,
        *,
        timestamp: Optional[str] = None,
        images: Optional[List[Dict[str, Any]]] = None,
        files: Optional[List[Dict[str, Any]]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """添加消息，并完整保留附件与工具调用等结构化字段。"""
        if isinstance(message_or_role, dict):
            source = message_or_role
            message: Dict[str, Any] = {
                "role": source.get("role", "user"),
                "content": source.get("content", ""),
                "timestamp": source.get("timestamp") or datetime.utcnow().isoformat(),
            }
            for key in ("images", "files", "tool_calls"):
                if source.get(key) is not None:
                    message[key] = source[key]
        else:
            message = {
                "role": message_or_role,
                "content": content or "",
                "timestamp": timestamp or datetime.utcnow().isoformat(),
            }
            if images is not None:
                message["images"] = images
            if files is not None:
                message["files"] = files
            if tool_calls is not None:
                message["tool_calls"] = tool_calls

        messages = self._parse_messages(session.messages)
        messages.append(message)
        session.messages = json.dumps(messages, ensure_ascii=False)
        session.message_count = len(messages)
        session.updated_at = datetime.utcnow()
        self.db.commit()

    # ========== IM Agent IRQ 路由 ==========

    def _is_im_agent(self) -> bool:
        """检查当前 Agent 是否为 IM 模式（agent_mode='im'）。"""
        from models.database import AgentProfile
        agent = self.db.query(AgentProfile).filter(
            AgentProfile.hash == self.agent_hash
        ).first()
        return bool(agent and getattr(agent, "agent_mode", "classic") == "im")

    def _build_queued_sse(self, user_input: str, session, channel: str) -> str:
        """构造 IM Agent 占位 SSE 事件并投递 IRQ。

        - 前端 SSE 流立刻以 queued 事件结束
        - WorkLoop 异步处理 IRQ，处理完后通过 WebSocket push 回前端
        """
        from services.interrupt_controller import (
            InterruptController,
            Interrupt,
            InterruptType,
            Priority,
        )

        ic = InterruptController.instance()
        ic.dispatch(Interrupt(
            irq_type=InterruptType.MESSAGE,
            agent_hash=self.agent_hash,
            priority=Priority.HIGH,
            payload={
                "channel": channel,
                "user_id": self.user_id,
                "agent_hash": self.agent_hash,
                "session_id": session.session_id,
                "msg_id": None,
                "trigger_content": user_input[:1000],
                "trigger_sender": "用户",
            },
        ))
        logger.info(
            f"[WebChannel] IM Agent IRQ 投递 agent={self.agent_hash} "
            f"user={self.user_id} session={session.session_id}"
        )
        payload = {
            "status": "queued",
            "agent_hash": self.agent_hash,
            "channel": channel,
            "session_id": session.session_id,
            "message": "IM Agent 收到消息，将在 WorkLoop 中异步处理。回复将通过 WebSocket 推送。",
        }
        return f"event: queued\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # ========== Gen 2 (IM Agent) Channel-Driven Stream ==========

    def get_or_create_im_session(self, channel: str) -> ConversationSession:
        """按 (agent_hash, channel) 获取或创建会话（Gen 2 IM Agent 用）。

        与经典路径 (`get_or_create_session`) 不同：
        - Gen 2 不允许客户端指定 session_id — 由服务端基于渠道组合 key 推断
        - 同一 IM Agent 的同一渠道在唯一约束下只能有一个会话
        """
        import uuid

        existing = (
            self.db.query(ConversationSession)
            .filter(
                ConversationSession.agent_hash == self.agent_hash,
                ConversationSession.channel == channel,
                ConversationSession.user_id == self.user_id,
            )
            .first()
        )
        if existing:
            return existing

        new_session_id = f"sess_{uuid.uuid4().hex[:16]}"
        now = datetime.utcnow()
        session = ConversationSession(
            session_id=new_session_id,
            agent_hash=self.agent_hash,
            user_id=self.user_id,
            channel=channel,
            messages="[]",
            created_at=now,
            updated_at=now,
            message_count=0,
        )
        # topic 渠道前缀保留（与 web/mobile 一致）
        session.topic = f"[{channel}]"
        try:
            self.db.add(session)
            self.db.commit()
            self.db.refresh(session)
        except Exception as e:
            # 唯一约束触发（race）：回滚后用直查返回已有会话
            self.db.rollback()
            logger.warning(
                f"[WebChannel] IM session race on (agent_hash={self.agent_hash}, channel={channel}): {e}"
            )
            existing = (
                self.db.query(ConversationSession)
                .filter(
                    ConversationSession.agent_hash == self.agent_hash,
                    ConversationSession.channel == channel,
                    ConversationSession.user_id == self.user_id,
                )
                .first()
            )
            if existing:
                return existing
            raise
        return session

    async def _im_chat_stream(
        self,
        user_input: str,
        channel: str,
        image_url: Optional[str] = None,
        file_path: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Gen 2 (IM Agent) Channel-Driven 流式聊天

        与经典路径的关键差异：
        - **忽略客户端传入的 session_id** — 服务端按 (agent_hash, channel) 自动组合会话
        - 消息直接 dispatch 给协处理器（IM Agent 自驱，不再一问一答）
        - CoprocessorService 跑在常驻协程中，IRQ 经 WorkSession 异步处理后通过 WS push 回复

        Channel 校验在路由层 (`routers/feclaw_chat.py`) 已完成。
        """
        from services.interrupt_controller import (
            InterruptController,
            Interrupt,
            InterruptType,
            Priority,
        )

        # 1. 按 (agent_hash, channel) 取/建会话
        session = self.get_or_create_im_session(channel)

        # 2. 图片/文件预处理（与经典路径同源，未来可下沉复用）
        actual_user_input = user_input
        vfs_path = None
        if image_url:
            vfs_path, _ = await _download_and_save_image_to_vfs(
                image_url, self.user_id, agent_hash=self.agent_hash
            )
            if vfs_path:
                prefix = (
                    "\n【用户上传图片】\n"
                    f"图片路径: {vfs_path}\n"
                    "⚠️ 后续工具调用引用此图片必须使用以上路径值。"
                )
                actual_user_input = (
                    f"{prefix}\n\n{user_input}" if user_input else prefix
                )
            else:
                actual_user_input = (
                    f"【图片上传失败】\n\n{user_input}" if user_input else "【图片上传失败】"
                )

        # 3. 记录用户消息到会话，保留附件结构化元数据。
        self.add_message(
            session,
            "user",
            user_input,
            images=([{"url": vfs_path or image_url}] if image_url else None),
            files=(
                [{"path": file_path, "name": file_name or file_path.rsplit("/", 1)[-1]}]
                if file_path
                else None
            ),
        )

        # 4. 投递 IRQ → Coprocessor WorkLoop（不调用 ChatService 直接 chat）
        ic = InterruptController.instance()
        ic.dispatch(Interrupt(
            irq_type=InterruptType.MESSAGE,
            agent_hash=self.agent_hash,
            priority=Priority.HIGH,
            payload={
                "channel": channel,
                "user_id": self.user_id,
                "agent_hash": self.agent_hash,
                "session_id": session.session_id,
                "msg_id": None,
                "trigger_content": actual_user_input[:1000],
                "trigger_sender": "用户",
            },
        ))
        logger.info(
            f"[WebChannel:IM] IRQ dispatched agent={self.agent_hash} "
            f"channel={channel} user={self.user_id} session={session.session_id}"
        )

        # 5. SSE 流：返回 queued 占位 + done 终止
        queued_payload = {
            "status": "queued",
            "agent_hash": self.agent_hash,
            "channel": channel,
            "session_id": session.session_id,
            "message": "IM Agent 收到消息，将在 WorkLoop 中异步处理。回复将通过 WebSocket 推送。",
        }
        yield f"event: queued\ndata: {json.dumps(queued_payload, ensure_ascii=False)}\n\n"
        yield f"event: done\ndata: {json.dumps({'session_id': session.session_id, 'usage': {}}, ensure_ascii=False)}\n\n"
