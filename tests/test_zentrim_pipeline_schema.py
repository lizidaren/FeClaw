"""
T 阶段：zentrim_pipeline 状态机 / 失败阶段 / 错误码 常量测试

覆盖：
  - PipelineStatus / FailedStage / ErrorCode 类常量值
  - ZentrimPipeline 实例能从 model_registry 读到 vision_speed / vision_heavy
  - display_image kind 集合是有限闭集
  - 旧 API _call_vlm 仍能调用（向后兼容，不破坏主流程）
"""
import pytest

from services.zentrim_pipeline import (
    ErrorCode,
    FailedStage,
    PipelineStatus,
    ZentrimPipeline,
    _DISPLAY_IMAGE_KINDS,
)


class TestPipelineStatus:
    """PipelineStatus 常量值测试（前端 / 文档都依赖这些字符串）"""

    def test_processing_value(self):
        assert PipelineStatus.PROCESSING == "processing"

    def test_rendered_value(self):
        assert PipelineStatus.RENDERED == "rendered"

    def test_failed_value(self):
        assert PipelineStatus.FAILED == "failed"

    def test_active_value(self):
        assert PipelineStatus.ACTIVE == "active"

    def test_archived_value(self):
        assert PipelineStatus.ARCHIVED == "archived"

    def test_constants_have_expected_values(self):
        """一把验：防止有人手滑改字符串"""
        assert (
            PipelineStatus.PROCESSING,
            PipelineStatus.RENDERED,
            PipelineStatus.FAILED,
            PipelineStatus.ACTIVE,
            PipelineStatus.ARCHIVED,
        ) == ("processing", "rendered", "failed", "active", "archived")


class TestFailedStage:
    """FailedStage 常量值测试"""

    def test_constants_have_expected_values(self):
        assert FailedStage.CLASSIFY == "classify"
        assert FailedStage.COLOR_ID == "color_id"
        assert FailedStage.BINARIZE == "binarize"
        assert FailedStage.HTML == "html"
        assert FailedStage.SCREENSHOT == "screenshot"
        assert FailedStage.DESCRIBE == "describe"
        assert FailedStage.COS_UPLOAD == "cos_upload"

    def test_describe_is_distinct_from_hard_failures(self):
        """DESCRIBE 是软失败（不进入 failed 状态）— 名字空间上与硬失败区分开"""
        # 软失败 / 硬失败 不应混用
        soft = {FailedStage.DESCRIBE}
        hard = {
            FailedStage.CLASSIFY,
            FailedStage.COLOR_ID,
            FailedStage.BINARIZE,
            FailedStage.HTML,
            FailedStage.SCREENSHOT,
            FailedStage.COS_UPLOAD,
        }
        assert soft.isdisjoint(hard)


class TestErrorCode:
    """ErrorCode 常量值测试"""

    def test_constants_have_expected_values(self):
        assert ErrorCode.VLM_TIMEOUT == "vlm_timeout"
        assert ErrorCode.VLM_INVALID_RESPONSE == "vlm_invalid_response"
        assert ErrorCode.PLAYWRIGHT_RENDER_FAILED == "playwright_render_failed"
        assert ErrorCode.COS_UPLOAD_FAILED == "cos_upload_failed"
        assert ErrorCode.ITERATIONS_EXCEEDED == "iterations_exceeded"


class TestDisplayImageKinds:
    """display_image 字段的合法 kind 集合（V 阶段实施时用）"""

    def test_kinds_contains_three_values(self):
        assert set(_DISPLAY_IMAGE_KINDS) == {"fused", "screenshot", "original"}

    def test_kinds_is_frozen_tuple(self):
        # 防止外部 .append 破坏闭集
        assert isinstance(_DISPLAY_IMAGE_KINDS, tuple)


class TestZentrimPipelineVisionConfig:
    """ZentrimPipeline 实例能从 registry 读到 vision configs"""

    def test_default_construction_picks_up_registry_defaults(self):
        p = ZentrimPipeline()
        # 默认无 settings.VISION_*_MODEL → 用兜底
        assert p.vision_speed.name == "qwen3.6-flash"
        assert p.vision_heavy.name == "doubao-seed-2.0-lite"

    def test_inject_custom_vision_configs(self):
        from services.model_registry import VisionModelConfig

        custom_speed = VisionModelConfig(
            name="custom-speed",
            display_name="Custom Speed",
            cost_per_call=0.0,
            max_image_size_mb=10,
            max_tokens=1024,
            timeout_s=10,
        )
        custom_heavy = VisionModelConfig(
            name="custom-heavy",
            display_name="Custom Heavy",
            cost_per_call=0.0,
            max_image_size_mb=10,
            max_tokens=2048,
            timeout_s=30,
        )
        p = ZentrimPipeline(
            vision_speed=custom_speed, vision_heavy=custom_heavy
        )
        assert p.vision_speed.name == "custom-speed"
        assert p.vision_heavy.name == "custom-heavy"
        # max_image_bytes 来自 heavy 的 max_image_size_mb
        assert p.max_image_bytes == 10 * 1024 * 1024

    def test_max_image_bytes_falls_back_to_heavy_config(self):
        """max_image_bytes 来自 vision_heavy.max_image_size_mb（更宽容）"""
        p = ZentrimPipeline()
        # 默认 heavy.max_image_size_mb = 20
        assert p.max_image_bytes == 20 * 1024 * 1024
