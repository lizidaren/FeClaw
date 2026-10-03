"""
M3 收敛回归测试（FIX-H）

- `SlidingWindowLimiter` 并发下计数精确（无锁 dict 会漏计 → 注入缺陷报红）。
- 登录限流单点：`routers.user` 已删除内联无锁 dict，复用
  `services.rate_limiter.login_limiter`。
"""

from concurrent.futures import ThreadPoolExecutor

from services.rate_limiter import SlidingWindowLimiter, login_limiter


class TestSlidingWindowLimiterConcurrency:
    def test_concurrent_counts_exactly(self):
        # 注入缺陷：无锁 dict 并发下会漏计，allowed 会 > max_attempts（报红）
        limiter = SlidingWindowLimiter(max_attempts=10, window_seconds=300)
        key = "concurrent-key"
        with ThreadPoolExecutor(max_workers=50) as ex:
            results = list(ex.map(limiter.is_limited, [key] * 100))
        allowed = sum(1 for r in results if r is False)
        limited = sum(1 for r in results if r is True)
        assert allowed == 10
        assert limited == 90


class TestLoginRateLimitSinglePoint:
    def test_login_limiter_enforces_10_per_window(self):
        login_limiter.clear()
        key = "192.0.2.1:alice"
        for _ in range(10):
            assert login_limiter.is_limited(key) is False
        assert login_limiter.is_limited(key) is True

    def test_user_router_uses_single_point(self):
        # 注入缺陷：若 router 重新内联无锁 dict，此断言会报红（符号消失 / 引用不对）
        import routers.user as user_mod

        assert not hasattr(user_mod, "_login_attempts")
        assert not hasattr(user_mod, "_login_rate_limited")
        assert user_mod._login_limiter is login_limiter
