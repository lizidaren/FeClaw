"""
微信接入路由 (iLink协议)

提供微信登录、消息收发等功能
"""
import base64
import os
import pickle
import re
import threading
import traceback
from datetime import datetime

import asyncio
import aiohttp
import logging
from typing import Optional, List, Dict

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db, User
from services.wechat_service import wechat_service, WeChatService
from config import settings
from services.wechat_channel_service import WeChatChannelService
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wechat", tags=["微信接入"])

# ========== 请求/响应模型 ==========

class QRCodeResponse(BaseModel):
    """二维码响应"""
    qrcode_token: str
    qrcode_image: Optional[str] = None
    qrcode_url: Optional[str] = None


class QRCodeStatusResponse(BaseModel):
    """二维码状态响应"""
    status: str  # idle/waiting/scanned/confirmed/expired
    bot_token: Optional[str] = None
    ilink_bot_id: Optional[str] = None
    ilink_user_id: Optional[str] = None
    base_url: Optional[str] = None


class BindRequest(BaseModel):
    """绑定请求"""
    ilink_user_id: str
    agent_hash: str = ""  # which agent to bind to
    bot_token: Optional[str] = None
    ilink_bot_id: Optional[str] = None
    base_url: Optional[str] = None


class BindResponse(BaseModel):
    """绑定响应"""
    status: str
    message: str
    binding_id: Optional[int] = None


class WeChatMessageResponse(BaseModel):
    """微信消息响应"""
    id: int
    direction: str
    content: str
    message_type: str
    created_at: str


class SendMessageRequest(BaseModel):
    """发送消息请求"""
    to_user_id: str
    text: str
    context_token: Optional[str] = None


class SendMessageResponse(BaseModel):
    """发送消息响应"""
    status: str
    success: bool


class LoginStatusResponse(BaseModel):
    """登录状态响应"""
    logged_in: bool
    status: str = "idle"  # idle/waiting/scanned/confirmed/expired
    ilink_bot_id: Optional[str] = None
    ilink_user_id: Optional[str] = None


# ========== 全局变量 ==========

# 保存登录状态供轮询使用
_login_data: dict = {}


# ========== 路由 ==========

@router.get("/qrcode", response_model=QRCodeResponse)
async def get_qrcode(user: User = Depends(get_current_user)):
    """
    获取登录二维码

    返回二维码token和图片内容
    """
    try:
        qr_data = await wechat_service.get_qrcode()
        return QRCodeResponse(**qr_data)
    except Exception as e:
        logger.error(f"[WeChat] Failed to get qrcode: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": "获取二维码失败，请稍后重试"}
        )


@router.get("/status", response_model=QRCodeStatusResponse)
async def get_qrcode_status(user: User = Depends(get_current_user)):
    """
    检查登录状态

    轮询此接口直到 status 变为 confirmed
    """
    try:
        # 先获取二维码（如果还没有）
        token = wechat_service.login_state.get("qrcode_token")
        if not token:
            # 获取新二维码
            await wechat_service.get_qrcode()
            token = wechat_service.login_state.get("qrcode_token")

        status_data = await wechat_service.check_qrcode_status(token)
        logger.debug(f"[WeChat] /status returning: {status_data}")
        return QRCodeStatusResponse(**status_data)
    except ValueError as e:
        # 没有 token，返回 idle 状态
        logger.info(f"[WeChat] /status: no token, returning idle: {e}")
        return QRCodeStatusResponse(status="idle")
    except Exception as e:
        logger.error(f"[WeChat] Failed to check status: {e}", exc_info=True)
        error_msg = str(e) or type(e).__name__
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": error_msg}
        )


@router.post("/bind", response_model=BindResponse)
async def bind_wechat(request: BindRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    绑定微信账号

    在扫码确认后调用此接口完成绑定
    """
    try:
        # user 已通过 get_current_user 验证，直接使用
        current_user = user

        # 前端通过代理直接调 iLink 轮询状态，所以 _login_state 可能没同步
        # 不再依赖 _login_state["status"]，直接用请求数据绑定
        # 同时更新 _login_state（兼容其他端点）
        # 验证 agent_hash 属于当前用户
        if not request.agent_hash:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="agent_hash 不能为空，请从 Agent 设置页进入绑定"
            )
        from models.agent_profile import AgentProfile
        agent_owner = db.query(AgentProfile).filter(
            AgentProfile.hash == request.agent_hash,
            AgentProfile.user_id == current_user.id
        ).first()
        if not agent_owner:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agent not found or does not belong to the current user"
            )

        login_data = {
            "bot_token": request.bot_token or "",
            "ilink_bot_id": request.ilink_bot_id or "",
            "ilink_user_id": request.ilink_user_id or "",
            "base_url": request.base_url or "",
            "agent_hash": request.agent_hash,
        }

        # 更新全局状态（供其他端点使用）
        wechat_service.login_state.update({
            "status": "confirmed",
            **login_data
        })

        binding = wechat_service.bind_user(
            user_id=current_user.id,
            wx_openid=request.ilink_user_id,
            login_data=login_data
        )

        # ========== Fake Login: 保存 SDK 凭证到 DB ==========
        # 构造 Credentials 对象并存入 WeChatBinding.ilink_token
        # 这样 start_polling() 会从 DB 加载凭证，SDK 的 start() 会跳过 login()
        wechat_service.save_sdk_credentials(
            user_id=current_user.id,
            bot_token=request.bot_token or "",
            ilink_bot_id=request.ilink_bot_id or "",
            ilink_user_id=request.ilink_user_id or "",
            base_url=request.base_url or "",
            agent_hash=request.agent_hash
        )

        # 启动消息接收（从 DB 加载凭证，无需传 login_data）
        await wechat_service.start_polling(user_id=current_user.id)

        # ========== 打招呼逻辑已禁用 ==========
        # # 确保工作区文件存在
        # ensure_agent_files(str(current_user.id), db)
        #
        # # 检查工作区是否已初始化
        # workspace_initialized = is_workspace_initialized(str(current_user.id))
        #
        # if workspace_initialized:
        #     # 工作区已初始化，发送打招呼消息
        #     try:
        #         greeting_msg = await _generate_greeting_message(str(current_user.id))
        #         if greeting_msg:
        #             await wechat_service.send_message(
        #                 to_user_id=request.ilink_user_id,
        #                 text=greeting_msg
        #             )
        #             logger.info(f"[WeChat] Sent greeting to {request.ilink_user_id}")
        #     except Exception as e:
        #         logger.error(f"[WeChat] Failed to send greeting: {e}")
        # else:
        #     # 未初始化，发送默认打招呼消息
        #     try:
        #         default_greeting = "你好！我是 Fe，很高兴认识你！我是一个AI学习助手，可以帮助你解答问题、制定学习计划等。有什么我可以帮助你的吗？"
        #         await wechat_service.send_message(
        #             to_user_id=request.ilink_user_id,
        #             text=default_greeting
        #         )
        #         logger.info(f"[WeChat] Sent default greeting to {request.ilink_user_id}")
        #     except Exception as e:
        #         logger.error(f"[WeChat] Failed to send default greeting: {e}")

        return BindResponse(
            status="success",
            message="绑定成功",
            binding_id=binding.id
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[WeChat] Failed to bind: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": "绑定失败，请稍后重试"}
        )


@router.get("/messages", response_model=List[WeChatMessageResponse])
async def get_messages(user: User = Depends(get_current_user), limit: int = 50, db: Session = Depends(get_db)):
    """
    获取聊天记录

    Args:
        limit: 返回条数
    """
    try:
        messages = wechat_service.get_messages(user.id, limit)
        return [WeChatMessageResponse(**msg) for msg in messages]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[WeChat] Failed to get messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": str(e)}
        )


@router.post("/send", response_model=SendMessageResponse)
async def send_message(request: SendMessageRequest, user: User = Depends(get_current_user)):
    """
    发送消息

    Args:
        to_user_id: 接收方用户ID
        text: 消息文本
        context_token: 上下文token
    """
    try:
        success = await wechat_service.send_message(
            to_user_id=request.to_user_id,
            text=request.text,
            context_token=request.context_token
        )
        return SendMessageResponse(
            status="success" if success else "error",
            success=success
        )
    except Exception as e:
        logger.error(f"[WeChat] Failed to send message: {e}")
        return SendMessageResponse(
            status="error",
            success=False
        )


@router.get("/login-status", response_model=LoginStatusResponse)
async def login_status(user: User = Depends(get_current_user)):
    """
    检查登录状态
    """
    login_state = wechat_service.login_state
    return LoginStatusResponse(
        logged_in=login_state.get("status") == "confirmed",
        ilink_bot_id=login_state.get("ilink_bot_id"),
        ilink_user_id=login_state.get("ilink_user_id")
    )


class BindingInfoResponse(BaseModel):
    """绑定信息响应"""
    bound: bool
    openid: Optional[str] = None
    ilink_bot_id: Optional[str] = None


@router.get("/binding", response_model=BindingInfoResponse)
async def get_binding_info(user: User = Depends(get_current_user)):
    """
    查询当前用户的微信绑定状态
    """
    binding = wechat_service.get_binding_by_user(user.id)
    if binding:
        return BindingInfoResponse(
            bound=True,
            openid=binding.wx_openid,
            ilink_bot_id=binding.ilink_bot_id,
        )
    return BindingInfoResponse(bound=False)


@router.post("/logout")
async def logout(user: User = Depends(get_current_user)) -> dict:
    """
    解绑微信 - 清除 bot 鉴权数据

    同时清理本地状态和数据库绑定记录
    """
    try:
        current_user = user

        # 1. 停止长轮询
        await wechat_service.stop_polling()

        # 2. 清除数据库绑定记录
        wechat_service.unbind_user(current_user.id)

        # 3. 重置本地状态
        wechat_service.reset_login_state()
        return {"status": "success", "message": "已解绑"}
    except Exception as e:
        logger.error(f"[WeChat] Failed to logout: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": str(e)}
        )


@router.delete("/unbind")
async def unbind_wechat(user: User = Depends(get_current_user)) -> dict:
    """
    解绑微信 - DELETE 方法（与前端对齐）
    """
    try:
        await wechat_service.stop_polling()
        wechat_service.unbind_user(user.id)
        wechat_service.reset_login_state()
        return {"status": "success", "message": "已解绑"}
    except Exception as e:
        logger.error(f"[WeChat] Failed to unbind: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": str(e)}
        )



async def _download_and_save_image_to_vfs(image_url: str, user_id: int, agent_hash: str = None) -> Optional[str]:
    """
    保存图片到 Agent VFS 工作区

    Args:
        image_url: 图片 URL 或 data:image/...;base64,... URI
        user_id: 用户 ID（仅用于去重）
        agent_hash: Agent hash（用于确定存储路径）

    Returns:
        VFS 文件路径，失败返回 None
    """
    import base64
    import time

    if not image_url:
        return None

    try:
        # 处理 data URI 或直接 URL
        if image_url.startswith('data:'):
            header, data = image_url.split(',', 1)
            image_bytes = base64.b64decode(data)
        else:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(image_url)
                response.raise_for_status()
                image_bytes = response.content

        # 图片去重
        from services.vfs_image_dedup import VFSImageDeduplicationService
        dedup = VFSImageDeduplicationService(user_id=str(user_id), agent_hash=agent_hash)
        existing_path = dedup.find_duplicate(image_bytes)
        if existing_path:
            logger.info(f"[WeChat] Image deduplicated: reusing {existing_path}")
            return existing_path

        # 生成文件名
        timestamp = int(time.time() * 1000)
        filename = f"temp_{timestamp}.png"
        vfs_path = f"/workspace/images/{filename}"

        # 写入文件
        from services.storage_service import StorageService
        storage = StorageService()
        if agent_hash:
            # 保存到 Agent 工作区（FUSE 可见）
            abs_key = f"feclaw/agents/{agent_hash}/workspace/images/{filename}"
        else:
            # 回退：保存到用户工作区
            abs_key = f"feclaw/user_workspaces/{user_id}/workspace/images/{filename}"
        vfs_path = f"/workspace/images/{filename}"
        storage.upload_file(
            file_bytes=image_bytes,
            key=abs_key
        )
        
        # 注册图片到去重清单
        dedup.register_image(vfs_path, image_bytes)

        logger.info(f"[WeChat] Saved image to VFS: {vfs_path}")
        return vfs_path

    except Exception as e:
        logger.warning(f"[WeChat] Failed to save image to VFS: {e}")
        return None


# ========== 消息处理集成 ==========

_message_handler_setup = False


async def setup_message_handler():
    """设置消息处理器 - 对接 AI 服务（防止重复注册）"""
    global _message_handler_setup
    if _message_handler_setup:
        return
    _message_handler_setup = True

    # 纯图片（无文字）缓存：user_id → {"path": ..., "desc": ...}
    # 图片先到时缓存3D描述，等用户文字来了再注入
    _PENDING_IMAGE_CACHE: Dict[str, dict] = {}
    _PENDING_IMAGE_LOCK = threading.Lock()

    async def handle_wechat_message(msg: dict):
        """处理微信消息"""
        import json as _json  # noqa
        import time as _time
        _trace_ts = int(_time.time() * 1000)
        _trace_dir = f"/tmp/wechat_trace/{_trace_ts}"
        os.makedirs(_trace_dir, exist_ok=True)
        _trace_log = []
        def _trace(desc, **kv):
            entry = {"step": desc}
            entry.update(kv)
            _trace_log.append(entry)
            extra = " | ".join(f"{k}={v}" for k, v in kv.items()) if kv else ""
            line = f"[WX_TRACE] {desc}"
            if extra:
                line += f" | {extra}"
            logger.debug(line)
        # 性能计时
        _t0 = _time.time()
        _timing = {"entry": _t0}
        def _log_perf(stage: str):
            elapsed = _time.time() - _t0
            logger.info(f"[PERF] handle_wechat_message: {stage} ({elapsed:.2f}s from entry)")
            _timing[stage] = elapsed

        _log_perf("msg_received")
        def _write_trace_json():
            try:
                _trace_file = os.path.join(_trace_dir, "trace.json")
                _safe_msg = {}
                for _k, _v in msg.items():
                    try:
                        if isinstance(_v, bytes):
                            _safe_msg[_k] = f"<bytes len={len(_v)}>"
                        elif isinstance(_v, (str, int, float, bool, type(None))):
                            _safe_msg[_k] = _v
                        else:
                            _safe_msg[_k] = str(_v)[:500]
                    except Exception:
                        _safe_msg[_k] = "<unserializable>"
                with open(_trace_file, "w", encoding="utf-8") as _f:
                    _json.dump({"timestamp": _trace_ts, "trace": _trace_log, "msg": _safe_msg}, _f, ensure_ascii=False, indent=2, default=str)
                _raw_file = os.path.join(_trace_dir, "full_msg.json")
                with open(_raw_file, "w", encoding="utf-8") as _f:
                    _json.dump(msg, _f, ensure_ascii=False, indent=2, default=str)
            except Exception:
                pass
        _trace("handle_wechat_message 入口",
               ct=msg.get('content_type'),
               url=str(msg.get('image_url'))[:80] if msg.get('image_url') else None,
               media=str(msg.get('_media_info'))[:200] if msg.get('_media_info') else None)
        try:
            _trace("try 外层开始")
            msg_type = msg.get("msg_type")
            from_user_id = msg.get("from_user_id")
            content = msg.get("content", {})
            context_token = msg.get("context_token")
            # 注意：msg 里有两层 content_type
            # 1. msg["content_type"] - 我们设置的语义类型（text/image/voice）
            # 2. msg["raw"]["content_type"] - SDK 协议的字段，不要混淆
            # 因此先取 msg 层，再用 raw 取 image_url
            content_type = msg.get("content_type", "text")
            raw_msg = msg.get("raw", {})
            # 优先使用 msg 层的 image_url（已由 wechat_service 保存到 VFS）
            image_url = msg.get("image_url") or None
            if not image_url:
                # 回退：从 raw 消息中提取（旧 SDK 格式）
                images = raw_msg.get("images", [])
                image_url = images[0].get("url") if images else None
                _trace("分支: image_url为空, 从raw提取", image_url=str(image_url)[:80] if image_url else None)
            else:
                _trace("分支: image_url 已有值", image_url=str(image_url)[:80])
            _trace("关键变量提取完成",
                   from_user_id=str(from_user_id)[:30] if from_user_id else None,
                   content_type=content_type,
                   is_image=(content_type == "image"),
                   user_input_raw=str(msg.get("text", ""))[:50] if msg.get("text") else None,
                   image_url=str(image_url)[:80] if image_url else None,
                   msg_type=msg_type)
            # 调试：打印完整的msg内容
            import json
            debug_msg = {
                "content_type": content_type,
                "image_url": image_url,
                "raw_keys": list(raw_msg.keys()) if isinstance(raw_msg, dict) else type(raw_msg),
                "msg_text": msg.get("text", "")[:50] if msg.get("text") else None
            }
            logger.debug(f"[WeChat] 消息调试: {json.dumps(debug_msg, ensure_ascii=False)}")
            logger.debug(f"[WeChat] DEBUG raw images: {images if 'images' in dir() and 'images' in locals() else '(not set)'}, extracted url: {image_url}")
            logger.debug(f"[WeChat] DEBUG raw keys: {list(raw_msg.keys()) if isinstance(raw_msg, dict) else 'not dict'}")

            logger.debug(f"[WeChat] handle_wechat_message: START for {from_user_id}")

            # 只处理用户消息
            if msg_type != 1:
                _trace("分支: msg_type != 1, 忽略返回", msg_type=msg_type)
                logger.info(f"[WeChat] Ignoring non-user message type: {msg_type}")
                _trace("return 前: 非用户消息类型")
                _write_trace_json()
                return

            # 获取用户输入
            if isinstance(content, dict):
                user_input = content.get("text", "")
                _trace("分支: content 是 dict", user_input=user_input[:50] if user_input else "")
            else:
                user_input = str(content)
                _trace("分支: content 不是 dict, str()转换", user_input=user_input[:50])

            # 判断是否为图片消息（content_type == "image" 表示 SDK 检测到图片）
            is_image = content_type == "image"
            logger.debug(f"[WeChat] handle_wechat_message [{msg.get('_trace_id', 'N/A')}]: content_type={content_type}, image_url={image_url}")

            # 预先查找绑定（CDN 下载需要 user_id）
            binding = wechat_service.get_binding_by_ilink_user_id(from_user_id)
            internal_user_id = binding.user_id if binding else None
            _trace("binding 查找结果",
                   from_user_id=str(from_user_id)[:30] if from_user_id else None,
                   binding_found=binding is not None,
                   internal_user_id=internal_user_id,
                   agent_hash=binding.agent_hash if binding else None)

            # 如果是图片但没有 image_url，检查是否有 media 信息可以下载
            if is_image:
                _trace("分支: is_image=True, 进入图片处理")
                # Debug: save msg dict to pickle for offline debugging (gated by env var)
                if os.environ.get("FECLAW_DEBUG_PICKLE"):
                    _trace("分支: FECLAW_DEBUG_PICKLE 已设置, 保存 pickle")
                    try:
                        _trace("try pickle dump 开始", path='/tmp/wechat_image_msg.pkl')
                        with open('/tmp/wechat_image_msg.pkl', 'wb') as f:
                            pickle.dump(msg, f)
                        _trace("try pickle dump 成功")
                        logger.debug(f"[WeChat] Pickled msg to /tmp/wechat_image_msg.pkl, content_type={msg.get('content_type')}")
                    except Exception as e:
                        _trace("except pickle dump 失败", error=str(e)[:100])
                        logger.debug(f"[WeChat] Pickle failed: {e}")

                if not image_url:
                    _trace("分支: is_image 但 image_url 为空, 检查 CDN media")
                    media_info = msg.get("_media_info")
                    media = None
                    logger.debug(f"[WeChat] Image detected, media_info={media_info is not None}, is_image={is_image}")
                    if media_info:
                        _trace("分支: media_info 存在", media_info_type=str(type(media_info)))
                        logger.debug(f"[WeChat] CDN media_info: type={type(media_info)}")
                        if isinstance(media_info, dict):
                            media = media_info.get("media")
                            _trace("分支: media_info 是 dict", media_type=str(type(media)) if media else "None")
                        else:
                            media = getattr(media_info, 'media', None)
                            _trace("分支: media_info 是 object", media_type=str(type(media)) if media else "None")
                    else:
                        _trace("分支: media_info 不存在")
                    # CDN 下载：使用 media.encrypt_query_param 和 media.aes_key
                    if media:
                        encrypt_query_param = getattr(media, 'encrypt_query_param', None)
                        # aes_key 的取值层级（SDK 协议）：
                        # image_item.aeskey → ImageContent.aes_key (优先)
                        # image_item.media.aes_key → CDNMedia.aes_key (可能为空字符串)
                        aes_key = (media_info.get("aes_key") if isinstance(media_info, dict) else getattr(media_info, 'aes_key', None)) or getattr(media, 'aes_key', None) or None
                        _trace("CDN 参数提取",
                               encrypt_query_param=repr(encrypt_query_param[:20] if encrypt_query_param else None),
                               aes_key=repr(aes_key[:20] if aes_key else None))
                        if encrypt_query_param and aes_key:
                            _trace("分支: encrypt_query_param 和 aes_key 都有, 进入 CDN 下载")
                            try:
                                _trace("try CDN 下载开始")
                                from urllib.parse import quote
                                cdn_url = f"https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param={quote(encrypt_query_param)}"
                                logger.debug(f"[WeChat] CDN URL: {cdn_url[:80]}...")
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(cdn_url) as resp:
                                        ciphertext = await resp.read()
                                        logger.debug(f"[WeChat] Downloaded {len(ciphertext)} bytes, status={resp.status}")
                                if ciphertext and len(ciphertext) > 100:
                                    _trace("分支: CDN 下载成功", ciphertext_len=len(ciphertext), status=resp.status)
                                    import time as _time
                                    ts = int(_time.time() * 1000)
                                    cdn_dir = "/tmp/wechat_cdn"
                                    os.makedirs(cdn_dir, exist_ok=True)
                                    _enc_path = os.path.join(cdn_dir, f"cdn_{ts}_encrypted.bin")
                                    _trace("save 前: 写入加密 CDN 数据", path=_enc_path, size=len(ciphertext))
                                    with open(_enc_path, "wb") as f:
                                        f.write(ciphertext)
                                    _trace("save 后: 加密 CDN 数据已保存")
                                    logger.info(f"[WeChat] Saved encrypted CDN data: {_enc_path} ({len(ciphertext)} bytes)")

                                    import base64
                                    from services.wechatbot_sdk.crypto import decrypt_aes_ecb
                                    from services.wechatbot_sdk.crypto import decode_aes_key
                                    key = decode_aes_key(aes_key)
                                    plaintext = decrypt_aes_ecb(ciphertext, key)
                                    _trace("CDN 解密完成", plaintext_len=len(plaintext))
                                    logger.debug(f"[WeChat] Decrypted to {len(plaintext)} bytes")

                                    _dec_path = os.path.join(cdn_dir, f"cdn_{ts}_decrypted.png")
                                    _trace("save 前: 写入解密 CDN 数据", path=_dec_path, size=len(plaintext))
                                    with open(_dec_path, "wb") as f:
                                        f.write(plaintext)
                                    _trace("save 后: 解密 CDN 数据已保存")
                                    logger.info(f"[WeChat] Saved decrypted CDN data: {_dec_path} ({len(plaintext)} bytes)")
                                    if internal_user_id and binding and binding.agent_hash:
                                        _trace("分支: 有 internal_user_id/agent_hash, VFS 保存图片",
                                               internal_user_id=internal_user_id,
                                               agent_hash=binding.agent_hash)
                                        _trace("save 前: _download_and_save_image_to_vfs")
                                        vfs_path = await _download_and_save_image_to_vfs(
                                            f"data:image/png;base64,{base64.b64encode(plaintext).decode()}",
                                            internal_user_id,
                                            agent_hash=binding.agent_hash
                                        )
                                        _trace("save 后: _download_and_save_image_to_vfs", vfs_path=vfs_path)
                                        if vfs_path:
                                            _trace("分支: VFS 保存成功", vfs_path=vfs_path)
                                            image_url = vfs_path
                                            
                                            # 延时 3D 预识别（只有当有上下文对话框才立即跑）
                                            image_desc = None
                                            _do_3d_now = None
                                            try:
                                                from models.database import WeChatMessage, SessionLocal
                                                _db = SessionLocal()
                                                try:
                                                    _recent = _db.query(WeChatMessage).filter(
                                                        WeChatMessage.binding_id == binding.id,
                                                    ).order_by(WeChatMessage.created_at.desc()).limit(2).all()
                                                    if len(_recent) >= 2 and _recent[0].direction == "sent":
                                                        _do_3d_now = True
                                                finally:
                                                    _db.close()
                                            except Exception:
                                                pass
                                            if _do_3d_now:
                                                try:
                                                    from services.image_describer import describe_image
                                                    _trace("Pre-LLM 描述开始")
                                                    image_desc = await describe_image(plaintext, timeout=15.0)
                                                    if image_desc:
                                                        _log_perf("pre_llm_desc_done")
                                                        logger.info(f"[PERF] Pre-LLM desc: OK ({len(image_desc)} chars)")
                                                    else:
                                                        logger.info("[PERF] Pre-LLM desc: empty result")
                                                except Exception as e:
                                                    _trace("Pre-LLM 描述异常", error=str(e)[:60])
                                            else:
                                                logger.info("[PERF] Pre-LLM desc: skipped (no context, deferred)")

                                            # 用 prefix 格式作为 user_input，这样保存到历史记录时更清晰
                                            user_input = (
                                                f'用户给你发送了一张图片，已保存到"{vfs_path}"。\n'
                                                + (f'图片概述: {image_desc}\n\n' if image_desc else '')
                                                + '（以上为预识别描述，供参考）\n'
                                                + '⚠️ 后续任何工具调用中如需引用此图片，必须使用上述路径的值，不得使用其他路径或自己构造路径（如 current_image.png 等）。'
                                            )
                                            logger.debug(f"[WeChat] Image saved to VFS: {vfs_path}")
                                            # 更新数据库中的消息内容（使用同样格式）
                                            # 使用 internal_user_id 查找 binding（更可靠）
                                            if internal_user_id:
                                                _trace("分支: internal_user_id 存在, 更新 DB 消息")
                                                binding_for_update = wechat_service.get_binding_by_user(internal_user_id)
                                                if binding_for_update:
                                                    _trace("分支: binding_for_update 存在, 执行 update_message_content")
                                                    wechat_service.update_message_content(
                                                        binding_for_update.id,
                                                        "[image]",
                                                        user_input
                                                    )
                                                else:
                                                    _trace("分支: binding_for_update 不存在")
                                                    logger.debug(f"[WeChat] No binding found for internal_user_id={internal_user_id}")
                                            else:
                                                _trace("分支: internal_user_id 为空")
                                                logger.debug(f"[WeChat] internal_user_id is None")
                                        else:
                                            _trace("分支: VFS 保存失败")
                                    else:
                                        _trace("分支: 缺少 internal_user_id/agent_hash, 跳过 VFS 保存",
                                               internal_user_id=internal_user_id,
                                               has_binding=binding is not None,
                                               has_agent_hash=bool(binding.agent_hash if binding else None))
                                else:
                                    _trace("分支: CDN 下载内容无效", ciphertext_len=len(ciphertext) if ciphertext else 0)
                                    logger.debug(f"[WeChat] Download failed or token expired")
                            except Exception as e:
                                _trace("except CDN 下载异常", error=str(e)[:100])
                                logger.debug(f"[WeChat] Download error: {e}")
                        else:
                            _trace("分支: encrypt_query_param 或 aes_key 缺失")
                            logger.debug(f"[WeChat] media missing download_url or key3")
                    else:
                        _trace("分支: media 不存在")
                else:
                    _trace("分支: is_image 但 image_url 已存在, 跳过 CDN")

            if is_image and (not user_input or user_input in ("[image]", "[图片]")):
                _trace("分支: 图片消息 user_input 为空或占位符", user_input=user_input, image_url=str(image_url)[:80] if image_url else None)
                # 如果 CDN 下载没执行但有 image_url（已由 wechat_service 保存）
                if image_url:
                    _trace("分支: 有 image_url, 检查上下文决定是否继续")
                    image_desc = msg.get("_image_description")
                    if image_desc:
                        logger.info(f"[PERF] Using pre-LLM desc from wechat_service ({len(image_desc)} chars)")
                        _log_perf("pre_llm_desc_done")

                    # 检查是否有近期上下文（最近有任意已读消息）
                    _has_recent_context = False
                    try:
                        from models.database import WeChatMessage, SessionLocal
                        db = SessionLocal()
                        try:
                            recent_msgs = db.query(WeChatMessage).filter(
                                WeChatMessage.binding_id == binding.id,
                            ).order_by(WeChatMessage.created_at.desc()).limit(1).all()
                            # 只要有 1 条以上消息就算有上下文（不限制方向）
                            if len(recent_msgs) >= 1:
                                _has_recent_context = True
                        finally:
                            db.close()
                    except Exception:
                        pass

                    if _has_recent_context:
                        # 有上下文→尝试获取/生成图片描述
                        if not image_desc and image_url:
                            # 没有预识别结果 → 从已保存路径读图跑 4D/3D
                            try:
                                from config import settings
                                from services.storage_service import StorageService
                                _storage = StorageService()
                                _rel = image_url.lstrip("/")
                                _cos_key = f"{settings.TENCENT_COS_PREFIX}agents/{binding.agent_hash}/workspace/{_rel}"
                                _img_data = _storage.get_file_content(_cos_key)
                                if _img_data:
                                    _sr_on = False
                                    try:
                                        from models.database import AgentProfile, SessionLocal
                                        _sdb = SessionLocal()
                                        try:
                                            _ap = _sdb.query(AgentProfile).filter(
                                                AgentProfile.hash == binding.agent_hash
                                            ).first()
                                            if _ap:
                                                _sr_on = _ap.sr_enabled
                                        finally:
                                            _sdb.close()
                                    except Exception:
                                        pass
                                    if not _sr_on:
                                        from services.image_describer import describe_image_4d
                                        _pre_desc = await asyncio.wait_for(
                                            describe_image_4d(_img_data, timeout=15.0), timeout=15.0
                                        )
                                        if _pre_desc:
                                            image_desc = _pre_desc
                                            logger.info(f"[PERF] 4D on-the-fly for ctx image ({len(_pre_desc)} chars)")
                                    else:
                                        from services.image_describer import describe_image
                                        _pre_desc = await asyncio.wait_for(
                                            describe_image(_img_data, timeout=15.0), timeout=15.0
                                        )
                                        if _pre_desc:
                                            image_desc = _pre_desc
                            except Exception:
                                pass

                        user_input = (
                            f'用户给你发送了一张图片，已保存到"{image_url}"。\n'
                            + (f'图片概述: {image_desc}\n\n' if image_desc else '')
                            + "（以上为预识别描述，供参考）\n"
                            + "⚠️ 后续任何工具调用中如需引用此图片，必须使用上述路径的值，不得使用其他路径或自己构造路径（如 current_image.png 等）。"
                        )
                        logger.warning(f"[WeChat] Image with context, passing to LLM: desc={len(image_desc or '')} chars")
                    else:
                        # 无上下文→缓存，预识别 + 回复并行
                        # SR ON: 3D + SR 并行；SR OFF: 等待4D → 主模型
                        _pre_task = None
                        _sr_enabled = False
                        try:
                            from models.database import SessionLocal, AgentProfile
                            _db = SessionLocal()
                            try:
                                _ap = _db.query(AgentProfile).filter(
                                    AgentProfile.hash == binding.agent_hash
                                ).first()
                                if _ap:
                                    _sr_enabled = _ap.sr_enabled
                            finally:
                                _db.close()
                        except Exception:
                            pass

                        with _PENDING_IMAGE_LOCK:
                            _PENDING_IMAGE_CACHE[from_user_id] = {
                            "path": image_url,
                            "desc": None  # 预识别完成后回填
                        }

                        if _sr_enabled:
                            # SR ON: 3D + SR → SR 生成人设回复，预识别异步缓存
                            try:
                                _img_bytes = locals().get("plaintext") or None
                                if _img_bytes:
                                    from services.image_describer import describe_image
                                    _pre_task = asyncio.create_task(describe_image(_img_bytes, timeout=15.0))
                            except Exception:
                                pass

                            try:
                                from services.smart_router import SmartRouter
                                _persona_parts = []
                                if _ap:
                                    _persona_parts.append(f"名称：{_ap.name}")
                                    if _ap.description:
                                        _persona_parts.append(f"简介：{_ap.description}")
                                try:
                                    from services.agent_tools_service import AgentToolsService
                                    _tools = AgentToolsService(binding.agent_hash)
                                    _soul = _tools.vfs.cat("/workspace/agent/soul.md")
                                    if _soul and not _soul.startswith('Error'):
                                        _persona_parts.append(f"人格：{_soul[:500]}")
                                except Exception:
                                    pass
                                _persona = "\n".join(_persona_parts) if _persona_parts else None
                                _sr_router = SmartRouter()
                                _sr_dec = await _sr_router.route(
                                    text="", context=None,
                                    image_info={"has_image": True, "description": ""},
                                    persona=_persona,
                                )
                                _reply_text = _sr_dec.direct_reply or "收到图片📸"
                            except Exception:
                                _reply_text = "收到图片📸"

                            # 预识别回填缓存
                            if _pre_task:
                                try:
                                    _pre_desc = await asyncio.wait_for(_pre_task, timeout=15.0)
                                    if _pre_desc:
                                        with _PENDING_IMAGE_LOCK:
                                            _PENDING_IMAGE_CACHE[from_user_id]["desc"] = _pre_desc
                                except Exception:
                                    pass

                            await wechat_service.send_message(
                                to_user_id=from_user_id,
                                text=_reply_text,
                                context_token=context_token
                            )
                            asyncio.create_task(
                                wechat_service.send_typing(from_user_id, context_token)
                            )
                            logger.warning(f"[WeChat] Image no-context, SR replied with '{_reply_text}', 3D cached")
                            _trace("return 前: SR ON 无上下文，等待用户文字")
                            _write_trace_json()
                            return

                        # SR OFF: 等4D → 直接走主模型（不缓存、不等待文字）
                        try:
                            _img_bytes = locals().get("plaintext") or None
                            _pre_desc = ""
                            if _img_bytes:
                                from services.image_describer import describe_image_4d
                                _pre_desc = await asyncio.wait_for(
                                    describe_image_4d(_img_bytes, timeout=15.0), timeout=15.0
                                )
                        except Exception:
                            _pre_desc = ""

                        if _pre_desc:
                            with _PENDING_IMAGE_LOCK:
                                _PENDING_IMAGE_CACHE[from_user_id]["desc"] = _pre_desc
                            logger.info(f"[PERF] 4D cached for later ({len(_pre_desc)} chars)")

                        # 构造用户消息，带4D描述，直接走主模型
                        _user_input = f'用户给你发送了一张图片，已保存到VFS路径"{image_url}"。\n⚠️ 后续任何工具调用中如需引用此图片，必须使用上述路径的值，不得使用其他路径或自己构造路径（如 current_image.png 等）。'
                        if _pre_desc:
                            _user_input += f'\n\n图片概述：\n{_pre_desc}\n\n（此为预识别结果，供参考。如有疑问，请重新查看图片。）'

                        await wechat_service.send_message(
                            to_user_id=from_user_id,
                            text="让我看看……🔍",
                            context_token=context_token
                        )
                        asyncio.create_task(
                            wechat_service.send_typing(from_user_id, context_token)
                        )

                        _wc = WeChatChannelService(
                            wechat_service, user_id=user_id, agent_hash=binding.agent_hash,
                            to_user_id=from_user_id, context_token=context_token
                        )
                        await _wc.stream_response(
                            user_input=_user_input,
                            image_url=image_url,
                            msg_id=msg.get("msg_id"),
                            client_id=msg.get("client_id"),
                        )

                        _trace("return 前: SR OFF 无上下文，4D+主模型完成")
                        _write_trace_json()
                        return
                else:
                    _trace("分支: 无 image_url, 图片处理失败, 告知用户并返回")
                    # 图片下载失败，告知用户并且不转发给 Agent
                    await wechat_service.send_message(
                        to_user_id=from_user_id,
                        text="图片处理失败，请稍后重试。",
                        context_token=context_token
                    )
                    logger.debug(f"[WeChat] handle_wechat_message: DONE (image download failed) for {from_user_id}")
                    _trace("return 前: 图片下载失败")
                    _write_trace_json()
                    return

            if not user_input and not is_image:
                # 空消息：忽略
                _trace("分支: 空消息且非图片, 忽略返回")
                logger.debug(f"[WeChat] handle_wechat_message: empty message, ignoring for {from_user_id}")
                _trace("return 前: 空消息")
                _write_trace_json()
                return

            logger.debug(f"[WeChat] Received message from {from_user_id}: {user_input[:50] if user_input else '[image]'}...")
            logger.debug(f"[WeChat] handle_wechat_message: parsing done for {from_user_id}")

            # 绑定已在前面查找过
            if not binding:
                _trace("分支: binding 不存在, 忽略返回", from_user_id=str(from_user_id)[:30] if from_user_id else None)
                logger.warning(f"[WeChat] No binding for openid: {from_user_id}")
                _trace("return 前: binding 不存在")
                _write_trace_json()
                return
            logger.debug(f"[WeChat] handle_wechat_message: binding found for {from_user_id}")

            # 发送 typing 状态（带超时保护，避免阻塞）
            logger.debug(f"[WeChat] handle_wechat_message: sending typing for {from_user_id}")
            try:
                _trace("try send_typing 开始")
                await asyncio.wait_for(
                    wechat_service.send_typing(from_user_id, context_token),
                    timeout=10.0
                )
                _trace("try send_typing 成功")
            except asyncio.TimeoutError:
                _trace("except send_typing 超时")
                logger.warning(f"[WeChat] send_typing timed out for {from_user_id}")
            except Exception as e:
                _trace("except send_typing 异常", error=str(e)[:100])
                logger.warning(f"[WeChat] send_typing error: {e}")
            logger.debug(f"[WeChat] handle_wechat_message: typing sent for {from_user_id}")

            _log_perf("pre_llm_complete")
            logger.info(f"[PERF] handle_wechat_message: calling stream_ai_service (total: {_time.time()-_t0:.2f}s)")
            _trace("调用 stream_ai_service", user_id=binding.user_id, agent_hash=binding.agent_hash)
            try:
                _trace("try stream_ai_service 开始")
                _wc = WeChatChannelService(
                    wechat_service, user_id=binding.user_id, agent_hash=binding.agent_hash,
                    to_user_id=from_user_id, context_token=context_token
                )
                await asyncio.wait_for(
                    _wc.stream_response(
                        user_input=user_input,
                        image_url=image_url if is_image else None,
                        msg_id=msg.get("msg_id"),
                        client_id=msg.get("client_id"),
                    ),
                    timeout=300.0
                )
                _trace("try stream_ai_service 成功")
            except asyncio.TimeoutError:
                _trace("except stream_ai_service 超时")
                logger.debug(f"[WeChat] AI service timed out for {from_user_id}")
                logger.error(f"[WeChat] AI service timed out for {from_user_id}")
                await wechat_service.send_message(
                    to_user_id=from_user_id,
                    text="抱歉，AI 处理超时，请稍后再试。",
                    context_token=context_token
                )
            except Exception as e:
                _trace("except stream_ai_service 异常", error=str(e)[:100])
                logger.debug(f"[WeChat] AI service error for {from_user_id}: {e}")
                logger.debug(f"[WeChat] AI service error traceback:\n{traceback.format_exc()}")
                logger.error(f"[WeChat] AI service error: {e}")
                await wechat_service.send_message(
                    to_user_id=from_user_id,
                    text="抱歉，AI 服务暂时不可用，请稍后再试。",
                    context_token=context_token
                )
            _trace("handle_wechat_message 正常结束", from_user_id=str(from_user_id)[:30] if from_user_id else None)
            logger.debug(f"[WeChat] handle_wechat_message: DONE for {from_user_id}")

        except Exception as e:
            _trace("except 外层异常", error=str(e)[:200])
            logger.debug(f"[WeChat] Error handling message: {e}")
            logger.debug(f"[WeChat] Error handling message traceback:\n{traceback.format_exc()}")
            logger.error(f"[WeChat] Error handling message: {e}")
        finally:
            _trace("finally: 写入 trace JSON 到磁盘", trace_dir=_trace_dir, steps=len(_trace_log))
            _write_trace_json()

    # 设置 session 过期回调
    async def handle_session_expired():
        """处理 session 过期"""
        logger.warning("[WeChat] Session expired, need re-login")
        # 清理数据库中的绑定信息
        # 注意：不删除绑定记录，只更新状态为 expired
        from models.database import SessionLocal
        db = SessionLocal()
        try:
            ilink_user_id = wechat_service.login_state.get("ilink_user_id", "")
            if ilink_user_id:
                from models.database import WeChatBinding
                binding = db.query(WeChatBinding).filter(
                    WeChatBinding.ilink_user_id == ilink_user_id,
                    WeChatBinding.status == "active"
                ).first()
                if binding:
                    binding.status = "expired"
                    db.commit()
        except Exception as e:
            logger.error(f"[WeChat] Failed to update binding status: {e}")
        finally:
            db.close()

    # 注意：handle_wechat_message 是 local 函数，不会被 wechat_service.on_message 调用
    # wechat_service._on_message 在 start_polling 中由 _on_sdk_message 设置
    # 这里只是定义函数，不需要注册

    # _on_sdk_message 负责接收 SDK 消息，构建 msg dict，然后调用 _on_message(msg_dict)
    # 我们只需要把 handle_wechat_message 设置为 _on_message 的处理器
    async def wrapped_handler(msg_dict):
        """包装 handle_wechat_message，确保传入的是 dict"""
        # msg_dict 应该是我们构建的字典，不是 IncomingMessage
        if isinstance(msg_dict, dict) and "msg_type" in msg_dict:
            await handle_wechat_message(msg_dict)
        else:
            logger.error(f"[Setup] handle_wechat_message received non-dict: {type(msg_dict)}")

    logger.info(f"[Setup] Setting _on_message to wrapped_handler")
    wechat_service.on_message(wrapped_handler)

    # 设置 session 过期回调
    wechat_service.on_session_expired(handle_session_expired)

    # 问题1修复：启动时恢复所有 active 绑定的轮询
    await wechat_service.restore_all_polling()
    # 问题4修复：启动看门狗
    wechat_service.start_watchdog()


async def ensure_message_handler() -> None:
    """确保消息处理器已设置（防止重复注册）"""
    await setup_message_handler()


# 仅允许本地访问的调试接口
ALLOWED_DEBUG_HOSTS = ["localhost", "127.0.0.1", "::1"]


def _check_debug_localhost(request: Request) -> bool:
    """检查请求是否来自本地"""
    client_host = request.client.host if request.client else ""
    return client_host in ALLOWED_DEBUG_HOSTS


@router.post("/debug/test-message")
async def debug_test_message(request: Request) -> dict:
    """
    调试接口：模拟微信消息处理流程
    用于测试会话管理功能
    
    ⚠️ 仅允许本地访问
    """
    # 安全检查：仅允许本地请求
    if not _check_debug_localhost(request):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=403,
            content={"error": "此调试接口仅允许本地访问"}
        )
    
    from pydantic import BaseModel
    from services.wechat_service import WeChatService
    from services.agent_tools_service import AgentToolsService

    class TestMessageRequest(BaseModel):
        user_id: int
        message: str
        skip_history: bool = False  # 是否跳过历史加载

    try:
        body = await request.json()
        user_id = body.get("user_id", 2)
        message = body.get("message", "")
        skip_history = body.get("skip_history", False)
    except Exception:
        return {"error": "Invalid JSON"}

    if not message:
        return {"error": "Message is required"}

    result = {
        "input": message,
        "user_id": user_id,
        "steps": [],
    }

    # Step 1: Command detection
    is_list_sessions = "列出" in message and "会话" in message
    is_search = "搜索" in message

    result["steps"].append({
        "step": "command_detection",
        "is_list_sessions": is_list_sessions,
        "is_search": is_search,
    })

    # Step 2: Load history
    history_count = 0
    if not skip_history:
        try:
            wechat_service = WeChatService()
            binding = wechat_service.get_binding_by_user(str(user_id))
            if binding:
                from models.database import WeChatMessage, SessionLocal
                db = SessionLocal()
                try:
                    db_messages = db.query(WeChatMessage).filter(
                        WeChatMessage.binding_id == binding.id,
                        WeChatMessage.agent_hash == binding.agent_hash
                    ).order_by(WeChatMessage.created_at.asc()).limit(50).all()
                    history_count = len(db_messages)
                finally:
                    db.close()
        except Exception as e:
            result["steps"].append({"step": "history_load_error", "error": str(e)})

    result["steps"].append({
        "step": "history_loaded",
        "count": history_count,
        "mode": "normal"
    })

    # Step 3: Session management tools test
    # Find agent_hash from binding or AgentProfile
    agent_hash = None
    if binding and binding.agent_hash:
        agent_hash = binding.agent_hash
    else:
        from models.database import SessionLocal as _SessionLocal
        from models.database import AgentProfile
        _db = _SessionLocal()
        try:
            agent = _db.query(AgentProfile).filter(AgentProfile.user_id == user_id).first()
            if agent:
                agent_hash = agent.hash
        finally:
            _db.close()
    if not agent_hash:
        result["steps"].append({"step": "agent_tools_error", "error": "No agent_hash found for user"})
    else:
        agent_tools = AgentToolsService(agent_hash=agent_hash)

        if is_list_sessions:
            list_result = agent_tools.list_conversations()
            result["steps"].append({
                "step": "list_conversations",
                "result": list_result[:500] if len(list_result) > 500 else list_result
            })

        if is_search:
            # Extract search query
            import re
            match = re.search(r'关于(.+?)的会话', message)
            query = match.group(1) if match else message.replace("搜索", "").replace("会话", "").strip()
            search_result = agent_tools.search_sessions(query)
            result["steps"].append({
                "step": "search_sessions",
                "query": query,
                "result": search_result[:500] if len(search_result) > 500 else search_result
            })

        # Step 4: Intent analysis (if applicable)
        if not is_list_sessions and not is_search:
            intent_result = agent_tools.analyze_intent(message)
            result["steps"].append({
                "step": "intent_analysis",
                "input": message,
                "result": intent_result
            })

            # Auto suggest
            auto_suggest = agent_tools.auto_suggest_session(message)
            if auto_suggest:
                result["steps"].append({
                    "step": "auto_suggest",
                    "result": auto_suggest
                })

    return result
