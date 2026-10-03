"""
FIX-F G2 —— 出站 SSRF：3 处「绕开校验器」的出站接上校验 + 禁重定向

审计（AUDIT-R1 §G2）读到 3 处未走 `utils.url_validation.validate_public_http_url`
的出站，或虽校验却因 aiohttp 默认跟随重定向可被 302→内网绕过：
  1. services/wechat_service.py::_download_cdn_media      —— 无校验
  2. services/wechat/sdk_adapter.py::download_wechat_image_from_media —— 无校验
  3. services/wechat_service.py::download_media            —— 有校验但跟随重定向

本测试验证：内网/云元数据 URL 在发请求前就被拦（不再建会话）；且 download_media
对合法公网 URL 以 allow_redirects=False 发起（fail-closed，302 不被跟随）。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _wechat_service_stub():
    from services.wechat_service import WeChatService
    return object.__new__(WeChatService)


class TestDownloadMediaRedirect:
    def test_relative_cdn_path_disables_redirects(self):
        svc = _wechat_service_stub()
        captured = {}

        class FakeResp:
            status = 200

            async def read(self):
                return b"x"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSession:
            def get(self, url, **kw):
                captured["url"] = url
                captured["kw"] = kw
                return FakeResp()

        async def _fake_get_session(*a, **k):
            return FakeSession()

        svc._get_session = _fake_get_session
        # 以 "/" 开头的 CDN 相对路径（信任基座，跳过公网校验），验证 allow_redirects=False
        asyncio.run(svc.download_media("/media/x.png"))
        assert captured["kw"].get("allow_redirects") is False

    def test_internal_url_blocked_before_request(self):
        svc = _wechat_service_stub()
        called = {}

        async def _fake_get_session(*a, **k):
            called["session"] = True
            raise AssertionError("should not reach session")

        svc._get_session = _fake_get_session
        with pytest.raises(Exception) as exc:
            asyncio.run(svc.download_media("http://127.0.0.1/secret"))
        assert "session" not in called
        assert "SSRF" in str(exc.value)


class TestCdnMediaValidation:
    def test_download_cdn_media_blocks_internal_url(self):
        svc = _wechat_service_stub()
        media = SimpleNamespace(
            encrypt_query_param=None,
            download_url="http://127.0.0.1/secret",
            aes_key=None,
        )
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(svc._download_cdn_media(media))
        assert "SSRF" in str(exc.value)

    def test_download_cdn_media_blocks_metadata_url(self):
        svc = _wechat_service_stub()
        media = SimpleNamespace(
            encrypt_query_param=None,
            download_url="http://169.254.169.254/latest/meta-data/",
            aes_key=None,
        )
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(svc._download_cdn_media(media))
        assert "SSRF" in str(exc.value)


class TestSdkAdapterValidation:
    def test_download_wechat_image_blocks_internal_url(self):
        from services.wechat.sdk_adapter import download_wechat_image_from_media

        media = SimpleNamespace(
            download_url="http://127.0.0.1/evil.png",
            encrypt_query_param=None,
            aes_key=None,
        )
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(download_wechat_image_from_media(media))
        assert "SSRF" in str(exc.value)

    def test_download_wechat_image_blocks_metadata_url(self):
        from services.wechat.sdk_adapter import download_wechat_image_from_media

        media = SimpleNamespace(
            download_url="http://169.254.169.254/latest/meta-data/",
            encrypt_query_param=None,
            aes_key=None,
        )
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(download_wechat_image_from_media(media))
        assert "SSRF" in str(exc.value)
