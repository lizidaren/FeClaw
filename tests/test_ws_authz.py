"""
FIX-F B1 —— WS 主聊天通道 `/api/chat/ws` 三重校验回归测试

审计（AUDIT-R1）实测：`routers/feclaw_chat.py` 的 WS 端点只 `decode_jwt_token(token)`
+ 判 `payload.get("user_id")`，导致三种令牌都能收到首条 `{"type":"ping"}`（视为鉴权
通过）：
  [C] session token -> 归属 agent     : 放行（正确）
  [T] TOTP token (scoped 8d85) -> 343b : 越界放行（✗ 应为 403）
  [A] agent JWT (typ=agent) -> 8d85    : 类型混淆放行（✗ 应为 401）

本测试用 `TestClient.websocket_connect` 复现三令牌矩阵：越界 / 类型混淆的闭包
必须收到 close 或 error 帧，**不得**收到 `{"type":"ping"}`。同时覆盖 `?token=` 与
`Authorization: Bearer` 两种取法。
"""
import datetime

import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient
from jose import jwt
from starlette.websockets import WebSocketDisconnect

from main import app
from config import settings


@pytest.fixture
def client():
    return TestClient(app)


def _mk(typ=None, user_id=1, **extra):
    """构造 FeClaw HS256 token（与 utils.auth 同一密钥/算法）。"""
    payload = {
        "user_id": user_id,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1),
    }
    if typ is not None:
        payload["typ"] = typ
    payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def _first_event(ws):
    """读 WS 首条消息；服务端直接 close 时返回哨兵（close 也算「被拒」）。"""
    try:
        return ws.receive_json()
    except WebSocketDisconnect:
        return {"type": "__closed__"}


class TestChatWsTokenMatrix:
    def _seed(self, real_db):
        from models.database import User
        from models.agent_profile import AgentProfile
        from utils.auth import hash_password

        db = real_db()
        u = User(username="ws_owner", password_hash=hash_password("pw"), salt=None, is_admin=False)
        db.add(u)
        db.commit()
        uid = u.id
        db.add(AgentProfile(user_id=uid, hash="8d85", totp_secret="X" * 32, name="a", status="pending"))
        db.add(AgentProfile(user_id=uid, hash="343b", totp_secret="Y" * 32, name="b", status="pending"))
        db.commit()
        db.close()
        return uid

    def test_session_token_allowed_to_own_agent(self, real_db, client):
        uid = self._seed(real_db)
        tok = _mk("session", user_id=uid)
        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(f"/api/chat/ws?token={tok}&agent_hash=8d85") as ws:
                msg = _first_event(ws)
        assert msg.get("type") == "ping", msg

    def test_session_token_allowed_via_header(self, real_db, client):
        uid = self._seed(real_db)
        tok = _mk("session", user_id=uid)
        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(
                "/api/chat/ws?agent_hash=8d85",
                headers={"Authorization": f"Bearer {tok}"},
            ) as ws:
                msg = _first_event(ws)
        assert msg.get("type") == "ping", msg

    def test_totp_token_cannot_cross_agent_scope(self, real_db, client):
        uid = self._seed(real_db)
        # TOTP 令牌只授权 8d85，却去连 343b —— 必须被拒
        tok = _mk("totp", auth_method="totp", agent_hash="8d85", user_id=uid)
        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(f"/api/chat/ws?token={tok}&agent_hash=343b") as ws:
                msg = _first_event(ws)
        assert msg.get("type") != "ping", msg

    def test_agent_jwt_rejected(self, real_db, client):
        uid = self._seed(real_db)
        tok = _mk("agent", type="agent_jwt", user_id=uid)
        with patch("routers.feclaw_chat.SessionLocal", real_db):
            with client.websocket_connect(f"/api/chat/ws?token={tok}&agent_hash=8d85") as ws:
                msg = _first_event(ws)
        assert msg.get("type") != "ping", msg
