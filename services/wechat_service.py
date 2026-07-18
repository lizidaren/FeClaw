from __future__ import annotations

"""
WeChat Service - iLink协议实现

支持微信接入的核心服务，处理扫码登录、消息收发等功能。
"""

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
from threading import Lock
from uuid import uuid4
from typing import Optional, Dict, Any, List, Callable

import aiohttp

from models.database import SessionLocal, WeChatBinding, WeChatMessage, ChatHistory
from services.wechatbot_sdk import IncomingMessage

from .wechat.models import (
    MSG_TYPE_TEXT, MSG_TYPE_IMAGE, MSG_TYPE_BOT,
    ERR_SESSION_EXPIRED,
    WATCHDOG_INTERVAL, INACTIVITY_THRESHOLD, MAX_RESTART_COUNT,
    ILINK_API_BASE, ILINK_CDN_BASE,
    get_msg_type_name,
)
from .wechat.sdk_adapter import (
    DatabaseBackedClient,
    download_wechat_image_from_media,
    auth_headers,
)

logger = logging.getLogger(__name__)


class WeChatService:
    """微信服务类 - iLink协议实现"""

    _instance = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        # HTTP 会话
        self._session: Optional[aiohttp.ClientSession] = None

        # 登录状态
        self._login_state = {
            "qrcode_token": None,
            "qrcode_image": None,
            "status": "idle",  # idle/waiting/scanned/confirmed/expired
            "bot_token": None,
            "ilink_bot_id": None,
            "ilink_user_id": None,
            "base_url": None,
        }

        # 保存当前 base_info（用于重连）
        self._current_base_info: Dict[str, Any] = {}

        # 事件回调
        self._on_qrcode: Optional[Callable] = None
        self._on_scanned: Optional[Callable] = None
        self._on_confirmed: Optional[Callable] = None
        self._on_message: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_session_expired: Optional[Callable] = None

        # ============================================================
        # 看门狗机制：per-user 轮询状态
        # ============================================================
        # user_id -> asyncio.Task for the polling executor thread
        self._polling_tasks: Dict[int, asyncio.Task] = {}
        # user_id -> bool, is polling running
        self._polling_running: Dict[int, bool] = {}
        # user_id -> ThreadPoolExecutor for SDK client
        self._sdk_executors: Dict[int, ThreadPoolExecutor] = {}
        # user_id -> DatabaseBackedClient instance (for watchdog restart)
        self._user_clients: Dict[int, DatabaseBackedClient] = {}
        # user_id -> last heartbeat time (updated on each get_updates call in SDK loop)
        self._last_heartbeat: Dict[int, float] = {}
        # user_id -> last message received time (for activity tracking)
        self._last_activity: Dict[int, float] = {}
        # user_id -> consecutive restart count (for watchdog)
        self._restart_counts: Dict[int, int] = {}
        # watchdog background task
        self._watchdog_task: Optional[asyncio.Task] = None
        # main event loop reference (set on first start_polling call)
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

        # context_token 缓存: (accountId, userId) -> context_token
        self._context_tokens: Dict[tuple, str] = {}

        # cursor for long polling
        self._cursor: Optional[str] = None

        # ========== 性能优化：状态缓存 ==========
        self._status_cache: Dict[str, Dict[str, Any]] = {}
        self._status_cache_ttl = 1.0  # 缓存有效期（秒）

    @property
    def login_state(self) -> Dict[str, Any]:
        """公开属性：返回当前登录状态字典"""
        return self._login_state.copy()

    def reset_login_state(self):
        """重置登录状态为初始值"""
        self._login_state = {
            "qrcode_token": None,
            "qrcode_image": None,
            "status": "idle",
            "bot_token": None,
            "ilink_bot_id": None,
            "ilink_user_id": None,
            "base_url": None,
        }
        self._current_base_info = {}

    # ========== 消息字典构建 ==========

    @staticmethod
    def _build_message_dict(msg: IncomingMessage) -> dict:
        """Extract info from SDK IncomingMessage into a flat dict."""
        d = {
            "user_id": msg.user_id,
            "text": msg.text,
            "content_type": msg.type,
        }
        # Image
        if msg.images:
            img = msg.images[0]
            d["_media_info"] = {"media": img.media, "aes_key": img.aes_key}
            d["image_url"] = img.url
        # Voice
        if msg.voices:
            v = msg.voices[0]
            d["_voice_info"] = {
                "media": v.media,
                "aes_key": v.media.aes_key if v.media else None,
                "text": v.text,
                "duration_ms": v.duration_ms,
            }
        # File
        if msg.files:
            f = msg.files[0]
            d["_file_info"] = {
                "media": f.media,
                "aes_key": f.media.aes_key if f.media else None,
                "file_name": f.file_name,
                "size": f.size,
                "md5": f.md5,
            }
        # Video
        if msg.videos:
            v = msg.videos[0]
            d["_video_info"] = {
                "media": v.media,
                "aes_key": v.media.aes_key if v.media else None,
            }
        return d

    async def _get_session(self, timeout_total: float = 40) -> aiohttp.ClientSession:
        """获取或创建共享 HTTP 会话（连接池复用）"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=timeout_total)
            connector = aiohttp.TCPConnector(keepalive_timeout=35, limit=10)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/json",
                    "iLink-App-ClientVersion": "1",
                }
            )
        return self._session

    async def _get_session_short_timeout(self) -> aiohttp.ClientSession:
        """获取短超时的 HTTP 会话（用于频繁轮询的 API）"""
        return await self._get_session(timeout_total=5)

    async def close_session(self):
        """关闭共享 HTTP 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _json_response(self, resp: aiohttp.ClientResponse) -> Dict[str, Any]:
        """处理 iLink API 的响应，自动处理错误 content-type"""
        text = await resp.text()
        logger.debug("[WeChat] _json_response status={}, text_len={}".format(resp.status, len(text)))
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("[WeChat] Failed to parse JSON: {}".format(text[:200]))
            raise Exception("JSON parse error: {}".format(text[:100]))

    async def close(self):
        """关闭服务"""
        # 停止所有轮询
        for uid in list(self._polling_running.keys()):
            self._polling_running[uid] = False
        for uid, task in list(self._polling_tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # 停止看门狗
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        # 关闭所有 executor
        for executor in self._sdk_executors.values():
            executor.shutdown(wait=False)
        if self._session and not self._session.closed:
            await self._session.close()

    # ========== 登录相关 ==========

    async def get_qrcode(self) -> Dict[str, Any]:
        """获取登录二维码"""
        url = "{}/ilink/bot/get_bot_qrcode".format(ILINK_API_BASE)
        params = {"bot_type": 3}

        session = await self._get_session()
        async with session.get(url, params=params) as resp:
            content_type = resp.headers.get('Content-Type', '')
            logger.info("[WeChat] get_qrcode response status: {}, content-type: {}".format(resp.status, content_type))
            
            text = await resp.text()
            
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("[WeChat] get_qrcode non-JSON response: {}".format(text[:500]))
                raise Exception("Failed to parse JSON response: {}".format(text[:200]))
            
            logger.info("[WeChat] get_qrcode response: {}".format(data))

            if data.get("ret") == 0:
                self._login_state["qrcode_token"] = data.get("qrcode")
                self._login_state["qrcode_image"] = data.get("qrcode_img_content")
                self._login_state["status"] = "waiting"

                return {
                    "qrcode_token": self._login_state["qrcode_token"],
                    "qrcode_image": None,
                    "qrcode_url": self._login_state["qrcode_image"],
                }
            else:
                raise Exception("获取二维码失败: {}".format(data))

    async def check_qrcode_status(self, qrcode_token: str = None) -> Dict[str, Any]:
        """检查二维码扫描状态"""
        if qrcode_token is None:
            qrcode_token = self._login_state.get("qrcode_token")

        if not qrcode_token:
            raise ValueError("qrcode_token is required")

        # 检查缓存
        now = time.time()
        cache_key = qrcode_token
        cached = self._status_cache.get(cache_key)
        if cached and (now - cached.get("timestamp", 0)) < self._status_cache_ttl:
            logger.debug("[WeChat] check_qrcode_status using cache for {}...".format(qrcode_token[:20]))
            return cached.get("data", {"status": "idle"})

        url = "{}/ilink/bot/get_qrcode_status".format(ILINK_API_BASE)
        params = {"qrcode": qrcode_token}

        try:
            session = await self._get_session_short_timeout()
            async with session.get(url, params=params) as resp:
                data = await self._json_response(resp)
                logger.info("[WeChat] check_qrcode_status response: {}".format(data))

                raw_status = data.get("status")
                logger.debug("[WeChat] Raw iLink status: {}".format(raw_status))

                status_mapping = {
                    "wait": "scanned",
                    "scaned": "scanned",
                    "scanned": "scanned",
                    "confirmed": "confirmed",
                    "expired": "expired",
                    "waiting": "waiting",
                    "idle": "idle",
                }
                status = status_mapping.get(raw_status, raw_status)
                logger.info("[WeChat] Mapped status: {} -> {}".format(raw_status, status))

                self._login_state["status"] = status

                result_data = {
                    "status": status,
                    "bot_token": data.get("bot_token"),
                    "ilink_bot_id": data.get("ilink_bot_id"),
                    "ilink_user_id": data.get("ilink_user_id"),
                    "base_url": data.get("baseurl"),
                }

                self._status_cache[cache_key] = {
                    "data": result_data,
                    "timestamp": now,
                }

                if status == "confirmed":
                    self._login_state["bot_token"] = data.get("bot_token")
                    self._login_state["ilink_bot_id"] = data.get("ilink_bot_id")
                    self._login_state["ilink_user_id"] = data.get("ilink_user_id")
                    self._login_state["base_url"] = data.get("baseurl")

                    if self._on_confirmed:
                        await self._on_confirmed(self._login_state.copy())

                elif status == "scanned" and self._on_scanned:
                    await self._on_scanned()

                return result_data

        except asyncio.TimeoutError:
            logger.warning("[WeChat] check_qrcode_status timeout for {}...".format(qrcode_token[:20]))
            return {
                "status": "timeout",
                "bot_token": None,
                "ilink_bot_id": None,
                "ilink_user_id": None,
                "base_url": None,
            }
        except aiohttp.ClientError as e:
            logger.error("[WeChat] check_qrcode_status client error: {}".format(e))
            raise

    async def login(self, timeout: int = 120) -> Dict[str, Any]:
        """获取二维码并等待扫码确认"""
        qr_data = await self.get_qrcode()

        if self._on_qrcode:
            await self._on_qrcode(qr_data)

        start_time = time.time()
        while time.time() - start_time < timeout:
            status_data = await self.check_qrcode_status()

            if status_data["status"] == "confirmed":
                return {
                    "status": "confirmed",
                    "bot_token": status_data["bot_token"],
                    "ilink_bot_id": status_data["ilink_bot_id"],
                    "ilink_user_id": status_data["ilink_user_id"],
                    "base_url": status_data["base_url"],
                }

            if status_data["status"] == "expired":
                raise Exception("二维码已过期，请重新获取")

            if status_data["status"] == "scanned":
                await asyncio.sleep(2)
            else:
                await asyncio.sleep(3)

        raise Exception("登录超时，请重试")

    # ========== 消息接收（长轮询）==========
    # 注意：get_updates 保留用于兼容性，但不再被 _polling_loop 调用

    async def get_updates(self, base_info: Dict[str, Any] = None) -> Dict[str, Any]:
        """长轮询获取消息"""
        if base_info is None:
            base_info = {
                "bot_token": self._login_state.get("bot_token"),
                "ilink_bot_id": self._login_state.get("ilink_bot_id"),
                "ilink_user_id": self._login_state.get("ilink_user_id"),
                "baseurl": self._login_state.get("base_url"),
            }

        if not base_info.get("bot_token"):
            raise ValueError("bot_token is required")

        url = "{}/ilink/bot/getupdates".format(ILINK_API_BASE)

        payload = {
            "get_updates_buf": self._cursor or "",
            "base_info": base_info,
        }

        session = await self._get_session()
        try:
            async with session.post(url, json=payload) as resp:
                data = await self._json_response(resp)
                logger.debug("[WeChat] get_updates response: {}".format(data))

                ret = data.get("ret")
                if ret == 0:
                    self._cursor = data.get("get_updates_buf", "")
                    return data
                elif ret == ERR_SESSION_EXPIRED:
                    logger.warning("[WeChat] Session expired, need re-login")
                    if self._on_session_expired:
                        await self._on_session_expired()
                    if self._on_error:
                        await self._on_error({"code": ERR_SESSION_EXPIRED, "message": "会话过期"})
                    return data
                else:
                    logger.error("[WeChat] get_updates error: {}".format(data))
                    return data
        except asyncio.TimeoutError:
            logger.debug("[WeChat] get_updates timeout, continuing...")
            return {"ret": 0, "msgs": [], "get_updates_buf": self._cursor or ""}

    # ============================================================
    # 问题 1 & 4: Per-user 轮询管理 + 看门狗
    # ============================================================

    async def restore_all_polling(self):
        """启动时恢复所有 active 绑定的轮询（问题1修复）"""
        logger.info("[WeChat] restore_all_polling: STARTING")
        db = SessionLocal()
        try:
            bindings = db.query(WeChatBinding).filter(
                WeChatBinding.status == "active"
            ).all()
            logger.info("[WeChat] restore_all_polling: found {} active bindings".format(len(bindings)))
            tasks = []
            for binding in bindings:
                logger.debug("[WeChat] restore_all_polling: checking user {}".format(binding.user_id))
                if not binding.ilink_token:
                    logger.warning("[WeChat] restore_all_polling: user {} has no ilink_token, skipping".format(binding.user_id))
                    continue
                logger.info("[WeChat] restore_all_polling: scheduling start_polling for user {}".format(binding.user_id))
                tasks.append(asyncio.wait_for(
                    self.start_polling(user_id=binding.user_id),
                    timeout=30.0
                ))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        logger.error("[WeChat] restore_all_polling: task {} failed: {}".format(i, result))
                    else:
                        logger.info("[WeChat] Restored polling for task {}".format(i))
                logger.info("[WeChat] restore_all_polling: {} polling tasks completed".format(len(tasks)))
        finally:
            db.close()
            logger.info("[WeChat] restore_all_polling: DONE")

    # ========== SDK 凭证保存（fake login）==========

    def save_sdk_credentials(self, user_id: int, bot_token: str, ilink_bot_id: str,
                             ilink_user_id: str, base_url: str,
                             agent_hash: str = None) -> None:
        """
        保存 SDK 凭证到数据库（fake login 模式）

        在用户扫码绑定后调用，将凭证存入 WeChatBinding.ilink_token 字段。
        后续 start_polling() 会从 DB 加载这些凭证，SDK 的 start() 会跳过 login()。

        Args:
            agent_hash: 按 agent_hash 查找正确绑定（同一用户多 agent 时必需）
        """
        db = SessionLocal()
        try:
            query = db.query(WeChatBinding).filter(
                WeChatBinding.user_id == user_id
            )
            if agent_hash:
                query = query.filter(WeChatBinding.agent_hash == agent_hash)
            binding = query.order_by(WeChatBinding.id.desc()).first()
            if not binding:
                logger.error("[WeChat] save_sdk_credentials: no binding for user_id={}".format(user_id))
                return

            cred_data = {
                "token": bot_token,
                "base_url": base_url,
                "account_id": ilink_bot_id,
                "user_id": ilink_user_id
            }
            binding.ilink_token = json.dumps(cred_data)
            db.commit()
            logger.info("[WeChat] Saved SDK credentials for user_id={}".format(user_id))
        except Exception as e:
            logger.error("[WeChat] save_sdk_credentials error: {}".format(e))
            db.rollback()
        finally:
            db.close()

    async def start_polling(self, user_id: int = None, base_info: Dict[str, Any] = None):
        """
        为单个用户启动 SDK 轮询（per-user 版本）

        支持两种调用方式：
        1. start_polling(user_id) - 从 DB 加载凭证（推荐，fake login 模式）
        2. start_polling(base_info={...}) - 兼容旧调用方式

        SDK 的 start() 方法会先调用 _load_credentials()，如果 DB 中有凭证则跳过 login()。
        """
        # 兼容旧调用：如果传入 base_info，从中提取 ilink_user_id 查 user_id
        if base_info is not None and user_id is None:
            ilink_uid = base_info.get("ilink_user_id")
            if not ilink_uid:
                logger.error("[WeChat] start_polling: ilink_user_id not available in base_info")
                return
            db = SessionLocal()
            try:
                binding = db.query(WeChatBinding).filter(
                    WeChatBinding.ilink_user_id == ilink_uid,
                    WeChatBinding.status == "active"
                ).first()
                if not binding:
                    logger.error("[WeChat] start_polling: no active binding for ilink_user_id={}".format(ilink_uid))
                    return
                user_id = binding.user_id
            finally:
                db.close()

        if user_id is None:
            logger.error("[WeChat] start_polling: user_id is required")
            return

        # Force restart check: if polling_running is True but the executor thread is dead,
        # this means a previous loop crashed/was abandoned - force restart by cleaning up stale state
        if self._polling_running.get(user_id, False):
            executor = self._sdk_executors.get(user_id)
            thread_alive = False
            if executor:
                # Check if any thread in the executor is still alive
                for thread in list(executor._threads):
                    if thread.is_alive():
                        thread_alive = True
                        break

            if not thread_alive:
                # Stale state detected - clean up before restarting
                logger.warning("[WeChat] Detected stale polling state for user {} (running=True but thread dead), force restarting".format(user_id))
                self._polling_running[user_id] = False
                # Clean up stale executor
                if executor:
                    executor.shutdown(wait=False)
                self._sdk_executors.pop(user_id, None)
                # Clean up stale client
                self._user_clients.pop(user_id, None)
                self._polling_tasks.pop(user_id, None)
                # Keep restart_counts for tracking
            else:
                logger.info("[WeChat] Polling already running for user {}, skipping start".format(user_id))
                return

        self._polling_running[user_id] = True
        self._last_activity[user_id] = time.time()
        self._last_heartbeat[user_id] = time.time()
        if user_id not in self._restart_counts:
            self._restart_counts[user_id] = 0

        # Capture and store the main event loop (for callback scheduling from SDK thread)
        loop = asyncio.get_running_loop()
        self._main_loop = loop

        # 创建 per-user executor
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wechat_sdk_{}_".format(user_id))
        self._sdk_executors[user_id] = executor

        # Heartbeat callback: updates _last_heartbeat from SDK thread
        def heartbeat_callback():
            self._last_heartbeat[user_id] = time.time()

        # 创建 SDK client 并注册回调
        client = DatabaseBackedClient(user_id, on_heartbeat=heartbeat_callback)
        self._user_clients[user_id] = client

        # Callback wrapper that uses the captured main loop to schedule async handler
        def make_callback(uid: int, main_loop: asyncio.AbstractEventLoop):
            def callback(msg: IncomingMessage):
                self._on_sdk_message(msg, uid, main_loop)
            return callback

        client.on_message(make_callback(user_id, loop))

        def sdk_loop():
            logger.debug("[WeChat] sdk_loop started for user {}".format(user_id))
            retry_count = 0
            max_retries = 3
            bot_thread = None

            def run_bot():
                """Run client.start() in a separate thread with its own event loop."""
                bot_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(bot_loop)
                try:
                    bot_loop.run_until_complete(client.start())
                finally:
                    bot_loop.close()

            while self._polling_running.get(user_id, False) and retry_count < max_retries:
                try:
                    logger.debug("[WeChat] sdk_loop calling client.start() for user {}".format(user_id))
                    # Run bot.start() in a separate daemon thread with its own event loop
                    # This prevents blocking the main event loop
                    bot_thread = threading.Thread(target=run_bot, daemon=True)
                    bot_thread.start()

                    # Wait for thread with timeout so we can check _polling_running
                    while bot_thread.is_alive() and self._polling_running.get(user_id, False):
                        bot_thread.join(timeout=0.5)

                    logger.debug("[WeChat] sdk_loop client.start() RETURNED for user {}".format(user_id))
                    break
                except RuntimeError as e:
                    # Handle "Event loop is closed" or similar
                    if "Event loop" in str(e):
                        logger.error("[WeChat] sdk_loop event loop error for user {}: {}".format(user_id, e))
                    raise
                except Exception as e:
                    err_msg = str(e)
                    logger.warning("[WeChat] SDK polling exception for user {}: {} (retry {}/{})".format(
                        user_id, err_msg, retry_count, max_retries))
                    if "Session expired" in err_msg or "未登录" in err_msg:
                        if retry_count < max_retries - 1:
                            retry_count += 1
                            time.sleep(5)
                            continue
                        else:
                            # Max retries exceeded - ensure cleanup
                            self._polling_running[user_id] = False
                            if self._on_session_expired:
                                asyncio.run_coroutine_threadsafe(
                                    self._on_session_expired(),
                                    loop
                                )
                            if self._on_error:
                                asyncio.run_coroutine_threadsafe(
                                    self._on_error({"code": ERR_SESSION_EXPIRED, "message": "会话过期，已停止轮询"}),
                                    loop
                                )
                    break
            # Ensure _polling_running is False when loop exits (in case of other exit paths)
            self._polling_running[user_id] = False

        task = loop.run_in_executor(executor, sdk_loop)
        self._polling_tasks[user_id] = task
        logger.info("[WeChat] Started SDK polling loop for user {} in executor".format(user_id))

    async def stop_polling(self, user_id: int = None):
        """停止轮询：指定 user_id 则停该用户，否则停所有"""
        if user_id is not None:
            users_to_stop = [user_id] if user_id in self._polling_running else []
        else:
            users_to_stop = list(self._polling_running.keys())

        for uid in users_to_stop:
            self._polling_running[uid] = False
            task = self._polling_tasks.get(uid)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # 关闭 executor
            executor = self._sdk_executors.pop(uid, None)
            if executor:
                executor.shutdown(wait=False)
            # 清理 client 和状态
            self._user_clients.pop(uid, None)
            self._polling_tasks.pop(uid, None)
            self._last_heartbeat.pop(uid, None)
            self._last_activity.pop(uid, None)
            logger.info("[WeChat] Stopped polling for user {}".format(uid))

    # ============================================================
    # 问题 2 修复: _on_sdk_message 用 add_done_callback 捕获异常
    # ============================================================

    def _on_sdk_message(self, msg: IncomingMessage, user_id: int, main_loop: asyncio.AbstractEventLoop):
        """SDK 消息回调（在 SDK 线程中调用）—— 使用官方 SDK 的 IncomingMessage 类型
        import traceback as _tb
        logger.debug(f"[WX_DEBUG] _on_sdk_message: type={msg.type}, images={len(msg.images)}, content={str(msg.content)[:100] if msg.content else "None"}")
        if msg.images:
            for i, img in enumerate(msg.images):
                logger.debug(f"[WX_DEBUG]   image[{i}]: url={img.url}, media={*** if img.media else 'None'}")

        Args:
            msg: The incoming message from the SDK
            user_id: The user_id this polling session belongs to
            main_loop: The main service's event loop (captured from start_polling)
        """
        # 更新看门狗心跳
        self._last_activity[user_id] = time.time()

        # 官方 SDK IncomingMessage 属性：
        # - user_id: 发送者 (from_user_id)
        # - text: 消息文本
        # - type: 内容类型 (text/image/voice/file/video)
        # - raw: 原始消息字典
        # - _context_token: 上下文令牌

        # 从 raw 字典提取更多信息
        raw_msg = msg.raw
        from_user_id = msg.user_id or ""  # 官方 SDK 中 user_id 就是发送者
        to_user_id = raw_msg.get("to_user_id") or ""
        context_token = msg.context_token or ""
        msg_type = raw_msg.get("message_type") or 1  # 1 = USER message

        # 使用 _build_message_dict 提取媒体信息（图片/语音/文件/视频）
        import uuid
        trace_id = str(uuid.uuid4())[:8]
        msg_text = msg.text or ""
        logger.debug(f"[WeChat] _on_sdk_message [{trace_id}]: msg.type={repr(msg.type)}, msg.text={repr(msg_text[:30])}")

        base = self._build_message_dict(msg)

        msg_dict = {
            "msg_type": msg_type,
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "content": {"text": msg_text},
            "context_token": context_token,
            "client_id": raw_msg.get("client_id") or "",
            "msg_id": raw_msg.get("msg_id") or "",
            "raw": raw_msg,
            "content_type": msg.type or "text",
            "image_url": base.get("image_url"),
            "_media_info": base.get("_media_info"),
            "_voice_info": base.get("_voice_info"),
            "_file_info": base.get("_file_info"),
            "_video_info": base.get("_video_info"),
            "_trace_id": trace_id,
        }

        logger.debug(f"[WeChat] _on_sdk_message [{trace_id}]: msg_dict['content_type']={msg_dict.get('content_type')}, _trace_id={msg_dict.get('_trace_id')}")
        logger.debug(f"[WeChat] _on_sdk_message [{trace_id}]: msg.type={msg.type}, images_count={len(msg.images)}")

        logger.info("[WeChat] Received message from {}: {}...".format(
            from_user_id, msg_text[:50] if msg_text else f"[{msg.type or 'unknown'}]"))

        # DEBUG: 打印图片信息
        logger.debug(f"[WeChat] DEBUG: msg.images={msg.images}, msg.type={msg.type}, msg_text={msg_text[:30] if msg_text else 'None'}")
        if msg.images:
            for i, img in enumerate(msg.images):
                logger.debug(f"[WeChat] DEBUG: image[{i}]: url={img.url}, has_media={img.media is not None}")

        # Schedule the async handler in the main event loop (not the SDK's loop)
        future = asyncio.run_coroutine_threadsafe(
            self._handle_message(msg_dict),
            main_loop
        )

        def log_future_result(f: asyncio.Future):
            try:
                f.result()
            except Exception as e:
                logger.error("[WeChat] _handle_message in executor error for user {}: {}".format(user_id, e))

        future.add_done_callback(log_future_result)

    async def _polling_loop(self, base_info: Dict[str, Any] = None):
        """长轮询循环（保留用于兼容性，不再被 start_polling 调用）"""
        retry_count = 0
        max_retries = 3

        first_key = next(iter(self._polling_running), None)
        while first_key and self._polling_running.get(first_key, False):
            try:
                data = await self.get_updates(base_info or self._current_base_info)

                ret = data.get("ret")
                if ret == ERR_SESSION_EXPIRED:
                    logger.warning("[WeChat] Session expired (retry {}/{})".format(retry_count, max_retries))
                    if retry_count < max_retries:
                        retry_count += 1
                        await asyncio.sleep(5)
                        continue
                    else:
                        logger.error("[WeChat] Max retries reached, stopping polling")
                        if self._on_session_expired:
                            await self._on_session_expired()
                        if self._on_error:
                            await self._on_error({"code": ERR_SESSION_EXPIRED, "message": "会话过期，已停止轮询"})
                        break

                retry_count = 0

                msgs = data.get("msgs", [])
                if msgs:
                    logger.info("[WeChat] Received {} messages".format(len(msgs)))
                    for msg in msgs:
                        await self._handle_message(msg, user_id=None)

                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[WeChat] Polling error: {}".format(e))
                await asyncio.sleep(5)

    # ============================================================
    # 消息分发：按 content_type 路由到对应 handler
    # ============================================================

    async def _handle_message(self, msg: Dict[str, Any], user_id: int = None):
        """统一消息入口，按 content_type 分发到对应 handler。

        由 _on_sdk_message (SDK 线程回调 → run_coroutine_threadsafe) 调用。
        也兼容旧的 _polling_loop 路径。
        """
        logger.debug(f"[WeChat] _handle_message CALLED: msg_type={msg.get('msg_type')}, from_user_id={msg.get('from_user_id')}")
        try:
            from_user_id = msg.get("from_user_id") or msg.get("user_id", "")
            to_user_id = msg.get("to_user_id", "")
            context_token = msg.get("context_token", "")

            if context_token and from_user_id:
                self._context_tokens[(from_user_id, to_user_id)] = context_token

            binding = self.get_binding_by_ilink_user_id(from_user_id)
            logger.debug(f"[WeChat] _handle_message: binding found={binding is not None}, binding_id={binding.id if binding else None}")
            if binding:
                self._update_last_msg_time(binding)
                logger.info("[WeChat] msg from_user_id={} | stored wx_openid={} ilink_user_id={}".format(
                    from_user_id, binding.wx_openid, binding.ilink_user_id))
            else:
                logger.warning("[WeChat] No binding found for from_user_id (ilink_user_id)={}".format(from_user_id))
                return

            # 先保存 WeChatMessage（handlers 可以后续更新内容）
            msg_type = msg.get("msg_type", 1)
            content = msg.get("content", {})
            client_id = msg.get("client_id", "")
            logger.debug(f"[WeChat] _handle_message: calling _save_message, content type={type(content)}, content={str(content)[:100]}")
            self._save_message(binding, "received", content, msg_type, client_id, msg.get("msg_id"), agent_hash=binding.agent_hash)

            # 按 content_type 分发
            content_type = msg.get("content_type", "text")
            logger.debug(f"[WeChat] _handle_message: dispatching content_type={content_type}")

            if content_type == "text":
                await self._handle_text_message(msg, binding)
            elif content_type == "image":
                await self._handle_image_message(msg, binding)
            elif content_type == "voice":
                await self._handle_voice_message(msg, binding)
            elif content_type == "file":
                await self._handle_file_message(msg, binding)
            elif content_type == "video":
                await self._handle_video_message(msg, binding)
            else:
                logger.info(f"[WeChat] Unknown content_type={content_type}, falling back to text")
                await self._handle_text_message(msg, binding)

        except Exception as e:
            logger.error("[WeChat] Error handling message: {}".format(e))

    # ── Text Handler ────────────────────────────────────────────────

    async def _handle_text_message(self, msg: dict, binding):
        """Handle text message — calls _on_message to trigger agent."""
        if not self._on_message:
            return

        msg_type = msg.get("msg_type", 1)
        from_user_id = msg.get("from_user_id", "")
        to_user_id = msg.get("to_user_id", "")
        content = msg.get("content", {})
        context_token = msg.get("context_token", "")
        client_id = msg.get("client_id", "")
        msg_id = msg.get("msg_id", "")

        await self._on_message({
            "msg_type": msg_type,
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "content": content,
            "context_token": context_token,
            "client_id": client_id,
            "msg_id": msg_id,
            "raw": msg.get("raw", msg),
            "content_type": "text",
            "image_url": None,
            "_media_info": None,
            "_trace_id": msg.get("_trace_id", ""),
        })

    # ── Image Handler ───────────────────────────────────────────────

    async def _handle_image_message(self, msg: dict, binding):
        """Handle image message — CDN download + VFS save + VLM description.

        Extracted from the old _handle_message image logic.
        """
        msg_type = msg.get("msg_type", 1)
        from_user_id = msg.get("from_user_id", "")
        to_user_id = msg.get("to_user_id", "")
        content = msg.get("content", {})
        context_token = msg.get("context_token", "")
        client_id = msg.get("client_id", "")
        msg_id = msg.get("msg_id", "")
        _media_info = msg.get("_media_info")
        image_url = msg.get("image_url")
        _trace_id = msg.get("_trace_id", "")
        raw = msg.get("raw", msg)

        if not self._on_message:
            return

        # 下载并保存到 Agent 工作区
        saved_image_path = None
        _img_desc = None
        try:
            agent_hash = binding.agent_hash
            if agent_hash:
                from services.storage_service import StorageService
                import uuid as _uuid
                from datetime import datetime as _datetime
                storage = StorageService()
                ts = _datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"images/wechat_{ts}_{_uuid.uuid4().hex[:4]}.png"
                cos_key = f"feclaw/agents/{agent_hash}/workspace/{filename}"

                # CDN 下载
                img_bytes = None
                if _media_info and _media_info.get("media"):
                    try:
                        img_bytes = await self._download_cdn_media(_media_info["media"])
                    except Exception as e:
                        logger.error(f"[WeChat] CDN download via _download_cdn_media failed: {e}")
                        # 回退到旧函数
                        img_bytes = await download_wechat_image_from_media(
                            _media_info["media"],
                            aes_key_override=_media_info.get("aes_key") if isinstance(_media_info, dict) else None
                        )
                elif image_url:
                    import httpx
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.get(image_url)
                        if resp.status_code == 200:
                            img_bytes = resp.content

                if img_bytes:
                    storage.put_object(cos_key, img_bytes)
                    saved_image_path = f"/workspace/{filename}"
                    logger.info(f"[WeChat] Saved image to {cos_key}")

                    # VLM 描述（并发送 typing）
                    _use_4d = False
                    try:
                        from models.database import AgentProfile, SessionLocal
                        _adb = SessionLocal()
                        try:
                            _ap = _adb.query(AgentProfile).filter(
                                AgentProfile.hash == binding.agent_hash
                            ).first()
                            if _ap and not _ap.sr_enabled:
                                _use_4d = True
                        finally:
                            _adb.close()
                    except Exception as e:
                        logger.debug(f"[WeChat] Failed to check SR config: {e}")

                    async def _run_pre_and_typing():
                        async def _do_pre():
                            try:
                                if _use_4d:
                                    from services.image_describer import describe_image_4d
                                    _t = time.time()
                                    _desc = await describe_image_4d(img_bytes, timeout=15.0)
                                    if _desc:
                                        logger.info(f"[PERF] wechat_service: 4d_parallel ({time.time()-_t:.1f}s, {len(_desc)} chars)")
                                    return _desc
                                else:
                                    from services.image_describer import describe_image_3d
                                    _t = time.time()
                                    _desc = await describe_image_3d(img_bytes, timeout=15.0)
                                    if _desc:
                                        logger.info(f"[PERF] wechat_service: 3d_parallel ({time.time()-_t:.1f}s, {len(_desc)} chars)")
                                    return _desc
                            except Exception as e:
                                logger.debug(f"[DEBUG] Pre-recognition failed: {e}")
                                return None

                        async def _do_typing():
                            try:
                                await self.send_typing(from_user_id, context_token)
                            except Exception as e:
                                logger.debug(f"[WeChat] Typing indicator failed: {e}")

                        desc_task = asyncio.create_task(_do_pre())
                        await _do_typing()
                        return await desc_task

                    _img_desc = await _run_pre_and_typing()
        except Exception as e:
            logger.error(f"[WeChat] Failed to save image: {e}", exc_info=True)

        # 调用 _on_message 回调
        _img_desc_kv = {}
        if _img_desc:
            _img_desc_kv["_image_description"] = _img_desc
        await self._on_message({
            "msg_type": msg_type,
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "content": content,
            "context_token": context_token,
            "client_id": client_id,
            "msg_id": msg_id,
            "raw": raw,
            "content_type": "image",
            "image_url": saved_image_path or image_url,
            "_media_info": _media_info,
            "_trace_id": _trace_id,
            **_img_desc_kv,
        })

    # ── Voice Handler ───────────────────────────────────────────────

    async def _handle_voice_message(self, msg: dict, binding):
        """Handle voice message — Phase A: prefer SDK self-provided transcript.

        No ASR, no pilk, no ffmpeg. Falls back to "暂不支持处理" if no transcript.
        """
        voice_info = msg.get("_voice_info", {})
        transcript = voice_info.get("text")  # SDK self-provided transcript

        if transcript:
            content_text = f"[语音] {transcript}"
            logger.info(f"[WeChat] Voice message with transcript: {transcript[:50]}...")
        else:
            # Download SILK bytes and store for Phase B
            media = voice_info.get("media")
            if media:
                try:
                    voice_bytes = await self._download_cdn_media(media)
                    vfs_path = self._save_media_to_vfs(voice_bytes, "voice", "voice.silk", msg, binding)
                    if vfs_path:
                        logger.info(f"[WeChat] Voice saved to VFS: {vfs_path}")
                except Exception as e:
                    logger.error(f"[WeChat] Voice download failed: {e}")
            content_text = "[语音]（暂不支持处理）"

        # Update WeChatMessage content
        self._save_chat_history(msg, binding, "user", content_text)

        # Trigger agent with voice content as text
        if self._on_message:
            msg_type = msg.get("msg_type", 1)
            await self._on_message({
                "msg_type": msg_type,
                "from_user_id": msg.get("from_user_id", ""),
                "to_user_id": msg.get("to_user_id", ""),
                "content": {"text": content_text},
                "context_token": msg.get("context_token", ""),
                "client_id": msg.get("client_id", ""),
                "msg_id": msg.get("msg_id", ""),
                "raw": msg.get("raw", msg),
                "content_type": "voice",
                "image_url": None,
                "_media_info": None,
                "_trace_id": msg.get("_trace_id", ""),
            })

    # ── File Handler ────────────────────────────────────────────────

    async def _handle_file_message(self, msg: dict, binding):
        """Handle file message — CDN download + VFS store + notify Agent.

        Phase A: downloads and stores, does NOT parse content.
        """
        file_info = msg.get("_file_info", {})
        media = file_info.get("media")
        if not media:
            logger.warning("[WeChat] File message with no media, skipping")
            return

        # Size check (50MB max)
        file_size = file_info.get("size", 0) or 0
        max_size = 50 * 1024 * 1024
        if file_size > max_size:
            content_text = f"[文件] {file_info.get('file_name', '?')}（{file_size // 1024 // 1024}MB，超过限制）"
            logger.info(f"[WeChat] File too large: {content_text}")
            self._save_chat_history(msg, binding, "user", content_text)
            if self._on_message:
                await self._on_message({
                    "msg_type": msg.get("msg_type", 1),
                    "from_user_id": msg.get("from_user_id", ""),
                    "to_user_id": msg.get("to_user_id", ""),
                    "content": {"text": content_text},
                    "context_token": msg.get("context_token", ""),
                    "client_id": msg.get("client_id", ""),
                    "msg_id": msg.get("msg_id", ""),
                    "raw": msg.get("raw", msg),
                    "content_type": "file",
                    "image_url": None,
                    "_media_info": None,
                    "_trace_id": msg.get("_trace_id", ""),
                })
            return

        # Download
        try:
            file_bytes = await self._download_cdn_media(media)
        except Exception as e:
            logger.error(f"[WeChat] File download failed: {e}")
            content_text = "[文件]（下载失败）"
            self._save_chat_history(msg, binding, "user", content_text)
            return

        # Sanitize filename
        safe_name = os.path.basename(file_info.get("file_name") or "file.bin")
        if ".." in safe_name or "\0" in safe_name:
            safe_name = "file.bin"

        # Save to VFS
        vfs_path = self._save_media_to_vfs(file_bytes, "file", safe_name, msg, binding)

        # Notify Agent
        content_text = f"[文件] {safe_name}（{len(file_bytes)} 字节）"
        logger.info(f"[WeChat] File saved: {content_text}, vfs={vfs_path}")
        self._save_chat_history(msg, binding, "user", content_text)

        if self._on_message:
            await self._on_message({
                "msg_type": msg.get("msg_type", 1),
                "from_user_id": msg.get("from_user_id", ""),
                "to_user_id": msg.get("to_user_id", ""),
                "content": {"text": content_text},
                "context_token": msg.get("context_token", ""),
                "client_id": msg.get("client_id", ""),
                "msg_id": msg.get("msg_id", ""),
                "raw": msg.get("raw", msg),
                "content_type": "file",
                "image_url": None,
                "_media_info": None,
                "_trace_id": msg.get("_trace_id", ""),
            })

    # ── Video Handler ───────────────────────────────────────────────

    async def _handle_video_message(self, msg: dict, binding):
        """Handle video message — download + store + "not supported" reply.

        Phase A: downloads and stores, does NOT process video content.
        """
        video_info = msg.get("_video_info", {})
        media = video_info.get("media")
        if media:
            try:
                video_bytes = await self._download_cdn_media(media)
                vfs_path = self._save_media_to_vfs(video_bytes, "video", "video.bin", msg, binding)
                if vfs_path:
                    logger.info(f"[WeChat] Video saved to VFS: {vfs_path} ({len(video_bytes)} bytes)")
            except Exception as e:
                logger.warning(f"[WeChat] Video download failed: {e}")

        content_text = "[视频]（暂不支持查看）"
        self._save_chat_history(msg, binding, "user", content_text)

        # Send a direct reply (don't trigger agent for unsupported video)
        from_user_id = msg.get("from_user_id", "")
        context_token = msg.get("context_token", "")
        if from_user_id:
            try:
                await self.send_message(
                    to_user_id=from_user_id,
                    text="收到视频，暂不支持查看",
                    context_token=context_token
                )
            except Exception as e:
                logger.error(f"[WeChat] Failed to send video reply: {e}")

    def _update_last_msg_time(self, binding: WeChatBinding):
        """更新最后消息时间（直接接受 binding 对象，避免重复查询）"""
        db = SessionLocal()
        try:
            db_binding = db.query(WeChatBinding).filter(
                WeChatBinding.id == binding.id
            ).first()
            if db_binding:
                db_binding.last_msg_at = datetime.now()
                db.commit()
        except Exception as e:
            logger.error("[WeChat] Error updating last_msg_at: {}".format(e))
            db.rollback()
        finally:
            db.close()

    def _save_message(
        self,
        binding: WeChatBinding,
        direction: str,
        content: Dict[str, Any],
        msg_type: int,
        client_id: str,
        msg_id: str = None,
        agent_hash: str = None
    ):
        """保存消息到数据库（直接接受 binding 对象）

        Args:
            binding: 微信绑定对象
            direction: 消息方向 ("sent" 或 "received")
            content: 消息内容字典
            msg_type: 消息类型编号
            client_id: 客户端 ID
            msg_id: 消息 ID
            agent_hash: Agent hash（用于消息隔离）
        """
        db = SessionLocal()
        db_binding = None
        text_content = ""
        try:
            # 消息去重：如果已有相同 msg_id 的消息，跳过保存
            if msg_id:
                existing = db.query(WeChatMessage).filter(
                    WeChatMessage.msg_id == msg_id
                ).first()
                if existing:
                    logger.debug(f"[WeChat] Duplicate message {msg_id}, skipping")
                    return

            db_binding = db.query(WeChatBinding).filter(
                WeChatBinding.id == binding.id
            ).first()
            if not db_binding:
                logger.warning("[WeChat] No binding found by id: {}".format(binding.id))
                return

            # Handle None values properly: .get(key, default) returns None when key exists with value None
            if isinstance(content, dict):
                text_content = content.get("text") or ""
            else:
                text_content = str(content) if content is not None else ""

            # Ensure all non-nullable fields have valid values
            # wx_openid is required but may be None in DB - use ilink_user_id as fallback
            wx_openid = db_binding.wx_openid or db_binding.ilink_user_id or "unknown"
            # direction must not be None
            safe_direction = direction or "unknown"
            # content must not be empty for non-nullable column
            safe_content = text_content or ""

            # agent_hash: 如果传入则使用，否则从 binding 获取（binding.agent_hash 是绑定时指定的）
            msg_agent_hash = agent_hash or db_binding.agent_hash or ""

            message = WeChatMessage(
                binding_id=db_binding.id,
                agent_hash=msg_agent_hash,
                wx_openid=wx_openid,
                direction=safe_direction,
                content=safe_content,
                message_type=get_msg_type_name(msg_type),
                client_id=client_id or "",
                msg_id=msg_id,
                created_at=datetime.utcnow(),
            )
            db.add(message)
            db.commit()
            logger.info("[WeChat] Saved message: binding_id={}, direction={}, content_len={}".format(
                db_binding.id, safe_direction, len(safe_content)))
        except Exception as e:
            logger.error("[WeChat] Error saving message: {} | binding_id={} wx_openid={} direction={} content_len={}".format(
                e, db_binding.id if db_binding else None,
                db_binding.wx_openid if db_binding else None,
                direction, len(text_content) if text_content else 0))
            db.rollback()
        finally:
            db.close()

    def update_message_content(self, binding_id: int, old_content: str, new_content: str):
        """
        更新消息内容（用于图片消息下载到VFS后更新路径）

        Args:
            binding_id: 绑定ID
            old_content: 原来的内容（如 "[image]"）
            new_content: 新的内容（包含VFS路径）
        """
        db = SessionLocal()
        try:
            # 查找最新的匹配消息
            message = db.query(WeChatMessage).filter(
                WeChatMessage.binding_id == binding_id,
                WeChatMessage.direction == "received",
                WeChatMessage.content == old_content
            ).order_by(WeChatMessage.created_at.desc()).first()

            if message:
                message.content = new_content
                db.commit()
                logger.info(f"[WeChat] Updated message content: id={message.id}, new_content={new_content}")
            else:
                logger.warning(f"[WeChat] No message found to update: binding_id={binding_id}, old_content={old_content}")
        except Exception as e:
            logger.error(f"[WeChat] Error updating message content: {e}")
            db.rollback()
        finally:
            db.close()

    # ============================================================
    # 看门狗机制
    # ============================================================

    async def _watchdog_loop(self):
        """看门狗协程：定期检查各用户轮询线程是否存活"""
        logger.info("[WeChat] Watchdog loop started")
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            try:
                now = time.time()
                dead_users = []
                for uid, is_running in list(self._polling_running.items()):
                    if not is_running:
                        continue

                    # Check if the executor thread is actually alive
                    executor = self._sdk_executors.get(uid)
                    thread_alive = False
                    if executor:
                        for thread in list(executor._threads):
                            if thread.is_alive():
                                thread_alive = True
                                break

                    if thread_alive:
                        # Thread is alive - log heartbeat status for diagnostics
                        last_hb = self._last_heartbeat.get(uid, 0)
                        last_msg = self._last_activity.get(uid, 0)
                        hb_age = int(now - last_hb)
                        msg_age = int(now - last_msg)
                        # Log every 5 minutes (10 cycles) or if heartbeat is stale
                        if hb_age > WATCHDOG_INTERVAL * 2 or msg_age > 300:
                            logger.info("[WeChat] Watchdog: user {} alive, last_heartbeat={}s ago, last_message={}s ago".format(
                                uid, hb_age, msg_age))
                    else:
                        # Only declare "dead" if polling_running=True but thread is actually dead
                        dead_users.append(uid)

                for uid in dead_users:
                    restart_count = self._restart_counts.get(uid, 0)
                    if restart_count >= MAX_RESTART_COUNT:
                        logger.error("[WeChat] Watchdog: user {} exceeded max restart count ({}), giving up and cleaning up state".format(
                            uid, MAX_RESTART_COUNT))
                        # Clean up all state for this user
                        self._polling_running[uid] = False
                        executor = self._sdk_executors.pop(uid, None)
                        if executor:
                            executor.shutdown(wait=False)
                        self._user_clients.pop(uid, None)
                        self._polling_tasks.pop(uid, None)
                        self._restart_counts.pop(uid, None)
                        self._last_activity.pop(uid, None)
                        self._last_heartbeat.pop(uid, None)
                        # Trigger error callback to notify about giving up
                        if self._on_error:
                            asyncio.create_task(self._on_error({
                                "code": ERR_SESSION_EXPIRED,
                                "message": "看门狗放弃重启，已清理用户状态",
                                "user_id": uid
                            }))
                        continue

                    logger.warning("[WeChat] Watchdog: user {} polling thread is dead, restarting...".format(uid))
                    self._restart_counts[uid] = restart_count + 1

                    # 停止旧的
                    await self.stop_polling(uid)

                    # 重新启动：需要重新获取 base_info
                    db = SessionLocal()
                    try:
                        binding = db.query(WeChatBinding).filter(
                            WeChatBinding.user_id == uid,
                            WeChatBinding.status == "active"
                        ).first()
                        if binding and binding.ilink_token:
                            # 使用 user_id 调用，凭证已存在 DB 中
                            await self.start_polling(user_id=uid)
                            logger.info("[WeChat] Watchdog: restarted polling for user {} (restart #{})".format(
                                uid, restart_count + 1))
                        else:
                            logger.warning("[WeChat] Watchdog: no valid binding for user {}, not restarting".format(uid))
                    except Exception as e:
                        logger.error("[WeChat] Watchdog: failed to restart user {}: {}".format(uid, e))
                    finally:
                        db.close()

            except asyncio.CancelledError:
                logger.info("[WeChat] Watchdog loop cancelled")
                break
            except Exception as e:
                logger.error("[WeChat] Watchdog loop error: {}".format(e))

    def start_watchdog(self):
        """启动看门狗（一次性，在 setup_message_handler 时调用）"""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
            logger.info("[WeChat] Watchdog task started")

    # ========== 发送消息 ==========

    async def send_message(
        self,
        to_user_id: str,
        text: str,
        context_token: str = None,
        base_info: Dict[str, Any] = None
    ) -> bool:
        """发送消息，超过 2000 字符时分片发送"""
        text = text or ""
        # 微信 Markdown 渲染器会将 {…} 识别为 LaTeX 公式起止符
        # 使用零宽空格断开大括号序列，防止渲染异常但视觉不变
        text = text.replace("{", "{\u200b").replace("}", "\u200b}")
        # 微信消息长度限制：超过 2000 字符时分片
        MAX_LEN = 2000
        if text and len(text) > MAX_LEN:
            parts = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]
            logger.info(f"[WeChat] send_message: text length {len(text)} > {MAX_LEN}, splitting into {len(parts)} parts")
            all_ok = True
            for idx, part in enumerate(parts):
                prefix = f"({idx+1}/{len(parts)}) " if len(parts) > 1 else ""
                ok = await self._send_single_message(to_user_id, prefix + part, context_token, base_info)
                if not ok:
                    all_ok = False
            return all_ok

        return await self._send_single_message(to_user_id, text, context_token, base_info)

    async def _send_single_message(
        self,
        to_user_id: str,
        text: str,
        context_token: str = None,
        base_info: Dict[str, Any] = None
    ) -> bool:
        """发送单条消息（内部方法）"""
        # ========== 详细日志 ==========
        logger.info("[WeChat] _send_single_message ENTER: to_user_id={}, text_len={}, context_token={}, base_info={}".format(
            to_user_id, len(text) if text else 0,
            "provided" if context_token else None,
            "provided" if base_info else None))
        if base_info:
            logger.info("[WeChat] _send_single_message base_info: bot_token={}, ilink_bot_id={}, ilink_user_id={}, baseurl={}".format(
                "***" if base_info.get("bot_token") else None,
                base_info.get("ilink_bot_id"),
                base_info.get("ilink_user_id"),
                base_info.get("baseurl")))

        # 如果没有传 base_info，尝试从数据库绑定记录中获取
        if base_info is None:
            # 先尝试从 _login_state 获取（兼容旧逻辑）
            base_info = {
                "bot_token": self._login_state.get("bot_token"),
                "ilink_bot_id": self._login_state.get("ilink_bot_id"),
                "ilink_user_id": self._login_state.get("ilink_user_id"),
                "baseurl": self._login_state.get("base_url"),
            }
            logger.info("[WeChat] _send_single_message: fell back to _login_state, bot_token={}, ilink_user_id={}".format(
                "***" if base_info.get("bot_token") else None,
                base_info.get("ilink_user_id")))

            # 如果 _login_state 没有有效凭证，从数据库绑定记录获取
            if not base_info.get("bot_token"):
                logger.info("[WeChat] _send_single_message: _login_state has no bot_token, querying binding by to_user_id={}".format(to_user_id))
                # to_user_id 是用户的 ilink_user_id，binding.ilink_user_id 存的也是用户的 ilink_user_id
                binding = self.get_binding_by_ilink_user_id(to_user_id)
                logger.info("[WeChat] _send_single_message: get_binding_by_ilink_user_id result: {}".format(
                    "found binding id={}".format(binding.id) if binding else "None"))
                if binding:
                    # 尝试从 ilink_token（SDK 凭证）解析
                    if binding.ilink_token:
                        try:
                            cred_data = json.loads(binding.ilink_token)
                            base_info = {
                                "bot_token": cred_data.get("token"),
                                "ilink_bot_id": cred_data.get("account_id"),
                                "ilink_user_id": cred_data.get("user_id"),
                                "baseurl": cred_data.get("base_url", ILINK_API_BASE),
                            }
                            logger.info("[WeChat] _send_single_message: parsed ilink_token, got bot_token={}, ilink_user_id={}".format(
                                "***" if base_info.get("bot_token") else None,
                                base_info.get("ilink_user_id")))
                        except (json.JSONDecodeError, TypeError) as e:
                            logger.warning("[WeChat] _send_single_message: failed to parse ilink_token: {}".format(e))
                    # 如果 ilink_token 无效，使用绑定记录中的字段
                    if not base_info.get("bot_token"):
                        base_info = {
                            "bot_token": binding.bot_token,
                            "ilink_bot_id": binding.ilink_bot_id,
                            "ilink_user_id": binding.ilink_user_id,
                            "baseurl": binding.base_url,
                        }
                        logger.info("[WeChat] _send_single_message: used binding fields, bot_token={}, ilink_user_id={}".format(
                            "***" if base_info.get("bot_token") else None,
                            base_info.get("ilink_user_id")))
                else:
                    logger.warning("[WeChat] _send_single_message: no binding found for to_user_id={}, tried ilink_bot_id lookup".format(to_user_id))

        if not base_info.get("bot_token"):
            raise ValueError("bot_token is required, please login first")

        if context_token is None:
            # context_token 缓存 key 格式: (user_id, bot_id)
            # _handle_message 存储时: key = (from_user_id, to_user_id) = (user_ilink, bot_ilink)
            # send_message 检索时也需要 (user_ilink, bot_ilink) 才能匹配
            # to_user_id = 用户的 ilink_user_id（调用方传入的 from_user_id）
            # base_info["ilink_user_id"] = bot 的 ilink_user_id
            cache_key = (to_user_id, base_info.get("ilink_user_id"))
            context_token = self._context_tokens.get(cache_key, "") or ""
            logger.debug("[WeChat] _send_single_message: context_token cache lookup key={}, found={}".format(
                cache_key, "yes" if context_token else "no"))

        # 确保 context_token 不为 None（微信 API 不接受 None）
        if context_token is None:
            context_token = ""

        url = "{}/ilink/bot/sendmessage".format(ILINK_API_BASE)

        # 按官方 SDK build_text_message 的格式，from_user_id 应为空字符串
        # 让 iLink API 自动填充发送者信息
        from_user_id = ""

        # 构建消息体（按官方 SDK build_text_message 的格式）
        msg_body = {
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "client_id": str(uuid4()),  # 官方 SDK 使用 uuid 字符串
            "message_type": MSG_TYPE_BOT,
            "message_state": 2,  # FINISH = 2，官方 SDK 必须此字段
            "context_token": context_token,
            "item_list": [
                {
                    "type": MSG_TYPE_TEXT,
                    "text_item": {"text": text}  # 官方 SDK 用 text_item 不是 content
                }
            ],
        }

        # 按官方 SDK protocol.py send_message 的格式：body = {"msg": msg, "base_info": ...}
        payload = {
            "msg": msg_body,
            "base_info": {
                "channel_version": "2.0.0"
            }
        }

        logger.warning("[WeChat] _send_single_message REQUEST: to_user_id={}, from_user_id={}, context_token='{}', payload={}".format(
            to_user_id, from_user_id, context_token[:50] if context_token else "empty", json.dumps(payload, ensure_ascii=False)[:800]))

        session = await self._get_session()
        headers = auth_headers(base_info.get("bot_token", ""))
        async with session.post(url, json=payload, headers=headers) as resp:
            api_response_text = await resp.text()
            logger.info("[WeChat] _send_single_message response: status={}, body={}".format(resp.status, api_response_text[:500]))
            try:
                data = json.loads(api_response_text) if api_response_text else {}
            except json.JSONDecodeError:
                data = {}
                logger.warning("[WeChat] _send_single_message: JSON parse error, body={}".format(api_response_text[:200]))

            # 成功：ret == 0，或响应为空但状态码 200，或空 JSON body {}
            if data.get("ret") == 0 or (resp.status == 200 and not api_response_text.strip()) or (resp.status == 200 and data == {}):
                # 从响应中提取新的 context_token 并更新缓存
                # iLink API 在响应中返回新的 context_token，用于下一次发送
                new_context_token = data.get("context_token", "")
                if new_context_token and to_user_id:
                    # 缓存 key = (bot_id, user_id) = (to_user_id, ilink_user_id)
                    self._context_tokens[(to_user_id, base_info.get("ilink_user_id"))] = new_context_token
                    logger.debug("[WeChat] Updated context_token cache after send: key=({}, {}), new_token_len={}".format(
                        to_user_id, base_info.get("ilink_user_id"), len(new_context_token)))

                return True
            else:
                logger.warning("[WeChat] _send_single_message failed: data={}, http_status={}, raw_text={}".format(
                    data, resp.status, api_response_text[:200]))
                return False

    async def send_typing(
        self,
        to_user_id: str,
        context_token: str,
        base_info: Dict[str, Any] = None
    ) -> bool:
        """发送 typing 状态"""
        # 如果没有传 base_info，尝试从数据库绑定记录中获取
        if base_info is None:
            base_info = {
                "bot_token": self._login_state.get("bot_token"),
                "ilink_bot_id": self._login_state.get("ilink_bot_id"),
                "ilink_user_id": self._login_state.get("ilink_user_id"),
                "baseurl": self._login_state.get("base_url"),
            }

            # 如果 _login_state 没有有效凭证，从数据库绑定记录获取
            if not base_info.get("bot_token"):
                binding = self.get_binding_by_ilink_user_id(to_user_id)
                if binding:
                    if binding.ilink_token:
                        try:
                            cred_data = json.loads(binding.ilink_token)
                            base_info = {
                                "bot_token": cred_data.get("token"),
                                "ilink_bot_id": cred_data.get("account_id"),
                                "ilink_user_id": cred_data.get("user_id"),
                                "baseurl": cred_data.get("base_url", ILINK_API_BASE),
                            }
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if not base_info.get("bot_token"):
                        base_info = {
                            "bot_token": binding.bot_token,
                            "ilink_bot_id": binding.ilink_bot_id,
                            "ilink_user_id": binding.ilink_user_id,
                            "baseurl": binding.base_url,
                        }

        typing_ticket = await self._get_typing_ticket(to_user_id, context_token, base_info)
        if not typing_ticket:
            logger.warning("[WeChat] send_typing failed: no typing_ticket")
            return False

        url = "{}/ilink/bot/sendtyping".format(ILINK_API_BASE)

        # 按官方 SDK protocol.py send_typing 的格式
        payload = {
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": 1,  # 1 = 开始输入
            "base_info": {
                "channel_version": "2.0.0"
            }
        }

        session = await self._get_session()
        headers = auth_headers(base_info.get("bot_token", ""))
        async with session.post(url, json=payload, headers=headers) as resp:
            data = await self._json_response(resp)
            if data.get("ret") == 0:
                return True
            logger.warning("[WeChat] send_typing failed: {}".format(data))
            return False

    async def _get_typing_ticket(
        self,
        to_user_id: str,
        context_token: str,
        base_info: Dict[str, Any]
    ) -> Optional[str]:
        """获取 typing ticket（按官方 SDK get_config 协议）"""
        url = "{}/ilink/bot/getconfig".format(ILINK_API_BASE)

        # 按官方 SDK：POST body 包含 ilink_user_id, context_token, base_info
        body = {
            "ilink_user_id": to_user_id,
            "context_token": context_token,
            "base_info": {
                "channel_version": "2.0.0"
            }
        }

        session = await self._get_session()
        headers = auth_headers(base_info.get("bot_token", ""))
        async with session.post(url, json=body, headers=headers) as resp:
            data = await self._json_response(resp)
            if data.get("ret") == 0:
                return data.get("typing_ticket")
            logger.warning("[WeChat] _get_typing_ticket failed: {}".format(data))
            return None

    # ========== 媒体下载 ==========

    async def download_media(self, media_url: str) -> bytes:
        """下载媒体文件"""
        if media_url.startswith("/"):
            media_url = "{}{}".format(ILINK_CDN_BASE, media_url)

        session = await self._get_session()
        async with session.get(media_url) as resp:
            if resp.status != 200:
                raise Exception("Failed to download media: {}".format(resp.status))
            return await resp.read()

    async def _download_cdn_media(self, media) -> bytes:
        """Download and decrypt media from WeChat CDN (image/voice/file/video).

        Reuses the same CDN download + AES decrypt pattern as
        sdk_adapter.download_wechat_image_from_media, generalized for all media types.
        """
        from urllib.parse import quote
        from services.wechatbot_sdk.crypto import decrypt_aes_ecb, decode_aes_key

        if not media:
            raise ValueError("Missing media")

        # Build download URL
        if hasattr(media, 'encrypt_query_param') and media.encrypt_query_param:
            download_url = (
                "https://novac2c.cdn.weixin.qq.com/c2c/download"
                "?encrypted_query_param=" + quote(media.encrypt_query_param)
            )
        elif hasattr(media, 'download_url') and media.download_url:
            download_url = media.download_url
        else:
            raise ValueError("No download URL or encrypt_query_param")

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(download_url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"CDN download failed: HTTP {resp.status}")
                ciphertext = await resp.read()

        # Decrypt
        aes_key = getattr(media, 'aes_key', None)
        if aes_key:
            key = decode_aes_key(aes_key)
            return decrypt_aes_ecb(ciphertext, key)
        return ciphertext

    # ========== 媒体存储辅助方法 ==========

    def _save_media_to_vfs(self, media_bytes: bytes, media_type: str, filename: str,
                           msg: dict, binding=None) -> str | None:
        """Save downloaded media to Agent VFS (COS-backed).

        Returns the VFS path or None on failure.
        """
        try:
            from services.storage_service import StorageService
            import uuid
            from datetime import datetime

            agent_hash = None
            if binding and binding.agent_hash:
                agent_hash = binding.agent_hash
            else:
                from_user_id = msg.get("from_user_id") or msg.get("user_id", "")
                b = self.get_binding_by_ilink_user_id(from_user_id)
                if b:
                    agent_hash = b.agent_hash

            if not agent_hash:
                logger.warning("[WeChat] _save_media_to_vfs: no agent_hash")
                return None

            storage = StorageService()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = os.path.basename(filename)
            if ".." in safe_name or "\0" in safe_name:
                safe_name = f"{media_type}.bin"

            vfs_rel = f"media/{media_type}/{ts}_{uuid.uuid4().hex[:4]}_{safe_name}"
            cos_key = f"feclaw/agents/{agent_hash}/workspace/{vfs_rel}"
            storage.put_object(cos_key, media_bytes)
            vfs_path = f"/workspace/{vfs_rel}"
            logger.info(f"[WeChat] Saved {media_type} to VFS: {vfs_path} ({len(media_bytes)} bytes)")
            return vfs_path
        except Exception as e:
            logger.error(f"[WeChat] _save_media_to_vfs failed: {e}")
            return None

    def _save_chat_history(self, msg: dict, binding, role: str, content: str):
        """Save a ChatHistory record for the message."""
        try:
            from models.database import ChatHistory, SessionLocal
            db = SessionLocal()
            try:
                record = ChatHistory(
                    user_id=binding.user_id,
                    agent_hash=binding.agent_hash or "",
                    role=role,
                    content=content,
                    channel="wechat",
                    session_id="wechat_main",
                    wechat_msg_id=msg.get("msg_id"),
                    created_at=datetime.utcnow(),
                )
                db.add(record)
                db.commit()
                logger.debug(f"[WeChat] Saved ChatHistory: role={role}, content_len={len(content)}")
            except Exception as e:
                logger.error(f"[WeChat] _save_chat_history DB error: {e}")
                db.rollback()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[WeChat] _save_chat_history failed: {e}")

    # ========== 事件回调 ==========

    def on_qrcode(self, handler: Callable):
        """设置二维码回调"""
        self._on_qrcode = handler

    def on_scanned(self, handler: Callable):
        """设置已扫码回调"""
        self._on_scanned = handler

    def on_confirmed(self, handler: Callable):
        """设置确认登录回调"""
        self._on_confirmed = handler

    def on_message(self, handler: Callable):
        """设置消息回调"""
        self._on_message = handler

    def on_error(self, handler: Callable):
        """设置错误回调"""
        self._on_error = handler

    def on_session_expired(self, handler: Callable):
        """设置 session 过期回调"""
        self._on_session_expired = handler

    # ========== 数据库操作 ==========

    def bind_user(self, user_id: int, wx_openid: str, login_data: Dict[str, Any]) -> WeChatBinding:
        """绑定用户（按 agent_hash 查找已有绑定，支持同一用户多个 agent 各自绑定）"""
        db = SessionLocal()
        try:
            existing = db.query(WeChatBinding).filter(
                WeChatBinding.user_id == user_id,
                WeChatBinding.agent_hash == login_data["agent_hash"]
            ).first()

            if existing:
                existing.wx_openid = wx_openid
                existing.bot_token = login_data.get("bot_token", "")
                existing.ilink_bot_id = login_data.get("ilink_bot_id", "")
                existing.ilink_user_id = login_data.get("ilink_user_id", "")
                existing.base_url = login_data.get("base_url", "")
                existing.status = "active"
                existing.agent_hash = login_data.get("agent_hash", existing.agent_hash or "")
                existing.bound_at = datetime.now()
                db.commit()
                binding_id = existing.id
                db.close()
                return WeChatBinding(id=binding_id)

            binding = WeChatBinding(
                user_id=user_id,
                wx_openid=wx_openid,
                bot_token=login_data.get("bot_token", ""),
                ilink_bot_id=login_data.get("ilink_bot_id", ""),
                ilink_user_id=login_data.get("ilink_user_id", ""),
                base_url=login_data.get("base_url", ""),
                agent_hash=login_data.get("agent_hash", ""),
                status="active",
            )
            db.add(binding)
            db.commit()
            binding_id = binding.id
            db.close()
            return WeChatBinding(id=binding_id)
        finally:
            if db and db.is_active:
                db.close()

    def get_binding_by_user(self, user_id: int, agent_hash: str = None) -> Optional[WeChatBinding]:
        """获取用户的微信绑定"""
        db = SessionLocal()
        try:
            query = db.query(WeChatBinding).filter(
                WeChatBinding.user_id == user_id,
                WeChatBinding.status == "active"
            )
            if agent_hash:
                query = query.filter(WeChatBinding.agent_hash == agent_hash)
            return query.first()
        finally:
            db.close()

    def get_binding_by_openid(self, wx_openid: str) -> Optional[WeChatBinding]:
        """根据 openid 获取绑定"""
        db = SessionLocal()
        try:
            return db.query(WeChatBinding).filter(
                WeChatBinding.wx_openid == wx_openid,
                WeChatBinding.status == "active"
            ).first()
        finally:
            db.close()

    def get_binding_by_ilink_user_id(self, ilink_user_id: str) -> Optional[WeChatBinding]:
        """根据 ilink_user_id 获取绑定（返回最新的 active 绑定）"""
        db = SessionLocal()
        try:
            return db.query(WeChatBinding).filter(
                WeChatBinding.ilink_user_id == ilink_user_id,
                WeChatBinding.status == "active"
            ).order_by(WeChatBinding.id.desc()).first()
        finally:
            db.close()

    def get_binding_by_ilink_bot_id(self, ilink_bot_id: str) -> Optional[WeChatBinding]:
        """根据 bot 的 ilink_user_id（ilink_bot_id）获取绑定

        注意：binding.ilink_user_id 存的是 bot 的 ilink_user_id，
        所以用 ilink_bot_id 来查找才能正确匹配。
        """
        db = SessionLocal()
        try:
            return db.query(WeChatBinding).filter(
                WeChatBinding.ilink_bot_id == ilink_bot_id,
                WeChatBinding.status == "active"
            ).first()
        finally:
            db.close()

    def get_messages(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        """获取用户的微信消息记录"""
        db = SessionLocal()
        try:
            binding = self.get_binding_by_user(user_id)
            if not binding:
                return []

            messages = db.query(WeChatMessage).filter(
                WeChatMessage.binding_id == binding.id
            ).order_by(WeChatMessage.created_at.desc()).limit(limit).all()

            return [
                {
                    "id": msg.id,
                    "direction": msg.direction,
                    "content": msg.content,
                    "message_type": msg.message_type,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                }
                for msg in messages
            ]
        finally:
            db.close()

    def unbind_user(self, user_id: int, agent_hash: str = None) -> bool:
        """解除绑定（agent_hash 非空时只解绑指定 Agent）"""
        db = SessionLocal()
        try:
            query = db.query(WeChatBinding).filter(
                WeChatBinding.user_id == user_id,
                WeChatBinding.status == "active"
            )
            if agent_hash:
                query = query.filter(WeChatBinding.agent_hash == agent_hash)
            binding = query.first()
            if binding:
                binding.status = "logout"
                binding.bot_token = ""
                binding.ilink_bot_id = ""
                binding.ilink_user_id = ""
                binding.base_url = ""
                db.commit()
                return True
            return False
        finally:
            db.close()

    def has_active_binding(self, user_id: int) -> bool:
        """检查用户是否还有活跃的微信绑定（用于决定是否停止轮询）"""
        db = SessionLocal()
        try:
            return db.query(WeChatBinding).filter(
                WeChatBinding.user_id == user_id,
                WeChatBinding.status == "active"
            ).first() is not None
        finally:
            db.close()

    # ========== 24小时动态提醒 ==========

    def check_inactive_users(self) -> List[int]:
        """检查24小时未活跃的用户"""
        db = SessionLocal()
        try:
            threshold = datetime.now() - timedelta(hours=24)
            bindings = db.query(WeChatBinding).filter(
                WeChatBinding.status == "active",
                WeChatBinding.last_msg_at < threshold
            ).all()
            return [b.user_id for b in bindings]
        finally:
            db.close()


# 全局单例
wechat_service = WeChatService()
