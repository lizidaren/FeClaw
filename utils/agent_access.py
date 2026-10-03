"""
Agent 访问控制 —— 单点判定（Q12）

本模块只回答一个问题：**这个登录用户，能不能碰这个 agent？**

判据（截至 Q12，读代码后的口径）：
    `AgentProfile.user_id == 登录用户 id`

为什么不是「群 / 组织」语义：现有代码中**不存在**把 agent 工作区共享给其他人类
用户的机制 ——
  - `models/group.py` + `services/group_service.py`：群只把 agent 拉进来做群内
    dispatch（Agent 之间互相调用），不授予人类用户对该 agent VFS 的访问；
    且 `routers/group.py:add_member` 强制要求 `agent.user_id == 群主 user_id`。
  - `models/organization.py`：组织共享的是 `.ref/` 知识库（跨群），与 agent
    工作区无关。

因此本模块把原先散落在 `routers/feclaw_domain.py` 的归属校验收敛为**唯一入口**，
供页面路由与全部 VFS 文件接口复用（不要在别处再手写一套）。

越权返回值约定：
  - 无法解析出 agent hash       → 400（沿用旧约定）
  - agent 不存在 / 不属于本人   → **统一 403**，两者响应完全一致，
    避免攻击者用响应码差异枚举他人 agent hash。
"""

from typing import Optional

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from config import settings
from models.agent_profile import AgentProfile
from models.database import User, get_db
from utils.auth import (
    decode_jwt_token,
    is_token_revoked,
    user_id_from_payload,
    token_type,
    TOKEN_TYPE_SESSION,
    TOKEN_TYPE_TOTP,
)

# hash 合法长度区间（hex 字符数）。
# 统一口径：老 agent 为 4 位；`services/agent_init_service.create_agent` 新建为 8 位；
# DB 列 `agent_profiles.hash` 为 String(8)。
HASH_MIN_LEN = 4
HASH_MAX_LEN = 8


# ────────────────────────────────────────────────────────────────────
# 域名 / hash 解析
# ────────────────────────────────────────────────────────────────────

def _allowed_domain_suffixes() -> list:
    """允许的域名后缀（防止 X-Forwarded-Host 头注入），从 FECLAW_PUBLIC_URL 推导。"""
    domain = settings.FECLAW_PUBLIC_URL
    if domain:
        return [f".{domain}", domain]
    return []


def extract_hash_from_host(host: str) -> Optional[str]:
    """从域名提取 agent hash，如 b92d.feclaw.chat → b92d。

    统一接受 **4–8 位 hex**（老 agent 4 位，新 agent 8 位），与前端
    `Auth.parseAgentHash` 口径一致。
    """
    if not host:
        return None
    parts = host.split(".")
    if len(parts) >= 3 and HASH_MIN_LEN <= len(parts[0]) <= HASH_MAX_LEN:
        try:
            int(parts[0], 16)
            return parts[0]
        except ValueError:
            pass
    return None


def get_request_domain(request: Request) -> str:
    """获取请求域名，优先 X-Forwarded-Host（CDN 代理），回退 Host。

    对 X-Forwarded-Host 做白名单校验，防止头注入攻击。
    """
    forwarded = request.headers.get("X-Forwarded-Host", "")
    if forwarded:
        domain = forwarded.split(",")[0].strip()
        allowed = _allowed_domain_suffixes()
        if any(domain == suffix or domain.endswith(suffix) for suffix in allowed):
            return domain
        return request.headers.get("host", "")
    return request.headers.get("host", "")


def resolve_agent_hash(request: Request, agent_hash: str = "") -> Optional[str]:
    """确定本次请求作用的 agent hash：显式 query 参数优先，其次域名子域。

    注意：两者都属于**未校验的输入**，调用方必须再过 `user_owns_agent` /
    `get_authorized_agent_hash` 才能使用。
    """
    if agent_hash:
        return agent_hash
    return extract_hash_from_host(get_request_domain(request))


# ────────────────────────────────────────────────────────────────────
# 归属判定（唯一判据入口）
# ────────────────────────────────────────────────────────────────────

def agent_belongs_to_user(agent, user_id) -> bool:
    """归属比较的**唯一实现**：已取到 agent 对象时，`str()` 归一比较 user_id。

    兼容 user_id 为 int / str 两种来源（`workspace_service` 等存 `str(user_id)`，
    原始 `==` 会判错）。agent 为 None（不存在）或 user_id 不匹配时返回 False。
    任何「agent 归属判定」都必须经本函数或 `user_owns_agent`，勿再手写 `==`。
    """
    return agent is not None and str(agent.user_id) == str(user_id)


def user_owns_agent(db: Session, agent_hash: str, user_id) -> bool:
    """该 agent 是否属于该用户 —— 全仓唯一判据（按 hash 查询）。

    委托 `agent_belongs_to_user` 做 `str()` 归一比较，兼容 user_id 为 int / str。
    """
    if not agent_hash:
        return False
    agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
    return agent_belongs_to_user(agent, user_id)


def generate_agent_hash(db: Session, *, length_bytes: int = 4, max_attempts: int = 100) -> str:
    """生成**唯一**的 agent hash（hex）—— 全仓唯一生成入口。

    新 agent 口径为 8 位（`length_bytes=4`）；`services/totp_service.create_agent`
    是仅被测试引用的老入口，仍传 `length_bytes=2`（4 位），行为保持不变。
    碰撞时重试，超过 `max_attempts` 抛 `ValueError`。
    """
    import secrets

    for _ in range(max_attempts):
        candidate = secrets.token_hex(length_bytes)
        if db.query(AgentProfile).filter(AgentProfile.hash == candidate).first() is None:
            return candidate
    raise ValueError("Failed to generate unique hash")


# ────────────────────────────────────────────────────────────────────
# token → user 解析（FIX-A：session 全量 / totp 按 Agent 作用域）
# ────────────────────────────────────────────────────────────────────

def extract_agent_token(request: Request) -> Optional[str]:
    """提取 Agent 作用域可用的 token：Bearer header → `feclaw_jwt` cookie →
    `feclaw_jwt_totp_{agent_hash}` cookie（按请求子域名）。"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    token = request.cookies.get("feclaw_jwt")
    if token:
        return token
    agent_hash = extract_hash_from_host(get_request_domain(request))
    if agent_hash:
        totp_token = request.cookies.get(f"feclaw_jwt_totp_{agent_hash}")
        if totp_token:
            return totp_token
    return None


def resolve_user_id_from_token(token: str, db: Optional[Session] = None) -> Optional[int]:
    """token → FeClaw 本地 user_id（**含登出吊销**）。

    - Platform 格式 token → UserLink 映射（不把 Platform id 当本地 id）
    - session（全量） / totp（Agent 作用域）→ 吊销校验后返回 user_id
    - agent / refresh → 拒绝（None）
    """
    raw = decode_jwt_token(token)
    if raw is None:
        return None

    from utils.oauth_helpers import is_platform_format_token, resolve_user_from_platform
    if is_platform_format_token(raw):
        provider_uid = raw.get("user_id") or raw.get("sub")
        if provider_uid is None:
            return None
        own_session = db is None
        session = db
        if own_session:
            from models.database import SessionLocal
            session = SessionLocal()
        try:
            user = resolve_user_from_platform(session, str(provider_uid))
            return user.id if user else None
        finally:
            if own_session:
                session.close()

    typ = token_type(raw)
    if typ not in (TOKEN_TYPE_SESSION, TOKEN_TYPE_TOTP):
        return None
    if is_token_revoked(raw, db=db):
        return None
    return user_id_from_payload(raw)


def totp_scoped_agent_hash(token: Optional[str]) -> Optional[str]:
    """token 为 totp 类型时返回其 `agent_hash`（作用域）；session/其他 → None。"""
    if not token:
        return None
    raw = decode_jwt_token(token)
    if raw is None:
        return None
    if token_type(raw) == TOKEN_TYPE_TOTP:
        return raw.get("agent_hash")
    return None


def require_totp_scope(request: Request, agent_hash: str) -> None:
    """断言：若请求 token 是 totp（Agent 作用域），其 agent_hash 必须等于目标 agent。

    用于 `/api/chat/*` 等「目标 agent 在服务层解析」的接口 —— 防止 totp 令牌
    越界访问同用户的其他 Agent。
    """
    scope = totp_scoped_agent_hash(extract_agent_token(request))
    if scope is not None and scope != agent_hash:
        raise HTTPException(status_code=403, detail="Token scoped to a different agent")


def require_agent_owner(db: Session, agent_hash: str, user) -> None:
    """Q19：断言 agent 归属；不满足抛 403（与 `get_authorized_agent_hash` 同一口径）。

    供那些**已经手动解析出 agent_hash**（如请求体 / 非域名来源）却尚未校验归属的
    接口复用（sandbox.py / vfs_view.py 等），避免各处再手写 `if str(...) != str(...)`。
    """
    if not agent_hash or not user_owns_agent(db, agent_hash, user.id):
        raise HTTPException(status_code=403, detail="无权访问该 Agent")


async def get_authorized_agent_hash(
    request: Request,
    agent_hash: str = Query(""),
    db: Session = Depends(get_db),
) -> str:
    """FastAPI 依赖：解析 agent hash 并校验归属，返回**已授权**的 hash。

    VFS 文件接口必须 `Depends(this)` 后直接使用返回值，不要再自行解析或校验。

    FIX-A：认证从「只认 session」放宽为「session（全量）或 totp（**仅限该 agent**）」，
    让「分享一个 Agent」的用户（只有 totp 令牌）仍能读写该 Agent 的文件；
    但 totp 令牌不能越界碰其他 Agent（`agent_hash` 作用域校验）。

    Raises:
        HTTPException(400): 参数与域名都解析不出 hash
        HTTPException(401): 无 token / token 无效 / 类型不对
        HTTPException(403): agent 不存在 或 不属于当前用户（两者同一响应，防枚举），
                           或 totp 令牌作用域与目标 agent 不一致
    """
    # 先认证（无 token ⇒ 401），保持「匿名必 401」的既有契约 —— 否则会退化成 400
    # 的存在性/参数 oracle，破坏 tests/test_q21_route_authz.py 的匿名 401 断言。
    token = extract_agent_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = resolve_user_id_from_token(token, db=db)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")

    resolved = resolve_agent_hash(request, agent_hash)
    if not resolved:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")

    scope = totp_scoped_agent_hash(token)
    if scope is not None and scope != resolved:
        raise HTTPException(status_code=403, detail="Token scoped to a different agent")

    if not user_owns_agent(db, resolved, user_id):
        # 故意不区分「不存在」与「不属于你」
        raise HTTPException(status_code=403, detail="无权访问该 Agent")
    return resolved


async def get_agent_scoped_user_id(
    request: Request,
    db: Session = Depends(get_db),
) -> int:
    """FastAPI 依赖：返回已认证的 user_id（session 全量 或 totp 作用域），**不含**归属校验。

    供 `/api/chat/*` 使用 —— 归属校验由 WebChannelService.resolve_agent 内部完成；
    totp 的 Agent 作用域约束由调用方再用 `require_totp_scope` 断言。

    Raises:
        HTTPException(401): 无 token / token 无效 / 类型不对
    """
    token = extract_agent_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = resolve_user_id_from_token(token, db=db)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    return user_id


__all__ = [
    "HASH_MIN_LEN",
    "HASH_MAX_LEN",
    "extract_hash_from_host",
    "get_request_domain",
    "resolve_agent_hash",
    "user_owns_agent",
    "agent_belongs_to_user",
    "generate_agent_hash",
    "require_agent_owner",
    "get_authorized_agent_hash",
    "extract_agent_token",
    "resolve_user_id_from_token",
    "totp_scoped_agent_hash",
    "require_totp_scope",
    "get_agent_scoped_user_id",
]
