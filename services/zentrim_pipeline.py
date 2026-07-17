"""
Zentrim AI Pipeline — 异步处理入口

设计参考：docs/v1/02-zentrim.md §9 Pipeline
任务参考：claude-work/zentrim-pipeline.md

处理链路：
  拍照 → process_photo → VLM 形态判断 → VLM→HTML / 智能二值化标记 → 写计算层 blocks.text → 向量索引
  录音 → process_audio → (ASR 占位，暂不实现) → 写 blocks.text → 向量索引
  手写 → process_ink   → VLM 瓦片语义提取 → 写 blocks.text → 向量索引

后台处理：asyncio.create_task，不引入 Celery/Redis。
状态流：entry.status: active → processing → active（完成）/ active（失败，不阻塞用户）
"""

import asyncio
import base64
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.orm import Session

from config import settings
from models.database import SessionLocal
from models.zentrim import ZentrimBlock, ZentrimEntry
from services.model_registry import resolve as _model_resolve
from services.zentrim_service import ZentrimService, _generate_ulid

logger = logging.getLogger(__name__)

# ─── VLM 配置 ───
_vlm_info = _model_resolve(settings.MAIN_VISION_MODEL)
VLM_MODEL = settings.MAIN_VISION_MODEL
VLM_BASE_URL = f"{_vlm_info['base_url']}/chat/completions"
VLM_API_KEY: str = os.getenv(_vlm_info.get("api_key_attr", ""), "")

VLM_TIMEOUT = 60.0  # VLM 调用超时
VLM_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB 图片上限


# ─── 提示词 ───
PROMPT_PHOTO_HTML_PRINTED = (
    "你是一个 OCR + HTML 生成助手。根据用户提供的图片，提取图片中的文本内容，生成结构化的 HTML。要求：\n"
    "1. 保持原文的层级结构（标题、段落、列表等）\n"
    "2. 用 <h1>, <h2>, <p>, <ul>, <li>, <strong> 等语义化标签\n"
    "3. 如果是试卷/习题，保留题目编号和选项\n"
    "4. 如果是笔记，保留结构和重点标记\n"
    "5. 不要添加原文没有的内容\n"
    "6. 只输出纯 HTML，不要 markdown 包裹"
)

PROMPT_PHOTO_HTML_MIXED = (
    "你是一个 OCR + HTML 生成助手。图片包含印刷体文字和手写批注。\n"
    "1. 提取印刷体文字 → 结构化 HTML\n"
    "2. 在 HTML 底部添加 <div class=\"handwriting-notes\"> 手写内容描述 </div>\n"
    "3. 手写内容用自然语言描述（\"在氧化还原反应标题旁补充了'电子转移'\"）"
)

PROMPT_PHOTO_CLASSIFY = (
    "判断这张图片的内容形态，只回复一个词：\n"
    "- printed（纯印刷体文字）\n"
    "- handwritten（纯手写）\n"
    "- mixed（印刷体+手写混合）"
)

PROMPT_INK_SEMANTIC = (
    "你是一个手写画布分析助手。根据提供的手写图片瓦片：\n"
    "1. 提取所有可辨识的文字\n"
    "2. 描述图片中的非文字元素（图表、箭头、标注等）\n"
    "3. 总结整张瓦片的核心内容"
)


# ─── 运行中任务跟踪 ───
_running_tasks: Dict[str, asyncio.Task] = {}  # key = f"{entry_id}:{block_id}"


def _task_key(entry_id: str, block_id: str) -> str:
    return f"{entry_id}:{block_id}"


# fix(P0-3): cos_key 路径白名单 — 防止 COS 路径穿越 / 跨用户读取
# 合法路径：feclaw/zentrim/user_{uid}/attachments/{entry_id}_{file_type}.{ext}
#          feclaw/zentrim/user_{uid}/blocks/{block_id}_*.{ext}
_COS_KEY_PATTERN_TEMPLATE = r"^feclaw/zentrim/user_{uid}/[A-Za-z0-9_\-/]+\.[a-z0-9]{1,5}$"


def _is_valid_cos_key(cos_key: str, user_id: Optional[int]) -> bool:
    """校验 cos_key 是否在白名单内且归属当前用户。

    若 user_id 为 None（无法校验归属），则只做格式 + 禁字符校验，
    但记 warning（因为 pipeline 默认 user_id 来自信任域，None 是异常情况）。
    """
    if not cos_key or not isinstance(cos_key, str):
        return False
    # 禁字符防御
    if ".." in cos_key or "//" in cos_key or "\x00" in cos_key:
        return False
    if user_id is None:
        # user_id 未知 — 放宽到只校验路径前缀
        pattern = r"^feclaw/zentrim/user_\d+/[A-Za-z0-9_\-/]+\.[a-z0-9]{1,5}$"
        if not re.match(pattern, cos_key):
            return False
        logger.warning(
            f"[Pipeline] cos_key validated without user_id check (user_id=None): {cos_key!r}"
        )
        return True
    pattern = _COS_KEY_PATTERN_TEMPLATE.format(uid=user_id)
    return bool(re.match(pattern, cos_key))


# ════════════════════════════════════════
# ZentrimPipeline
# ════════════════════════════════════════
class ZentrimPipeline:
    """Zentrim AI 管线 — 异步处理入口

    所有方法返回 asyncio.Task，后台执行，不阻塞调用方。
    失败时 entry 会被恢复为 active，block.text 写入错误信息。
    """

    def __init__(self, db: Optional[Session] = None):
        self._db = db

    # ─── 公开接口 ───

    def process_photo(self, entry_id: str, block_id: str, cos_key: str, user_id: int) -> asyncio.Task:
        """拍照入库管线（Step 1a）

        1. 状态 → processing
        2. 下载图片
        3. VLM 判断形态（印刷体/手写/混合）
        4. 印刷体 → VLM→HTML
           手写 → 标记，暂存描述
           混合 → VLM→HTML + 手写描述
        5. 写 blocks.text（计算层）+ blocks.data.html
        6. 向量索引
        7. 状态 → active
        """
        task = asyncio.create_task(
            self._run_photo_pipeline(entry_id, block_id, cos_key, user_id)
        )
        _running_tasks[_task_key(entry_id, block_id)] = task
        task.add_done_callback(lambda t: _running_tasks.pop(_task_key(entry_id, block_id), None))
        return task

    def process_audio(self, entry_id: str, block_id: str, cos_key: str, user_id: int) -> asyncio.Task:
        """录音管线（Step 1b）— ASR 占位

        当前版本仅标记 processing → 直接恢复 active，text 写占位。
        ASR 实际调用待接入。
        """
        task = asyncio.create_task(
            self._run_audio_pipeline(entry_id, block_id, cos_key, user_id)
        )
        _running_tasks[_task_key(entry_id, block_id)] = task
        task.add_done_callback(lambda t: _running_tasks.pop(_task_key(entry_id, block_id), None))
        return task

    def process_ink(self, entry_id: str, block_id: str, cos_key: str, user_id: int) -> asyncio.Task:
        """手写画布管线（Step 1d）

        1. 状态 → processing
        2. 下载画布缩略图/瓦片
        3. VLM 语义提取（文字+图表描述+总结）
        4. 写 blocks.text
        5. 向量索引
        6. 状态 → active
        """
        task = asyncio.create_task(
            self._run_ink_pipeline(entry_id, block_id, cos_key, user_id)
        )
        _running_tasks[_task_key(entry_id, block_id)] = task
        task.add_done_callback(lambda t: _running_tasks.pop(_task_key(entry_id, block_id), None))
        return task

    def get_status(self, entry_id: str, block_id: str) -> str:
        """获取管线处理状态"""
        key = _task_key(entry_id, block_id)
        task = _running_tasks.get(key)
        if task is None:
            return "idle"
        if task.done():
            return "done"
        return "processing"

    # ─── 内部工具 ───

    def _get_db(self) -> Session:
        """获取 DB Session（优先用注入的，否则新建）"""
        if self._db is not None:
            return self._db
        return SessionLocal()

    def _set_processing(self, entry_id: str, block_id: str, user_id: int) -> None:
        """标记 entry 为 processing，block.text 为占位"""
        db = self._get_db()
        own_session = self._db is None
        try:
            svc = ZentrimService(db)
            # fix(P0-5): 使用计数器而非直接改 entry.status
            self._increment_processing_count(db, entry_id, user_id)
            # 更新 block.text 为处理中占位
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                block.text = "[处理中...]"
                db.commit()
        except Exception as e:
            logger.error(f"[Pipeline] set_processing failed: entry={entry_id} block={block_id} err={e}")
        finally:
            if own_session:
                db.close()

    async def _set_completed(
        self,
        entry_id: str,
        block_id: str,
        user_id: int,
        text: str,
        html: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> None:
        """完成处理：写 blocks.text + data.html + 向量索引 + 状态恢复

        fix(P0-4): 改为 async — pipeline 本身是 async 函数，所有调用都用 await。
        DB session 在 finally 中 close，确保向量写回后再关闭。
        """
        db = self._get_db()
        own_session = self._db is None
        try:
            # 更新 block（text + html + model_name）
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                block.text = text
                block.model_name = model_name or VLM_MODEL
                if html:
                    data = block.data if isinstance(block.data, dict) else {}
                    data["html"] = html
                    block.data = data
                db.commit()

            # fix(P0-4): 向量索引 — 用 await 直接等待，vector_id 成功后才写回 DB
            vector_id = await self._index_block(block_id, text, entry_id, user_id)

            # 如果 _index_block 返回了 vector_id，单独写到 DB（新 session 防止已关闭）
            if vector_id:
                try:
                    db2 = self._get_db() if not own_session else SessionLocal()
                    try:
                        blk = db2.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
                        if blk:
                            blk.vector_id = vector_id
                            db2.commit()
                    finally:
                        if own_session:
                            db2.close()
                except Exception as e:
                    logger.warning(f"[Pipeline] vector_id write-back failed: block={block_id} err={e}")

            # 恢复 entry 状态（fix(P0-5): 使用计数器）
            self._decrement_processing_count(db, entry_id, user_id)
        except Exception as e:
            logger.error(f"[Pipeline] set_completed failed: entry={entry_id} block={block_id} err={e}")
            # 恢复 entry 状态即使失败
            try:
                self._decrement_processing_count(db, entry_id, user_id)
            except Exception:
                pass
        finally:
            if own_session:
                db.close()

    async def _set_failed(self, entry_id: str, block_id: str, user_id: int, error: str) -> None:
        """失败处理：恢复 entry 状态，block.text 写错误

        fix(P0-4): 改为 async，调用方用 await。
        """
        db = self._get_db()
        own_session = self._db is None
        try:
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                block.text = f"[处理失败: {error[:200]}]"
                db.commit()

            # fix(P0-5): 使用计数器递减
            self._decrement_processing_count(db, entry_id, user_id)
        except Exception as e:
            logger.error(f"[Pipeline] set_failed failed: entry={entry_id} block={block_id} err={e}")
        finally:
            if own_session:
                db.close()

    # ─── fix(P0-5): entry 级别 processing_count 计数器 ───

    def _increment_processing_count(self, db: Session, entry_id: str, user_id: int) -> None:
        """递增 entry.metadata_.pipeline_active_count；若从 0→1，标记 entry.status='processing'"""
        entry = db.query(ZentrimEntry).filter(
            ZentrimEntry.id == entry_id,
            ZentrimEntry.user_id == user_id,
        ).first()
        if not entry:
            return
        meta = entry.metadata_ if isinstance(entry.metadata_, dict) else {}
        count = int(meta.get("pipeline_active_count", 0) or 0) + 1
        meta["pipeline_active_count"] = count
        entry.metadata_ = meta
        if count == 1:
            entry.status = "processing"
        entry.updated_at = datetime.now(timezone.utc)
        db.commit()

    def _decrement_processing_count(self, db: Session, entry_id: str, user_id: int) -> None:
        """递减 entry.metadata_.pipeline_active_count；若降到 0，恢复 entry.status='active'"""
        entry = db.query(ZentrimEntry).filter(
            ZentrimEntry.id == entry_id,
            ZentrimEntry.user_id == user_id,
        ).first()
        if not entry:
            return
        meta = entry.metadata_ if isinstance(entry.metadata_, dict) else {}
        count = int(meta.get("pipeline_active_count", 0) or 0) - 1
        if count < 0:
            count = 0
        meta["pipeline_active_count"] = count
        entry.metadata_ = meta
        # 只有计数归零（最后一个 block 完成）才恢复 active
        if count == 0:
            entry.status = "active"
        entry.updated_at = datetime.now(timezone.utc)
        db.commit()

    async def _index_block(self, block_id: str, text: str, entry_id: str, user_id: int) -> Optional[str]:
        """将 block 文本写入向量索引 idx-zentrim-{user_id}

        fix(P0-4): 改为 async — 直接 await vs.index_text(...)，不再用
        asyncio.ensure_future / run_until_complete / asyncio.run 三层兜底。

        Returns:
            vector_id on success, None on failure.
        """
        if not text or not text.strip():
            return None
        try:
            from services.vector_search_service import VectorSearchService

            vs = VectorSearchService(agent_hash=None)
            index_name = f"idx-zentrim-{user_id}"
            vector_id = f"zentrim:{block_id}"

            # fix(P0-4): 直接 await，不再用 fire-and-forget
            await vs.index_text(
                key=vector_id,
                text=text,
                index=index_name,
                metadata={"entry_id": entry_id, "block_id": block_id, "user_id": user_id},
            )

            logger.info(f"[Pipeline] indexed block={block_id} to {index_name}")
            return vector_id  # 返回给调用方写回 DB
        except Exception as e:
            logger.warning(f"[Pipeline] vector index failed (non-fatal): block={block_id} err={e}")
            return None

    def _download_file(self, cos_key: str, user_id: Optional[int] = None) -> Optional[bytes]:
        """从存储后端下载文件

        fix(P0-3): 加 cos_key 路径白名单校验（defense-in-depth）。
        Router 层已做 `_validate_cos_key` 校验，但 pipeline 也可能被
        `zentrim_service.save_blocks` auto-trigger 直接调用（不经 router），
        所以这里二次校验。
        """
        # fix(P0-3): 白名单校验 — 必须以 feclaw/zentrim/user_{uid}/ 开头
        if not cos_key or not _is_valid_cos_key(cos_key, user_id):
            logger.error(
                f"[Pipeline] cos_key rejected by whitelist: key={cos_key!r} user_id={user_id}"
            )
            return None
        try:
            from services.file_storage import create_file_storage
            storage = create_file_storage(mode=settings.STORAGE_MODE)
            return storage.get_file_content(cos_key)
        except Exception as e:
            logger.error(f"[Pipeline] download failed: key={cos_key} err={e}")
            return None

    async def _call_vlm(self, image_b64: str, mime: str, prompt: str, max_tokens: int = 2048) -> Optional[str]:
        """调用 VLM 模型，返回文本结果

        参考 services/image_describer.py 的调用模式。
        """
        content = [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            {"type": "text", "text": prompt},
        ]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(VLM_TIMEOUT)) as client:
                response = await client.post(
                    VLM_BASE_URL,
                    headers={
                        "Authorization": f"Bearer {VLM_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": VLM_MODEL,
                        "messages": [{"role": "user", "content": content}],
                        "stream": False,
                        "thinking": {"type": "disabled"},
                        "max_tokens": max_tokens,
                    },
                )
            if response.status_code != 200:
                logger.warning(
                    f"[Pipeline] VLM API error: HTTP {response.status_code}, "
                    f"body={response.text[:200]}"
                )
                return None
            result = response.json()
            text = (
                result.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            return text or None
        except httpx.TimeoutException:
            logger.warning(f"[Pipeline] VLM timeout after {VLM_TIMEOUT}s")
            return None
        except Exception as e:
            logger.error(f"[Pipeline] VLM call failed: {e}", exc_info=True)
            return None

    @staticmethod
    def _detect_image_format(data: bytes) -> str:
        """通过文件魔数检测图片格式"""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if data[:2] in (b"\xff\xd8",):
            return "jpeg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        return "png"

    @staticmethod
    def _encode_image(image_data: bytes) -> tuple:
        """base64 编码图片，返回 (b64_str, mime)"""
        ext = ZentrimPipeline._detect_image_format(image_data)
        mime = f"image/{ext}"
        b64 = base64.b64encode(image_data).decode("utf-8")
        return b64, mime

    # ─── 管线实现 ───

    async def _run_photo_pipeline(self, entry_id: str, block_id: str, cos_key: str, user_id: int) -> None:
        """拍照管线主流程"""
        try:
            # Step 1: 标记 processing
            self._set_processing(entry_id, block_id, user_id)

            # Step 2: 下载图片（fix(P0-3): 传 user_id 做 cos_key 白名单校验）
            image_data = self._download_file(cos_key, user_id=user_id)
            if not image_data:
                await self._set_failed(entry_id, block_id, user_id, "文件下载失败")
                return

            if len(image_data) > VLM_MAX_IMAGE_BYTES:
                await self._set_failed(entry_id, block_id, user_id, f"图片过大 ({len(image_data)} bytes)")
                return

            b64, mime = self._encode_image(image_data)

            # Step 3: VLM 形态判断
            classification = await self._call_vlm(b64, mime, PROMPT_PHOTO_CLASSIFY, max_tokens=32)
            classification = (classification or "").strip().lower()

            # Step 4: 根据形态选择处理路径
            if classification == "handwritten":
                # 纯手写 → VLM 语义描述（替代二值化，直接提取文字+描述）
                semantic = await self._call_vlm(b64, mime, PROMPT_INK_SEMANTIC, max_tokens=1024)
                text = semantic or "[手写内容，VLM 描述失败]"
                await self._set_completed(entry_id, block_id, user_id, text=text, model_name=VLM_MODEL)

            elif classification == "mixed":
                # 混合 → VLM→HTML + 手写描述
                html = await self._call_vlm(b64, mime, PROMPT_PHOTO_HTML_MIXED, max_tokens=4096)
                if not html:
                    await self._set_failed(entry_id, block_id, user_id, "VLM HTML 生成失败")
                    return
                # 从 HTML 中提取纯文本供搜索
                text = self._strip_html(html)
                await self._set_completed(
                    entry_id, block_id, user_id,
                    text=text, html=html, model_name=VLM_MODEL,
                )

            else:
                # printed 或未识别 → VLM→HTML
                html = await self._call_vlm(b64, mime, PROMPT_PHOTO_HTML_PRINTED, max_tokens=4096)
                if not html:
                    await self._set_failed(entry_id, block_id, user_id, "VLM HTML 生成失败")
                    return
                text = self._strip_html(html)
                await self._set_completed(
                    entry_id, block_id, user_id,
                    text=text, html=html, model_name=VLM_MODEL,
                )

        except Exception as e:
            logger.exception(f"[Pipeline] photo pipeline error: entry={entry_id} block={block_id}")
            await self._set_failed(entry_id, block_id, user_id, str(e))

    async def _run_audio_pipeline(self, entry_id: str, block_id: str, cos_key: str, user_id: int) -> None:
        """录音管线 — ASR 占位

        当前版本仅标记处理中 → 恢复 active。
        ASR 实际调用待接入（需接入DashScope paraformer 或类似服务）。
        """
        try:
            self._set_processing(entry_id, block_id, user_id)

            # ASR 占位：当前不实现实际转录
            # TODO: 接入 ASR 服务（DashScope paraformer-v2 等）
            # fix(P0-3): 传 user_id 做 cos_key 白名单校验
            audio_data = self._download_file(cos_key, user_id=user_id)
            if not audio_data:
                await self._set_failed(entry_id, block_id, user_id, "音频下载失败")
                return

            # 写占位文本
            text = "[ASR 转写待接入]"
            await self._set_completed(entry_id, block_id, user_id, text=text, model_name="asr-placeholder")

        except Exception as e:
            logger.exception(f"[Pipeline] audio pipeline error: entry={entry_id} block={block_id}")
            await self._set_failed(entry_id, block_id, user_id, str(e))

    async def _run_ink_pipeline(self, entry_id: str, block_id: str, cos_key: str, user_id: int) -> None:
        """手写画布管线

        画布保存为图片（缩略图或瓦片），VLM 提取语义。
        当前实现：直接对整张图片做 VLM 语义提取。
        未来扩展：切 1024x1024 瓦片，并行 VLM，LLM 聚合。
        """
        try:
            self._set_processing(entry_id, block_id, user_id)

            # fix(P0-3): 传 user_id 做 cos_key 白名单校验
            image_data = self._download_file(cos_key, user_id=user_id)
            if not image_data:
                await self._set_failed(entry_id, block_id, user_id, "画布图片下载失败")
                return

            if len(image_data) > VLM_MAX_IMAGE_BYTES:
                await self._set_failed(entry_id, block_id, user_id, f"图片过大 ({len(image_data)} bytes)")
                return

            b64, mime = self._encode_image(image_data)

            # VLM 语义提取
            semantic = await self._call_vlm(b64, mime, PROMPT_INK_SEMANTIC, max_tokens=2048)
            text = semantic or "[手写内容，VLM 描述失败]"

            await self._set_completed(entry_id, block_id, user_id, text=text, model_name=VLM_MODEL)

        except Exception as e:
            logger.exception(f"[Pipeline] ink pipeline error: entry={entry_id} block={block_id}")
            await self._set_failed(entry_id, block_id, user_id, str(e))

    @staticmethod
    def _strip_html(html: str) -> str:
        """简单 HTML → 纯文本提取（供搜索用 blocks.text）"""
        import re
        # 移除 script/style
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # 移除标签
        text = re.sub(r"<[^>]+>", " ", html)
        # 合并空白
        text = re.sub(r"\s+", " ", text).strip()
        return text


# ─── 全局实例 ───
pipeline = ZentrimPipeline()
