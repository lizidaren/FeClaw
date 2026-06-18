"""
FeClaw 智能体网关平台配置文件
包含 MySQL 数据库配置、OAuth、COS 存储、域名等配置
"""

from pydantic_settings import BaseSettings
from typing import List
import os


class Settings(BaseSettings):
    """FeClaw 应用配置"""

    # 服务器配置
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    DEBUG: bool = True

    # JWT 配置
    JWT_SECRET: str = ""  # 必须通过 .env 设置，否则启动时报错
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24 * 7  # 7天过期

    # MySQL 数据库配置（预留连接信息）
    # 注意：MySQL 可能未安装，需要确保 MySQL 服务已启动
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = ""  # 预留，生产环境需配置
    MYSQL_DATABASE: str = "FeClaw"

    # 数据库 URL（MySQL 格式，需要安装 pymysql）
    # 如果 MySQL 未安装，可以临时使用 SQLite
    DATABASE_URL: str = "mysql+pymysql://root:@localhost:3306/FeClaw"
    # SQLite 备用配置（开发环境）
    DATABASE_URL_FALLBACK: str = "sqlite:///data/feclaw.db"

    # 使用 MySQL 还是 SQLite（生产环境建议 MySQL）
    USE_MYSQL: bool = False  # 默认使用 SQLite，MySQL 安装后改为 True

    # LLM 配置
    ZHIPU_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""
    DOUBAO_API_KEY: str = ""
    QWEN_API_KEY: str = ""
    DEFAULT_LLM_PROVIDER: str = "zhipuai"
    DEFAULT_LLM_MODEL: str = "glm-4.7"
    DEFAULT_FORMATTING_PROVIDER: str = "deepseek"

    # ─── 主模型配置（新） ───
    MAIN_TEXT_MODEL: str = "deepseek-v4-flash"        # 主文本模型
    MAIN_VISION_MODEL: str = "qwen3.6-35b-a3b"       # 主视觉模型
    MAIN_EMBEDDING_MODEL: str = "text-embedding-v4"   # 主嵌入模型

    # Agent 模型配置（向后兼容，已弃用，请使用 MAIN_TEXT_MODEL）
    AGENT_LLM_MODEL: str = "deepseek-v4-flash"
    # AGENT_LLM_PROVIDER 已移除 — provider 现在从 model_registry 根据模型名自动解析
    AGENT_LLM_REASONING_EFFORT: str = "off"  # "off" | "high" | "max"（仅 deepseek 有效）
    FALLBACK_LLM_PROVIDER: str = "zhipuai"  # 主 LLM 失败时 fallback 的提供商，空 = 不启用 fallback
    FALLBACK_LLM_MODEL: str = "glm-4.7"
    DEFAULT_RECOGNITION_MODEL: str = "glm-4.6v"

    # OAuth / OIDC Client 配置
    # 默认使用 FirstEntrancePlatform 兼容路径，可覆盖以接入任意 OIDC Provider
    OAUTH_PROVIDER_URL: str = ""  # OIDC Provider URL（如 https://sso.example.com）
    OAUTH_CLIENT_ID: str = "feclaw"
    OAUTH_CLIENT_SECRET: str = ""  # OAuth Client Secret
    OAUTH_REDIRECT_URI: str = ""  # OAuth 回调 URL（如 https://feclaw.example.com/api/oauth/callback）

    # 可覆盖的 OAuth 端点 URL（为空时从 OAUTH_PROVIDER_URL 推导 OIDC 标准路径）
    OAUTH_AUTHORIZE_URL: str = ""   # 默认: {OAUTH_PROVIDER_URL}/authorize
    OAUTH_TOKEN_URL: str = ""       # 默认: {OAUTH_PROVIDER_URL}/token
    OAUTH_USERINFO_URL: str = ""    # 默认: {OAUTH_PROVIDER_URL}/userinfo
    OAUTH_JWKS_URL: str = ""        # 默认: {OAUTH_PROVIDER_URL}/.well-known/jwks.json
    OAUTH_END_SESSION_URL: str = ""  # 默认: {OAUTH_PROVIDER_URL}/oauth/end-session
    ADMIN_API_KEY: str = ""

    # Feature Flag：设为 True 启用 OAuth，禁止本地注册/登录
    _oauth_enabled: bool = False

    @property
    def OAUTH_ENABLED(self) -> bool:
        return self._oauth_enabled

    # 腾讯云 COS 存储配置
    TENCENT_COS_SECRET_ID: str = ""
    TENCENT_COS_SECRET_KEY: str = ""
    TENCENT_COS_REGION: str = "ap-guangzhou"
    TENCENT_COS_BUCKET: str = ""
    TENCENT_COS_APPID: str = ""
    TENCENT_COS_PREFIX: str = "feclaw/"

    # TOTP 安全策略
    TOTP_STRICT_OWNERSHIP: bool = True  # TOTP 登录时严格检查 Agent 归属，True=仅能访问自己的 Agent；False=可以通过 TOTP 访问任何 Agent

    # FeClaw 域名配置
    FECLAW_DOMAIN: str = ""  # FeClaw 主域名（如 feclaw.example.com），从 FECLAW_DOMAIN 环境变量读取
    FECLAW_CDN_DOMAIN: str = ""  # CDN 域名，默认同 FECLAW_DOMAIN
    FECLAW_API_DOMAIN: str = ""  # API 域名，默认同 FECLAW_DOMAIN
    FECLAW_STATIC_DOMAIN: str = ""  # 静态资源域名，默认同 FECLAW_DOMAIN

    # 微信配置（iLink 协议）
    WECHAT_ILINK_BASE_URL: str = ""
    WECHAT_ILINK_API_KEY: str = ""

    # 联网搜索配置（三级搜索架构）
    # L1 极简搜索：腾讯搜索（~1s，原始结果）
    TENCENT_SEARCH_API_KEY: str = ""
    TENCENT_SEARCH_URL: str = "https://api.wsa.cloud.tencent.com/SearchPro"
    # L2 高级搜索：Kimi（~15s，LLM 总结）
    KIMI_API_KEY: str = ""
    KIMI_BASE_URL: str = "https://api.moonshot.cn/v1"
    KIMI_MODEL: str = "moonshot-v1-8k"
    # L3 研究级搜索：百度千帆（~40s，深度分析）
    BAIDU_SEARCH_API_KEY: str = ""
    BAIDU_SEARCH_URL: str = "https://qianfan.baidubce.com/v2/chat/completions"

    # 豆包 / 火山引擎配置
    DOUBAO_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"
    DOUBAO_SEEDREAM_MODEL: str = "doubao-seedream-5-0-260128"

    # Session Memory 配置
    SESSION_MEMORY_ENABLED: bool = True

    # Redis 配置
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_USERNAME: str = ""
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0

    @property
    def REDIS_ENABLED(self) -> bool:
        return bool(self.REDIS_PASSWORD) or bool(self.REDIS_USERNAME)

    # 缓存配置
    CACHE_TTL: int = 300  # 搜索结果缓存 TTL（秒）
    TOOL_TIMEOUT: int = 120  # 工具调用超时（秒）
    SESSION_CLEANUP_DAYS: int = 7  # 会话历史保留天数

    # 日志配置
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    # 积分配置
    DAILY_FREE_POINTS: int = 1000
    MAX_POINTS_PER_USE: int = 10

    # MinerU 文档解析服务 Token
    MINERU_TOKEN: str = ""

    # 文件上传配置
    MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024  # 50MB
    ALLOWED_EXTENSIONS: List[str] = [
        ".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".gif",
        ".mp4", ".mp3", ".wav", ".zip", ".rar"
    ]

    # 静态网站托管配置
    STATIC_SITE_MAX_SIZE: int = 100 * 1024 * 1024  # 100MB
    STATIC_SITE_SUBDOMAIN_LENGTH: int = 8

    # 沙箱执行环境配置
    SANDBOX_MAX_CONCURRENT: int = 5
    SANDBOX_MAX_RUNTIME_HOURS: int = 12
    SANDBOX_READ_RATE_LIMIT: int = 1 * 1024 * 1024      # 1 MB/s
    SANDBOX_WRITE_RATE_LIMIT: int = 1 * 1024 * 1024     # 1 MB/s
    SANDBOX_MAX_FILE_SIZE: int = 100 * 1024 * 1024      # 100 MB

    # FUSE 文件系统配置
    FUSE_ENABLED: bool = True
    FUSE_MOUNT_DIR: str = "/tmp/feclaw-fuse"  # 开发环境用 tmp，生产改 /mnt/feclaw
    FUSE_CACHE_TTL: int = 60  # FUSE 属性缓存 TTL（秒）
    FUSE_AUTO_FALLBACK: bool = True  # FUSE 不可用时自动回退到仿真模式

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'
        case_sensitive = True


# 创建全局配置实例
settings = Settings()

# 获取当前目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 确保数据目录存在
data_dir = os.path.join(BASE_DIR, "data")
os.makedirs(data_dir, exist_ok=True)

# 最终数据库 URL（根据配置选择）
if settings.USE_MYSQL:
    DATABASE_URL = settings.DATABASE_URL
else:
    DATABASE_URL = settings.DATABASE_URL_FALLBACK
    # 确保 SQLite 数据目录存在
    db_path = DATABASE_URL.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)