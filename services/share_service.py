"""
分享链接服务 - 创建安全的文件分享链接
token 使用 share_hash 引用路径，不在 token 中暴露 vfs_path 明文

T17a「分享边界」改造（本线程 S3）：

1. **解析只认映射**：分享解析只用 `ShareMapping` 里已确认的 `agent_hash` / `user_id`
   推导**唯一**存储 key（`agents/<hash>/workspace/...` 或 `user_workspaces/<uid>/workspace/...`）。
   删除 `feclaw/vfs{path}` 全局回退与硬编码 `feclaw/user_workspaces/2/...` 兜底；
   身份缺失/不可解析/与 Agent 归属冲突时 fail closed（404/403），不猜。
2. **主动内容隔离**：`html/htm/xhtml/svg/js/jsxgraph/ggb` 等会被渲染成页面的内容，
   未配置独立分享域（`SHARE_ACTIVE_ORIGIN`）时由路由按附件 + nosniff 返回；
   配置独立域时只 302 到独立域。
3. **默认有效期**：新分享默认 24 小时、只读；`expires_hours=0`（永久）只有显式配置
   `SHARE_ALLOW_PERMANENT=true` 才生效，否则降级为默认 24h。已有 mapping 的过期时间不修改。
4. **密码不再走 query**：新增 `POST /s/{slug}/unlock` → 短时 HMAC 签名 cookie
   （绑定 slug + share_hash，默认约 10 分钟）；`?password=` 只做兼容并标记 deprecated。
5. **引用路径从映射推导**：`load_authorized_reference` / `create_reference_record` 校验
   父 share 存在、未过期、未撤销、密码已验证，并只用 mapping 的路径，禁止任意 `vfs_path`。
"""
import hashlib
import hmac
import time
import base64
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional
from config import settings

logger = logging.getLogger(__name__)

# HMAC 签名密钥（派生自 JWT_SECRET）
_SHARE_SECRET = hashlib.sha256(f"share:{settings.JWT_SECRET}".encode()).digest()

# ── 路径/身份边界常量 ────────────────────────────────────────────────────────
WORKSPACE_DIR = "workspace"
# 租户级存储根：分享路径一旦命中这些首段，说明它试图跨出 mapping 的工作区
_SCOPE_ROOTS = frozenset({"agents", "user_workspaces", "public", "config", "agent"})
# agent hash：老 4 位 / 新 8 位 hex（与 utils.agent_access.HASH_MIN_LEN/MAX_LEN 一致）
_AGENT_HASH_RE = re.compile(r"^[0-9a-f]{4,8}$")
# owner id：DB 为数字，历史数据可能为字符串；只接受安全的短 token
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_REL_PATH_LEN = 480

# 会被浏览器当成页面/脚本执行的扩展名（T17a：未配置独立分享域时禁止内联）
SHARE_ACTIVE_EXTENSIONS = frozenset({
    ".html", ".htm", ".xhtml", ".svg", ".js", ".mjs",
    ".jsxgraph", ".ggb", ".2dggb", ".3dggb",
})

# 可安全内联展示的扩展名（图片/纯文本），其余一律按附件下载
SHARE_INLINE_SAFE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".txt", ".log", ".csv", ".py", ".json",
})

# 新分享默认 24 小时（T17a：默认短效只读分享）
DEFAULT_SHARE_EXPIRES_HOURS = 24
# “永久”分享在 DB 里的实际时间跨度（仅当显式允许时使用）
_PERMANENT_SHARE_YEARS = 100

_UNLOCK_COOKIE_NAME = "feclaw_share_unlock"
_UNLOCK_DEFAULT_TTL_SECONDS = 600  # 约 10 分钟
_UNLOCK_MIN_TTL_SECONDS = 60
_UNLOCK_MAX_TTL_SECONDS = 3600


class ShareBoundaryError(Exception):
    """分享边界违规 —— 一律 fail closed，由路由映射成 HTTP 状态码。

    status_code: 404（不存在/身份不可确认）/ 403（跨租户冲突）/ 410（过期/撤销）/
                 401（需要密码）/ 400（路径非法）
    reason:      机器可读的稳定原因码（不返回给最终用户，用于日志与测试断言）
    message:     面向用户的说明
    """

    def __init__(self, status_code: int, reason: str, message: str = ""):
        self.status_code = int(status_code)
        self.reason = reason
        self.message = message or reason
        super().__init__(f"{reason}: {self.message}")


# ── 配置读取（不修改 config.py；缺省值即安全默认） ──────────────────────────

def _cfg(name: str, default):
    """读取配置项，缺省/读取失败时返回安全默认值（T17a 不改 config.py）。"""
    try:
        value = getattr(settings, name, default)
    except Exception:
        return default
    return default if value is None else value


def share_active_origin() -> str:
    """独立分享域（cookie-free origin）。为空表示未配置 —— 主动内容只能下载。"""
    raw = _cfg("SHARE_ACTIVE_ORIGIN", "") or ""
    try:
        return str(raw).strip().rstrip("/")
    except Exception:
        return ""


def allow_permanent_shares() -> bool:
    """是否允许创建永久分享（expires_hours=0）。默认不允许。"""
    return bool(_cfg("SHARE_ALLOW_PERMANENT", False))


def default_share_expiry_hours() -> int:
    """新分享默认有效期（小时）。配置非法时回落到 24。"""
    try:
        hours = int(_cfg("SHARE_DEFAULT_EXPIRES_HOURS", DEFAULT_SHARE_EXPIRES_HOURS))
    except Exception:
        hours = DEFAULT_SHARE_EXPIRES_HOURS
    return hours if hours > 0 else DEFAULT_SHARE_EXPIRES_HOURS


def max_share_expiry_hours() -> int:
    """分享有效期上限（小时）。0 表示不额外限制。"""
    try:
        return int(_cfg("SHARE_MAX_EXPIRES_HOURS", 0))
    except Exception:
        return 0


def unlock_cookie_ttl() -> int:
    """解锁 cookie 有效期（秒），限制在 60s–3600s，默认 600s。"""
    try:
        ttl = int(_cfg("SHARE_UNLOCK_TTL_SECONDS", _UNLOCK_DEFAULT_TTL_SECONDS))
    except Exception:
        ttl = _UNLOCK_DEFAULT_TTL_SECONDS
    return max(_UNLOCK_MIN_TTL_SECONDS, min(ttl, _UNLOCK_MAX_TTL_SECONDS))


def unlock_cookie_name() -> str:
    return _UNLOCK_COOKIE_NAME


def storage_prefix() -> str:
    """存储前缀，统一以 `/` 结尾。"""
    prefix = str(_cfg("STORAGE_PREFIX", "feclaw/") or "feclaw/")
    return prefix if prefix.endswith("/") else prefix + "/"


# 友好短链用词库（3 英文词组合，好读好记）
_SHARE_WORDS = [
    "sunset", "ocean", "river", "forest", "mountain", "valley", "meadow", "garden",
    "eagle", "falcon", "sparrow", "robin", "swallow", "owl", "hawk", "raven",
    "dolphin", "whale", "seal", "otter", "turtle", "salmon", "coral", "kelp",
    "willow", "maple", "cedar", "pine", "birch", "oak", "elm", "ash",
    "amber", "jade", "opal", "ruby", "sapphire", "emerald", "crystal", "pearl",
    "mercury", "venus", "mars", "jupiter", "saturn", "neptune", "orion", "aurora",
    "summer", "winter", "spring", "autumn", "morning", "evening", "twilight", "midnight",
    "breeze", "thunder", "rainbow", "snowfall", "sunbeam", "moonlight", "starlight", "dewdrop",
    "puzzle", "riddle", "mosaic", "canvas", "melody", "sonata", "sonnet", "fable",
    "voyage", "journey", "odyssey", "quest", "expedition", "trek", "tour", "ramble",
    "harbor", "lighthouse", "anchor", "compass", "captain", "voyager", "mariner", "beacon",
    "velvet", "silk", "linen", "cotton", "satin", "denim", "tweed", "plaid",
    "clover", "thistle", "daisy", "lily", "rosebud", "poppy", "lotus", "iris",
    "panda", "koala", "rabbit", "squirrel", "hedgehog", "badger", "raccoon", "beaver",
    "cinnamon", "vanilla", "ginger", "saffron", "caramel", "toffee", "mocha", "latte",
    "canyon", "glacier", "tundra", "savanna", "desert", "jungle", "delta", "fjord",
    "lantern", "candle", "torch", "bonfire", "comet", "nebula", "galaxy", "eclipse",
    "cascade", "ripple", "whisper", "echo", "glimmer", "shimmer", "sparkle", "glow",
    "acacia", "jasmine", "lavender", "wisteria", "magnolia", "dahlia", "hyacinth", "camellia",
    "pelican", "flamingo", "peacock", "kingfisher", "sandpiper", "woodpecker", "nightingale", "bluebird",
    "biscuit", "muffin", "waffle", "pancake", "bagel", "scone", "croissant", "pretzel",
    "chisel", "hammer", "anvil", "forge", "lathe", "spindle", "loom", "kiln",
    "zephyr", "monsoon", "cyclone", "squall", "mistral", "sirocco", "chinook", "bora",
    "acorn", "chestnut", "hazelnut", "pecan", "walnut", "almond", "cashew", "pistachio",
    "blossom", "pollen", "nectar", "timber", "sprout", "seedling", "sapling", "thicket",
    "cobalt", "indigo", "violet", "crimson", "scarlet", "bronze", "copper", "silver",
    "boulder", "cliff", "hilltop", "plateau", "prairie", "grove", "creek", "brook",
]

# 确定词库大小
_SHARE_WORDS_COUNT = len(_SHARE_WORDS)
# 组合数: 200^3 = 8,000,000，碰撞概率极低


def _generate_slug() -> str:
    """生成 3 词英文友好短链 + CSPRNG 随机后缀（如 'sunset-ocean-river-Ab3x...'）。

    Q20/H19：`random.randint` 是 MT19937（可由观测恢复状态），3 词仅 ~23 bit。
    改为 `secrets` CSPRNG，并追加 128 bit 随机后缀，总熵 ≥128 bit，杜绝枚举。
    """
    w1 = _SHARE_WORDS[secrets.randbelow(_SHARE_WORDS_COUNT)]
    w2 = _SHARE_WORDS[secrets.randbelow(_SHARE_WORDS_COUNT)]
    w3 = _SHARE_WORDS[secrets.randbelow(_SHARE_WORDS_COUNT)]
    return f"{w1}-{w2}-{w3}-{secrets.token_urlsafe(16)}"


def _slug_exists(db, slug: str) -> bool:
    """检查 slug 是否已被占用"""
    from models.database import ShareMapping
    return db.query(ShareMapping).filter(ShareMapping.slug == slug).first() is not None


def get_unique_slug(db) -> str:
    """生成不重复的友好短链（碰撞时重试，无限循环概率 ≈ 0）"""
    for _ in range(10):
        slug = _generate_slug()
        if not _slug_exists(db, slug):
            return slug
    # 极低概率：连续 10 次碰撞，用时间戳做后缀兜底
    return f"{_generate_slug()}-{int(time.time()) % 10000}"


def resolve_slug(slug: str, agent_hash: str = None, db=None):
    """通过 slug 解析分享映射，支持按 agent_hash 隔离过滤。
    Returns ShareMapping or None."""
    from models.database import ShareMapping, SessionLocal
    should_close = db is None
    if db is None:
        db = SessionLocal()
    try:
        query = db.query(ShareMapping).filter(ShareMapping.slug == slug)
        if agent_hash:
            query = query.filter(ShareMapping.agent_hash == agent_hash)
        return query.first()
    finally:
        if should_close:
            db.close()


def get_mapping_by_share_hash(share_hash: str, db=None):
    """按 share_hash 取分享映射（不做任何路径解码，避免暴露/信任明文路径）。"""
    from models.database import ShareMapping, SessionLocal
    if not share_hash:
        return None
    should_close = db is None
    if db is None:
        db = SessionLocal()
    try:
        return db.query(ShareMapping).filter(
            ShareMapping.share_hash == share_hash
        ).first()
    finally:
        if should_close:
            db.close()


def _compute_share_hash(vfs_path: str) -> str:
    """计算 vfs_path 的 HMAC 引用标识（不暴露路径明文）

    仅作为向后兼容的路径指纹工具保留；sharing 实际使用每分享随机 share_hash。
    """
    return hmac.new(_SHARE_SECRET, vfs_path.encode(), hashlib.sha256).hexdigest()[:16]


def _hash_share_password(password: Optional[str]) -> Optional[str]:
    """Q20/H18：分享密码不再明文落库，存 bcrypt 哈希（None/空串表示无密码）。"""
    if not password:
        return None
    from utils.auth import hash_password
    return hash_password(password)


def verify_share_password(password: str, password_hash: str) -> bool:
    """Q20/H18：校验分享密码（bcrypt，兼容 legacy SHA-256）。"""
    if not password or not password_hash:
        return False
    from utils.auth import verify_password
    return verify_password(password, password_hash)


# ── 路径规范化（T17a-1） ─────────────────────────────────────────────────────

def normalize_vfs_path(raw_vfs_path) -> str:
    """规范化分享路径：去前导 `/`，拒绝 `..`、绝对路径/盘符、NUL 与控制字符。

    返回形如 `workspace/sub/a.md` 的 POSIX 相对路径（不含前导 `/`）。
    违规一律抛 ShareBoundaryError（fail closed，不做“尽力而为”的纠正）。
    """
    if not isinstance(raw_vfs_path, str):
        raise ShareBoundaryError(404, "share_path_missing", "分享路径缺失")
    path = raw_vfs_path.strip()
    if not path:
        raise ShareBoundaryError(404, "share_path_missing", "分享路径缺失")
    if _CONTROL_RE.search(path):
        # NUL / 换行 / 控制字符：可能用于截断或混淆路径，直接拒绝
        raise ShareBoundaryError(400, "share_path_invalid", "分享路径包含非法字符")
    if "\\" in path:
        raise ShareBoundaryError(400, "share_path_invalid", "分享路径包含非法分隔符")

    # 允许并剥离前导 `/`（VFS 规范写法 /workspace/...）
    path = path.lstrip("/")
    if not path:
        raise ShareBoundaryError(404, "share_path_missing", "分享路径缺失")
    if re.match(r"^[A-Za-z]:", path):
        raise ShareBoundaryError(400, "share_path_invalid", "分享路径不能是绝对路径")

    segments = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise ShareBoundaryError(403, "share_path_escape", "分享路径不允许包含 ..")
        if len(segment) > 255:
            raise ShareBoundaryError(400, "share_path_invalid", "分享路径片段过长")
        segments.append(segment)
    if not segments:
        raise ShareBoundaryError(404, "share_path_missing", "分享路径缺失")

    normalized = "/".join(segments)
    if len(normalized) > _MAX_REL_PATH_LEN:
        raise ShareBoundaryError(400, "share_path_invalid", "分享路径过长")
    return normalized


def mapping_workspace_relpath(raw_vfs_path) -> str:
    """把 mapping 的 vfs_path 归一成**工作区内**的相对路径。

    - `/workspace/a/b.md` → `a/b.md`（工作区内任意子目录名都允许）
    - `a/b.md` → `a/b.md`（相对工作区）
    - `agents/x/...`、`user_workspaces/2/...`、`public/...`、`config/...`、`agent/...`
      → 403：试图跨出 mapping 的工作区（含旧的硬编码 /2 兜底路径）
    """
    rel = normalize_vfs_path(raw_vfs_path)
    if rel == WORKSPACE_DIR or rel.startswith(WORKSPACE_DIR + "/"):
        inner = rel[len(WORKSPACE_DIR):].lstrip("/")
        if not inner:
            raise ShareBoundaryError(404, "share_path_missing", "分享路径指向工作区根目录")
        return inner
    head = rel.split("/", 1)[0]
    if head in _SCOPE_ROOTS:
        raise ShareBoundaryError(403, "share_path_scope_conflict", "分享路径试图跨出租户工作区")
    return rel


def file_extension(rel_path: str) -> str:
    try:
        return os.path.splitext(rel_path or "")[1].lower()
    except Exception:
        return ""


def is_active_content(rel_path: str) -> bool:
    """该扩展名是否会被浏览器渲染成页面/脚本（主动内容）。"""
    return file_extension(rel_path) in SHARE_ACTIVE_EXTENSIONS


# 主动内容永远不内联：即使误配进白名单也以 active 判定优先
SHARE_INLINE_SAFE_EFFECTIVE = SHARE_INLINE_SAFE_EXTENSIONS - SHARE_ACTIVE_EXTENSIONS


def is_inline_safe(rel_path: str) -> bool:
    return file_extension(rel_path) in SHARE_INLINE_SAFE_EFFECTIVE


# ── 身份解析（T17a-1：只认 mapping 已确认的身份） ────────────────────────────

def _agent_owner(db, agent_hash: str) -> Optional[str]:
    """从可信的 AgentProfile 读取 owner；agent 不存在返回 None。"""
    from models.agent_profile import AgentProfile
    profile = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
    if profile is None:
        return None
    return str(profile.user_id)


def resolve_mapping_storage_key(mapping, db=None) -> dict:
    """从 mapping 已确认的身份推导**唯一**存储 key（不再做全局/跨用户回退）。

    返回：
        {"scope": "agent"|"user_workspace", "agent_hash", "user_id",
         "rel_path": 工作区内相对路径, "key": 存储 key}

    fail closed：
    - mapping 为空 → 404
    - agent_hash / user_id 都缺失 → 404（旧映射无法确认 owner，不猜）
    - 身份格式非法 → 404（不尝试“修好”它）
    - 传了 db 且 agent 不存在 / owner 与 agent 归属冲突 → 404 / 403
    """
    if mapping is None:
        raise ShareBoundaryError(404, "share_mapping_missing", "分享链接不存在")

    agent_hash = (getattr(mapping, "agent_hash", "") or "").strip().lower()
    user_id = str(getattr(mapping, "user_id", "") or "").strip()
    rel = mapping_workspace_relpath(getattr(mapping, "vfs_path", ""))
    prefix = storage_prefix()

    if agent_hash:
        if not _AGENT_HASH_RE.match(agent_hash):
            raise ShareBoundaryError(
                404, "share_mapping_identity_ambiguous", "分享映射的 Agent 身份无法确认"
            )
        if user_id and not _USER_ID_RE.match(user_id):
            raise ShareBoundaryError(
                404, "share_mapping_identity_ambiguous", "分享映射的 owner 身份无法确认"
            )
        if db is not None:
            owner = _agent_owner(db, agent_hash)
            if owner is None:
                raise ShareBoundaryError(
                    404, "share_mapping_identity_ambiguous", "分享对应的 Agent 已不存在，无法确认 owner"
                )
            if user_id and owner != user_id:
                raise ShareBoundaryError(
                    403, "share_mapping_identity_conflict", "分享映射的 owner 与 Agent 归属冲突"
                )
            if not user_id:
                # 由可信 AgentProfile 派生 owner（只用于校验/日志，不写回 mapping）
                user_id = owner
        return {
            "scope": "agent",
            "agent_hash": agent_hash,
            "user_id": user_id,
            "rel_path": rel,
            "key": f"{prefix}agents/{agent_hash}/workspace/{rel}",
        }

    if not user_id:
        raise ShareBoundaryError(
            404, "share_mapping_identity_missing", "分享映射缺少 owner/agent 身份"
        )
    if not _USER_ID_RE.match(user_id):
        raise ShareBoundaryError(
            404, "share_mapping_identity_ambiguous", "分享映射的 owner 身份无法确认"
        )
    return {
        "scope": "user_workspace",
        "agent_hash": "",
        "user_id": user_id,
        "rel_path": rel,
        "key": f"{prefix}user_workspaces/{user_id}/workspace/{rel}",
    }


# ── 过期 / 撤销（T17a：过期与撤销后 HTTP 与 tool 都拒绝） ─────────────────────

def _utcnow() -> datetime:
    return datetime.utcnow()


def is_mapping_expired(mapping, now: Optional[datetime] = None) -> bool:
    expires_at = getattr(mapping, "expires_at", None)
    if not expires_at:
        return False
    return (now or _utcnow()) > expires_at


def is_mapping_revoked(mapping) -> bool:
    """撤销标记。

    说明：`ShareMapping` 目前没有 revoked 列（models/database.py 不在 T17a 的
    改动范围）。这里用 `getattr(mapping, "revoked_at", None)` 前向兼容：
    列一旦加上（建议 `revoked_at DATETIME NULL`），本函数立即生效，无需改分享代码。
    列尚未存在时的“撤销”表现为删除 mapping 行（解析得到 404）。
    """
    return getattr(mapping, "revoked_at", None) is not None


def assert_mapping_live(mapping) -> None:
    """父分享必须存在、未过期、未撤销，否则抛 ShareBoundaryError。"""
    if mapping is None:
        raise ShareBoundaryError(404, "share_mapping_missing", "分享链接不存在或已过期")
    if is_mapping_revoked(mapping):
        raise ShareBoundaryError(410, "share_revoked", "分享链接已被撤销")
    if is_mapping_expired(mapping):
        raise ShareBoundaryError(410, "share_expired", "分享链接已过期")


# ── 解锁 cookie（T17a-4：密码不进 query） ─────────────────────────────────────

def _unlock_secret() -> bytes:
    """解锁 cookie 的独立派生密钥（与 token 签名域分离）。"""
    return hmac.new(_SHARE_SECRET, b"share-unlock-v1", hashlib.sha256).digest()


def encode_unlock_cookie(slug: Optional[str], share_hash: Optional[str],
                         ttl_seconds: Optional[int] = None) -> tuple:
    """签发短时解锁 cookie 值：base64url(v1|slug|share_hash|exp|sig)。

    Returns: (value, ttl_seconds)
    """
    ttl = unlock_cookie_ttl() if ttl_seconds is None else max(
        _UNLOCK_MIN_TTL_SECONDS, min(int(ttl_seconds), _UNLOCK_MAX_TTL_SECONDS)
    )
    expires_at = int(time.time()) + ttl
    payload = f"v1|{slug or ''}|{share_hash or ''}|{expires_at}"
    sig = hmac.new(_unlock_secret(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}"
    value = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return value, ttl


def decode_unlock_cookie(value: Optional[str]) -> Optional[dict]:
    """校验并解出解锁 cookie；签名错误/过期/结构不符一律返回 None。"""
    if not value or not isinstance(value, str):
        return None
    try:
        padded = value + "=" * ((4 - len(value) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        payload, sig = raw.rsplit("|", 1)
        expected = hmac.new(_unlock_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        fields = payload.split("|")
        if len(fields) != 4 or fields[0] != "v1":
            return None
        _, slug, share_hash, expires_at = fields
        expires_at_i = int(expires_at)
        if time.time() > expires_at_i:
            return None
        return {"slug": slug, "share_hash": share_hash, "expires_at": expires_at_i}
    except Exception:
        return None


def unlock_grant_valid(grant: Optional[dict], mapping, slug: Optional[str] = None) -> bool:
    """cookie 是否确实解开了**这个**分享（绑定 slug + share_hash）。"""
    if not grant or mapping is None:
        return False
    if (grant.get("share_hash") or "") != (getattr(mapping, "share_hash", "") or ""):
        return False
    expected_slug = (mapping.slug or "") if slug is None else (slug or "")
    return (grant.get("slug") or "") == (expected_slug or "")


def password_proof_status(mapping, *, cookie_value: Optional[str] = None,
                          query_password: Optional[str] = None) -> dict:
    """判断本次访问的密码证明。

    Returns: {"required": bool, "ok": bool, "proof": "none"|"cookie"|"query"|"missing"}
    """
    required = bool(getattr(mapping, "password", None))
    if not required:
        return {"required": False, "ok": True, "proof": "none"}
    grant = decode_unlock_cookie(cookie_value)
    if unlock_grant_valid(grant, mapping):
        return {"required": True, "ok": True, "proof": "cookie"}
    if query_password:
        try:
            if verify_share_password(query_password, mapping.password):
                return {"required": True, "ok": True, "proof": "query"}
        except Exception:
            logger.warning("[Share] password verification failed unexpectedly", exc_info=True)
    return {"required": True, "ok": False, "proof": "missing"}


# ── token 编解码（保持既有契约，只额外暴露 mapping） ──────────────────────────

def _encode_share_token(share_hash: str, expires_at: int) -> str:
    """编码分享 token：base64(expires_at|share_hash|signature)

    Q20/H19：签名不再截断到 16 hex（64 bit），改用完整 SHA-256 HMAC hex（256 bit）。
    """
    payload = f"{expires_at}|{share_hash}"
    sig = hmac.new(_SHARE_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    token = f"{expires_at}|{share_hash}|{sig}"
    return base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")


def _decode_share_token_payload(token: str) -> Optional[tuple]:
    """校验 token 签名与内嵌过期时间，返回 (expires_at, share_hash) 或 None。"""
    if not token or not isinstance(token, str):
        return None
    try:
        padding = 4 - len(token) % 4
        if padding != 4:
            token += "=" * padding
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        parts = raw.rsplit("|", 2)
        if len(parts) != 3:
            return None
        expires_at_str, share_hash, sig = parts
        expires_at = int(expires_at_str)

        if time.time() > expires_at:
            return None

        payload = f"{expires_at}|{share_hash}"
        expected_sig = hmac.new(_SHARE_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        return expires_at, share_hash
    except Exception:
        return None


def decode_share_token_mapping(token: str, db=None):
    """解码 token 并返回其 ShareMapping（路由应使用本函数，而不是明文 vfs_path）。"""
    payload = _decode_share_token_payload(token)
    if not payload:
        return None
    _, share_hash = payload
    try:
        return get_mapping_by_share_hash(share_hash, db=db)
    except Exception:
        logger.warning("[Share] token->mapping lookup failed", exc_info=True)
        return None


def decode_share_token(token: str, db=None) -> Optional[str]:
    """解码分享 token 并返回 mapping 的 vfs_path（兼容旧调用）。

    注意：路由**不应**再直接用返回值拼存储 key；请用
    `decode_share_token_mapping()` + `resolve_mapping_storage_key()`，让路径
    严格来自 mapping 身份。
    """
    mapping = decode_share_token_mapping(token, db=db)
    if mapping is None:
        return None
    return mapping.vfs_path


def cleanup_expired_references(db) -> int:
    """清理过期的 ShareReference 记录"""
    from models.database import ShareReference
    now = datetime.utcnow()
    deleted = db.query(ShareReference).filter(
        ShareReference.expires_at.isnot(None),
        ShareReference.expires_at < now
    ).delete()
    if deleted:
        db.commit()
    return deleted


# ── 引用令牌（T17a-5：路径从映射推导，禁止任意 vfs_path） ──────────────────────

def load_authorized_reference(db, ref_hash: str, *, cookie_value: Optional[str] = None,
                              query_password: Optional[str] = None) -> dict:
    """读取引用令牌并校验父分享边界。

    校验项：引用存在/未过期、父 share 存在、父 share 未过期未撤销、
    父 share 的密码已验证（若设置）。返回的 `vfs_path` **始终**取自父 mapping，
    不信任 `ShareReference.vfs_path`（老数据/被篡改记录一律以 mapping 为准）。
    """
    from models.database import ShareReference

    if not ref_hash or not isinstance(ref_hash, str) or len(ref_hash) != 8:
        raise ShareBoundaryError(404, "reference_invalid", "引用不存在或已过期")

    ref = db.query(ShareReference).filter(ShareReference.ref_hash == ref_hash).first()
    if ref is None:
        raise ShareBoundaryError(404, "reference_missing", "引用不存在或已过期")

    ref_expires_at = getattr(ref, "expires_at", None)
    if ref_expires_at and _utcnow() > ref_expires_at:
        raise ShareBoundaryError(410, "reference_expired", "引用已过期")

    mapping = get_mapping_by_share_hash(ref.share_hash, db=db)
    if mapping is None:
        # 父分享被删除 = 撤销
        raise ShareBoundaryError(404, "share_mapping_missing", "引用所属的分享已不存在")

    assert_mapping_live(mapping)

    proof = password_proof_status(
        mapping, cookie_value=cookie_value, query_password=query_password
    )
    if not proof["ok"]:
        raise ShareBoundaryError(401, "share_password_required", "引用所属的分享需要密码")

    derived_rel = mapping_workspace_relpath(mapping.vfs_path)
    stored = (getattr(ref, "vfs_path", "") or "").strip()
    if stored != (mapping.vfs_path or "").strip():
        logger.warning(
            "[Share] reference path mismatch ignored: ref=%s stored=%r mapping=%r",
            ref_hash, stored[:120], (mapping.vfs_path or "")[:120],
        )

    return {
        "ref_hash": ref.ref_hash,
        "share_hash": mapping.share_hash,
        "slug": mapping.slug or "",
        "agent_hash": mapping.agent_hash or "",
        "user_id": str(mapping.user_id or ""),
        "vfs_path": mapping.vfs_path,
        "rel_path": derived_rel,
        "selected_text": ref.selected_text,
        "context_before": ref.context_before or "",
        "context_after": ref.context_after or "",
        "expires_at": ref_expires_at,
    }


def create_reference_record(db, mapping, selected_text: str, *, ref_hash: Optional[str] = None,
                            context_before: str = "", context_after: str = "",
                            creator_ip: str = "", expires_days: int = 90) -> str:
    """在已授权父分享下创建引用记录，路径只取 mapping.vfs_path。"""
    from models.database import ShareReference

    assert_mapping_live(mapping)
    rel = mapping_workspace_relpath(mapping.vfs_path)  # 父 share 路径本身必须合法

    text = (selected_text or "").strip()
    if not text:
        raise ShareBoundaryError(422, "reference_text_empty", "选中文本不能为空")

    if ref_hash:
        if not isinstance(ref_hash, str) or len(ref_hash) != 8:
            raise ShareBoundaryError(422, "reference_hash_invalid", "ref_hash 长度必须为 8 位")
        existing = db.query(ShareReference).filter(
            ShareReference.ref_hash == ref_hash
        ).first()
        if existing:
            raise ShareBoundaryError(409, "reference_hash_conflict", "引用标识已存在")
    else:
        for _ in range(10):
            candidate = "".join(secrets.choice(
                "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            ) for _ in range(8))
            if not db.query(ShareReference).filter(
                ShareReference.ref_hash == candidate
            ).first():
                ref_hash = candidate
                break
        else:
            raise ShareBoundaryError(500, "reference_hash_exhausted", "生成引用标识失败，请重试")

    ref = ShareReference(
        ref_hash=ref_hash,
        share_hash=mapping.share_hash,
        agent_hash=mapping.agent_hash or None,
        vfs_path=mapping.vfs_path,  # 只信任 mapping，不接受客户端路径
        selected_text=text,
        context_before=(context_before or "")[:500],
        context_after=(context_after or "")[:500],
        creator_ip=creator_ip or None,
        expires_at=datetime.utcnow() + timedelta(days=expires_days),
    )
    db.add(ref)
    db.commit()
    logger.info(
        "[Share] reference created ref=%s share=%s rel=%s",
        ref_hash, (mapping.share_hash or "")[:8], rel,
    )
    return ref_hash


# ── 创建分享（T17a-3：默认 24h 只读，永久需显式配置） ─────────────────────────

def normalize_expiry_hours(expires_hours) -> tuple:
    """规范化请求的有效期。

    Returns: (hours, clamped) —— hours==0 表示“永久”（仅当配置允许）
    """
    try:
        requested = int(expires_hours)
    except Exception:
        requested = default_share_expiry_hours()

    if requested <= 0:
        if allow_permanent_shares():
            return 0, False
        # 未显式允许永久：降级为默认 24h（不拒绝请求，但绝不静默给永久）
        return default_share_expiry_hours(), True

    cap = max_share_expiry_hours()
    if cap and requested > cap:
        return cap, True
    return requested, False


def confirm_share_identity(db, agent_hash: Optional[str], user_id: Optional[str]) -> dict:
    """创建分享前确认 owner/agent 身份（fail closed）。"""
    from models.agent_profile import AgentProfile

    agent = (agent_hash or "").strip().lower()
    uid = str(user_id).strip() if user_id is not None else ""

    if agent:
        if not _AGENT_HASH_RE.match(agent):
            raise ShareBoundaryError(400, "share_identity_invalid", "Agent 身份格式不合法")
        profile = db.query(AgentProfile).filter(AgentProfile.hash == agent).first()
        if profile is None:
            raise ShareBoundaryError(404, "share_agent_not_found", "Agent 不存在，无法创建分享")
        owner = str(profile.user_id)
        if uid and uid != owner:
            raise ShareBoundaryError(
                403, "share_identity_mismatch", "user_id 与该 Agent 的归属不一致"
            )
        return {"agent_hash": agent, "user_id": owner}

    if not uid:
        raise ShareBoundaryError(400, "share_identity_missing", "缺少 owner/agent 身份，拒绝创建分享")
    if not _USER_ID_RE.match(uid):
        raise ShareBoundaryError(400, "share_identity_invalid", "user_id 格式不合法")
    return {"agent_hash": "", "user_id": uid}


def create_share_link(
    vfs_path: str = "",
    mode: str = "share",
    password: Optional[str] = None,
    user_id: Optional[str] = None,
    expires_hours: Optional[int] = None,
    agent_hash: Optional[str] = None,
    db=None,
) -> Optional[dict]:
    """
    创建文件分享链接（默认 24 小时、只读、仅授权文件）

    Args:
        vfs_path: VFS 文件路径（如 `/workspace/output/a.md`）
        mode: "share" 生成 token 链接（默认）；"path" 为历史兼容模式，已废弃
        password: 可选密码保护（只校验，不落明文）
        user_id: 用户 ID（owner）
        expires_hours: 过期小时数；缺省=24h；0=永久（仅 SHARE_ALLOW_PERMANENT 时生效）
        agent_hash: Agent 的 4/8 位 hash
        db: 数据库会话（可选，不传则自动创建）

    Returns:
        成功: {"url","token","slug","expires_at","expires_hours","expiry_clamped",...}
        失败: {"_error": "<code>", "message": "..."} / None
    """
    from models.database import ShareMapping, SessionLocal

    # 历史兼容：mode=path 只拼 URL，不落库。已废弃（URL 含明文路径，且 /share/{path}
    # 现在必须能解码成 token），仅保留给既有调用/测试，勿在新代码使用。
    if mode == "path":
        logger.warning("[Share] mode='path' is deprecated and leaks the VFS path in the URL")
        url = f"https://{_cfg('FECLAW_STATIC_DOMAIN', '')}/share/{str(vfs_path).lstrip('/')}"
        return {"url": url, "deprecated": True}

    try:
        rel_path = mapping_workspace_relpath(vfs_path)
    except ShareBoundaryError as exc:
        logger.warning("[Share] create rejected: %s (%s)", exc.reason, exc.message)
        return {"_error": "invalid_path", "message": exc.message, "reason": exc.reason}

    should_close = db is None
    if db is None:
        db = SessionLocal()
    try:
        try:
            identity = confirm_share_identity(db, agent_hash, user_id)
        except ShareBoundaryError as exc:
            logger.warning("[Share] identity rejected: %s (%s)", exc.reason, exc.message)
            return {
                "_error": "identity_missing" if exc.status_code == 400 else
                          ("identity_mismatch" if exc.status_code == 403 else "identity_unknown"),
                "message": exc.message,
                "reason": exc.reason,
            }

        prefix = storage_prefix()
        if identity["agent_hash"]:
            storage_key = f"{prefix}agents/{identity['agent_hash']}/workspace/{rel_path}"
        else:
            storage_key = f"{prefix}user_workspaces/{identity['user_id']}/workspace/{rel_path}"

        # 文件必须真实存在（与 VFS 共用 create_file_storage()，感知 STORAGE_MODE）
        try:
            from services.file_storage import create_file_storage
            storage = create_file_storage(mode=getattr(settings, "STORAGE_MODE", "auto"))
            if not storage.file_exists(storage_key):
                logger.warning("[Share] file not found in VFS: %s", storage_key)
                return {"_error": "not_found", "vfs_path": vfs_path}
        except Exception as exc:
            # 存储后端本身不可用（COS 未配置、磁盘不可写）—— 带出真因，不吞
            logger.error("[Share] storage unavailable for share create: %s", exc, exc_info=True)
            return {"_error": "storage_error", "message": str(exc)}

        hours, clamped = normalize_expiry_hours(expires_hours)
        if hours == 0:
            expires_at = int(time.time()) + _PERMANENT_SHARE_YEARS * 365 * 24 * 3600
        else:
            expires_at = int(time.time()) + hours * 3600

        # Q20/H19：share_hash 为每分享随机唯一值（CSPRNG），不再由 vfs_path 确定性派生
        share_hash = secrets.token_hex(8)  # 16 hex = 64 bit CSPRNG，配合 unique 约束
        token = _encode_share_token(share_hash, expires_at)

        slug = get_unique_slug(db)
        sub = f"{identity['agent_hash']}." if identity["agent_hash"] else ""
        url = f"https://{sub}{_cfg('FECLAW_PUBLIC_URL', '')}/s/{slug}"

        mapping = ShareMapping(
            user_id=identity["user_id"],
            agent_hash=identity["agent_hash"],
            vfs_path=vfs_path,
            share_hash=share_hash,
            slug=slug,
            mode="share",
            # Q20/H18：密码不再明文落库，存 bcrypt 哈希
            password=_hash_share_password(password),
            created_at=datetime.utcnow(),
            expires_at=datetime.utcfromtimestamp(expires_at),
        )
        db.add(mapping)
        db.commit()
    except Exception as exc:
        # 不吞异常：写日志、回滚、把可判定的失败码返回给调用方
        logger.error("[Share] failed to create share link: %s", exc, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return {"_error": "create_failed", "message": "创建分享链接失败（服务内部错误）"}
    finally:
        if should_close:
            db.close()

    if clamped:
        logger.warning(
            "[Share] expiry request=%r clamped to %sh (SHARE_ALLOW_PERMANENT=%s)",
            expires_hours, hours, allow_permanent_shares(),
        )
    logger.info(
        "[Share] link created user=%s agent=%s rel=%s expires_hours=%s password=%s",
        identity["user_id"], identity["agent_hash"] or "-", rel_path, hours, bool(password),
    )

    return {
        "url": url,
        "token": token,
        "slug": slug,
        "expires_at": expires_at,
        "expires_hours": hours,
        "expiry_clamped": clamped,
        "permanent": hours == 0,
        "password_protected": bool(password),
        "mode": "share",
        "vfs_path": vfs_path,
    }
