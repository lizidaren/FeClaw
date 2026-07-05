"""
腾讯云对象存储服务 + CosStorage (FileStorage 实现)
"""

import logging
import asyncio
from config import settings
from qcloud_cos import CosConfig, CosS3Client
from typing import Tuple, Optional, List, Dict
import uuid

from services.file_storage import FileStorage

logger = logging.getLogger(__name__)


# ── V2: DB 劫持路由 ────────────────────────────────────────────
# 某些高频读写小文件直接走 DB（SQLite/MySQL），绕过 COS 以提升性能。
# Agent 无感——file_read/file_write 行为完全不变。
#
# 路由规则：key 以这些文件名结尾的请求，路由到 agent_config 表
# （key 命名为 agents/{hash}/system/{filename}）
_DB_HIJACK_FILES = {
    "todos.json",
    "coprocessor_config.json",
}


def _is_db_hijack_key(key: str) -> bool:
    """检查 key 是否为 DB 劫持路径"""
    if not key:
        return False
    key = key.rstrip("/")
    basename = key.rsplit("/", 1)[-1] if "/" in key else key
    return basename in _DB_HIJACK_FILES


def _db_hijack_read(key: str) -> Optional[bytes]:
    """从 agent_config 表读取特殊文件"""
    try:
        from models.database import AgentConfig, SessionLocal
        db = SessionLocal()
        try:
            row = db.query(AgentConfig).filter(AgentConfig.key == key).first()
            if row and row.value is not None:
                return row.value.encode("utf-8")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[StorageService] DB hijack read failed for {key}: {e}")
    return None


def _db_hijack_write(key: str, data: bytes) -> bool:
    """写入特殊文件到 agent_config 表（upsert）"""
    try:
        from datetime import datetime
        from models.database import AgentConfig, SessionLocal
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        # 解析 agent_hash from key（feclaw/agents/{hash}/system/todos.json）
        agent_hash = None
        parts = key.split("/")
        if "agents" in parts:
            idx = parts.index("agents")
            if idx + 1 < len(parts):
                agent_hash = parts[idx + 1]
        db = SessionLocal()
        try:
            row = db.query(AgentConfig).filter(AgentConfig.key == key).first()
            if row:
                row.value = text
                row.updated_at = datetime.utcnow()
            else:
                row = AgentConfig(
                    key=key,
                    value=text,
                    agent_hash=agent_hash,
                    permission="readwrite",
                    description="V2 system file (DB-hijacked)",
                    updated_at=datetime.utcnow(),
                )
                db.add(row)
            db.commit()
            return True
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[StorageService] DB hijack write failed for {key}: {e}")
    return False


class CosStorage(FileStorage):
    """COS 文件存储实现 — 继承 FileStorage，实现 5 个抽象方法"""

    def __init__(self):
        """初始化 COS 客户端"""
        if not all([settings.TENCENT_COS_SECRET_ID, settings.TENCENT_COS_SECRET_KEY, settings.TENCENT_COS_BUCKET]):
            raise ValueError("腾讯云COS配置不完整，请检查TENCENT_COS_SECRET_ID、TENCENT_COS_SECRET_KEY和TENCENT_COS_BUCKET")

        # 创建COS配置
        self.config = CosConfig(
            Region=settings.TENCENT_COS_REGION,
            SecretId=settings.TENCENT_COS_SECRET_ID,
            SecretKey=settings.TENCENT_COS_SECRET_KEY
        )

        # 创建COS客户端
        self.client = CosS3Client(self.config)

    # ==================== FileStorage 抽象方法实现 ====================

    def get_file_content(self, key: str) -> Optional[bytes]:
        """获取文件内容（V2：DB 劫持路由）"""
        # V2 DB 劫持：特定文件走 DB 而非 COS
        if _is_db_hijack_key(key):
            data = _db_hijack_read(key)
            if data is not None:
                logger.debug(f"[CosStorage] DB-hijack read: {key}, size={len(data)}")
                return data
            # 不存在 → 返回 None（与 COS 行为一致）
            return None
        try:
            response = self.client.get_object(
                Bucket=settings.TENCENT_COS_BUCKET,
                Key=key
            )
            # response['Body'] 是 StreamBody 对象
            chunks = []
            while True:
                chunk = response['Body'].read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            content = b''.join(chunks)
            logger.info(f"[CosStorage] 文件读取成功: {key}, 大小: {len(content)} bytes")
            return content
        except Exception as e:
            logger.error(f"[CosStorage] 读取文件失败: {key}, 错误: {e}")
            return None

    def put_object(self, key: str, file_bytes: bytes) -> None:
        """上传文件到 COS（V2：DB 劫持路由）"""
        # V2 DB 劫持：特定文件走 DB 而非 COS
        if _is_db_hijack_key(key):
            ok = _db_hijack_write(key, file_bytes)
            if ok:
                logger.debug(f"[CosStorage] DB-hijack write: {key}, size={len(file_bytes)}")
                return
            # 写失败：降级到 COS（fallback）
            logger.warning(f"[CosStorage] DB hijack write failed, falling back to COS for {key}")
        self.client.put_object(
            Bucket=settings.TENCENT_COS_BUCKET,
            Body=file_bytes,
            Key=key
        )
        logger.info(f"[CosStorage] 文件上传成功: {key}")

    def delete_file_by_key(self, key: str) -> bool:
        """通过 COS key 直接删除文件"""
        try:
            self.client.delete_object(
                Bucket=settings.TENCENT_COS_BUCKET,
                Key=key
            )
            logger.info(f"[CosStorage] 文件删除成功(key): {key}")
            return True
        except Exception as e:
            logger.error(f"[CosStorage] 删除文件失败(key): {key}, 错误: {e}")
            return False

    def list_objects(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        """列出指定前缀下的所有对象"""
        try:
            all_objects = []
            marker = ""
            while True:
                response = self.client.list_objects(
                    Bucket=settings.TENCENT_COS_BUCKET,
                    Prefix=prefix,
                    MaxKeys=min(max_keys, 1000),
                    Marker=marker
                )
                contents = response.get("Contents", [])
                all_objects.extend(contents)
                # 检查是否还有更多对象
                if response.get("IsTruncated") == "true":
                    marker = contents[-1]["Key"]
                else:
                    break
                if len(all_objects) >= max_keys:
                    break
            return all_objects
        except Exception as e:
            logger.error(f"[CosStorage] 列出对象失败: {prefix}, 错误: {e}")
            return None

    def file_exists(self, key: str) -> Optional[Dict]:
        """检查 COS 文件是否存在（head_object，不下载内容）"""
        try:
            result = self.client.head_object(
                Bucket=settings.TENCENT_COS_BUCKET,
                Key=key
            )
            return {
                "exists": True,
                "size": result.get("ContentLength", 0),
                "content_type": result.get("ContentType", ""),
            }
        except Exception:
            return None

    # ==================== COS 路径生成方法 ====================

    def generate_original_file_key(self, user_id: int, filename: str) -> str:
        """
        生成原图文件路径（用户上传的原图）

        格式: {prefix}user_{user_id}/original/{uuid}.{ext}
        示例: firstentrance/mistakes/user_1/original/550e8400-e29b-41d4-a716-446655440000.jpg
        """
        ext = filename.split('.')[-1] if '.' in filename else 'jpg'
        unique_name = f"{uuid.uuid4()}.{ext}"
        return f"{settings.TENCENT_COS_PREFIX}user_{user_id}/original/{unique_name}"

    def generate_cleaned_file_key(self, user_id: int, file_sha1: str) -> str:
        """
        生成去手写图文件路径

        格式: {prefix}user_{user_id}/cleaned/cleaned_{sha1}.jpg
        示例: firstentrance/mistakes/user_1/cleaned/cleaned_a1b2c3d4e5f6...jpg
        """
        return f"{settings.TENCENT_COS_PREFIX}user_{user_id}/cleaned/cleaned_{file_sha1}.jpg"

    def generate_file_key(self, user_id: int, filename: str) -> str:
        """
        生成存储文件路径（兼容旧版方法）

        建议使用 generate_original_file_key 替代
        """
        ext = filename.split('.')[-1] if '.' in filename else 'jpg'
        unique_name = f"{uuid.uuid4()}.{ext}"
        return f"{settings.TENCENT_COS_PREFIX}user_{user_id}/{unique_name}"

    def get_user_id_from_key(self, key: str) -> Optional[int]:
        """
        从文件key中提取用户ID

        Args:
            key: COS文件key

        Returns:
            用户ID，如果无法提取则返回 None
        """
        try:
            if "/user_" in key:
                parts = key.split("/user_")
                if len(parts) > 1:
                    user_part = parts[1].split("/")[0]
                    return int(user_part)
        except Exception as e:
            logger.debug(f"[CosStorage] Failed to parse user_id from key {key}: {e}")
        return None

    # ==================== 文件上传下载方法 ====================

    def generate_presigned_url(self, user_id: int, filename: str) -> Tuple[str, str]:
        """
        生成预签名上传URL

        Args:
            user_id: 用户ID
            filename: 文件名

        Returns:
            (url, presigned_url): 完整的访问URL和预签名上传URL
        """
        key = self.generate_original_file_key(user_id, filename)

        # 生成预签名上传URL（PUT方式，有效期1小时）
        presigned_url = self.client.get_presigned_url(
            Method='PUT',
            Bucket=settings.TENCENT_COS_BUCKET,
            Key=key,
            Expired=3600
        )

        # 生成访问URL（HTTPS）
        url = f"https://{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/{key}"

        return url, presigned_url

    def get_object_public_url(self, key: str) -> str:
        """Build the public COS URL for an object key."""
        return f"https://{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/{key}"

    def generate_presigned_get_url(self, url: str, expired: int = 86400) -> str:
        """
        生成预签名下载URL（用于显示图片）

        Args:
            url: 图片URL
            expired: 有效期（秒）

        Returns:
            预签名下载URL
        """
        try:
            # 从URL中提取key
            key = url.split(f'/{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/')[-1]

            # 生成预签名GET URL
            presigned_url = self.client.get_presigned_url(
                Method='GET',
                Bucket=settings.TENCENT_COS_BUCKET,
                Key=key,
                Expired=expired
            )

            return presigned_url
        except Exception as e:
            logger.error(f"[CosStorage] 生成预签名GET URL失败: {e}")
            return url  # 失败时返回原URL

    def upload_file(self, file_bytes: bytes, key: str, max_retries: int = 3) -> str:
        """
        直接上传文件（服务端上传）

        Args:
            file_bytes: 文件字节数据
            key: 存储路径
            max_retries: 最大重试次数

        Returns:
            访问URL
        """
        from qcloud_cos.cos_exception import CosClientError, CosServiceError
        import time

        retry_count = 0
        last_error = None

        while retry_count < max_retries:
            try:
                response = self.client.put_object(
                    Bucket=settings.TENCENT_COS_BUCKET,
                    Body=file_bytes,
                    Key=key
                )

                url = f"https://{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/{key}"
                logger.info(f"[CosStorage] 文件上传成功: {key}")
                return url

            except CosServiceError as e:
                last_error = e
                logger.warning(f"[CosStorage] COS服务错误 (尝试 {retry_count + 1}/{max_retries}): {e}")
            except CosClientError as e:
                last_error = e
                logger.warning(f"[CosStorage] COS客户端错误 (尝试 {retry_count + 1}/{max_retries}): {e}")
            except Exception as e:
                last_error = e
                logger.warning(f"[CosStorage] 上传异常 (尝试 {retry_count + 1}/{max_retries}): {e}")

            retry_count += 1
            if retry_count < max_retries:
                wait_time = 0.1
                logger.info(f"[CosStorage] {wait_time}秒后重试...")
                time.sleep(wait_time)

        raise Exception(f"COS上传失败，已重试{max_retries}次: {last_error}")

    def delete_file(self, url: str) -> bool:
        """
        删除文件

        Args:
            url: 文件URL

        Returns:
            是否删除成功
        """
        try:
            # 从URL中提取key
            key = url.split(f'/{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/')[-1]

            self.client.delete_object(
                Bucket=settings.TENCENT_COS_BUCKET,
                Key=key
            )

            logger.info(f"[CosStorage] 文件删除成功: {key}")
            return True
        except Exception as e:
            logger.error(f"[CosStorage] 删除文件失败: {e}")
            return False

    def ensure_public_file(self, rel_path: str, content: str) -> bool:
        """
        确保公共文件存在于 COS 中，如果不存在则创建。

        Args:
            rel_path: 相对于 /public/ 的路径，如 "feclaw/index.md"
            content: 文件内容（UTF-8 字符串）

        Returns:
            True: 文件已存在或创建成功
            False: 检查或创建失败
        """
        from qcloud_cos.cos_exception import CosClientError, CosServiceError

        public_key = f"{settings.TENCENT_COS_PREFIX}public/{rel_path.lstrip('/')}"

        # 检查文件是否已存在
        try:
            self.client.head_object(
                Bucket=settings.TENCENT_COS_BUCKET,
                Key=public_key
            )
            logger.info(f"[CosStorage] Public file already exists: {public_key}")
            return True
        except CosServiceError as e:
            if e.get_status_code() == 404:
                pass  # 文件不存在，需要创建
            else:
                logger.error(f"[CosStorage] Head object error: {e}")
                return False
        except CosClientError as e:
            logger.error(f"[CosStorage] Head object client error: {e}")
            return False
        except Exception as e:
            logger.error(f"[CosStorage] Head object unexpected error: {e}")
            return False

        # 文件不存在，创建它
        try:
            self.client.put_object(
                Bucket=settings.TENCENT_COS_BUCKET,
                Body=content.encode("utf-8"),
                Key=public_key,
                ContentType="text/markdown; charset=utf-8"
            )
            logger.info(f"[CosStorage] Public file created: {public_key}")
            return True
        except (CosServiceError, CosClientError) as e:
            logger.error(f"[CosStorage] Create public file failed: {e}")
            return False

    def generate_presigned_put_url(self, key: str, expired: int = 3600) -> str:
        """
        生成预签名上传 URL（PUT 方式）

        Args:
            key: COS 文件 key（存储路径）
            expired: 有效期（秒），默认 1 小时

        Returns:
            预签名上传 URL
        """
        try:
            presigned_url = self.client.get_presigned_url(
                Method='PUT',
                Bucket=settings.TENCENT_COS_BUCKET,
                Key=key,
                Expired=expired
            )
            return presigned_url
        except Exception as e:
            logger.error(f"[CosStorage] 生成预签名 PUT URL 失败: {e}")
            return ""

    def generate_sts_credential(self, user_id: str, prefix: str = None, duration: int = 3600) -> dict:
        """
        生成临时 STS 凭证（用于前端直传 COS）

        Args:
            user_id: 用户 ID
            prefix: 允许访问的路径前缀（可选，默认为用户工作区）
            duration: 有效期（秒），默认 1 小时

        Returns:
            包含临时凭证的字典
        """

        if prefix is None:
            prefix = f"feclaw/user_workspaces/{user_id}/"

        try:
            from sts.sts import Sts
            import json as _json

            custom_policy = {
                "version": "2.0",
                "statement": [
                    {
                        "action": [
                            "name/cos:GetObject", "name/cos:PutObject",
                            "name/cos:DeleteObject", "name/cos:HeadObject",
                            "name/cos:ListMultipartUploads", "name/cos:ListParts",
                            "name/cos:UploadPart", "name/cos:CompleteMultipartUpload",
                            "name/cos:AbortMultipartUpload",
                        ],
                        "effect": "allow",
                        "resource": ["*"],
                        "condition": {
                            "string_like": {"cos:Prefix": f"{prefix}*"}
                        }
                    },
                    {
                        "action": ["name/cos:GetBucket", "name/cos:ListBucket"],
                        "effect": "allow",
                        "resource": ["*"]
                    }
                ]
            }

            sts = Sts({
                "secret_id": settings.TENCENT_COS_SECRET_ID,
                "secret_key": settings.TENCENT_COS_SECRET_KEY,
                "region": settings.TENCENT_COS_REGION,
                "bucket": settings.TENCENT_COS_BUCKET,
                "duration_seconds": duration,
                "policy": custom_policy,
            })

            response = sts.get_credential()

            if response.get("credentials"):
                return {
                    "credentials": {
                        "secret_id": response["credentials"]["tmpSecretId"],
                        "secret_key": response["credentials"]["tmpSecretKey"],
                        "session_token": response["credentials"]["sessionToken"],
                        "expired_time": response["expiredTime"],
                        "expiration": response["expiration"]
                    },
                    "bucket": settings.TENCENT_COS_BUCKET,
                    "region": settings.TENCENT_COS_REGION,
                    "prefix": prefix,
                    "base_url": f"https://{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com"
                }
            else:
                logger.error(f"[CosStorage] STS 响应无效: {response}")
                return None

        except Exception as e:
            logger.error(f"[CosStorage] 生成 STS 凭证失败: {e}")
            import traceback as _tb
            logger.debug(_tb.format_exc())
            return None

    # ==================== 异步包装方法 ====================

    async def upload_file_async(self, file_bytes: bytes, key: str, max_retries: int = 3) -> str:
        """异步包装：上传文件"""
        return await asyncio.to_thread(self.upload_file, file_bytes, key, max_retries)

    async def put_object_async(self, key: str, file_bytes: bytes) -> None:
        """异步包装：上传文件（别名）"""
        return await asyncio.to_thread(self.put_object, key, file_bytes)

    async def get_file_content_async(self, key: str) -> Optional[bytes]:
        """异步包装：获取文件内容"""
        return await asyncio.to_thread(self.get_file_content, key)

    async def list_objects_async(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        """异步包装：列出对象"""
        return await asyncio.to_thread(self.list_objects, prefix, max_keys)

    async def delete_file_by_key_async(self, key: str) -> bool:
        """异步包装：按 key 删除文件"""
        return await asyncio.to_thread(self.delete_file_by_key, key)

    async def delete_file_async(self, url: str) -> bool:
        """异步包装：按 URL 删除文件"""
        return await asyncio.to_thread(self.delete_file, url)

    async def ensure_public_file_async(self, rel_path: str, content: str) -> bool:
        """异步包装：确保公共文件存在"""
        return await asyncio.to_thread(self.ensure_public_file, rel_path, content)

    async def generate_presigned_url_async(self, user_id: int, filename: str) -> Tuple[str, str]:
        """异步包装：生成预签名上传 URL"""
        return await asyncio.to_thread(self.generate_presigned_url, user_id, filename)


class StorageService(CosStorage):
    """兼容旧代码——直接继承 CosStorage，一切接口不变

    所有调用 ``StorageService(...)`` 的旧代码无需改动。
    新增代码应使用 ``create_file_storage()`` 或直接 ``CosStorage()``。
    """
    pass


# 全局存储服务实例
storage_service = None

def get_storage_service():
    """获取存储服务实例（懒加载）"""
    global storage_service
    if storage_service is None:
        storage_service = StorageService()
    return storage_service
