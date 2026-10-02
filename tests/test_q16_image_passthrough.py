"""
Q16 验证测试：deepseek-flash 多模态标记 + 图片直通（vision→image block，非 vision→预描述兜底）

覆盖：
- 注册表：deepseek-flash 标记 supports_vision；deepseek-v4-flash 别名指向同一能力
- ChatService._build_messages：image_data_url 存在时产出 multimodal image block
- WebChannelService 图片分支：vision ⇒ 跳过 describe_image*，携带 image_data_url；
  非 vision ⇒ 回退 describe_image*，不携带 image_data_url
"""

import base64
import io

import pytest

from services.model_registry import (
    model_supports_vision,
    main_model_supports_vision,
    resolve,
)


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_deepseek_flash_supports_vision():
    info = resolve("deepseek-flash")
    assert info["provider"] == "deepseek"
    assert info["supports_vision"] is True
    assert info["supports_thinking"] is True
    assert model_supports_vision("deepseek-flash") is True


def test_deepseek_v4_flash_alias_still_resolves_vision():
    info = resolve("deepseek-v4-flash")
    assert info["_alias_of"] == "deepseek-flash"
    # back-compat 别名也必须按「支持视觉」处理，漏改的调用不至于炸
    assert info["supports_vision"] is True
    assert model_supports_vision("deepseek-v4-flash") is True


def test_nonvision_model_returns_false():
    assert model_supports_vision("glm-4.7") is False
    assert model_supports_vision("qwen3.6-flash") is False


def test_build_messages_emits_image_block():
    from services.chat_service import ChatService

    svc = object.__new__(ChatService)
    svc.agent_hash = "test"
    svc._pending_correction = None

    class _Ctx:
        history = []

    svc.context = _Ctx()

    messages = svc._build_messages(
        "sys", "这张图是什么？", "data:image/png;base64,AAAA"
    )
    last = messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    kinds = [c.get("type") for c in last["content"]]
    assert kinds == ["text", "image_url"]
    img_block = last["content"][1]
    assert img_block["image_url"]["url"].startswith("data:image/png;base64,")


def test_build_messages_no_image_stays_text():
    from services.chat_service import ChatService

    svc = object.__new__(ChatService)
    svc.agent_hash = "test"
    svc._pending_correction = None

    class _Ctx:
        history = []

    svc.context = _Ctx()

    messages = svc._build_messages("sys", "普通文本")
    assert messages[-1] == {"role": "user", "content": "普通文本"}


# ─── WebChannelService 图片分支（mock 驱动） ───

async def _drive_image_branch(monkeypatch, vision_ok: bool):
    """驱动 WebChannelService.chat_stream 的图片分支，返回 (describe_called, image_data_url)。"""
    from services import web_channel_service as wcs

    describe_calls = []

    async def _fake_desc(*args, **kwargs):
        describe_calls.append(args[0][:4] if args else b"")
        return "预识别描述"

    monkeypatch.setattr(
        "services.image_describer.describe_image_4d", _fake_desc
    )
    monkeypatch.setattr(
        "services.image_describer.describe_image_3d", _fake_desc
    )
    monkeypatch.setattr(
        "services.model_registry.main_model_supports_vision", lambda: vision_ok
    )

    async def _fake_download(image_url, user_id, agent_hash=None):
        return "/workspace/images/t.png", PNG_1PX

    monkeypatch.setattr(wcs, "_download_and_save_image_to_vfs", _fake_download)

    captured = {}

    class _FakeChat:
        def __init__(self, **kw):
            pass

        async def chat(self, input=None, **kw):
            captured["attachments"] = [
                (a.type, a.url, a.image_data_url) for a in (input.attachments or [])
            ]
            if False:
                yield None

    monkeypatch.setattr(wcs, "ChatService", _FakeChat)

    svc = wcs.WebChannelService.__new__(wcs.WebChannelService)
    svc.user_id = 1
    svc._agent_hash = "abcd"
    svc._agent = None
    svc.channel = "web"

    # 非 vision 回退路径会查 AgentProfile.sr_enabled 决定 4D/3D —— 给个假 DB
    class _FakeAgent:
        sr_enabled = True  # True ⇒ 3D；False ⇒ 4D

    class _FakeQuery:
        def filter(self, *a):
            return self

        def first(self):
            return _FakeAgent()

    class _FakeDb:
        def query(self, *a):
            return _FakeQuery()

    svc.db = _FakeDb()

    class _Session:
        messages = "[]"
        session_id = "s1"

    svc.get_or_create_session = lambda *a, **k: _Session()
    svc._get_owned_agent = lambda: type("A", (), {"hash": "abcd"})()
    svc._is_im_agent = lambda: False
    svc.add_message = lambda *a, **k: None
    svc._parse_messages = lambda *a, **k: []

    events = []
    async for ev in svc.chat_stream(
        user_input="看图", image_url="data:image/png;base64,AAAA", channel="web"
    ):
        events.append(ev)

    att = captured.get("attachments") or []
    image_data_url = att[0][2] if att else None
    return bool(describe_calls), image_data_url, events


def test_web_channel_vision_skips_pre_description(monkeypatch):
    import asyncio
    describe_called, image_data_url, _events = asyncio.run(_drive_image_branch(monkeypatch, True))
    assert describe_called is False, "vision 模型不应调用 describe_image*"
    assert image_data_url and image_data_url.startswith("data:image/png;base64,")


def test_web_channel_nonvision_falls_back_to_pre_description(monkeypatch):
    import asyncio
    describe_called, image_data_url, _events = asyncio.run(_drive_image_branch(monkeypatch, False))
    assert describe_called is True, "非 vision 模型应回退到 describe_image*"
    assert image_data_url is None
