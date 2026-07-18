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

    # 配置向导完成标记（由 services.setup_service 自动写入 .env）
    SETUP_COMPLETE: bool = False

    # 冷启动临时鉴权 token（首次启动时由 main.py 自动生成并写入 .env）
    # 冷启动期间用户访问 /setup* 必须带 ?token=<SETUP_TOKEN> 才能通过验证。
    # 正常启动后此 token 被清空，setup 路由降级为 JWT 鉴权。
    SETUP_TOKEN: str = ""

    # MySQL 数据库配置
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = ""
    MYSQL_DATABASE: str = "FeClaw"

    # 数据库 URL
    DATABASE_URL: str = "mysql+pymysql://root:@localhost:3306/FeClaw"

    # LLM 配置
    ZHIPU_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""
    DOUBAO_API_KEY: str = ""
    QWEN_API_KEY: str = ""
    MIMO_API_KEY: str = ""
    MINIMAX_API_KEY: str = ""
    DEFAULT_LLM_PROVIDER: str = "zhipuai"
    DEFAULT_LLM_MODEL: str = "glm-4.7"
    DEFAULT_VISION_MODEL: str = ""        # 默认视觉模型；为空由 SmartRouter 自动选
    DEFAULT_EMBEDDING_MODEL: str = ""     # 默认嵌入模型；为空由 SmartRouter 自动选
    DEFAULT_FORMATTING_PROVIDER: str = "deepseek"

    # ─── 主模型配置（新） ───
    MAIN_TEXT_MODEL: str = "deepseek-v4-flash"        # 主文本模型
    MAIN_VISION_MODEL: str = "qwen3.6-35b-a3b"       # 主视觉模型
    MAIN_EMBEDDING_MODEL: str = "text-embedding-v4"   # 主嵌入模型

    # TTS 模型（model_registry.TTS_MODEL_REGISTRY 中的 key）
    TTS_MODEL: str = "cosyvoice-v1"

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
    OAUTH_PROVIDER_NAME: str = "OAuth/OIDC 认证服务"  # OAuth 提供商显示名称（登录页面展示用）
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
    STORAGE_PREFIX: str = "feclaw/"  # 存储前缀，隔离多实例（本地/COS 均生效）
    STORAGE_PREFIX: str = "feclaw/"  # 向下兼容，已由 STORAGE_PREFIX 取代

    # COS 向量存储桶自动扩容配置
    VECTOR_BUCKET_PREFIX: str = "feclaw-vec"    # 新建向量桶的前缀（不含-APPID）
    MAX_INDEXES_PER_BUCKET: int = 85            # 每个桶最大索引数（留余量给 100 上限）

    # 文件存储后端: "auto" | "cos" | "local"
    STORAGE_MODE: str = "auto"
    LOCAL_STORAGE_ROOT: str = "./feclaw-storage"
    PUBLIC_STORAGE_ROOT: str = "./feclaw-public"

    # 向量存储后端: "cos"（腾讯云）或 "numpy"（本地回退）
    VECTOR_STORAGE_BACKEND: str = "cos"

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

    # 联网搜索后端选择（balanced 级别生效）：
    # qwen（默认） | glm | kimi | auto
    # auto 模式下按可用 API Key 自动选择首选后端
    DEFAULT_SEARCH_ENGINE: str = "qwen"

    # 豆包 / 火山引擎配置
    DOUBAO_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"
    DOUBAO_SEEDREAM_MODEL: str = "doubao-seedream-5-0-260128"
    COOKIE_SECURE: bool = False  # JWT cookie secure 标志；为空时自动检测（推荐）

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
    TOOL_TIMEOUT: int = 300  # 工具调用超时（秒）
    SESSION_CLEANUP_DAYS: int = 7  # 会话历史保留天数

    # ─── 上下文 / 压缩限制（P1.1: 魔法数字收口） ───
    CONTEXT_LIMIT_TOKENS: int = 110000          # 触发上下文压缩的 token 阈值
    CONTEXT_COMPACTION_THRESHOLD: float = 0.15   # 压缩后保留比例（group_service 用）
    COMPACTION_MAX_TOKENS: int = 80000           # MessageCompactor 压缩目标 token 数

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

    # Desktop 集成配置
    DESKTOP_ENABLED: bool = False      # 是否启用 Desktop WS 通道
    DESKTOP_WS_URL: str = "ws://127.0.0.1:19999"  # Desktop 监听的 WS 地址

    # 沙箱执行环境配置
    SANDBOX_MAX_CONCURRENT: int = 5
    SANDBOX_MAX_RUNTIME_HOURS: int = 12
    SANDBOX_READ_RATE_LIMIT: int = 1 * 1024 * 1024      # 1 MB/s
    SANDBOX_WRITE_RATE_LIMIT: int = 1 * 1024 * 1024     # 1 MB/s
    SANDBOX_MAX_FILE_SIZE: int = 100 * 1024 * 1024      # 100 MB
    # Python venv 路径（沙箱内绑定到 /venv），默认服务器路径
    FECLAW_VENV_PATH: str = "/home/ubuntu/FeClaw/venv"

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

# 最终数据库 URL 直接来自配置
DATABASE_URL = settings.DATABASE_URL