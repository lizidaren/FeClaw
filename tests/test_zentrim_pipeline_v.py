"""
V 阶段：zentrim_pipeline 后端管线增强测试

覆盖：
  - _classify_document 对 "DOCUMENT" / "CASUAL" 的解析
  - _fuse_channels 多色合成 webp
  - _extract_json 容错解析
  - 3 次退避重试 _describe_with_retry 的调用路径（mock VLM）
  - 色道二值化 mask 生成（不依赖 VLM）
"""
import pytest
from unittest.mock import AsyncMock, patch

import numpy as np

from services.zentrim_pipeline import (
    BinarizedChannel,
    ColorChannel,
    ErrorCode,
    FailedStage,
    PipelineStatus,
    ZentrimPipeline,
    _COLOR_TO_RGB,
)


# ════════════════════════════════════════
# _classify_document
# ════════════════════════════════════════

class TestClassifyDocument:
    @pytest.mark.asyncio
    async def test_returns_document_when_speed_says_document(self):
        p = ZentrimPipeline()
        # 用一个最小 PNG 作为输入，避免真的调 VLM
        dummy_png = self._make_png_white()
        with patch.object(p, "_call_vlm_speed", new=AsyncMock(return_value="DOCUMENT")):
            result = await p._classify_document(dummy_png)
            assert result == "document"

    @pytest.mark.asyncio
    async def test_returns_casual_when_speed_says_casual(self):
        p = ZentrimPipeline()
        dummy_png = self._make_png_white()
        with patch.object(p, "_call_vlm_speed", new=AsyncMock(return_value="casual")):
            result = await p._classify_document(dummy_png)
            assert result == "casual"

    @pytest.mark.asyncio
    async def test_returns_casual_when_speed_none(self):
        p = ZentrimPipeline()
        dummy_png = self._make_png_white()
        with patch.object(p, "_call_vlm_speed", new=AsyncMock(return_value=None)):
            result = await p._classify_document(dummy_png)
            assert result == "casual"

    @pytest.mark.asyncio
    async def test_returns_document_when_response_contains_document(self):
        p = ZentrimPipeline()
        dummy_png = self._make_png_white()
        # 模拟 VLM 返回带前后缀的字符串
        with patch.object(p, "_call_vlm_speed", new=AsyncMock(return_value="  DOCUMENT. ")):
            result = await p._classify_document(dummy_png)
            assert result == "document"

    @staticmethod
    def _make_png_white() -> bytes:
        # 构造一个 4x4 白色 PNG bytes（cv2 可解码）
        import cv2
        img = np.full((4, 4, 3), 255, dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)
        assert ok
        return buf.tobytes()


# ════════════════════════════════════════
# _fuse_channels
# ════════════════════════════════════════

class TestFuseChannels:
    def test_fuse_multiple_colors_produces_webp(self):
        """_fuse_channels 把多色 mask 合成一张 webp，且背景为白，笔迹染色。"""
        import cv2
        h, w = 20, 20
        # 原图：灰色背景
        original = np.full((h, w, 3), 200, dtype=np.uint8)
        ok, orig_bytes = cv2.imencode(".png", original)
        assert ok

        # 黑色通道：左上 10x10 方块是笔迹
        black_mask = np.zeros((h, w), dtype=np.uint8)
        black_mask[0:10, 0:10] = 255
        ok, black_webp = cv2.imencode(".webp", black_mask)
        assert ok

        # 红色通道：右下 10x10 方块是笔迹
        red_mask = np.zeros((h, w), dtype=np.uint8)
        red_mask[10:20, 10:20] = 255
        ok, red_webp = cv2.imencode(".webp", red_mask)
        assert ok

        channels = [
            BinarizedChannel(color="black", threshold=100, iterations=1,
                             image_bytes_webp=black_webp.tobytes()),
            BinarizedChannel(color="red", threshold=120, iterations=1,
                             image_bytes_webp=red_webp.tobytes()),
        ]

        fused_bytes = ZentrimPipeline._fuse_channels(orig_bytes.tobytes(), channels)
        assert isinstance(fused_bytes, bytes)
        assert len(fused_bytes) > 0
        # 解码验证
        arr = np.frombuffer(fused_bytes, dtype=np.uint8)
        fused_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        assert fused_img is not None
        assert fused_img.shape == (h, w, 3)
        # 背景应为白（webp 有损，>=250 即可）
        assert fused_img[5, 15].min() >= 250
        # 左上角应为黑（三通道都 < 10）
        assert fused_img[5, 5].max() <= 10
        # 右下角应为红（R 通道高，B/G 低；webp 有损，按方向判断）
        pix = fused_img[15, 15]  # BGR
        assert pix[2] > 180  # R > 180
        assert pix[0] < 80   # B < 80
        assert pix[1] < 80   # G < 80

    def test_fuse_empty_channels_stays_white(self):
        """无通道时，fused 应为全白（没有染色操作）。"""
        import cv2
        h, w = 8, 8
        original = np.full((h, w, 3), 180, dtype=np.uint8)
        ok, orig_bytes = cv2.imencode(".png", original)
        fused_bytes = ZentrimPipeline._fuse_channels(orig_bytes.tobytes(), [])
        arr = np.frombuffer(fused_bytes, dtype=np.uint8)
        fused_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        assert fused_img is not None
        assert (fused_img == 255).all()


# ════════════════════════════════════════
# _extract_json
# ════════════════════════════════════════

class TestExtractJson:
    def test_plain_json(self):
        result = ZentrimPipeline._extract_json('{"regularity_score": 0.7, "colors": []}')
        assert result == {"regularity_score": 0.7, "colors": []}

    def test_markdown_wrapped_json(self):
        text = '```json\n{"regularity_score": 0.5, "colors": [{"color": "black"}]}\n```'
        result = ZentrimPipeline._extract_json(text)
        assert result["regularity_score"] == 0.5
        assert result["colors"][0]["color"] == "black"

    def test_json_surrounded_by_text(self):
        text = 'Certainly! Here is the result:\n{"adjust": "higher", "amount": 10}\nHope this helps.'
        result = ZentrimPipeline._extract_json(text)
        assert result == {"adjust": "higher", "amount": 10}

    def test_invalid_json_returns_none(self):
        assert ZentrimPipeline._extract_json("not json") is None
        assert ZentrimPipeline._extract_json("") is None
        assert ZentrimPipeline._extract_json(None) is None


# ════════════════════════════════════════
# _binarize_channel_opencv 黑/红通道
# ════════════════════════════════════════

class TestBinarizeChannelOpencv:
    def test_black_channel_extracts_dark_pixels(self):
        import cv2
        h, w = 10, 10
        img = np.full((h, w, 3), 230, dtype=np.uint8)  # 白底
        img[2:5, 2:5] = (20, 20, 20)  # 深色块
        ch = ColorChannel(color="black")
        mask_webp = ZentrimPipeline._binarize_channel_opencv(img, ch, threshold=80)
        arr = np.frombuffer(mask_webp, dtype=np.uint8)
        mask = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        assert mask is not None
        # 深色块中心应被判定为笔迹（webp 有损：>= 200 视为笔迹）
        assert mask[3, 3] >= 200
        # 白色背景不应为笔迹
        assert mask[0, 0] <= 10

    def test_blue_channel_extracts_blue_pixels(self):
        import cv2
        h, w = 10, 10
        img = np.full((h, w, 3), 230, dtype=np.uint8)  # 白底（BGR）
        img[2:5, 2:5] = (200, 60, 60)  # BGR 蓝 (B=200, G=60, R=60)
        ch = ColorChannel(color="blue", hue_low=100, hue_high=130)
        mask_webp = ZentrimPipeline._binarize_channel_opencv(img, ch, threshold=120)
        arr = np.frombuffer(mask_webp, dtype=np.uint8)
        mask = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        assert mask[3, 3] >= 200
        assert mask[0, 0] <= 10


# ════════════════════════════════════════
# _describe_with_retry — 软失败语义
# ════════════════════════════════════════

class TestDescribeWithRetry:
    @pytest.mark.asyncio
    async def test_returns_none_after_all_retries_fail(self):
        """VLM 全失败时返回 None，不抛异常。"""
        p = ZentrimPipeline()
        import cv2
        img = np.full((4, 4, 3), 255, dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)
        img_bytes = buf.tobytes()
        with patch.object(p, "_call_vlm_heavy", new=AsyncMock(return_value=None)):
            with patch.object(p, "_encode_image", return_value=("aaaa", "image/png")):
                result = await p._describe_with_retry(img_bytes, img_bytes)
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_first_success(self):
        p = ZentrimPipeline()
        import cv2
        img = np.full((4, 4, 3), 255, dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)
        img_bytes = buf.tobytes()
        # 第一次 None，第二次返回描述
        mock_vlm = AsyncMock(side_effect=[None, "一张物理试卷"])
        with patch.object(p, "_call_vlm_heavy", mock_vlm):
            with patch.object(p, "_encode_image", return_value=("aaaa", "image/png")):
                result = await p._describe_with_retry(img_bytes, img_bytes)
            assert result == "一张物理试卷"


# ════════════════════════════════════════
# 常量回归
# ════════════════════════════════════════

class TestConstants:
    def test_pipeline_status_values(self):
        assert PipelineStatus.PROCESSING == "processing"
        assert PipelineStatus.RENDERED == "rendered"
        assert PipelineStatus.FAILED == "failed"
        assert PipelineStatus.ACTIVE == "active"

    def test_color_to_rgb_covers_known_colors(self):
        for c in ("black", "red", "blue", "green", "other"):
            assert c in _COLOR_TO_RGB
            rgb = _COLOR_TO_RGB[c]
            assert all(0 <= v <= 255 for v in rgb)

    def test_error_code_has_cv2_unavailable(self):
        assert ErrorCode.CV2_UNAVAILABLE == "cv2_unavailable"

    def test_failed_stage_describe_soft(self):
        # describe 是软失败（字符串值正确）
        assert FailedStage.DESCRIBE == "describe"
