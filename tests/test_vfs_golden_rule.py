"""
P1.2 Golden Rule 单元测试

验证 workspace 段重复检测：
- base_path 以 workspace/ 结尾 + vfs_path 以 workspace/ 开头 = 违规
- base_path 不含 workspace = 正常
- vfs_path 不以 workspace/ 开头 = 正常
- 宽容模式（默认）只 warning 不 raise
- 严格模式 raise ValueError
"""
import logging
import pytest

from services.vfs.paths import check_golden_rule, GOLDEN_RULE_STRICT


class TestGoldenRuleDetection:
    """Golden Rule 违规检测。"""

    def test_normal_path_no_violation(self):
        """标准路径：base 不含 workspace，vfs 以 workspace/ 开头 = OK"""
        result = check_golden_rule("feclaw/agents/abc1/", "workspace/foo")
        assert result is None

    def test_normal_path_no_workspace(self):
        """标准路径：两者都不含 workspace = OK"""
        result = check_golden_rule("feclaw/agents/abc1/", "agent/config.md")
        assert result is None

    def test_violation_base_ends_with_workspace(self):
        """违规：base 以 workspace/ 结尾，vfs 以 workspace/ 开头"""
        result = check_golden_rule("feclaw/agents/abc1/workspace/", "workspace/foo")
        assert result is not None
        assert "Golden Rule violation" in result

    def test_violation_base_ends_with_workspace_no_slash(self):
        """违规：base 以 workspace 结尾（无尾斜杠），vfs 以 workspace/ 开头"""
        result = check_golden_rule("feclaw/agents/abc1/workspace", "workspace/foo")
        assert result is not None

    def test_no_violation_when_vfs_not_workspace(self):
        """base 含 workspace 但 vfs 不以 workspace/ 开头 = OK"""
        result = check_golden_rule("feclaw/agents/abc1/workspace/", "foo.txt")
        assert result is None

    def test_no_violation_empty_vfs(self):
        """vfs 为空字符串 = OK"""
        result = check_golden_rule("feclaw/agents/abc1/workspace/", "")
        assert result is None


class TestGoldenRuleLenientMode:
    """宽容模式（默认）：只 warning 不 raise。"""

    def test_lenient_mode_logs_warning(self, caplog):
        """宽容模式下违规只产生 warning 日志"""
        with caplog.at_level(logging.WARNING, logger="services.vfs.paths"):
            result = check_golden_rule("feclaw/agents/abc1/workspace/", "workspace/foo")
        assert result is not None
        assert any("Golden Rule violation" in record.message for record in caplog.records)

    def test_lenient_mode_does_not_raise(self):
        """宽容模式下不 raise"""
        # GOLDEN_RULE_STRICT 默认为 False
        assert not GOLDEN_RULE_STRICT
        # 不应该 raise
        check_golden_rule("feclaw/agents/abc1/workspace/", "workspace/foo")


class TestGoldenRuleStrictMode:
    """严格模式：raise ValueError。"""

    def test_strict_mode_raises(self):
        """严格模式下违规 raise ValueError"""
        import services.vfs.paths as paths_module
        original = paths_module.GOLDEN_RULE_STRICT
        paths_module.GOLDEN_RULE_STRICT = True
        try:
            with pytest.raises(ValueError, match="Golden Rule violation"):
                check_golden_rule("feclaw/agents/abc1/workspace/", "workspace/foo")
        finally:
            paths_module.GOLDEN_RULE_STRICT = original

    def test_strict_mode_normal_path_still_ok(self):
        """严格模式下正常路径不 raise"""
        import services.vfs.paths as paths_module
        original = paths_module.GOLDEN_RULE_STRICT
        paths_module.GOLDEN_RULE_STRICT = True
        try:
            result = check_golden_rule("feclaw/agents/abc1/", "workspace/foo")
            assert result is None
        finally:
            paths_module.GOLDEN_RULE_STRICT = original