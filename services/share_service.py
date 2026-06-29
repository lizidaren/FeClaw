"""
分享链接服务 - 创建安全的文件分享链接
token 使用 share_hash 引用路径，不在 token 中暴露 vfs_path 明文
"""
import hashlib
import hmac
import json
import time
import base64
import logging
from datetime import datetime
from typing import Optional
from config import settings

logger = logging.getLogger(__name__)

# HMAC 签名密钥（派生自 JWT_SECRET）
_SHARE_SECRET = hashlib.sha256(f"share:{settings.JWT_SECRET}".encode()).digest()

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
    """生成 3 词英文友好短链，如 'sunset-ocean-river'"""
    import random
    w1 = _SHARE_WORDS[random.randint(0, _SHARE_WORDS_COUNT - 1)]
    w2 = _SHARE_WORDS[random.randint(0, _SHARE_WORDS_COUNT - 1)]
    w3 = _SHARE_WORDS[random.randint(0, _SHARE_WORDS_COUNT - 1)]
    return f"{w1}-{w2}-{w3}"


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
    import time
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


def _compute_share_hash(vfs_path: str) -> str:
    """计算 vfs_path 的 HMAC 引用标识（不暴露路径明文）"""
    return hmac.new(_SHARE_SECRET, vfs_path.encode(), hashlib.sha256).hexdigest()[:16]


def _encode_share_token(share_hash: str, expires_at: int) -> str:
    """编码分享 token：base64(expires_at|share_hash|signature)"""
    payload = f"{expires_at}|{share_hash}"
    sig = hmac.new(_SHARE_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
    token = f"{expires_at}|{share_hash}|{sig}"
    return base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")


def decode_share_token(token: str, db=None) -> Optional[str]:
    """解码分享 token，验证签名和过期时间，从 DB 查 vfs_path 或返回 None"""
    from models.database import ShareMapping, SessionLocal

    try:
        # 补齐 base64 padding
        padding = 4 - len(token) % 4
        if padding != 4:
            token += "=" * padding
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        parts = raw.rsplit("|", 2)
        if len(parts) != 3:
            return None
        expires_at_str, share_hash, sig = parts
        expires_at = int(expires_at_str)

        # 校验过期
        if time.time() > expires_at:
            return None

        # 校验签名
        payload = f"{expires_at}|{share_hash}"
        expected_sig = hmac.new(_SHARE_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected_sig):
            return None

        # 从 DB 查找 vfs_path（share_hash 为 HMAC 引用，不暴露路径明文）
        should_close = db is None
        if db is None:
            db = SessionLocal()
        try:
            mapping = db.query(ShareMapping).filter(
                ShareMapping.share_hash == share_hash
            ).first()
            if mapping:
                return mapping.vfs_path
        finally:
            if should_close:
                db.close()

        return None
    except Exception:
        return None


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


def create_share_link(
    vfs_path: str = "",
    mode: str = "share",
    password: Optional[str] = None,
    user_id: Optional[str] = None,
    expires_hours: int = 168,
    agent_hash: Optional[str] = None,
    db=None,
) -> Optional[dict]:
    """
    创建文件分享链接

    Args:
        vfs_path: VFS 文件路径
        mode: "share" 生成 token 链接, "path" 直接路径映射
        password: 可选密码保护
        user_id: 用户 ID
        expires_hours: 过期时间（小时）
        agent_hash: Agent 的 4 位 hash
        db: 数据库会话（可选，不传则自动创建）

    Returns:
        {"url": "..."} 或 None
    """
    from models.database import ShareMapping, SessionLocal

    try:
        # 检查文件是否真的存在
        if agent_hash and vfs_path:
            from services.storage_service import StorageService
            cos_key = f"feclaw/agents/{agent_hash}/{vfs_path.lstrip('/')}"
            if not StorageService().file_exists(cos_key):
                logger.warning(f"Share link failed: file not found in VFS: {cos_key}")
                return None

        if mode == "path":
            url = f"https://{settings.FECLAW_STATIC_DOMAIN}/share/{vfs_path.lstrip('/')}"
            return {"url": url}

        # share 模式：用 share_hash 引用 vfs_path，不暴露路径明文
        expires_at = int(time.time()) + expires_hours * 3600
        share_hash = _compute_share_hash(vfs_path)
        token = _encode_share_token(share_hash, expires_at)

        # 持久化到 DB
        should_close = db is None
        if db is None:
            db = SessionLocal()
        try:
            # 生成友好短链 slug
            slug = get_unique_slug(db)
            # 子域名 URL：{agent_hash}.feclaw.lizidaren.cn/s/{slug}
            _sub = f"{agent_hash}." if agent_hash else ""
            url = f"https://{_sub}{settings.FECLAW_DOMAIN}/s/{slug}"

            mapping = ShareMapping(
                user_id=user_id or "",
                agent_hash=agent_hash or "",
                vfs_path=vfs_path,
                share_hash=share_hash,
                slug=slug,
                mode=mode,
                password=password,
                created_at=datetime.utcnow(),
                expires_at=datetime.utcfromtimestamp(expires_at),
            )
            db.add(mapping)
            db.commit()
        finally:
            if should_close:
                db.close()

        if password:
            logger.info(f"Share link with password created for {vfs_path} by {user_id}")
        else:
            logger.info(f"Share link created for {vfs_path} by {user_id}")

        return {"url": url, "token": token, "slug": slug, "expires_at": expires_at}
    except Exception as e:
        logger.error(f"Failed to create share link: {e}")
        return None
