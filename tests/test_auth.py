"""
P0.4 密码 hash 迁移测试

覆盖：
1. 新用户 → hash 以 $2b$ 开头（bcrypt）
2. 老用户 legacy SHA-256 hash 仍可 verify（向后兼容）
3. 错密码拒绝
4. needs_rehash 正确识别 legacy 前缀
5. 透明懒迁移：login 成功后 legacy 自动升级到 bcrypt

这些测试是纯单元测试，不依赖 DB（hash/verify/rehash 是无状态函数）。
"""
import hashlib
import pytest

from utils.auth import (
    hash_password,
    verify_password,
    needs_rehash,
    generate_salt,
    _LEGACY_PREFIX,
)


class TestNewUserBcrypt:
    """用例 1：新用户走 bcrypt，salt 嵌入 hash 本身。"""

    def test_hash_starts_with_bcrypt_prefix(self):
        """新 hash 必须以 $2b$ 开头（bcrypt 默认 cost=12）。"""
        h = hash_password("hello-world-123")
        assert h.startswith("$2b$"), f"expected bcrypt prefix, got: {h[:20]}"

    def test_verify_succeeds_with_correct_password(self):
        """正确密码能通过 verify。"""
        h = hash_password("correct-password")
        assert verify_password("correct-password", h) is True

    def test_hash_is_different_each_call(self):
        """bcrypt 每次用随机 salt，相同明文 → 不同 hash（防彩虹表）。"""
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2

    def test_bcrypt_variants_accepted(self):
        """$2a$ / $2b$ / $2y$ 三种 bcrypt 前缀都能 verify（兼容历史）。"""
        import bcrypt
        # 模拟一个 $2a$ hash
        h = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4, prefix=b"2a")).decode()
        assert h.startswith("$2a$")
        assert verify_password("pw", h) is True


class TestLegacySsha256Compat:
    """用例 2：legacy SHA-256 自描述前缀仍可 verify（向后兼容）。"""

    def test_legacy_hash_format(self):
        """legacy hash 形如 $sha256v1$<salt>$<sha256hex>。"""
        salt = generate_salt()
        legacy = hash_password("legacy-pw", salt)
        assert legacy.startswith(_LEGACY_PREFIX)
        parts = legacy.split("$")
        # "$sha256v1$<salt>$<hash>".split("$") → ['', 'sha256v1', salt, hash]（首位空串因前缀 $）
        assert len(parts) == 4
        assert parts[0] == ""              # 前导 $ 切出空串
        assert parts[1] == "sha256v1"      # 版本标记
        assert parts[2] == salt            # salt 段
        assert parts[3] == hashlib.sha256(("legacy-pw" + salt).encode()).hexdigest()

    def test_legacy_verify_succeeds(self):
        """legacy hash 用对应 salt + 正确密码能 verify。"""
        salt = generate_salt()
        legacy = hash_password("legacy-pw", salt)
        assert verify_password("legacy-pw", legacy) is True

    def test_legacy_wrong_password_rejected(self):
        salt = generate_salt()
        legacy = hash_password("legacy-pw", salt)
        assert verify_password("wrong", legacy) is False


class TestNeedsRehash:
    """用例 3：needs_rehash 正确识别 legacy 前缀。"""

    def test_legacy_needs_rehash(self):
        salt = generate_salt()
        legacy = hash_password("x", salt)
        assert needs_rehash(legacy) is True

    def test_bcrypt_does_not_need_rehash(self):
        h = hash_password("x")
        assert needs_rehash(h) is False

    def test_empty_does_not_need_rehash(self):
        assert needs_rehash("") is False


class TestWrongPasswordRejected:
    """用例 4：错密码无论新旧格式都拒绝。"""

    def test_bcrypt_wrong_password(self):
        h = hash_password("right")
        assert verify_password("wrong", h) is False

    def test_empty_hash_returns_false(self):
        """空 hash 必须 False（防止 NoneType 异常）。"""
        assert verify_password("anything", "") is False
        assert verify_password("anything", None) is False

    def test_unknown_format_returns_false(self):
        """未知前缀的 hash 安全降级为 False。"""
        assert verify_password("x", "plaintext-no-prefix") is False
        assert verify_password("x", "$unknown$abc$def") is False


class TestTransparentRehashFlow:
    """用例 5：模拟 login 流程 — legacy 用户登录成功自动升级到 bcrypt。

    这模拟 routers/user.py login 流程的核心逻辑（hash + verify + 条件 rehash）。
    """

    def test_legacy_login_triggers_upgrade(self):
        salt = generate_salt()
        legacy_hash = hash_password("user-pw", salt)

        # 模拟 login：先 verify
        assert verify_password("user-pw", legacy_hash) is True
        assert needs_rehash(legacy_hash) is True

        # 透明懒迁移：用同一明文重新 hash（bcrypt）
        upgraded = hash_password("user-pw")
        assert upgraded.startswith("$2b$")
        assert upgraded != legacy_hash  # 旧 hash 与新 hash 不同

        # 升级后 verify 仍通过
        assert verify_password("user-pw", upgraded) is True

        # 升级后 needs_rehash = False（避免重复 rehash）
        assert needs_rehash(upgraded) is False

    def test_bcrypt_login_skips_rehash(self):
        """已是 bcrypt 的用户登录不会触发 rehash。"""
        h = hash_password("user-pw")
        assert verify_password("user-pw", h) is True
        assert needs_rehash(h) is False
        # 模拟 login 看到 needs_rehash=False → 跳过 upgrade → 二次登录不重复 rehash