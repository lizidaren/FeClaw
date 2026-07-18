from __future__ import annotations

"""
SDK 通信层 - DatabaseBackedClient + iLink HTTP helpers

- DatabaseBackedClient: 用数据库存储凭证的 WeChatBot 子类
- _random_wechat_uin: 生成随机 X-WECHAT-UIN header
- _auth_headers: 生成 iLink API 认证头
- _download_wechat_image_from_media: 从 WeChat CDN 下载并解密图片
"""
import base64
import asyncio
import json
import logging
import os
import struct
import time
from typing import Optional, Dict, Any, Callable

from models.database import SessionLocal, WeChatBinding
from services.wechatbot_sdk import WeChatBot, Credentials, IncomingMessage
from services.wechatbot_sdk.crypto import decrypt_aes_ecb, decode_aes_key

from .models import ILINK_API_BASE

logger = logging.getLogger(__name__)


# ========== CDN 图片下载 ==========

async def download_wechat_image_from_media(media, aes_key_override: str | None = None) -> bytes:
    """从 WeChat CDN 下载并解密图片（iLink 协议）

    Args:
        media: CDNMedia dataclass（可能 aes_key 为空）
        aes_key_override: 可选的 aes_key 覆盖（来自 ImageContent.aes_key）
    """
    import aiohttp
    from urllib.parse import quote

    if not media:
        raise ValueError("Missing media for image download")

    # CDNMedia 可能没有 download_url，只有 encrypt_query_param
    if hasattr(media, 'download_url') and media.download_url:
        download_url = media.download_url
    elif hasattr(media, 'encrypt_query_param') and media.encrypt_query_param:
        download_url = f"https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param={quote(media.encrypt_query_param)}"
    else:
        raise ValueError("CDNMedia has neither download_url nor encrypt_query_param")

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(download_url) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"CDN download failed: HTTP {resp.status}")
            ciphertext = await resp.read()

    # 保存原始加密数据到 /tmp/wechat_cdn/
    ts = int(time.time() * 1000)
    cdn_dir = "/tmp/wechat_cdn"
    os.makedirs(cdn_dir, exist_ok=True)
    _enc_path = os.path.join(cdn_dir, f"cdn_{ts}_encrypted.bin")
    with open(_enc_path, "wb") as f:
        f.write(ciphertext)
    logger.info(f"[WeChat] Saved encrypted CDN data: {_enc_path} ({len(ciphertext)} bytes)")

    # aes_key 优先取 override（ImageContent 层级）
    aes_key = aes_key_override or getattr(media, 'aes_key', None)
    if not aes_key:
        raise ValueError("aes_key is empty (neither CDNMedia.aes_key nor override provided)")
    key = decode_aes_key(aes_key)
    plaintext = decrypt_aes_ecb(ciphertext, key)

    # 保存解密后的数据
    _dec_path = os.path.join(cdn_dir, f"cdn_{ts}_decrypted.png")
    with open(_dec_path, "wb") as f:
        f.write(plaintext)
    logger.info(f"[WeChat] Saved decrypted CDN data: {_dec_path} ({len(plaintext)} bytes)")

    return plaintext


# ========== HTTP 帮助函数 ==========

def random_wechat_uin() -> str:
    """生成随机 X-WECHAT-UIN header 值（与官方 SDK 一致）"""
    val = struct.unpack(">I", os.urandom(4))[0]
    return base64.b64encode(str(val).encode("utf-8")).decode("ascii")


def auth_headers(token: str) -> Dict[str, str]:
    """生成 iLink API 认证头（与官方 SDK 一致）"""
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": random_wechat_uin(),
    }


# ========== DatabaseBackedClient ==========

class DatabaseBackedClient(WeChatBot):
    """用数据库存储凭证的 WeChatBot 子类"""

    def __init__(self, user_id: int, on_heartbeat: Optional[Callable[[], None]] = None):
        # 不传递 cred_path，我们用数据库存储
        super().__init__(cred_path=None, on_heartbeat=on_heartbeat)
        self.user_id = user_id
        # 直接加载凭证到 _credentials
        self._load_credentials_from_db()

    def _load_credentials_from_db(self) -> bool:
        """从 WeChatBinding.ilink_token 加载凭证到 _credentials"""
        logger.debug("[WeChat] _load_credentials_from_db for user_id={}".format(self.user_id))
        db = SessionLocal()
        try:
            binding = db.query(WeChatBinding).filter(
                WeChatBinding.user_id == self.user_id
            ).first()
            logger.debug("[WeChat] binding found={}, ilink_token not None={}".format(
                bool(binding), bool(binding.ilink_token) if binding else False))
            if not binding or not binding.ilink_token:
                logger.warning("[WeChat] No credentials in DB for user {}".format(self.user_id))
                return False

            cred_data = json.loads(binding.ilink_token)
            self._credentials = Credentials(
                token=cred_data["token"],
                base_url=cred_data.get("base_url", ILINK_API_BASE),
                account_id=cred_data.get("account_id"),
                user_id=cred_data.get("user_id")
            )
            self._base_url = self._credentials.base_url
            logger.info("[WeChat] Loaded credentials from DB: token={}...".format(self._credentials.token[:20]))
            return True
        except Exception as e:
            logger.error("[WeChat] Error loading credentials: {}".format(e))
            return False
        finally:
            db.close()

    async def login(self, *, force: bool = False):
        """重写 login 方法：如果已有凭证则跳过扫码登录"""
        if self._credentials and not force:
            logger.info("[WeChat] Using existing credentials, skipping QR login")
            return self._credentials
        # 如果没有凭证或 force=True，调用父类 login（需要扫码）
        logger.info("[WeChat] No credentials in DB, calling parent login()")
        creds = await super().login(force=force)
        # 保存新凭证到数据库
        self._save_credentials_to_db(creds)
        return creds

    def _save_credentials_to_db(self, creds: Credentials) -> None:
        """保存凭证到 WeChatBinding.ilink_token"""
        db = SessionLocal()
        try:
            binding = db.query(WeChatBinding).filter(
                WeChatBinding.user_id == self.user_id
            ).first()
            if binding:
                cred_data = {
                    "token": creds.token,
                    "base_url": creds.base_url,
                    "account_id": creds.account_id,
                    "user_id": creds.user_id
                }
                binding.ilink_token = json.dumps(cred_data)
                db.commit()
                logger.info("[WeChat] Saved credentials to DB for user {}".format(self.user_id))
        except Exception as e:
            logger.error("[WeChat] Error saving credentials: {}".format(e))
        finally:
            db.close()
