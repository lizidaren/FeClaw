"""
Zentrim AI Pipeline — 异步处理入口

设计参考：docs/v1/02-zentrim.md §9 Pipeline
任务参考：claude-work/zentrim-pipeline.md + /tmp/next-step-plan-0719.md

处理链路：
  拍照 → process_photo → Speed 形态判断 → (随手拍:保留原图 / 文档:二值化+可选HTML) → Heavy VLM 描述 → 向量索引
  录音 → process_audio → (ASR 占位，暂不实现) → 写 blocks.text → 向量索引
  手写 → process_ink   → VLM 瓦片语义提取 → 写 blocks.text → 向量索引

后台处理：asyncio.create_task，不引入 Celery/Redis。
状态流：entry.status: active → processing → active（完成/失败都通过计数器恢复）

────────────────────────────────────────────────────────────────────
Block.data.processed Schema（photo 类型，U 阶段前端参照）
────────────────────────────────────────────────────────────────────
{
  "original": {
    "key": "feclaw/zentrim/user_{uid}/attachments/{block_id}_original.jpg",
    "url": "https://...",
    "mime": "image/jpeg",
    "size": 2456789
  },
  "processed": {
    "status": "processing" | "rendered" | "failed" | "active" | "archived",
    "current_stage": null | "classify" | "color_id" | "binarize" | "html" | "screenshot" | "describe",
    "error": null | "[ec] human msg",
    "failed_stage": null | "classify" | "color_id" | ...,
    "binarized": {
      "channels": [
        {"color": "black", "threshold": 128, "iterations": 3,
         "key": ".../{block_id}_black.webp", "url": "..."},
        ...
      ],
      "fused": {"key": ".../{block_id}_fused.webp", "url": "..."}
    },
    "html": {
      "source_key": ".../{block_id}.html",
      "source_url": "...",
      "screenshot_key": ".../{block_id}_screenshot.webp",
      "screenshot_url": "..."
    },
    "display_image": {                     # ← 前端渲染时只读这一个字段
      "kind": "fused" | "screenshot" | "original",
      "key":  "<COS key>",
      "url":  "<presigned URL>"
    }
  },
  "vlm_description": "这张图是物理试卷...",
  "text": "...",
  "model_name": "doubao-seed-2.0-lite",
  "vector_id": "vec_abc123..."
}

display_image 字段语义：
  - kind = "screenshot" HTML 路径，显示 html.screenshot（Playwright 渲染截屏）
  - kind = "fused"      二值化路径，显示 binarized.fused（多色融合净图）
  - kind = "original"   随手拍 / 失败 / processing 时，显示 original 原图
原则：后端在写 processed 时就决定好显示哪张，前端不做选择。
"""

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from config import settings
from models.database import SessionLocal
from models.zentrim import ZentrimBlock, ZentrimEntry
from services.model_registry import (
    VisionModelConfig,
    get_vision_heavy,
    get_vision_speed,
    resolve as _model_resolve,
)
from services.zentrim_service import ZentrimService, _generate_ulid

logger = logging.getLogger(__name__)

# ─── cv2 / numpy 懒加载（二值化用；headless 服务器无 GUI） ───
try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore
    np = None   # type: ignore
    _CV2_AVAILABLE = False
    logger.warning("[zentrim_pipeline] cv2/numpy not available; binarization disabled")


# ─── VLM 全局配置（向后兼容；实例用 self.vision_speed/heavy） ───
_vlm_info = _model_resolve(settings.MAIN_VISION_MODEL)
VLM_MODEL = settings.MAIN_VISION_MODEL
VLM_BASE_URL = f"{_vlm_info['base_url']}/chat/completions"
VLM_API_KEY: str = os.getenv(_vlm_info.get("api_key_attr", ""), "")

VLM_TIMEOUT = 60.0
VLM_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB hard limit


# ════════════════════════════════════════
# 状态机 / 失败阶段 / 错误码常量（T 阶段定义，V/U 阶段用）
# ════════════════════════════════════════

class PipelineStatus:
    """photo block 的处理状态。"""
    PROCESSING = "processing"
    RENDERED = "rendered"
    FAILED = "failed"
    ACTIVE = "active"
    ARCHIVED = "archived"


class FailedStage:
    """失败发生在管线的哪个阶段。describe 是软失败，不入 failed 状态。"""
    CLASSIFY = "classify"
    COLOR_ID = "color_id"
    BINARIZE = "binarize"
    HTML = "html"
    SCREENSHOT = "screenshot"
    DESCRIBE = "describe"      # 软失败
    COS_UPLOAD = "cos_upload"


class ErrorCode:
    VLM_TIMEOUT = "vlm_timeout"
    VLM_INVALID_RESPONSE = "vlm_invalid_response"
    PLAYWRIGHT_RENDER_FAILED = "playwright_render_failed"
    COS_UPLOAD_FAILED = "cos_upload_failed"
    ITERATIONS_EXCEEDED = "iterations_exceeded"
    CV2_UNAVAILABLE = "cv2_unavailable"


_DISPLAY_IMAGE_KINDS = ("fused", "screenshot", "original")


# ─── 二值化数据结构 ───
@dataclass
class ColorChannel:
    """主色识别结果 — 一个颜色通道。"""
    color: Literal["black", "red", "blue", "green", "other"]
    hue_low: float = 0.0
    hue_high: float = 0.0
    description: str = ""


@dataclass
class BinarizedChannel:
    """单通道二值化结果。"""
    color: str
    threshold: int
    iterations: int
    image_bytes_webp: bytes


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

PROMPT_INK_OCR = (
    "你是一个手写 OCR 助手。请仔细提取这张画布图片中所有可辨识的文字内容。\n"
    "要求：\n"
    "1. 逐行提取文字，保留换行和段落结构\n"
    "2. 对于图表、公式、箭头等非文字元素，用 [图表]、[公式]、[箭头标注] 等标记其位置\n"
    "3. 如果图片中有多种颜色，标注颜色变化（如 [红色]重点内容[/红色]）\n"
    "4. 不要添加任何解释或总结，只输出提取的内容\n"
    "5. 如果某区域完全无法辨认，标注 [无法辨识]"
)

# 单块最大边长（像素），超过则分块
INK_TILE_MAX_SIZE = 2048
# 分块重叠比例
INK_TILE_OVERLAP = 0.1

# ─── V 阶段新增提示词 ───
PROMPT_DOCUMENT_CLASSIFY = (
    "你是图像分类器。判断这张图片是「文档类内容」还是「随手拍」。\n"
    "文档类包括：笔记、课本、试卷、白板、打印文档、作业、发票、表格、幻灯片照片等需要处理的图文内容。\n"
    "随手拍包括：风景、人物、食物、宠物、建筑、自拍、街景等生活照片。\n"
    "只回答一个词：DOCUMENT 或 CASUAL"
)

PROMPT_DETECT_COLORS = (
    "这张手写/印刷文档图片里，作者用了哪几种颜色的笔书写或印刷？\n"
    "通常包括：黑色（主色）、红色（批改/重点）、蓝色（批注）、绿色（标注）等。\n"
    "请判断：\n"
    "1. 这张图是否规整到足以转成 HTML（印刷清晰、排版整齐，评分 0-1，0.8+ 规整，0.5 以下是自由手写/拍照角度歪斜）\n"
    "2. 列出 1-5 种用到的颜色\n"
    "只输出 JSON，格式：\n"
    "{\"regularity_score\": 0.0-1.0, \"colors\": [{\"color\": \"black|red|blue|green|other\", \"description\": \"...\"}]}"
)

PROMPT_BINARIZE_EVAL = (
    "你在评估一个二值化阈值。左边是原始图片（{color}色墨迹），右边是当前阈值 ({threshold}) 二值化后的黑白图。\n"
    "判断：当前阈值是否合适？\n"
    "- 如果二值化图里笔画断裂、大量笔画缺失 → 阈值应该调高 (higher)\n"
    "- 如果二值化图里背景噪点很多、纸张阴影被误判成笔迹 → 阈值应该调低 (lower)\n"
    "- 如果笔画清晰、背景干净 → ok\n"
    "只输出 JSON：{\"adjust\": \"higher|lower|ok\", \"amount\": 整数 5-30}"
)

PROMPT_GENERATE_HTML = (
    "你是一个高级 OCR + HTML 生成助手。用户上传了一张规整的文档照片。\n"
    "请生成像素级对齐的 HTML：\n"
    "1. 还原原文布局（标题/段落/列表/公式/表格）\n"
    "2. 用 Tailwind 风格的 inline CSS (style=\"...\")，设置合理字体大小和行距\n"
    "3. body 背景白色，文字黑色，最大宽度 1000px，居中\n"
    "4. 如果是试卷保留题号和选项；笔记保留颜色层级（红色批注用 style=\"color:red\"）\n"
    "5. 只输出完整的 <!DOCTYPE html>... 文档，不要 markdown 包裹，不要解释"
)

PROMPT_DESCRIBE_IMAGE = (
    "用 1-3 句话描述这张图片的内容，包括标题（若有）、主题、关键概念/公式/人物/物品。\n"
    "要求简洁、利于搜索检索，不要冗长。直接输出描述文本。"
)


# ─── 预定义颜色 HSV 色相范围（OpenCV H: 0-179, S/V: 0-255） ───
# 参考：cvtColor BGR→HSV 后 H 范围 0-179
_COLOR_HUE_RANGES = {
    "red":    [(0, 10), (170, 179)],   # 红色跨 0°
    "blue":   [(100, 130)],
    "green":  [(40, 80)],
    # "black"/"other" 不以色相判定，走 V 通道
}

# 颜色 → 显示色（fused 时映射回 RGB）
_COLOR_TO_RGB = {
    "black": (0, 0, 0),
    "red":   (220, 30, 30),
    "blue":  (30, 80, 220),
    "green": (30, 160, 60),
    "other": (120, 60, 180),
}


# ─── 运行中任务跟踪 ───
_running_tasks: Dict[str, asyncio.Task] = {}


def _task_key(entry_id: str, block_id: str) -> str:
    return f"{entry_id}:{block_id}"


# COS key 白名单
_COS_KEY_PATTERN_TEMPLATE = r"^feclaw/zentrim/user_{uid}/[A-Za-z0-9_\-/]+\.[a-z0-9]{1,5}$"


def _is_valid_cos_key(cos_key: str, user_id: Optional[int]) -> bool:
    if not cos_key or not isinstance(cos_key, str):
        return False
    if ".." in cos_key or "//" in cos_key or "\x00" in cos_key:
        return False
    if user_id is None:
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
    """Zentrim AI 管线 — 异步处理入口"""

    def __init__(
        self,
        db: Optional[Session] = None,
        vision_speed: Optional[VisionModelConfig] = None,
        vision_heavy: Optional[VisionModelConfig] = None,
    ):
        self._db = db
        try:
            self.vision_speed = vision_speed or get_vision_speed()
        except Exception as e:
            logger.warning(f"[zentrim_pipeline] vision_speed load failed, using fallback: {e}")
            self.vision_speed = VisionModelConfig(
                name="qwen3.6-flash", display_name="Qwen3.6 Flash",
                cost_per_call=0.001, max_image_size_mb=20, max_tokens=2048, timeout_s=15,
            )
        try:
            self.vision_heavy = vision_heavy or get_vision_heavy()
        except Exception as e:
            logger.warning(f"[zentrim_pipeline] vision_heavy load failed, using fallback: {e}")
            self.vision_heavy = VisionModelConfig(
                name="doubao-seed-2.0-lite", display_name="豆包 Seed 2.0 Lite",
                cost_per_call=0.01, max_image_size_mb=20, max_tokens=4096, timeout_s=60,
            )
        # L0 OCR 模型（专用文字提取，非通用 VLM）
        self.ocr_model = "qwen3.5-ocr"
        self.ocr_max_tokens = 4096
        self.ocr_timeout_s = 15
        self.max_image_bytes = self.vision_heavy.max_image_size_mb * 1024 * 1024
        # Playwright 浏览器单例（lazy init，避免每次请求 launch）
        self._pw_browser = None
        self._pw_lock = asyncio.Lock()

    # ─── 公开接口 ───

    def process_photo(self, entry_id: str, block_id: str, cos_key: str, user_id: int) -> asyncio.Task:
        task = asyncio.create_task(
            self._run_photo_pipeline(entry_id, block_id, cos_key, user_id)
        )
        _running_tasks[_task_key(entry_id, block_id)] = task
        task.add_done_callback(lambda t: _running_tasks.pop(_task_key(entry_id, block_id), None))
        return task

    def process_audio(self, entry_id: str, block_id: str, cos_key: str, user_id: int) -> asyncio.Task:
        task = asyncio.create_task(
            self._run_audio_pipeline(entry_id, block_id, cos_key, user_id)
        )
        _running_tasks[_task_key(entry_id, block_id)] = task
        task.add_done_callback(lambda t: _running_tasks.pop(_task_key(entry_id, block_id), None))
        return task

    def process_ink(
        self, entry_id: str, block_id: str, cos_key: str, user_id: int,
        tier: str = "ocr",
    ) -> asyncio.Task:
        """触发 ink 管线。

        tier:
        - "ocr" (L0): qwen3.5-ocr 专用模型，单图无分块，用于每次保存后更新
        - "vlm" (L1): Heavy VLM + 分块 + 结构化，用于首次创建 / 定时 / 手动触发
        """
        task = asyncio.create_task(
            self._run_ink_pipeline(entry_id, block_id, cos_key, user_id, tier=tier)
        )
        _running_tasks[_task_key(entry_id, block_id)] = task
        task.add_done_callback(lambda t: _running_tasks.pop(_task_key(entry_id, block_id), None))
        return task

    def get_status(self, entry_id: str, block_id: str) -> str:
        key = _task_key(entry_id, block_id)
        task = _running_tasks.get(key)
        if task is None:
            return "idle"
        if task.done():
            return "done"
        return "processing"

    # ─── 内部工具（DB / 状态） ───

    def _get_db(self) -> Session:
        if self._db is not None:
            return self._db
        return SessionLocal()

    def _set_processing(self, entry_id: str, block_id: str, user_id: int) -> None:
        """标记 entry processing + block.text 占位 + block.data.processed.status=processing"""
        db = self._get_db()
        own_session = self._db is None
        try:
            svc = ZentrimService(db)
            self._increment_processing_count(db, entry_id, user_id)
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                block.text = "[处理中...]"
                data = block.data if isinstance(block.data, dict) else {}
                processed = data.get("processed") if isinstance(data.get("processed"), dict) else {}
                processed["status"] = PipelineStatus.PROCESSING
                processed["current_stage"] = None
                processed["error"] = None
                processed["failed_stage"] = None
                # display_image 默认回落到原图，processing 期间前端显示原图
                orig = data.get("original") if isinstance(data.get("original"), dict) else None
                if orig and orig.get("key"):
                    processed["display_image"] = {
                        "kind": "original",
                        "key": orig["key"],
                        "url": orig.get("url", ""),
                    }
                data["processed"] = processed
                block.data = data
                db.commit()
        except Exception as e:
            logger.error(f"[Pipeline] set_processing failed: entry={entry_id} block={block_id} err={e}")
        finally:
            if own_session:
                db.close()

    # ─── 旧版 _set_completed / _set_failed 保留给 audio/ink 管线 ───

    async def _set_completed(
        self,
        entry_id: str,
        block_id: str,
        user_id: int,
        text: str,
        html: Optional[str] = None,
        model_name: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> None:
        db = self._get_db()
        own_session = self._db is None
        try:
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                block.text = text
                block.model_name = model_name or VLM_MODEL
                if html:
                    data = block.data if isinstance(block.data, dict) else {}
                    data["html"] = html
                    block.data = data

                # 写入 OCR/VLM 分级元数据
                if tier:
                    data = block.data if isinstance(block.data, dict) else {}
                    ocr_meta = data.get("ocr") or {}
                    if not isinstance(ocr_meta, dict):
                        ocr_meta = {}
                    ocr_meta[f"{tier}_at"] = datetime.utcnow().isoformat()
                    # 记录此时的笔划数（用于后续冷却判断）
                    strokes = data.get("strokes")
                    if isinstance(strokes, list):
                        ocr_meta["stroke_count"] = len(strokes)
                    data["ocr"] = ocr_meta
                    block.data = data

                db.commit()

            result = await self._index_block(block_id, text, entry_id, user_id)
            if result:
                vector_id, emb_model = result
                try:
                    db2 = self._get_db() if not own_session else SessionLocal()
                    try:
                        blk = db2.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
                        if blk:
                            blk.vector_id = vector_id
                            blk.model_name = emb_model
                            db2.commit()
                    finally:
                        if own_session:
                            db2.close()
                except Exception as e:
                    logger.warning(f"[Pipeline] vector/embedding write-back failed: block={block_id} err={e}")

            self._decrement_processing_count(db, entry_id, user_id)
        except Exception as e:
            logger.error(f"[Pipeline] set_completed failed: entry={entry_id} block={block_id} err={e}")
            try:
                self._decrement_processing_count(db, entry_id, user_id)
            except Exception:
                pass
        finally:
            if own_session:
                db.close()

    async def _set_failed(self, entry_id: str, block_id: str, user_id: int, error: str) -> None:
        db = self._get_db()
        own_session = self._db is None
        try:
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                block.text = f"[处理失败: {error[:200]}]"
                db.commit()
            self._decrement_processing_count(db, entry_id, user_id)
        except Exception as e:
            logger.error(f"[Pipeline] set_failed failed: entry={entry_id} block={block_id} err={e}")
        finally:
            if own_session:
                db.close()

    # ─── V 阶段：photo 管线专用的 DB 更新助手 ───

    def _publish_stage(self, entry_id: str, block_id: str, stage_name: str) -> None:
        """把当前阶段写入 block.data.processed.current_stage。W 代理的 SSE 端点会轮询/订阅 DB。"""
        db = self._get_db()
        own_session = self._db is None
        try:
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                data = block.data if isinstance(block.data, dict) else {}
                processed = data.get("processed") if isinstance(data.get("processed"), dict) else {}
                processed["current_stage"] = stage_name
                processed["status"] = PipelineStatus.PROCESSING
                data["processed"] = processed
                block.data = data
                db.commit()
        except Exception as e:
            logger.warning(
                f"[Pipeline] _publish_stage failed: entry={entry_id} block={block_id} "
                f"stage={stage_name} err={e}"
            )
        finally:
            if own_session:
                db.close()

    def _ensure_storage(self):
        from services.file_storage import create_file_storage
        return create_file_storage(mode=settings.STORAGE_MODE)

    def _build_block_key(self, user_id: int, block_id: str, suffix: str) -> str:
        """生成 blocks/ 路径下的 COS key。suffix 如 '_black.webp' / '.html'"""
        return f"{settings.STORAGE_PREFIX}zentrim/user_{user_id}/blocks/{block_id}{suffix}"

    def _upload_bytes(self, key: str, content: bytes, mime: str) -> str:
        """上传 bytes 到存储，返回公开 URL。"""
        storage = self._ensure_storage()
        storage.put_object(key, content)
        # 用 ZentrimService.make_public_url 生成可访问 URL
        db = self._get_db()
        own_session = self._db is None
        try:
            svc = ZentrimService(db)
            url = svc.make_public_url(key, mime=mime)
            return url
        finally:
            if own_session:
                db.close()

    async def _set_photo_completed(
        self,
        entry_id: str,
        block_id: str,
        user_id: int,
        *,
        channels: List[dict],
        fused_key: Optional[str],
        fused_url: Optional[str],
        html_source_key: Optional[str],
        html_source_url: Optional[str],
        screenshot_key: Optional[str],
        screenshot_url: Optional[str],
        display_image: dict,
        vlm_description: Optional[str],
        model_name: str,
    ) -> None:
        """photo 管线成功完成：写 processed 全字段 + text + vector index。"""
        db = self._get_db()
        own_session = self._db is None
        try:
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if not block:
                logger.error(f"[Pipeline] _set_photo_completed: block {block_id} not found")
                return
            data = block.data if isinstance(block.data, dict) else {}

            binarized = {"channels": channels, "fused": None}
            if fused_key:
                binarized["fused"] = {"key": fused_key, "url": fused_url or ""}

            html_dict: Optional[dict] = None
            if html_source_key or screenshot_key:
                html_dict = {
                    "source_key": html_source_key,
                    "source_url": html_source_url,
                    "screenshot_key": screenshot_key,
                    "screenshot_url": screenshot_url,
                }

            processed = data.get("processed") if isinstance(data.get("processed"), dict) else {}
            processed.update({
                "status": PipelineStatus.RENDERED,
                "current_stage": None,
                "error": None,
                "failed_stage": None,
                "binarized": binarized,
                "html": html_dict,
                "display_image": display_image,
            })
            data["processed"] = processed
            data["vlm_description"] = vlm_description

            block.data = data
            block.text = vlm_description or "[图片无描述]"
            block.model_name = model_name
            db.commit()

            # 向量索引（用 vlm_description；为空则跳过）
            if vlm_description and vlm_description.strip():
                result = await self._index_block(
                    block_id, vlm_description, entry_id, user_id
                )
                if result:
                    vector_id, emb_model = result
                    try:
                        db2 = self._get_db() if not own_session else SessionLocal()
                        try:
                            blk = db2.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
                            if blk:
                                blk.vector_id = vector_id
                                blk.model_name = emb_model
                                db2.commit()
                        finally:
                            if own_session:
                                db2.close()
                    except Exception as e:
                        logger.warning(f"[Pipeline] vector_id write-back failed: {e}")

            self._decrement_processing_count(db, entry_id, user_id)
        except Exception as e:
            logger.exception(f"[Pipeline] _set_photo_completed failed: {e}")
            try:
                self._decrement_processing_count(db, entry_id, user_id)
            except Exception:
                pass
        finally:
            if own_session:
                db.close()

    async def _set_photo_active(
        self, entry_id: str, block_id: str, user_id: int, display_kind: str = "original"
    ) -> None:
        """随手拍路径：不做优化，状态置 active，显示原图。VLM 描述仍独立跑。"""
        db = self._get_db()
        own_session = self._db is None
        try:
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                data = block.data if isinstance(block.data, dict) else {}
                orig = data.get("original") if isinstance(data.get("original"), dict) else None
                processed = data.get("processed") if isinstance(data.get("processed"), dict) else {}
                display_image = {
                    "kind": display_kind,
                    "key": (orig or {}).get("key", ""),
                    "url": (orig or {}).get("url", ""),
                }
                processed.update({
                    "status": PipelineStatus.ACTIVE,
                    "current_stage": None,
                    "error": None,
                    "failed_stage": None,
                    "display_image": display_image,
                })
                data["processed"] = processed
                block.data = data
                db.commit()
            self._decrement_processing_count(db, entry_id, user_id)
        except Exception as e:
            logger.exception(f"[Pipeline] _set_photo_active failed: {e}")
            try:
                self._decrement_processing_count(db, entry_id, user_id)
            except Exception:
                pass
        finally:
            if own_session:
                db.close()

    async def _set_photo_failed(
        self,
        entry_id: str,
        block_id: str,
        user_id: int,
        error_code: str,
        error_msg: str,
        failed_stage: str,
    ) -> None:
        """photo 管线失败：写 processed.status=failed + error + failed_stage。"""
        db = self._get_db()
        own_session = self._db is None
        try:
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                data = block.data if isinstance(block.data, dict) else {}
                orig = data.get("original") if isinstance(data.get("original"), dict) else None
                processed = data.get("processed") if isinstance(data.get("processed"), dict) else {}
                processed.update({
                    "status": PipelineStatus.FAILED,
                    "current_stage": None,
                    "error": f"[{error_code}] {error_msg}",
                    "failed_stage": failed_stage,
                    "display_image": {
                        "kind": "original",
                        "key": (orig or {}).get("key", ""),
                        "url": (orig or {}).get("url", ""),
                    },
                })
                data["processed"] = processed
                block.data = data
                block.text = f"[处理失败: {error_msg[:200]}]"
                db.commit()
            self._decrement_processing_count(db, entry_id, user_id)
        except Exception as e:
            logger.exception(f"[Pipeline] _set_photo_failed failed: {e}")
            try:
                self._decrement_processing_count(db, entry_id, user_id)
            except Exception:
                pass
        finally:
            if own_session:
                db.close()

    # ─── entry 级别 processing_count 计数器 ───

    def _increment_processing_count(self, db: Session, entry_id: str, user_id: int) -> None:
        entry = db.query(ZentrimEntry).filter(
            ZentrimEntry.id == entry_id, ZentrimEntry.user_id == user_id,
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
        entry = db.query(ZentrimEntry).filter(
            ZentrimEntry.id == entry_id, ZentrimEntry.user_id == user_id,
        ).first()
        if not entry:
            return
        meta = entry.metadata_ if isinstance(entry.metadata_, dict) else {}
        count = int(meta.get("pipeline_active_count", 0) or 0) - 1
        if count < 0:
            count = 0
        meta["pipeline_active_count"] = count
        entry.metadata_ = meta
        if count == 0:
            entry.status = "active"
        entry.updated_at = datetime.now(timezone.utc)
        db.commit()

    async def _index_block(self, block_id: str, text: str, entry_id: str, user_id: int) -> Optional[tuple]:
        """索引一个 block 的文本到向量存储。

        Returns:
            (vector_id, embedding_model) 或 None（索引失败/文本为空）
        """
        if not text or not text.strip():
            return None
        try:
            from services.vector_search_service import VectorSearchService
            vs = VectorSearchService(agent_hash=None)
            index_name = f"idx-zentrim-{user_id}"
            vector_id = f"zentrim:{block_id}"
            emb_model = settings.MAIN_EMBEDDING_MODEL
            await vs.index_text(
                key=vector_id, text=text, index=index_name,
                metadata={
                    "entry_id": entry_id,
                    "block_id": block_id,
                    "user_id": user_id,
                    "embedding_model": emb_model,
                },
            )
            logger.info(
                f"[Pipeline] indexed block={block_id} to {index_name} "
                f"model={emb_model}"
            )
            return (vector_id, emb_model)
        except Exception as e:
            logger.warning(f"[Pipeline] vector index failed (non-fatal): block={block_id} err={e}")
            return None

    def _download_file(self, cos_key: str, user_id: Optional[int] = None) -> Optional[bytes]:
        if not cos_key or not _is_valid_cos_key(cos_key, user_id):
            logger.error(f"[Pipeline] cos_key rejected by whitelist: key={cos_key!r} user_id={user_id}")
            return None
        try:
            from services.file_storage import create_file_storage
            storage = create_file_storage(mode=settings.STORAGE_MODE)
            return storage.get_file_content(cos_key)
        except Exception as e:
            logger.error(f"[Pipeline] download failed: key={cos_key} err={e}")
            return None

    # ─── VLM 调用 ───

    async def _call_vlm_heavy(
        self, image_b64: str, mime: str, prompt: str, max_tokens: Optional[int] = None
    ) -> Optional[str]:
        cfg = self.vision_heavy
        return await self._do_call_vlm(
            model_name=cfg.name, image_b64=image_b64, mime=mime, prompt=prompt,
            max_tokens=max_tokens if max_tokens is not None else cfg.max_tokens,
            timeout_s=cfg.timeout_s,
        )

    async def _call_vlm_speed(
        self, image_b64: str, mime: str, prompt: str, max_tokens: Optional[int] = None
    ) -> Optional[str]:
        cfg = self.vision_speed
        return await self._do_call_vlm(
            model_name=cfg.name, image_b64=image_b64, mime=mime, prompt=prompt,
            max_tokens=max_tokens if max_tokens is not None else cfg.max_tokens,
            timeout_s=cfg.timeout_s,
        )

    async def _call_ocr(
        self, image_b64: str, mime: str, prompt: Optional[str] = None,
    ) -> Optional[str]:
        """L0: 调用 qwen3.5-ocr 专用 OCR 模型提取文字。

        比通用 VLM 便宜 ~10x、更快（~2-3s），适合每次保存时更新 block.text 以保证搜索实时性。
        输出纯文本，不带 bbox 坐标。
        """
        ocr_prompt = prompt or "逐行提取图中所有文字，保留换行和段落结构。对于图表和公式，用 [图表] [公式] 标注其位置。"
        return await self._do_call_vlm(
            model_name=self.ocr_model,
            image_b64=image_b64, mime=mime,
            prompt=ocr_prompt,
            max_tokens=self.ocr_max_tokens,
            timeout_s=self.ocr_timeout_s,
        )

    async def _call_vlm(
        self, image_b64: str, mime: str, prompt: str, max_tokens: int = 2048
    ) -> Optional[str]:
        """[向后兼容] 默认走 Heavy VLM。"""
        return await self._call_vlm_heavy(
            image_b64=image_b64, mime=mime, prompt=prompt, max_tokens=max_tokens
        )

    async def _do_call_vlm(
        self, *, model_name: str, image_b64: str, mime: str, prompt: str,
        max_tokens: int, timeout_s: int,
    ) -> Optional[str]:
        info = _model_resolve(model_name)
        base_url = f"{info['base_url']}/chat/completions"
        api_key = os.getenv(info.get("api_key_attr", ""), "")
        content = [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            {"type": "text", "text": prompt},
        ]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
                response = await client.post(
                    base_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": content}],
                        "stream": False,
                        "thinking": {"type": "disabled"},
                        "max_tokens": max_tokens,
                    },
                )
            if response.status_code != 200:
                logger.warning(
                    f"[Pipeline] VLM API error: model={model_name} "
                    f"HTTP {response.status_code}, body={response.text[:200]}"
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
            logger.warning(f"[Pipeline] VLM timeout: model={model_name} after {timeout_s}s")
            return None
        except Exception as e:
            logger.error(f"[Pipeline] VLM call failed: model={model_name} err={e}", exc_info=True)
            return None

    @staticmethod
    def _detect_image_format(data: bytes) -> str:
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
        ext = ZentrimPipeline._detect_image_format(image_data)
        mime = f"image/{ext}"
        b64 = base64.b64encode(image_data).decode("utf-8")
        return b64, mime

    @staticmethod
    def _tile_image(image_data: bytes, max_size: int = INK_TILE_MAX_SIZE,
                    overlap: float = INK_TILE_OVERLAP) -> list:
        """将大图拆成重叠的分块，每块边长不超过 max_size。
        返回 [(b64, mime, (x, y, w, h)), ...] 列表。
        如果原图不需要拆分，返回单元素列表。
        """
        from PIL import Image as PILImage
        import io

        img = PILImage.open(io.BytesIO(image_data))
        orig_w, orig_h = img.size

        # 不需要拆分
        if orig_w <= max_size and orig_h <= max_size:
            b64, mime = ZentrimPipeline._encode_image(image_data)
            return [(b64, mime, (0, 0, orig_w, orig_h))]

        tiles = []
        step_w = int(max_size * (1 - overlap))
        step_h = int(max_size * (1 - overlap))

        y = 0
        while y < orig_h:
            x = 0
            tile_h = min(max_size, orig_h - y)
            while x < orig_w:
                tile_w = min(max_size, orig_w - x)
                box = (x, y, x + tile_w, y + tile_h)
                tile = img.crop(box)
                # 如果分块仍超过 byte 限制，缩放到 max_size 以内
                buf = io.BytesIO()
                tile.save(buf, format="PNG")
                tile_bytes = buf.getvalue()
                if len(tile_bytes) > VLM_MAX_IMAGE_BYTES:
                    ratio = (VLM_MAX_IMAGE_BYTES / len(tile_bytes)) ** 0.5 * 0.9
                    new_w = max(1, int(tile_w * ratio))
                    new_h = max(1, int(tile_h * ratio))
                    tile = tile.resize((new_w, new_h), PILImage.LANCZOS)
                    buf = io.BytesIO()
                    tile.save(buf, format="PNG")
                    tile_bytes = buf.getvalue()
                b64, mime = ZentrimPipeline._encode_image(tile_bytes)
                tiles.append((b64, mime, box))
                x += step_w
            y += step_h

        logger.info(
            f"[Pipeline] tiled image {orig_w}x{orig_h} → {len(tiles)} tiles "
            f"(max={max_size}, overlap={overlap})"
        )
        return tiles

    # ════════════════════════════════════════
    # V 阶段：photo 管线新步骤
    # ════════════════════════════════════════

    # ─── Step 0: Speed 形态判断 ───

    async def _classify_document(self, image_bytes: bytes) -> Literal["document", "casual"]:
        """用 Speed VLM 判断是文档类还是随手拍。"""
        b64, mime = self._encode_image(image_bytes)
        resp = await self._call_vlm_speed(b64, mime, PROMPT_DOCUMENT_CLASSIFY, max_tokens=16)
        if resp and "DOCUMENT" in resp.upper():
            return "document"
        return "casual"

    # ─── Step 2: 主色识别（Heavy） ───

    async def _detect_main_colors(
        self, image_bytes: bytes
    ) -> Tuple[List[ColorChannel], float]:
        """识别主色 + 返回规整度评分。
        Returns: (channels, regularity_score)
        """
        b64, mime = self._encode_image(image_bytes)
        resp = await self._call_vlm_heavy(b64, mime, PROMPT_DETECT_COLORS, max_tokens=512)
        if not resp:
            # VLM 失败：兜底返回黑色单通道，规整度 0.0
            logger.warning("[Pipeline] _detect_main_colors: VLM returned None, fallback to black-only")
            return ([ColorChannel(color="black")], 0.0)

        # 尝试解析 JSON（容错：去掉 ```json 包裹）
        parsed = self._extract_json(resp)
        if not isinstance(parsed, dict):
            logger.warning(f"[Pipeline] _detect_main_colors: bad JSON: {resp[:200]}")
            return ([ColorChannel(color="black")], 0.0)

        regularity = float(parsed.get("regularity_score", 0.0) or 0.0)
        colors_raw = parsed.get("colors") or []
        channels: List[ColorChannel] = []
        seen = set()
        for c in colors_raw:
            if not isinstance(c, dict):
                continue
            color_name = str(c.get("color", "")).lower().strip()
            if color_name not in _COLOR_TO_RGB and color_name != "other":
                color_name = "other"
            if color_name in seen:
                continue
            seen.add(color_name)
            hue_ranges = _COLOR_HUE_RANGES.get(color_name, [(0, 0)])
            # 取第一个范围作为主要范围（red 跨 0° 单独处理）
            hue_low, hue_high = hue_ranges[0]
            channels.append(ColorChannel(
                color=color_name,  # type: ignore[arg-type]
                hue_low=float(hue_low),
                hue_high=float(hue_high),
                description=str(c.get("description", "")),
            ))
        # 保证至少有 black 通道（通常黑色是主色）
        if not channels:
            channels.append(ColorChannel(color="black"))
        if "black" not in seen:
            channels.insert(0, ColorChannel(color="black"))
        return (channels, regularity)

    @staticmethod
    def _extract_json(text: str) -> Optional[Any]:
        """从 VLM 响应中提取 JSON，容错 markdown 包裹。"""
        if not text:
            return None
        # 去除 markdown 代码块
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        # 找第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        try:
            return json.loads(text)
        except Exception:
            return None

    # ─── Step 3: 单通道二值化迭代（cv2 + Heavy） ───

    @staticmethod
    def _decode_cv2(image_bytes: bytes):
        """bytes → BGR numpy array。cv2 不可用返回 None。"""
        if not _CV2_AVAILABLE:
            return None
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img

    @staticmethod
    def _binarize_channel_opencv(
        img_bgr, channel: ColorChannel, threshold: int
    ) -> bytes:
        """对单个通道做二值化，返回 webp bytes（白色背景 + 该色墨水 → 白色背景 + 黑色 mask）。
        注意：返回的是单通道 mask（白=背景，黑=笔迹），供 fused 阶段染色。
        """
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        if channel.color == "black":
            # 黑色：V 通道低于 threshold 判定为笔迹
            v = hsv[:, :, 2]
            mask = (v < threshold).astype(np.uint8) * 255
        elif channel.color == "red":
            # 红色跨 0°：两段 hue 取并集，加上 S > threshold 的约束
            s = hsv[:, :, 1]
            mask1 = cv2.inRange(hsv, (0, max(40, threshold - 80), 50), (10, 255, 255))
            mask2 = cv2.inRange(hsv, (170, max(40, threshold - 80), 50), (179, 255, 255))
            mask = cv2.bitwise_or(mask1, mask2)
            # 用 S 阈值再过滤一次
            s_mask = (s > max(40, threshold - 80)).astype(np.uint8) * 255
            mask = cv2.bitwise_and(mask, s_mask)
        else:
            # 其他颜色：H 在 [hue_low, hue_high] + S 足够高
            s_thresh = max(40, threshold - 80)
            mask = cv2.inRange(
                hsv,
                (channel.hue_low, s_thresh, 50),
                (channel.hue_high, 255, 255),
            )
        # 反相：mask 中 255=笔迹 → 给 imencode 用的图：背景白(255)，笔迹黑(0)
        # 但我们直接存 mask（255=笔迹），fused 时按 mask 染色
        # 做一点去噪（开运算去除椒盐噪点）
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        # 编码 webp
        ok, buf = cv2.imencode(".webp", mask, [cv2.IMWRITE_WEBP_QUALITY, 90])
        if not ok:
            raise RuntimeError("cv2.imencode webp failed")
        return buf.tobytes()

    async def _binarize_channel(
        self, image_bytes: bytes, channel: ColorChannel
    ) -> BinarizedChannel:
        """对单个色道跑最多 5 轮迭代阈值调整。"""
        img_bgr = self._decode_cv2(image_bytes)
        if img_bgr is None:
            raise RuntimeError(f"[{ErrorCode.CV2_UNAVAILABLE}] cv2 not available")

        # black 初始阈值 100（V<100 即黑），颜色初始阈值 120（S 阈值映射）
        threshold = 100 if channel.color == "black" else 120
        iterations = 0
        max_iters = 5
        last_mask_bytes = self._binarize_channel_opencv(img_bgr, channel, threshold)

        for i in range(max_iters):
            iterations = i + 1
            # 发给 Heavy 评估
            # 拼接：原图 + 当前二值化 mask 作为一张左右拼接的 PNG
            eval_img = self._compose_eval_image(img_bgr, last_mask_bytes)
            b64, mime = self._encode_image(eval_img)
            prompt = PROMPT_BINARIZE_EVAL.format(
                color=channel.color, threshold=threshold
            )
            resp = await self._call_vlm_heavy(b64, mime, prompt, max_tokens=128)
            if not resp:
                # VLM 无响应：用当前结果
                break
            parsed = self._extract_json(resp)
            if not isinstance(parsed, dict):
                break
            adjust = str(parsed.get("adjust", "ok")).lower()
            amount = int(parsed.get("amount", 10) or 10)
            amount = max(5, min(30, amount))
            if adjust == "higher":
                threshold = min(255, threshold + amount)
            elif adjust == "lower":
                threshold = max(20, threshold - amount)
            else:
                break
            last_mask_bytes = self._binarize_channel_opencv(img_bgr, channel, threshold)

        return BinarizedChannel(
            color=channel.color,
            threshold=threshold,
            iterations=iterations,
            image_bytes_webp=last_mask_bytes,
        )

    @staticmethod
    def _compose_eval_image(img_bgr, mask_bytes: bytes) -> bytes:
        """把原图和 mask 左右拼成一张 PNG 给 VLM 评估。"""
        mask_arr = np.frombuffer(mask_bytes, dtype=np.uint8)
        mask_img = cv2.imdecode(mask_arr, cv2.IMREAD_GRAYSCALE)
        # mask 转 BGR 方便拼接
        mask_bgr = cv2.cvtColor(mask_img, cv2.COLOR_GRAY2BGR)
        # 统一高度
        h1, w1 = img_bgr.shape[:2]
        h2, w2 = mask_bgr.shape[:2]
        target_h = max(h1, h2)
        def _pad(img, target_h):
            h, w = img.shape[:2]
            if h == target_h:
                return img
            pad = np.full((target_h - h, w, 3), 255, dtype=np.uint8)
            return np.vstack([img, pad])
        a = _pad(img_bgr, target_h)
        b = _pad(mask_bgr, target_h)
        composed = np.hstack([a, b])
        ok, buf = cv2.imencode(".png", composed)
        return buf.tobytes()

    # ─── Step 3.3: 多色融合 ───

    @staticmethod
    def _fuse_channels(
        original_bytes: bytes, channels: List[BinarizedChannel]
    ) -> bytes:
        """把多个二值化 mask 合成一张白底彩字的 webp。"""
        if not _CV2_AVAILABLE:
            raise RuntimeError(f"[{ErrorCode.CV2_UNAVAILABLE}] cv2 not available")
        orig_arr = np.frombuffer(original_bytes, dtype=np.uint8)
        orig = cv2.imdecode(orig_arr, cv2.IMREAD_COLOR)
        h, w = orig.shape[:2]
        # 白底
        fused = np.full((h, w, 3), 255, dtype=np.uint8)
        for ch in channels:
            mask_arr = np.frombuffer(ch.image_bytes_webp, dtype=np.uint8)
            mask = cv2.imdecode(mask_arr, cv2.IMREAD_GRAYSCALE)
            if mask.shape != (h, w):
                mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
            rgb = _COLOR_TO_RGB.get(ch.color, (0, 0, 0))
            # mask > 128 即笔迹：把 fused 对应像素染成 RGB
            # OpenCV BGR 顺序
            bgr = (rgb[2], rgb[1], rgb[0])
            fused[mask > 128] = bgr
        ok, buf = cv2.imencode(".webp", fused, [cv2.IMWRITE_WEBP_QUALITY, 92])
        if not ok:
            raise RuntimeError("cv2.imencode fused webp failed")
        return buf.tobytes()

    # ─── Step 4: HTML 生成 + Playwright 截图 ───

    async def _generate_html(self, image_bytes: bytes) -> Optional[str]:
        """Heavy VLM 生成 HTML。失败返回 None。"""
        b64, mime = self._encode_image(image_bytes)
        html = await self._call_vlm_heavy(
            b64, mime, PROMPT_GENERATE_HTML, max_tokens=4096
        )
        if not html:
            return None
        # 剥掉 markdown 代码块
        m = re.search(r"```(?:html)?\s*(.*?)\s*```", html, re.DOTALL)
        if m:
            html = m.group(1)
        html = html.strip()
        if not html.lower().startswith("<!doctype") and not html.lower().startswith("<html"):
            # 包一层
            html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<style>body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;"
                "max-width:1000px;margin:40px auto;padding:0 20px;line-height:1.7;"
                "background:#fff;color:#111;}</style></head><body>"
                f"{html}</body></html>"
            )
        return html

    async def _screenshot_html(self, html: str) -> bytes:
        """Playwright 加载 HTML → PNG bytes。"""
        from playwright.async_api import async_playwright
        async with self._pw_lock:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 1200, "height": 1600})
                    await page.set_content(html, wait_until="networkidle", timeout=30000)
                    img = await page.screenshot(type="png", full_page=True)
                    return img
                finally:
                    await browser.close()

    # ─── Step 5: VLM 描述（3 次退避重试，软失败） ───

    async def _describe_with_retry(
        self, original_bytes: bytes, optimized_bytes: bytes
    ) -> Optional[str]:
        """VLM 描述：用优化后图片（优先）/原图，失败静默重试 3 次退避，全失败返回 None。"""
        # 优先 optimized
        for attempt, img_bytes in enumerate([optimized_bytes, original_bytes]):
            if not img_bytes:
                continue
            b64, mime = self._encode_image(img_bytes)
            # 每个图片源试 3 次（退避：2s, 4s）
            for retry in range(3):
                try:
                    resp = await self._call_vlm_heavy(
                        b64, mime, PROMPT_DESCRIBE_IMAGE, max_tokens=256
                    )
                    if resp and resp.strip():
                        return resp.strip()
                except Exception as e:
                    logger.warning(
                        f"[Pipeline] describe attempt {retry + 1} source={attempt} failed: {e}"
                    )
                if retry < 2:
                    await asyncio.sleep(2 ** (retry + 1))  # 2s, 4s
            # optimized 失败，尝试 original
        return None

    # ════════════════════════════════════════
    # 管线主流程
    # ════════════════════════════════════════

    async def _run_photo_pipeline(
        self, entry_id: str, block_id: str, cos_key: str, user_id: int
    ) -> None:
        """拍照新管线：形态判断 → 二值化 → HTML → VLM 描述。"""
        current_stage: Optional[str] = None
        try:
            # Step 1: 标记 processing（已写 display_image=original 兜底）
            self._set_processing(entry_id, block_id, user_id)

            # Step 2: 下载原图
            original_bytes = self._download_file(cos_key, user_id=user_id)
            if not original_bytes:
                await self._set_photo_failed(
                    entry_id, block_id, user_id,
                    ErrorCode.COS_UPLOAD_FAILED, "文件下载失败", FailedStage.COS_UPLOAD,
                )
                return
            if len(original_bytes) > self.max_image_bytes:
                await self._set_photo_failed(
                    entry_id, block_id, user_id,
                    ErrorCode.COS_UPLOAD_FAILED,
                    f"图片过大 ({len(original_bytes)} bytes)", FailedStage.COS_UPLOAD,
                )
                return

            # Step 3: Speed 形态判断
            current_stage = FailedStage.CLASSIFY
            self._publish_stage(entry_id, block_id, current_stage)
            kind = await self._classify_document(original_bytes)

            if kind == "casual":
                # 随手拍：放弃优化，仅独立跑 VLM 描述
                await self._set_photo_active(entry_id, block_id, user_id, display_kind="original")
                # 软失败隔离的 VLM 描述：失败不影响
                try:
                    vlm_desc = await self._describe_with_retry(original_bytes, original_bytes)
                    if vlm_desc:
                        await self._write_vlm_description(block_id, vlm_desc, entry_id, user_id)
                except Exception as e:
                    logger.warning(f"[Pipeline] casual VLM describe failed (non-fatal): {e}")
                return

            # ─── 文档类：必跑二值化 ───
            current_stage = FailedStage.COLOR_ID
            self._publish_stage(entry_id, block_id, current_stage)
            channels_meta, regularity_score = await self._detect_main_colors(original_bytes)
            if not channels_meta:
                await self._set_photo_failed(
                    entry_id, block_id, user_id,
                    ErrorCode.VLM_INVALID_RESPONSE, "主色识别失败", FailedStage.COLOR_ID,
                )
                return

            # Step 3: 各通道二值化
            current_stage = FailedStage.BINARIZE
            self._publish_stage(entry_id, block_id, current_stage)
            binarized_channels: List[BinarizedChannel] = []
            for ch in channels_meta:
                bn = await self._binarize_channel(original_bytes, ch)
                binarized_channels.append(bn)

            # 上传各色道 mask + 融合
            channels_meta_out: List[dict] = []
            for bn in binarized_channels:
                suffix = f"_{bn.color}.webp"
                key = self._build_block_key(user_id, block_id, suffix)
                url = self._upload_bytes(key, bn.image_bytes_webp, "image/webp")
                channels_meta_out.append({
                    "color": bn.color,
                    "threshold": bn.threshold,
                    "iterations": bn.iterations,
                    "key": key,
                    "url": url,
                })

            fused_bytes = self._fuse_channels(original_bytes, binarized_channels)
            fused_key = self._build_block_key(user_id, block_id, "_fused.webp")
            fused_url = self._upload_bytes(fused_key, fused_bytes, "image/webp")

            # Step 4: HTML 生成（规整度 >= 0.5 才跑）
            html_source_key: Optional[str] = None
            html_source_url: Optional[str] = None
            screenshot_key: Optional[str] = None
            screenshot_url: Optional[str] = None
            display_image = {"kind": "fused", "key": fused_key, "url": fused_url}

            if regularity_score >= 0.5:
                current_stage = FailedStage.HTML
                self._publish_stage(entry_id, block_id, current_stage)
                try:
                    html_source = await self._generate_html(original_bytes)
                    if html_source:
                        html_source_key = self._build_block_key(user_id, block_id, ".html")
                        html_source_url = self._upload_bytes(
                            html_source_key, html_source.encode("utf-8"), "text/html"
                        )
                        current_stage = FailedStage.SCREENSHOT
                        self._publish_stage(entry_id, block_id, current_stage)
                        try:
                            screenshot = await self._screenshot_html(html_source)
                            screenshot_key = self._build_block_key(
                                user_id, block_id, "_screenshot.png"
                            )
                            screenshot_url = self._upload_bytes(
                                screenshot_key, screenshot, "image/png"
                            )
                            display_image = {
                                "kind": "screenshot",
                                "key": screenshot_key,
                                "url": screenshot_url,
                            }
                        except Exception as e:
                            logger.warning(
                                f"[Pipeline] playwright screenshot failed, fallback to fused: {e}"
                            )
                            # 失败回落到 fused，不进 failed
                except Exception as e:
                    logger.warning(
                        f"[Pipeline] HTML generation failed, fallback to fused: {e}"
                    )

            # Step 5: VLM 描述（独立，软失败）
            current_stage = FailedStage.DESCRIBE
            self._publish_stage(entry_id, block_id, current_stage)
            vlm_desc: Optional[str] = None
            optimized_for_desc = screenshot if screenshot_key else fused_bytes
            try:
                vlm_desc = await self._describe_with_retry(original_bytes, optimized_for_desc)
            except Exception as e:
                logger.warning(f"[Pipeline] VLM describe failed (non-fatal): {e}")

            # Step 6: 落库 rendered
            await self._set_photo_completed(
                entry_id, block_id, user_id,
                channels=channels_meta_out,
                fused_key=fused_key, fused_url=fused_url,
                html_source_key=html_source_key, html_source_url=html_source_url,
                screenshot_key=screenshot_key, screenshot_url=screenshot_url,
                display_image=display_image,
                vlm_description=vlm_desc,
                model_name=self.vision_heavy.name,
            )

        except Exception as e:
            logger.exception(f"[Pipeline] photo pipeline error: entry={entry_id} block={block_id}")
            await self._set_photo_failed(
                entry_id, block_id, user_id,
                ErrorCode.VLM_TIMEOUT, str(e)[:200],
                current_stage or FailedStage.CLASSIFY,
            )

    async def _write_vlm_description(
        self, block_id: str, text: str, entry_id: str, user_id: int
    ) -> None:
        """随手拍路径专用：写完 vlm_description 后补向量索引（不经过 rendered 流程）。"""
        db = self._get_db()
        own_session = self._db is None
        try:
            block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
            if block:
                data = block.data if isinstance(block.data, dict) else {}
                data["vlm_description"] = text
                block.data = data
                if not block.text or block.text.startswith("["):
                    block.text = text
                block.model_name = self.vision_heavy.name
                db.commit()
            result = await self._index_block(block_id, text, entry_id, user_id)
            if result:
                vector_id, emb_model = result
                try:
                    db2 = self._get_db() if not own_session else SessionLocal()
                    try:
                        blk = db2.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
                        if blk:
                            blk.vector_id = vector_id
                            blk.model_name = emb_model
                            db2.commit()
                    finally:
                        if own_session:
                            db2.close()
                except Exception as e:
                    logger.warning(f"[Pipeline] vector_id write-back failed: {e}")
        except Exception as e:
            logger.warning(f"[Pipeline] _write_vlm_description failed: {e}")
        finally:
            if own_session:
                db.close()

    # ─── Audio / Ink 管线（保留不动） ───

    async def _run_audio_pipeline(self, entry_id: str, block_id: str, cos_key: str, user_id: int) -> None:
        from services.asr_service import transcribe as asr_transcribe
        from services.storage_service import CosStorage

        try:
            self._set_processing(entry_id, block_id, user_id)

            # 生成 COS 预签名 GET URL（48h 有效，阿里云可访问）
            storage = CosStorage()
            audio_url = storage.generate_presigned_get_url(cos_key, expired=172800)
            if not audio_url:
                # fallback: 用公开 URL 试试
                audio_url = f"https://{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/{cos_key}"

            # 调 ASR 服务（异步，等待完成）
            result = await asr_transcribe(audio_url)
            if not result:
                await self._set_failed(entry_id, block_id, user_id, "ASR 识别失败")
                return

            # 写入 block
            db = self._get_db()
            own_session = self._db is None
            try:
                block = db.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
                if block:
                    # text 字段：全文，供搜索和列表预览
                    block.text = result.full_text
                    block.model_name = result.model
                    # data 字段：完整转写结果（segments, words, diarization 等）
                    data = block.data if isinstance(block.data, dict) else {}
                    data["transcription"] = result.to_dict()
                    block.data = data
                    db.commit()

                # 向量索引
                result = await self._index_block(block_id, result.full_text, entry_id, user_id)
                if result and block:
                    vector_id, emb_model = result
                    try:
                        db2 = self._get_db() if not own_session else SessionLocal()
                        try:
                            blk = db2.query(ZentrimBlock).filter(ZentrimBlock.id == block_id).first()
                            if blk:
                                blk.vector_id = vector_id
                                blk.model_name = emb_model
                                db2.commit()
                        finally:
                            if own_session:
                                db2.close()
                    except Exception as e:
                        logger.warning(f"[Pipeline] vector_id write-back failed: block={block_id} err={e}")

                self._decrement_processing_count(db, entry_id, user_id)
                logger.info(
                    f"[Pipeline] audio ASR completed: entry={entry_id} block={block_id} "
                    f"model={result.model} segments={len(result.segments)}"
                )
            except Exception as e:
                logger.exception(f"[Pipeline] audio result write failed: {e}")
                self._decrement_processing_count(db, entry_id, user_id)
            finally:
                if own_session:
                    db.close()
        except Exception as e:
            logger.exception(f"[Pipeline] audio pipeline error: entry={entry_id} block={block_id}")
            await self._set_failed(entry_id, block_id, user_id, str(e))

    async def _run_ink_pipeline(
        self, entry_id: str, block_id: str, cos_key: str, user_id: int,
        tier: str = "ocr",
    ) -> None:
        """Ink 管线主入口。

        tier 参数：
        - "ocr" (L0): 调用 qwen3.5-ocr 专用模型，单图无分块，~3s，¥0.01。
          用于每次保存时快速更新 block.text 以保证搜索实时性。
        - "vlm" (L1): 调用 Heavy VLM + 分块 + 结构化提取，~30-60s。
          用于首次创建 / 定时（30min 冷却）/ 手动触发。解析复杂排版（公式、图表）。
        """
        try:
            self._set_processing(entry_id, block_id, user_id)
            image_data = self._download_file(cos_key, user_id=user_id)
            if not image_data:
                await self._set_failed(entry_id, block_id, user_id, "画布图片下载失败")
                return

            if tier == "ocr":
                # L0: 单图 OCR，不分块
                b64, mime = self._encode_image(image_data)
                text = await self._call_ocr(b64, mime)
                if text:
                    await self._set_completed(
                        entry_id, block_id, user_id,
                        text=text, model_name=self.ocr_model, tier=tier,
                    )
                else:
                    await self._set_completed(
                        entry_id, block_id, user_id,
                        text="[手写内容，OCR 识别失败]",
                        model_name=self.ocr_model, tier=tier,
                    )
            else:
                # L1: VLM + 分块
                tiles = self._tile_image(image_data)
                tile_count = len(tiles)
                parts: list[str] = []

                for i, (b64, mime, box) in enumerate(tiles):
                    x, y, w, h = box
                    if tile_count > 1:
                        prompt = (
                            f"这是画布的第 {i+1}/{tile_count} 块（位置: x={x}, y={y}, {w}x{h}）。\n"
                            + PROMPT_INK_OCR
                        )
                    else:
                        prompt = PROMPT_INK_OCR

                    text = await self._call_vlm(b64, mime, prompt, max_tokens=2048)
                    if text:
                        parts.append(f"[块 {i+1}/{tile_count}]\n{text}")
                    else:
                        parts.append(f"[块 {i+1}/{tile_count} 识别失败]")

                merged = "\n\n".join(parts) if parts else "[手写内容，VLM OCR 失败]"
                await self._set_completed(
                    entry_id, block_id, user_id,
                    text=merged, model_name=VLM_MODEL, tier=tier,
                )
        except Exception as e:
            logger.exception(f"[Pipeline] ink pipeline error: entry={entry_id} block={block_id} tier={tier}")
            await self._set_failed(entry_id, block_id, user_id, str(e))

    @staticmethod
    def _strip_html(html: str) -> str:
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text


# ─── 全局实例 ───
pipeline = ZentrimPipeline()
