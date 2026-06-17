"""
FeClaw 智能体网关平台
FastAPI 应用入口
"""

import logging
import os

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    force=True
)
logger = logging.getLogger(__name__)

# 应用 COS SDK 补丁（StreamBody.read() 默认只读一个 1024 chunk）
from services.cos_patch import apply_cos_patch
apply_cos_patch()

# 自定义日志过滤器：仅过滤 SQL 语句的 DEBUG 日志，保留 WARNING/ERROR
class SQLFilter(logging.Filter):
    def filter(self, record) -> bool:
        # 只过滤 DEBUG 级别的 SQL 日志，保留 WARNING 和 ERROR（连接池警告、查询失败等）
        if record.levelno <= logging.DEBUG:
            return False
        return True

# 配置日志过滤器
sql_filter = SQLFilter()
logging.getLogger('sqlalchemy').addFilter(sql_filter)
logging.getLogger('sqlalchemy.engine.Engine').addFilter(sql_filter)
logging.getLogger('sqlalchemy.pool').addFilter(sql_filter)
logging.getLogger('sqlalchemy.dialects').addFilter(sql_filter)
logging.getLogger('sqlalchemy.orm').addFilter(sql_filter)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware


class NoCacheMiddleware(BaseHTTPMiddleware):
    """防止 CDN 缓存 API 响应"""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path == "/files" or request.url.path == "/files/":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import uvicorn
from config import settings
from models.database import init_db, SessionLocal, User, engine
from utils.auth import generate_salt, hash_password
from routers import static_site, static_site_public, workspace, wechat, oauth, console, health, vfs_image_dedup, sandbox, share, vfs_view, apps_gateway
from routers.feclaw_domain import router as feclaw_domain_router
from routers.feclaw_chat import router as feclaw_chat_router
from routers.agent_config_ui import router as agent_config_ui_router
from routers.agent_config import router as agent_config_router
from routers.agent_config_chat import router as agent_config_chat_router
from routers.user import router as user_router
from routers.wechat import ensure_message_handler
from services.agent_init_service import ensure_default_agent_5178


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    from services.wechat_service import wechat_service

    # 启动时
    logger.info("Starting FeClaw Gateway...")

    # 检查必填配置
    if not settings.JWT_SECRET:
        logger.critical("JWT_SECRET 未配置！请在 .env 中设置 JWT_SECRET")
        raise RuntimeError("JWT_SECRET is required but not set")

    # 初始化数据库
    init_db()
    logger.info("Database initialized")

    # WSL DNS 预热：提前解析常用 LLM API 域名 + 安装全局 fallback
    from utils.dns_fallback import pre_resolve, install_global_fallback
    pre_resolve("api.deepseek.com", "open.bigmodel.cn", "ark.cn-beijing.volces.com",
                "dashscope.aliyuncs.com", "ilinkai.weixin.qq.com",
                "cn.bing.com", "api.moonshot.cn",
                "firstentrance-gz01-1257148458.cos.ap-guangzhou.myqcloud.com",
                "cos.ap-guangzhou.myqcloud.com",
                "sts.tencentcloudapi.com",
                "firstentrance-gzvec-1257148458.vectors.ap-guangzhou.coslake.com")
    install_global_fallback()
    logger.info("DNS cache warmed + global fallback installed")

    # 数据库迁移：检查并添加 email 列
    from sqlalchemy import text
    with engine.connect() as conn:
        # 检查 users 表是否有 email 列
        from config import DATABASE_URL
        if DATABASE_URL.startswith("mysql"):
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_NAME = 'users' AND TABLE_SCHEMA = DATABASE()"
            ))
        else:
            result = conn.execute(text("PRAGMA table_info(users)"))
        columns_raw = result.fetchall()
        # MySQL: row[0] = COLUMN_NAME (string); SQLite PRAGMA: row[1] = name
        columns = [row[0] for row in columns_raw] if columns_raw and isinstance(columns_raw[0][0], str) else [row[1] for row in columns_raw]
        if 'email' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(128)"))
            conn.commit()
            logger.info("Added email column to users table")

        # 数据库迁移：检查 agent_profiles 表是否有 sr_enabled 列
        if DATABASE_URL.startswith("mysql"):
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_NAME = 'agent_profiles' AND TABLE_SCHEMA = DATABASE()"
            ))
        else:
            result = conn.execute(text("PRAGMA table_info(agent_profiles)"))
        columns_raw = result.fetchall()
        columns = [row[0] for row in columns_raw] if columns_raw and isinstance(columns_raw[0][0], str) else [row[1] for row in columns_raw]
        if 'sr_enabled' not in columns:
            conn.execute(text("ALTER TABLE agent_profiles ADD COLUMN sr_enabled BOOLEAN DEFAULT 0"))
            conn.commit()
            logger.info("Added sr_enabled column to agent_profiles table")

    # 创建默认管理员用户（如果不存在）
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            salt = generate_salt()
            import hashlib
            default_password = hashlib.sha256("admin".encode()).hexdigest()
            admin = User(
                username="admin",
                password_hash=hash_password(default_password, salt),
                salt=salt,
                is_admin=True
            )
            db.add(admin)
            db.commit()
            logger.info("Created default admin user (password: admin)")

        # 创建测试用户
        test_user = db.query(User).filter(User.username == "test").first()
        if not test_user:
            salt = generate_salt()
            import hashlib
            default_password = hashlib.sha256("test".encode()).hexdigest()
            test_user = User(
                username="test",
                password_hash=hash_password(default_password, salt),
                salt=salt,
                is_admin=False
            )
            db.add(test_user)
            db.commit()
            logger.info("Created test user (password: test)")
    finally:
        db.close()

    # 创建默认 Agent 5178（如果不存在）
    try:
        agent_5178 = ensure_default_agent_5178()
        if agent_5178:
            logger.info(f"Agent 5178 ready: hash={agent_5178.hash}, status={agent_5178.status}")
        else:
            logger.info("Agent 5178 creation skipped")
    except Exception as e:
        logger.error(f"Failed to create Agent 5178: {e}")

    # 设置消息处理器并恢复微信 polling
    try:
        await ensure_message_handler()
        logger.info("WeChat message handler setup and polling restored")
    except Exception as e:
        logger.error(f"Failed to setup WeChat message handler: {e}")

    # 启动 sandbox 功能
    if settings.SANDBOX_MAX_CONCURRENT > 0:
        logger.info("Sandbox enabled (via HTTP 127.0.0.1:PORT)")

    # 启动 FUSE 守护进程
    fuse_mounted = False
    if settings.FUSE_ENABLED:
        from services.vfs_fuse_daemon import check_fuse_available
        if check_fuse_available():
            try:
                from services.vfs_fuse_daemon import start_fuse_background, unmount_fuse
                from services.virtual_filesystem import VirtualFileSystem

                vfs = VirtualFileSystem()
                fuse_thread = start_fuse_background(
                    vfs, settings.FUSE_MOUNT_DIR, settings.FUSE_CACHE_TTL,
                    cos_prefix="feclaw/"
                )
                fuse_mounted = True
                logger.info(f"FUSE daemon started: {settings.FUSE_MOUNT_DIR}")

                # Start FUSE health watchdog (Level 2 auto-recovery)
                import threading
                from services.vfs_fuse_daemon import fuse_health_watchdog
                watchdog_thread = threading.Thread(
                    target=fuse_health_watchdog,
                    args=(settings.FUSE_MOUNT_DIR, vfs, settings.FUSE_CACHE_TTL),
                    daemon=True,
                    name="fuse-watchdog",
                )
                watchdog_thread.start()
                logger.info("FUSE health watchdog started")
            except Exception as e:
                logger.warning(f"FUSE daemon failed to start: {e}")
        elif settings.FUSE_AUTO_FALLBACK:
            logger.warning("FUSE 不可用，回退到仿真模式")
        else:
            logger.error("FUSE 不可用，请检查环境（/dev/fuse, fusermount3, pyfuse3）")
            raise RuntimeError("FUSE is required but not available")

    try:
        yield
    finally:
        # 关闭时（无论启动是否成功，已挂载的资源都尝试清理）
        logger.info("Shutting down FeClaw Gateway...")

        # 停止所有微信 polling
        try:
            await wechat_service.stop_all_polling()
            logger.info("WeChat polling stopped")
        except Exception as e:
            logger.error(f"Failed to stop WeChat polling: {e}")

        # 卸载 FUSE
        if fuse_mounted:
            try:
                from services.vfs_fuse_daemon import unmount_fuse
                unmount_fuse(settings.FUSE_MOUNT_DIR)
                logger.info("FUSE daemon stopped")
            except Exception as e:
                logger.error(f"Failed to unmount FUSE at {settings.FUSE_MOUNT_DIR}: {e}")

        # 关闭共享 HTTP 客户端
        try:
            from services.llm_service import llm_service
            await llm_service.close_http_client()
            logger.info("LLM HTTP client closed")
        except Exception as e:
            logger.error(f"Failed to close LLM HTTP client: {e}")

        try:
            from services.rerank_service import close_rerank_client
            await close_rerank_client()
            logger.info("Rerank HTTP client closed")
        except Exception as e:
            logger.error(f"Failed to close Rerank HTTP client: {e}")

        try:
            await wechat_service.close_session()
            logger.info("WeChat HTTP session closed")
        except Exception as e:
            logger.error(f"Failed to close WeChat HTTP session: {e}")

        # 断开 Redis 连接
        try:
            from services.redis_client import disconnect
            await disconnect()
            logger.info("Redis disconnected")
        except Exception as e:
            logger.error(f"Redis disconnect failed: {e}")


# 创建应用
app = FastAPI(
    title="FeClaw Gateway",
    description="FeClaw 智能体网关平台",
    version="1.0.0",
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发阶段允许所有来源；生产环境请按需限制
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Type", "Content-Length", "Content-Encoding"],
)

# 防止 CDN 缓存
app.add_middleware(NoCacheMiddleware)

# 健康检查端点（必须在所有路由之前）
@app.get("/health")
async def health_check() -> dict:
    """健康检查"""
    return {"status": "healthy"}

# 静态文件服务（必须在 static_site_public.router 之前）
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

# 注册路由
app.include_router(apps_gateway.router)  # App 路由网关（必须在 feclaw_domain 之前）
app.include_router(feclaw_domain_router)  # FeClaw 域名专用路由
app.include_router(feclaw_chat_router)  # FeClaw 聊天 API
app.include_router(workspace.router)  # 工作区管理
app.include_router(wechat.router)  # 微信接入
app.include_router(console.router)  # 控制台 API (必须在 static_site_public 之前)
app.include_router(user_router)  # 用户 API (注册、登录)
app.include_router(agent_config_ui_router)  # Agent 配置界面
app.include_router(agent_config_router)  # Agent 配置 API
app.include_router(agent_config_chat_router)  # Agent 配置聊天 API
app.include_router(static_site.router)  # 静态网站托管 API
app.include_router(health.router)  # 健康检查 API (必须在 static_site_public 之前)
app.include_router(vfs_image_dedup.router)  # VFS 图片去重管理 API
app.include_router(sandbox.router)  # 安全沙箱执行环境 API
app.include_router(share.router)  # 分享链接解析
app.include_router(vfs_view.router)  # VFS 文件查看（历史图片/文件展示）
app.include_router(oauth.router)  # OAuth 认证 (必须在 static_site_public 之前)

app.include_router(static_site_public.router)  # 静态网站公开访问


# 注释掉：/ 路由由 feclaw_domain.py 处理，根据域名返回不同页面
# @app.get("/")
# async def root():
#     """根路径"""
#     return {
#         "name": "FeClaw Gateway",
#         "version": "1.0.0",
#         "status": "running"
#     }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG
    )