"""
T 阶段：VisionModelConfig + get_vision_speed() / get_vision_heavy() 单元测试

覆盖：
  - 兜底默认（settings 为空）
  - 注入式覆盖（settings 有值时换 name）
  - 显式重置 settings 字段（不污染其他测试）
"""
import pytest

from services.model_registry import (
    VisionModelConfig,
    get_vision_speed,
    get_vision_heavy,
)


class TestVisionModelConfig:
    """VisionModelConfig dataclass 基础属性"""

    def test_dataclass_fields(self):
        cfg = VisionModelConfig(
            name="x",
            display_name="X",
            cost_per_call=0.1,
            max_image_size_mb=10,
            max_tokens=1024,
            timeout_s=30,
        )
        assert cfg.name == "x"
        assert cfg.display_name == "X"
        assert cfg.cost_per_call == 0.1
        assert cfg.max_image_size_mb == 10
        assert cfg.max_tokens == 1024
        assert cfg.timeout_s == 30

    def test_dataclass_is_hashable_by_default_via_eq(self):
        # dataclass 默认 eq=True，fields 相等即相等
        a = VisionModelConfig("n", "d", 0.1, 10, 1024, 30)
        b = VisionModelConfig("n", "d", 0.1, 10, 1024, 30)
        assert a == b


class TestGetVisionSpeed:
    """get_vision_speed() 单元测试"""

    def setup_method(self):
        """每个 case 之前清掉 settings 上的 VISION_*_MODEL（防止其他测试污染）"""
        from config import settings
        self._original_speed = getattr(settings, "VISION_SPEED_MODEL", None)
        settings.VISION_SPEED_MODEL = ""

    def teardown_method(self):
        from config import settings
        settings.VISION_SPEED_MODEL = self._original_speed

    def test_returns_default_when_not_configured(self):
        """settings.VISION_SPEED_MODEL 为空 → 兜底默认（warning 已记日志）"""
        cfg = get_vision_speed()
        assert isinstance(cfg, VisionModelConfig)
        assert cfg.name == "qwen3.6-flash"
        assert cfg.display_name == "Qwen3.6 Flash"
        assert cfg.timeout_s == 15
        assert cfg.max_image_size_mb == 20
        assert cfg.max_tokens == 2048
        assert cfg.cost_per_call > 0

    def test_returns_overridden_when_configured(self):
        """settings.VISION_SPEED_MODEL 有值 → 换 name，其他字段沿用默认"""
        from config import settings
        settings.VISION_SPEED_MODEL = "glm-4.6v"

        cfg = get_vision_speed()
        assert cfg.name == "glm-4.6v"
        # 其他字段保持兜底默认（display_name 沿用旧 Qwen3.6 Flash）
        assert cfg.timeout_s == 15
        assert cfg.max_tokens == 2048

    def test_whitespace_only_treated_as_unset(self):
        """settings 值为纯空白 → 当作未配置（用兜底）"""
        from config import settings
        settings.VISION_SPEED_MODEL = "   "

        cfg = get_vision_speed()
        assert cfg.name == "qwen3.6-flash"  # 兜底


class TestGetVisionHeavy:
    """get_vision_heavy() 单元测试"""

    def setup_method(self):
        from config import settings
        self._original_heavy = getattr(settings, "VISION_HEAVY_MODEL", None)
        settings.VISION_HEAVY_MODEL = ""

    def teardown_method(self):
        from config import settings
        settings.VISION_HEAVY_MODEL = self._original_heavy

    def test_returns_default_when_not_configured(self):
        """settings.VISION_HEAVY_MODEL 为空 → 兜底默认"""
        cfg = get_vision_heavy()
        assert isinstance(cfg, VisionModelConfig)
        assert cfg.name == "doubao-seed-2.0-lite"
        assert cfg.display_name == "豆包 Seed 2.0 Lite"
        assert cfg.timeout_s == 60
        assert cfg.max_image_size_mb == 20
        assert cfg.max_tokens == 4096
        assert cfg.cost_per_call > 0

    def test_returns_overridden_when_configured(self):
        """settings.VISION_HEAVY_MODEL 有值 → 换 name"""
        from config import settings
        settings.VISION_HEAVY_MODEL = "qwen3-vl-plus"

        cfg = get_vision_heavy()
        assert cfg.name == "qwen3-vl-plus"
        # 兜底的 timeout/tokens 沿用
        assert cfg.timeout_s == 60
        assert cfg.max_tokens == 4096
