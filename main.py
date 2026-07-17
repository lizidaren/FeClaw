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
from routers import static_site, static_site_public, workspace, wechat, oauth, console, health, vfs_image_dedup, sandbox, share, share_reference, vfs_view, apps_gateway, fehub, dashboard
from routers.feclaw_domain import router as feclaw_domain_router
from routers.feclaw_chat import router as feclaw_chat_router
from routers.agent_config_ui import router as agent_config_ui_router
from routers.agent_config import router as agent_config_router
from routers.agent_config_chat import router as agent_config_chat_router
from routers.user import router as user_router
from routers.admin import router as admin_router
from routers.group import router as group_router
from routers.wechat import ensure_message_handler
from services.agent_init_service import ensure_default_agent_5178
from routers.desktop_ws import router as desktop_ws_router
from routers.well_known import router as well_known_router
from routers.upload import router as upload_router
from routers.desktop_api import router as desktop_api_router
from routers.zentrim import router as zentrim_router
from routers.metrics_internal import router as metrics_internal_router
from routers.setup import router as setup_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    from services.wechat_service import wechat_service

    # 启动时
    logger.info("Starting FeClaw Gateway...")

    # 冷启动检测：SETUP_COMPLETE != true → 只挂载 setup 路由
    if not bool(settings.SETUP_COMPLETE):
        # 冷启动：确保 SETUP_TOKEN 已生成（首次启动时）
        if not (settings.SETUP_TOKEN or "").strip():
            from services.setup_service import generate_setup_token, update_env
            _token = generate_setup_token()
            update_env({"SETUP_TOKEN": _token})
            # 重新加载 settings 让其读到刚写入的 token
            try:
                object.__setattr__(settings, "SETUP_TOKEN", _token)
            except Exception:
                pass
            # 也写一份给前端 banner（仅终端展示一次）
            _host = settings.HOST if settings.HOST not in ("0.0.0.0",) else "localhost"
            _url = f"http://{_host}:{settings.PORT}/setup?token={_token}"
            print(
                "\n"
                "  ╔══════════════════════════════════════════════════════════════╗\n"
                "  ║                                                              ║\n"
                "  ║   FeClaw 冷启动 — 首次运行，请完成配置向导                       ║\n"
                "  ║                                                              ║\n"
                f"  ║   配置地址: {_url:<49s}║\n"
                "  ║                                                              ║\n"
                "  ║   ⚠️  该 URL 含 setup token，配置完成后请勿分享                 ║\n"
                "  ║   ⚠️  配置完成后需重启后端服务（uvicorn / systemctl）            ║\n"
                "  ╚══════════════════════════════════════════════════════════════╝\n"
            )
            logger.warning(
                f"[Setup] 冷启动 token 已生成（{len(_token)} 字符）"
            )
        else:
            logger.info("[Setup] 冷启动模式：使用已有 SETUP_TOKEN")

        # 冷启动：跳到 yield（只挂载 setup 路由 + 首页，不跑数据库初始化）
        try:
            yield
        finally:
            logger.info("Shutting down FeClaw Gateway (cold-start mode)...")
        return

    # ───────────────────────────────────────────────────────────
    # 正常启动：SETUP_COMPLETE=true —— 跑全部初始化
    # ───────────────────────────────────────────────────────────
    if not settings.JWT_SECRET:
        logger.critical("JWT_SECRET 未配置！请在 .env 中设置 JWT_SECRET")
        raise RuntimeError("JWT_SECRET is required but not set")

    # 旧版 banner：admin 密码（保留向后兼容，正常启动时若 DB 里已有 admin 则不重置）
    try:
        from services.setup_service import (
            create_or_reset_admin,
            generate_admin_password,
            is_setup_complete,
            print_admin_banner,
        )
        db_setup = SessionLocal()
        try:
            if not is_setup_complete(db_setup):
                _new_pwd = generate_admin_password(16)
                create_or_reset_admin(db_setup, _new_pwd)
                _host = settings.HOST if settings.HOST not in ("0.0.0.0",) else "localhost"
                print_admin_banner(_new_pwd, host=_host, port=settings.PORT)
                logger.warning(
                    "[Setup] 检测到配置不完整，已生成随机 admin 密码并打印到终端"
                )
            else:
                logger.info("[Setup] 配置完整，跳过首次启动向导")
        finally:
            db_setup.close()
    except Exception as _e:
        logger.warning(f"[Setup] 首次启动检测异常（非致命）: {_e}")

    # 初始化数据库（导入 Group 模型以确保 create_all 覆盖新表）
    from models.group import Group, GroupMember, GroupMessage, GroupMoments  # noqa: F401
    from models.fehub import FePublish, AppData  # noqa: F401
    from models.agent_buffer import AgentBuffer  # noqa: F401  (Agent V2 ReplyBuffer)
    from models.zentrim import ZentrimEntry, ZentrimTimeline, ZentrimTimelineEntry, ZentrimReference  # noqa: F401  (Zentrim 格物所)
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
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_NAME = 'users' AND TABLE_SCHEMA = DATABASE()"
        ))
        columns = [row[0] for row in result.fetchall()]
        if 'email' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(128)"))
            conn.commit()
            logger.info("Added email column to users table")

        # 检查 agent_profiles 表是否有 sr_enabled 列
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_NAME = 'agent_profiles' AND TABLE_SCHEMA = DATABASE()"
        ))
        columns = [row[0] for row in result.fetchall()]
        if 'sr_enabled' not in columns:
            conn.execute(text("ALTER TABLE agent_profiles ADD COLUMN sr_enabled BOOLEAN DEFAULT 0"))
            conn.commit()
            logger.info("Added sr_enabled column to agent_profiles table")

        # 检查 agent_profiles 表是否有 agent_mode 列
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_NAME = 'agent_profiles' AND COLUMN_NAME = 'agent_mode' AND TABLE_SCHEMA = DATABASE()"
        ))
        columns = [row[0] for row in result.fetchall()]
        if 'agent_mode' not in columns:
            conn.execute(text("ALTER TABLE agent_profiles ADD COLUMN agent_mode VARCHAR(20) DEFAULT 'classic'"))
            conn.commit()
            logger.info("Added agent_mode column to agent_profiles table (V2 self-driven mode)")

        # P0.4 bcrypt 迁移：检查 users 表是否有 password_version 列
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'password_version' AND TABLE_SCHEMA = DATABASE()"
        ))
        if not result.fetchone():
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN password_version INT NOT NULL DEFAULT 1"
            ))
            conn.commit()
            logger.info("Added password_version column to users table (P0.4 bcrypt migration)")

        # 检查 users 表是否有 tier 列
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'tier' AND TABLE_SCHEMA = DATABASE()"
        ))
        if not result.fetchone():
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN tier VARCHAR(20) DEFAULT 'pro'"
            ))
            conn.commit()
            logger.info("Added tier column to users table")

        # P0.4 bcrypt 迁移：放宽 salt 列允许 NULL（bcrypt 用户不需要 salt）
        result = conn.execute(text(
            "SELECT IS_NULLABLE FROM information_schema.COLUMNS "
            "WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'salt' AND TABLE_SCHEMA = DATABASE()"
        ))
        row = result.fetchone()
        if row and row[0] == 'NO':
            conn.execute(text("ALTER TABLE users MODIFY COLUMN salt VARCHAR(64) NULL"))
            conn.commit()
            logger.info("Relaxed users.salt to nullable for bcrypt migration")

        # P1.3 agent_hash 列宽统一：扩到 VARCHAR(8)（MySQL 无损扩列，老数据不动）
        # 老 agent hash（4 位如 5656、8d85）保持不变 —— 涉及子域名 URL 兼容性
        _tables_with_agent_hash = [
            "wechat_binding", "wechat_messages", "file_permissions",
            "agent_config", "agent_usage_log", "share_mappings",
            "share_references", "chat_history", "sandbox_tokens",
        ]
        for _tbl in _tables_with_agent_hash:
            try:
                conn.execute(text(
                    f"ALTER TABLE {_tbl} MODIFY COLUMN agent_hash VARCHAR(8)"
                ))
                logger.info(f"P1.3: widened {_tbl}.agent_hash to VARCHAR(8)")
            except Exception as _e:
                logger.debug(f"P1.3: {_tbl}.agent_hash alter skipped: {_e}")
        # agent_profiles.hash（特殊列名，不是 agent_hash）
        try:
            conn.execute(text("ALTER TABLE agent_profiles MODIFY COLUMN hash VARCHAR(8)"))
            logger.info("P1.3: widened agent_profiles.hash to VARCHAR(8)")
        except Exception as _e:
            logger.debug(f"P1.3: agent_profiles.hash alter skipped: {_e}")
        try:
            conn.commit()
        except Exception:
            pass

    # 创建默认管理员用户（如果不存在）
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            import hashlib
            default_password = hashlib.sha256("admin".encode()).hexdigest()
            admin = User(
                username="admin",
                password_hash=hash_password(default_password),
                salt=None,
                password_version=2,
                is_admin=True
            )
            db.add(admin)
            db.commit()
            logger.info("Created default admin user (password: admin, bcrypt)")

        # 创建测试用户
        test_user = db.query(User).filter(User.username == "test").first()
        if not test_user:
            import hashlib
            default_password = hashlib.sha256("test".encode()).hexdigest()
            test_user = User(
                username="test",
                password_hash=hash_password(default_password),
                salt=None,
                password_version=2,
                is_admin=False
            )
            db.add(test_user)
            db.commit()
            logger.info("Created test user (password: test, bcrypt)")
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

    # 种子内置模板
    try:
        from services.template_manager import TemplateManager
        seed_db = SessionLocal()
        try:
            seeded = TemplateManager.seed_builtin_templates(seed_db)
            if seeded:
                logger.info(f"Seeded {seeded} built-in agent templates")
        finally:
            seed_db.close()
    except Exception as e:
        logger.warning(f"Template seeding failed (table may not exist yet): {e}")

    # Agent V2: 启动所有 IM Agent 的协处理器（cron / file_watch）
    try:
        from services.interrupt_controller import CoprocessorService
        started = await CoprocessorService.restart_all()
        logger.info(f"[Coprocessor] lifespan 启动了 {started} 个 IM Agent 协处理器")
    except Exception as e:
        logger.warning(f"[Coprocessor] restart_all 启动失败: {e}")

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
                from services.file_storage import create_file_storage

                storage = create_file_storage(mode=settings.STORAGE_MODE)
                vfs = VirtualFileSystem(storage=storage)
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

    # 启动定期清理任务（每小时清理过期的 ShareReference）
    import asyncio
    from services.share_service import cleanup_expired_references

    async def periodic_share_ref_cleanup():
        while True:
            await asyncio.sleep(3600)  # 每小时
            try:
                db = SessionLocal()
                try:
                    deleted = cleanup_expired_references(db)
                    if deleted:
                        logger.info(f"Cleaned up {deleted} expired share references")
                finally:
                    db.close()
            except Exception as e:
                logger.warning(f"Periodic share reference cleanup failed: {e}")

    cleanup_task = asyncio.create_task(periodic_share_ref_cleanup())

    try:
        yield
    finally:
        # 取消定期清理任务
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

        # Agent V2: 停止所有协处理器
        try:
            from services.interrupt_controller import CoprocessorService
            for agent_hash in list(CoprocessorService._agents.keys()):
                await CoprocessorService.stop(agent_hash)
            logger.info("[Coprocessor] 全部停止")
        except Exception as e:
            logger.warning(f"[Coprocessor] shutdown stop error: {e}")
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


# ───────────────────────────────────────────────────────────
# 路由挂载：按冷启动 vs 正常启动分流
# ───────────────────────────────────────────────────────────

_COLD_START = not bool(settings.SETUP_COMPLETE)

if _COLD_START:
    # 冷启动：只挂载 /setup* + 简单的欢迎首页。
    # 其他路由全部不挂载，防止用户在配置完成前误访问 API 出错。
    from fastapi.responses import HTMLResponse as _HTMLResponse

    @app.get("/", response_class=_HTMLResponse)
    async def _cold_start_home():
        """冷启动首页：只显示欢迎信息和设置入口。

        实际配置入口 URL 已在启动时打印到终端（含 SETUP_TOKEN）。
        """
        return _HTMLResponse(
            """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FeClaw · 冷启动</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #050510;
    color: #e0e0e0;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    text-align: center;
    padding: 24px;
  }
  .container { max-width: 480px; }
  h1 {
    font-size: 3em;
    font-weight: 700;
    background: linear-gradient(135deg, #667eea, #764ba2);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 16px;
    letter-spacing: -0.02em;
  }
  p { color: #888; line-height: 1.7; margin-bottom: 12px; }
  .hint { color: #666; font-size: 0.9em; margin-top: 24px; }
  code {
    background: rgba(255,255,255,0.08);
    padding: 2px 8px;
    border-radius: 4px;
    color: #ccc;
    font-size: 0.9em;
  }
</style>
</head>
<body>
  <div class="container">
    <h1>FeClaw</h1>
    <p>首次启动，请完成配置</p>
    <p class="hint">管理地址（含 token）已打印到启动终端：<br>
       <code>http://&lt;host&gt;:&lt;port&gt;/setup?token=&lt;...&gt;</code></p>
    <p class="hint">配置完成后需重启后端服务。</p>
  </div>
</body>
</html>"""
        )

    # 挂载 setup 路由（包含 /setup 页面 + /setup/* API）
    app.include_router(setup_router)
    logger.info("Cold-start mode: only /setup* + / are mounted")
else:
    # 正常启动：挂载全部路由
    app.include_router(apps_gateway.router)  # App 路由网关（必须在 feclaw_domain 之前）
    app.include_router(fehub.router)  # FeHub VCS + Publish API
    app.include_router(feclaw_domain_router)  # FeClaw 域名专用路由
    app.include_router(desktop_api_router)  # Desktop 客户端 API
    app.include_router(feclaw_chat_router)  # FeClaw 聊天 API
    app.include_router(workspace.router)  # 工作区管理
    app.include_router(wechat.router)  # 微信接入
    app.include_router(console.router)  # 控制台 API (必须在 static_site_public 之前)
    app.include_router(user_router)  # 用户 API (注册、登录)
    app.include_router(group_router)  # Group Chat API
    app.include_router(admin_router)  # 管理后台 API
    app.include_router(setup_router)  # 首次启动配置向导 API（正常启动时也挂载，供 admin 在后台调整）
    app.include_router(agent_config_ui_router)  # Agent 配置界面
    app.include_router(dashboard.router)  # Dashboard 页面
    app.include_router(agent_config_router)  # Agent 配置 API
    app.include_router(agent_config_chat_router)  # Agent 配置聊天 API
    app.include_router(static_site.router)  # 静态网站托管 API
    app.include_router(health.router)  # 健康检查 API (必须在 static_site_public 之前)
    app.include_router(vfs_image_dedup.router)  # VFS 图片去重管理 API
    app.include_router(sandbox.router)  # 安全沙箱执行环境 API
    app.include_router(share.router)  # 分享链接解析
    app.include_router(share_reference.router)  # 分享页引用令牌
    app.include_router(vfs_view.router)  # VFS 文件查看（历史图片/文件展示）
    app.include_router(oauth.router)  # OAuth 认证 (必须在 static_site_public 之前)
    # Desktop WS 通道（条件启用）

    if settings.DESKTOP_ENABLED:
        app.include_router(desktop_ws_router)
        logger.info("Desktop WS relay enabled")
    app.include_router(metrics_internal_router)  # P1.5: 最小 metrics endpoint（admin-only），必须在 static_site_public 前注册（后者有 catch-all）
    app.include_router(zentrim_router)  # Zentrim（格物所）API — 必须在 static_site_public 前面，避免 catch-all 拦截
    app.include_router(static_site_public.router)  # 静态网站公开访问
    logger.info("Upload session router registered")


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
    import sys as _sys

    # CLI: --reset-admin —— 启动前重置 admin 密码并打印 banner
    if "--reset-admin" in _sys.argv:
        _idx = _sys.argv.index("--reset-admin")
        _sys.argv.pop(_idx)
        # 冷启动时数据库尚未初始化，无法重置 admin
        if not bool(settings.SETUP_COMPLETE):
            print(
                "ERROR: --reset-admin 不可用 —— 当前为冷启动模式，"
                "请先通过 /setup 完成首次配置。"
            )
            sys.exit(1)
        # 需要等 lifespan 跑完才能访问 DB；直接在此处提前连接 SessionLocal
        from services.setup_service import (
            create_or_reset_admin,
            generate_admin_password,
            print_admin_banner,
        )
        _pwd = generate_admin_password(16)
        _db = SessionLocal()
        try:
            create_or_reset_admin(_db, _pwd)
        finally:
            _db.close()
        _host = settings.HOST if settings.HOST not in ("0.0.0.0",) else "localhost"
        print_admin_banner(_pwd, host=_host, port=settings.PORT)

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG
    )