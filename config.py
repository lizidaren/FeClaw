"""
FeClaw 智能体网关平台配置文件
包含 MySQL 数据库配置、OAuth、COS 存储、域名等配置
"""

from pydantic_settings import BaseSettings
from typing import List, Optional
import os


class Settings(BaseSettings):
    """FeClaw 应用配置"""

    # 服务器配置
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    DEBUG: bool = False
    # T18a/F8：/docs、/redoc、/openapi.json 默认不挂载（生产会暴露全部路由/schema）。
    # 仅调试或显式打开时暴露；DEBUG=true 也会间接开启。
    FECLAW_EXPOSE_DOCS: bool = False

    # JWT 配置
    JWT_SECRET: str = ""  # ⚠️ 必须设置！用于 JWT 签名的密钥。首次启动会提示设置。  # 必须通过 .env 设置，否则启动时报错
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24 * 7  # 7天过期

    # 配置向导完成标记（由 services.setup_service 自动写入 .env）
    SETUP_COMPLETE: bool = False

    # 冷启动临时鉴权 token（首次启动时由 main.py 自动生成并写入 .env）
    # 冷启动期间 /setup 的**写接口**必须带 ?token=<SETUP_TOKEN> 才能通过验证；
    # GET /setup 向导页本身不含敏感数据、可匿名打开。
    # 正常启动后此 token 被清空，setup 写接口降级为 JWT 管理员鉴权。
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
    # 阿里云 DashScope（语音识别 ASR 等）
    ALIYUN_DASHSCOPE_API_KEY: str = ""
    MIMO_API_KEY: str = ""
    MINIMAX_API_KEY: str = ""
    DEFAULT_LLM_PROVIDER: str = "zhipuai"
    DEFAULT_LLM_MODEL: str = "glm-4.7"
    DEFAULT_VISION_MODEL: str = ""        # 默认视觉模型；为空由 SmartRouter 自动选
    DEFAULT_EMBEDDING_MODEL: str = ""     # 默认嵌入模型；为空由 SmartRouter 自动选
    DEFAULT_FORMATTING_PROVIDER: str = "deepseek"

    # ─── 主模型配置（新） ───
    MAIN_TEXT_MODEL: str = "deepseek-flash"           # 主文本模型（多模态：支持图片直通）
    MAIN_VISION_MODEL: str = "qwen3.6-35b-a3b"       # 主视觉模型
    MAIN_EMBEDDING_MODEL: str = "text-embedding-v4"   # 主嵌入模型

    # ─── Vision 分级配置（T 阶段新增） ───
    # Zentrim pipeline 把视觉任务拆成两档：Speed（形态判断）+ Heavy（HTML/描述）
    # 留空 = 用 model_registry 的硬编码兜底
    VISION_SPEED_MODEL: str = ""   # 轻量快速多模态；空 → fallback "qwen3.6-flash"
    VISION_HEAVY_MODEL: str = ""   # 重量级多模态；空 → fallback "doubao-seed-2.0-lite"

    # TTS 模型（model_registry.TTS_MODEL_REGISTRY 中的 key）
    TTS_MODEL: str = "cosyvoice-v1"

    # Agent 模型配置（向后兼容，已弃用，请使用 MAIN_TEXT_MODEL）
    AGENT_LLM_MODEL: str = "deepseek-flash"
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

    # OAuth / SSO：设为 true 启用 OAuth 登录
    # 也可自动检测（有 OAUTH_PROVIDER_URL 且不为空时自动启用）
    OAUTH_ENABLED: bool = False

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
    VECTOR_STORAGE_BACKEND: str = "numpy"

    # TOTP 安全策略
    # Q21/M17：此开关原意是「False=可通过 TOTP 访问任何 Agent」，但自 Q19/C2 起
    # TOTP 归属已**无条件强制**（generate 只对 owner 签发、JWT 绑定 agent_hash 子域），
    # 该字段不再有弱化能力，仅保留给前端模板兼容 —— 不要据此关闭任何归属校验。
    TOTP_STRICT_OWNERSHIP: bool = True

    # FeClaw 域名配置
    FECLAW_PUBLIC_URL: str = ""  # 服务器访问地址（如 feclaw.example.com 或 139.199.68.185:8080）
    FECLAW_SUBDOMAIN_ENABLED: bool = False  # 是否启用 Agent 子域名（启用后 {agent}.public_url）
    FECLAW_CDN_DOMAIN: str = ""  # CDN 域名，默认同 FECLAW_PUBLIC_URL
    FECLAW_API_DOMAIN: str = ""  # API 域名，默认同 FECLAW_PUBLIC_URL
    FECLAW_STATIC_DOMAIN: str = ""  # 静态资源域名，默认同 FECLAW_PUBLIC_URL

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
    COOKIE_SECURE: Optional[bool] = None  # JWT cookie secure 标志；None=自动检测 HTTPS（推荐）；True=强制；False=仅非 HTTPS 时不设（HTTPS 仍 secure，G7）

    # Session Memory 配置
    SESSION_MEMORY_ENABLED: bool = True

    # Redis 配置
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_USERNAME: str = ""
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0

    # T18a：Redis 显式开关。
    #   - `REDIS_ENABLED=true`  → 启用（**即使没有用户名/密码**，本地无鉴权 Redis 也能用）
    #   - `REDIS_ENABLED=false` → 禁用（即使配了密码也不用）
    #   - 不设置（None）        → 保持历史行为：有 REDIS_USERNAME / REDIS_PASSWORD 即启用
    #
    # ⚠️ 旧实现是 `@property` 由「有密码/用户名」推导 ⇒ 无法在无密码的本地 Redis 上启用，
    # 也无法显式关闭。现改为「字段 + 构造时解析」：`Settings()` 之后
    # `settings.REDIS_ENABLED` 仍是一个普通 bool，所有既有调用点
    # （services/redis_client.py、services/active_tracker.py、services/sandbox/concurrency.py）
    # 语义不变，不需要改动。
    REDIS_ENABLED: Optional[bool] = None

    # ─── 限流（T18a）────────────────────────────────────────────────
    # 后端可切换：
    #   "memory" —— 进程内（默认；单 worker / 测试 / 无 Redis 部署）
    #   "redis"  —— 跨 worker 集中计数（多 worker 部署必须用这个，否则阈值被放大 N 倍）
    RATE_LIMIT_BACKEND: str = "memory"
    # Redis 后端不可用时的行为：
    #   "open"   —— 放行（记录 WARNING；等同降级为无限流，只在明确接受风险时使用）
    #   "closed" —— 拒绝并返回 503（默认；spec T18「Redis 不可用不能静默放无限」）
    RATE_LIMIT_FAIL_MODE: str = "closed"
    # Redis 计数键前缀（拼在 services/redis_client.KEY_PREFIX 之后）
    RATE_LIMIT_REDIS_PREFIX: str = "ratelimit:"
    # 单次 Redis 操作超时（秒）—— 避免限流把请求拖死
    RATE_LIMIT_REDIS_TIMEOUT: float = 0.5

    # 登录：IP+username 双维度（与历史 10/300s 一致）
    RATE_LIMIT_LOGIN_ATTEMPTS: int = 10
    RATE_LIMIT_LOGIN_WINDOW: int = 300
    # 登录：IP 单桶（防用户名喷洒；历史 100/300s）
    RATE_LIMIT_LOGIN_IP_ATTEMPTS: int = 100
    # 注册：IP 单桶（防批量注册；默认 5/小时）
    RATE_LIMIT_REGISTER_ATTEMPTS: int = 5
    RATE_LIMIT_REGISTER_WINDOW: int = 3600
    # 改密：IP+user 双维度
    RATE_LIMIT_PASSWORD_ATTEMPTS: int = 10
    RATE_LIMIT_PASSWORD_WINDOW: int = 300
    # TOTP verify：IP+agent_hash（与历史 10/300s 一致）
    RATE_LIMIT_TOTP_ATTEMPTS: int = 10
    RATE_LIMIT_TOTP_WINDOW: int = 300

    # ─── 注册门禁（T18a）────────────────────────────────────────────
    #   "open"   —— 任何人可注册
    #   "invite" —— 必须带正确邀请码（默认；spec T18「local registration 默认 invite」）
    #   "closed" —— 一律 403（本地账号由管理员开通）
    # invite 模式下若 REGISTRATION_INVITE_CODE 为空，按 closed 处理（fail-closed，
    # 不出现「配了 invite 但谁都能注册」的假门禁）。
    REGISTRATION_MODE: str = "invite"
    REGISTRATION_INVITE_CODE: str = ""
    # ⚠️ 「注册后等待管理员审批」需要 `users.is_active` 列，当前模型没有该列；
    # 在 T20a 增加列 + 迁移之前不提供该模式（见 T18a 报告的跨界请求）。
    # 新注册用户的默认档位。None → 使用 models.database.User.tier 的列默认值。
    # 设为 "free" 表示新用户不再默认无限 pro（spec T18「新学生不自动 pro 无限额度」）。
    REGISTRATION_DEFAULT_TIER: Optional[str] = "free"

    # ─── CSRF / Origin（T18a）──────────────────────────────────────
    # 总开关。关闭只应用于「已确认所有客户端都是非浏览器」的场景。
    CSRF_ENABLED: bool = True
    # 双提交 token：可读 cookie 名 + 请求头名（服务端校验两者相等）
    CSRF_COOKIE_NAME: str = "feclaw_csrf"
    CSRF_HEADER_NAME: str = "X-CSRF-Token"
    # 额外允许的 Origin（除 FECLAW_PUBLIC_URL / 请求自身 Host 之外的来源，
    # 例如独立的前端域名）。逗号分隔或 JSON 列表。
    CSRF_EXTRA_ORIGINS: List[str] = []
    # True：既无 Origin 也无 Referer 的不安全请求一律拒绝（完全禁止非浏览器
    # cookie 客户端）。默认 False —— 非浏览器客户端（curl / 桌面 / 移动）
    # 本来就不会被 CSRF 利用，靠双提交 token 已能覆盖浏览器场景。
    CSRF_REQUIRE_ORIGIN: bool = False

    # ─── WebSocket 来源允许列表（T18a）─────────────────────────────
    # 额外允许的 Origin。默认自动允许 FECLAW_PUBLIC_URL 与请求自身 Host（同源）。
    WS_EXTRA_ORIGINS: List[str] = []
    # True：WS 握手缺少 Origin 头一律拒绝（会挡住非浏览器 WS 客户端，默认关闭）
    WS_REQUIRE_ORIGIN: bool = False

    # ─── 文件分享（T17a 由 S3 接线；此处只提供字段，S3 全部用 getattr 读取）──
    SHARE_ACTIVE_ORIGIN: str = ""        # 独立 cookie-free 分享域；空 = 主动内容只下载
    SHARE_ALLOW_PERMANENT: bool = False  # 是否允许永久分享链接（默认否）
    SHARE_DEFAULT_EXPIRES_HOURS: int = 24
    SHARE_MAX_EXPIRES_HOURS: int = 0     # 0 = 不限制最长有效期
    SHARE_UNLOCK_TTL_SECONDS: int = 600  # share 密码解锁 proof 的有效期

    def model_post_init(self, __context) -> None:
        """解析派生配置（pydantic v2 在字段校验后调用一次）。

        `REDIS_ENABLED` 的 None → 历史自动推导（保留无密码场景不启用、
        有密码/用户名即启用的旧行为），显式 true/false 则原样生效。
        """
        if self.REDIS_ENABLED is None:
            self.REDIS_ENABLED = bool(self.REDIS_PASSWORD) or bool(self.REDIS_USERNAME)

        # 枚举值归一化（不合法值在各自 resolve 处再 fail-closed，不在这里抛）
        self.RATE_LIMIT_BACKEND = (self.RATE_LIMIT_BACKEND or "memory").strip().lower()
        self.RATE_LIMIT_FAIL_MODE = (self.RATE_LIMIT_FAIL_MODE or "closed").strip().lower()
        self.REGISTRATION_MODE = (self.REGISTRATION_MODE or "invite").strip().lower()

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
    FECLAW_VENV_PATH: str = "./venv"
    # Q25: 沙箱内存上限（MB）。deploy/helper/helper.c 读这同一个变量，
    # 默认 128MB 对 numpy/matplotlib 不够；helper 硬上限 512MB。
    FECLAW_RLIMIT_MEM_MB: int = 512

    # FUSE 文件系统配置
    FUSE_ENABLED: bool = True
    FUSE_MOUNT_DIR: str = "/tmp/feclaw-fuse"  # FUSE 挂载点目录（应放在 /tmp 下，避免 git 扫描）
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