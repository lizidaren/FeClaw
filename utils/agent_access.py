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
from utils.auth_dependencies import get_current_user

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

def user_owns_agent(db: Session, agent_hash: str, user_id) -> bool:
    """该 agent 是否属于该用户 —— 全仓唯一判据。

    比较用 `str()` 归一，兼容 user_id 为 int / str 两种来源。
    """
    if not agent_hash:
        return False
    agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
    return agent is not None and str(agent.user_id) == str(user_id)


async def get_authorized_agent_hash(
    request: Request,
    agent_hash: str = Query(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> str:
    """FastAPI 依赖：解析 agent hash 并校验归属，返回**已授权**的 hash。

    VFS 文件接口必须 `Depends(this)` 后直接使用返回值，不要再自行解析或校验。

    Raises:
        HTTPException(400): 参数与域名都解析不出 hash
        HTTPException(403): agent 不存在 或 不属于当前用户（两者同一响应，防枚举）
    """
    resolved = resolve_agent_hash(request, agent_hash)
    if not resolved:
        raise HTTPException(status_code=400, detail="Invalid agent hash from domain")
    if not user_owns_agent(db, resolved, user.id):
        # 故意不区分「不存在」与「不属于你」
        raise HTTPException(status_code=403, detail="无权访问该 Agent")
    return resolved


__all__ = [
    "HASH_MIN_LEN",
    "HASH_MAX_LEN",
    "extract_hash_from_host",
    "get_request_domain",
    "resolve_agent_hash",
    "user_owns_agent",
    "get_authorized_agent_hash",
]
