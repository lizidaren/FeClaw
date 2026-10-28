"""
FeClaw 智能体网关平台
FastAPI 应用入口
"""

import asyncio
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
try:
    from services.cos_patch import apply_cos_patch
    apply_cos_patch()
except ImportError:
    pass  # COS SDK 补丁可选，无 COS 时跳过

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
from routers.admin_panel import router as admin_panel_router
from routers.group import router as group_router
from routers.approval import router as approval_router  # P4: 审批 API
from routers.organization import router as organization_router  # P4: 组织 API
from routers.wechat import ensure_message_handler
from services.agent_init_service import ensure_default_agent_5178
from routers.client_ws import router as client_ws_router
from routers.well_known import router as well_known_router
from routers.upload import router as upload_router
from routers.upload_general import router as upload_general_router
from routers.desktop_api import router as desktop_api_router
from routers.zentrim import router as zentrim_router
from routers.metrics_internal import router as metrics_internal_router
from routers.setup import router as setup_router


def migrate_wechat_binding_supersede() -> list:
    """兼容入口：去重逻辑已迁到 `services/wechat_binding_repair.py`（T20a）。

    正常启动**不再调用**本函数；显式迁移请用：
        python scripts/migrate_learning_runtime.py --wechat-supersede --plan
        python scripts/migrate_learning_runtime.py --wechat-supersede --apply

    Returns: 被取代的 [(id, user_id, agent_hash), ...]
    """
    from services.wechat_binding_repair import supersede_duplicate_bindings

    db = SessionLocal()
    try:
        return supersede_duplicate_bindings(db, dry_run=False)
    finally:
        db.close()


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

    # T20a：启动期不再检测配置后自动创建/重置 admin，也不再打印口令。
    # 首次部署用 scripts/bootstrap_admin.py --create（显式、只打印一次）。
    logger.info("[Setup] 启动期不创建/重置管理员；如需引导请运行 "
                "scripts/bootstrap_admin.py --create")

    # 初始化数据库：init_db 只建缺失的表（新部署），**不**升级既有表；
    # 所有模型注册由 models.database.load_all_models() 统一负责。
    init_db()
    logger.info("Database initialized")

    # T20a：只读版本提示 —— 启动期不修库；版本不符时 /ready 返回 503，
    # 由运维显式跑迁移（--plan 预览 / --apply 执行）。
    try:
        from services.schema_version import SCHEMA_VERSION, current_version
        _schema_ver = current_version(engine)
        if _schema_ver is None or int(_schema_ver) < int(SCHEMA_VERSION):
            logger.warning(
                "[Schema] 数据库 schema 版本=%s，期望=%s；请运行 "
                "`python scripts/migrate_learning_runtime.py --plan` 预览后 --apply",
                _schema_ver, SCHEMA_VERSION,
            )
        else:
            logger.info("[Schema] version=%s", _schema_ver)
    except Exception as _e:  # 缺表/连不上时不阻塞启动，交给 /ready
        logger.warning("[Schema] 版本检查失败（不阻塞启动）: %s", _e)

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

    # T20a：启动期不再做 ALTER/MODIFY/数据回填。
    # 旧库升级必须显式执行：
    #     python scripts/migrate_learning_runtime.py --plan    # 只读预览
    #     python scripts/migrate_learning_runtime.py --apply   # 执行 expand
    # T20a：微信多绑定去重不在启动期执行（数据回填属显式迁移）：
    #   python scripts/migrate_learning_runtime.py --wechat-supersede --plan
    #   python scripts/migrate_learning_runtime.py --wechat-supersede --apply

    # T20a：管理员账号**不在启动期**创建或重置（旧实现会在配置不完整时
    # 自动生成口令、重置既有密码并打印到终端/日志）。首次部署请显式执行：
    #     python scripts/bootstrap_admin.py --create
    # 既有口令重置必须人工确认：
    #     python scripts/bootstrap_admin.py --reset --confirm-reset
    logger.info(
        "[Setup] 启动期不创建/重置管理员账号；如需要请运行 "
        "scripts/bootstrap_admin.py --create（或 --reset --confirm-reset）"
    )

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
        # T19：FUSE 的 import 本身也可能因缺 trio/pyfuse3 失败，
        # 必须和挂载逻辑一起放在 guard 里，否则 FUSE_AUTO_FALLBACK 救不了启动。
        try:
            from services.vfs_fuse_daemon import check_fuse_available
            _fuse_import_error = None
        except Exception as _fuse_exc:  # noqa: BLE001
            check_fuse_available = None
            _fuse_import_error = _fuse_exc
        if _fuse_import_error is not None:
            if settings.FUSE_AUTO_FALLBACK:
                logger.warning("FUSE 依赖不可用（%s），回退到仿真模式", _fuse_import_error)
            else:
                logger.error("FUSE 依赖不可用：%s", _fuse_import_error)
                raise RuntimeError("FUSE is required but dependency import failed")
        elif check_fuse_available():
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

    # 启动定期清理任务
    import asyncio
    from services.share_service import cleanup_expired_references
    from services.tool_log_service import cleanup_tool_logs

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

    async def periodic_tool_log_cleanup():
        while True:
            try:
                result = await asyncio.to_thread(cleanup_tool_logs)
                if result["deleted"] or result["failed"]:
                    logger.info(
                        "Tool log cleanup: deleted=%s failed=%s cutoff=%s",
                        result["deleted"],
                        result["failed"],
                        result["cutoff_date"],
                    )
            except Exception as e:
                logger.warning(f"Periodic tool log cleanup failed: {e}")
            await asyncio.sleep(86400)  # 每天

    cleanup_task = asyncio.create_task(periodic_share_ref_cleanup())
    tool_log_cleanup_task = asyncio.create_task(periodic_tool_log_cleanup())

    try:
        yield
    finally:
        # 取消定期清理任务
        cleanup_task.cancel()
        tool_log_cleanup_task.cancel()
        for task in (cleanup_task, tool_log_cleanup_task):
            try:
                await task
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
# T18a/F8：Swagger / ReDoc / OpenAPI 默认不暴露（生产会泄露全部路由与 schema）；
# 只有 DEBUG 或显式 FECLAW_EXPOSE_DOCS=1 时才挂载，供本地调试使用。
def _docs_enabled() -> bool:
    """是否暴露 /docs、/redoc、/openapi.json（T18a/F8）。

    默认关闭；`FECLAW_EXPOSE_DOCS=true` 或 `DEBUG=true` 时打开。
    走 config（pydantic-settings）而不是直接读 os.environ，保证配置单一权威、
    `.env` 与测试都能覆盖。
    """
    return bool(getattr(settings, "FECLAW_EXPOSE_DOCS", False)) or bool(
        getattr(settings, "DEBUG", False)
    )


_EXPOSE_DOCS = _docs_enabled()

app = FastAPI(
    title="FeClaw Gateway",
    description="FeClaw 智能体网关平台",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _EXPOSE_DOCS else None,
    redoc_url="/redoc" if _EXPOSE_DOCS else None,
    openapi_url="/openapi.json" if _EXPOSE_DOCS else None,
)

# 配置 CORS — 动态根据 FECLAW_PUBLIC_URL 设置
# Q21/M1：原实现未配置 FECLAW_PUBLIC_URL 时 `allow_origins=["*"]` 且
# `allow_credentials=True` —— Starlette 会在带 Cookie 的请求下把 Origin 原样
# 回显并附 `Access-Control-Allow-Credentials: true`（CWE-942 通配+凭证）。
# 修复：无 FECLAW_PUBLIC_URL 时不给任何跨域授权（fail-closed）。
cors_origins = []
if settings.FECLAW_PUBLIC_URL:
    cors_origins = [
        f"https://{settings.FECLAW_PUBLIC_URL}",
        f"http://{settings.FECLAW_PUBLIC_URL}",
    ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Type", "Content-Length", "Content-Encoding"],
)

# 防止 CDN 缓存
app.add_middleware(NoCacheMiddleware)


# T18a/F7：统一安全响应头（对所有响应生效，包括 4xx/5xx 与静态页）。
# 说明：CSP 先上 Report-Only —— 现有模板大量内联脚本/样式，直接强制会打挂页面；
# 这里只收集违规报告，不改变现有行为。
@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=(), payment=()",
    )
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    if proto == "https":
        headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains",
        )
    headers.setdefault(
        "Content-Security-Policy-Report-Only",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
        "style-src 'self' 'unsafe-inline' https:; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data: https:; "
        "connect-src 'self' https: wss:; "
        "frame-ancestors 'self'; base-uri 'self'; form-action 'self'",
    )
    return response


# ───────────────────────────────────────────────────────────
# 健康检查：/health = 轻量 liveness；/ready = 真实依赖 readiness（T02）
#
# 设计约束（规格 §5 T02 / §9 A22）：
#   • /health 保持轻量：不碰 DB/存储/模型，只证明事件循环还能响应。
#   • /ready 检查 DB 连接、目标 schema（版本 + 关键表）、存储配置可用；
#     失败一律 HTTP 503，成功 200 且 JSON status == "ready"（deploy.sh 精确判这个字段）。
#   • 只读：不做 DDL、不建表、不修数据、不重置 admin —— 这一点很重要，
#     因为 T20 会把这些启动期 ALTER/重置从 lifespan 拆走；/ready 不能反过来依赖它们。
#     因此这里用 SQLAlchemy inspector 读已有表/列，而不是假设某次 ALTER 已经跑过。
#   • 不泄露：响应只含 ok 布尔、后端名、异常**类名**；绝不回显 DSN/口令/堆栈。
# ───────────────────────────────────────────────────────────

from fastapi.responses import JSONResponse

# 关键表：缺任何一个 ⇒ 这个进程无法正确服务业务，ready 必须为 false。
READY_REQUIRED_TABLES = (
    "users",
    "agent_profiles",
    "agent_config",
    "chat_history",
    "conversation_sessions",
    "file_permissions",
    "scheduled_tasks",
)

# schema_version 由 T20（独立版本迁移）落地。T20a 后这里读 services.schema_version
# 的 SCHEMA_VERSION / CRITICAL_COLUMNS 作单一权威；表尚未落地时仍是**可选**检查：
# /ready 会如实报告 version_state="absent"，结构（关键表/关键列）仍然判定。
READY_SCHEMA_VERSION_TABLE = "schema_version"
READY_MIN_SCHEMA_VERSION = 1

# /ready 总预算：DB 不可达时不能把探活请求挂死，也不能让 load balancer 误判为健康。
READY_TIMEOUT_SECONDS = 5.0


def _schema_meta_module():
    """T20a 的 schema 元数据模块（SCHEMA_VERSION / CRITICAL_COLUMNS / 版本读取）。"""
    try:
        import importlib
        return importlib.import_module("services.schema_version")
    except Exception:  # noqa: BLE001 —— 模块缺失也不该让 /ready 500
        return None


def _ready_engine():
    """返回要检查的数据库引擎（测试可替换）。"""
    from models.database import engine as _engine
    return _engine


def _check_database(engine) -> dict:
    """DB 连接检查：只发 SELECT 1，异常只回类名。"""
    from sqlalchemy import text as _sql_text
    try:
        with engine.connect() as conn:
            conn.execute(_sql_text("SELECT 1"))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 —— 只报告类型，不回显异常文本
        return {"ok": False, "error": type(exc).__name__}


def _raw_schema_versions(engine) -> list:
    """兜底读取 schema_version 记录（T20a 模块不可用时的最小实现）。"""
    from sqlalchemy import text as _sql_text
    with engine.connect() as conn:
        rows = conn.execute(_sql_text(
            f"SELECT version FROM {READY_SCHEMA_VERSION_TABLE}"
        )).fetchall()
    return [{"version": r[0]} for r in rows]


def _check_schema(engine) -> dict:
    """目标 schema 检查：关键表 + 关键列（T20a 单一权威）+ 已记录的版本下限。

    判定规则（诚实分层，避免“结构坏了却只看版本号”）：
      • 关键表缺失            ⇒ 不 ready（核心功能直接不可用）
      • 关键列缺失            ⇒ 不 ready（相关接口必 500，等 migrate --apply）
      • schema_version 表缺失 ⇒ 可选（T20 尚未落地）：报告 absent，不据此失败
      • 表存在但无记录        ⇒ 报告 unrecorded：版本门禁尚未建立，结构检查仍生效
      • 记录版本 < 目标版本   ⇒ 不 ready（要求 operator 显式 migration）
    """
    from sqlalchemy import inspect as _sa_inspect

    sv = _schema_meta_module()
    target = int(getattr(sv, "SCHEMA_VERSION", READY_MIN_SCHEMA_VERSION)) if sv else READY_MIN_SCHEMA_VERSION
    report: dict = {"ok": False, "target_version": target}
    try:
        insp = _sa_inspect(engine)
        tables = set(insp.get_table_names())
    except Exception as exc:  # noqa: BLE001
        report.update({"error": type(exc).__name__, "version_state": "unknown"})
        return report

    missing = [t for t in READY_REQUIRED_TABLES if t not in tables]
    report.update({
        "required_tables": list(READY_REQUIRED_TABLES),
        "missing_tables": missing,
    })
    ok = not missing

    # 关键列：T20a CRITICAL_COLUMNS 是唯一清单，缺列=相关接口 500。
    critical = dict(getattr(sv, "CRITICAL_COLUMNS", {}) or {}) if sv else {}
    missing_columns: dict = {}
    for table, columns in critical.items():
        if table not in tables:
            missing_columns[table] = list(columns)
            continue
        try:
            have = {c["name"] for c in insp.get_columns(table)}
        except Exception:  # noqa: BLE001
            missing_columns[table] = list(columns)
            continue
        missing_for_table = [c for c in columns if c not in have]
        if missing_for_table:
            missing_columns[table] = missing_for_table
    report["critical_columns_missing"] = missing_columns
    if missing_columns:
        ok = False

    if READY_SCHEMA_VERSION_TABLE not in tables:
        report.update({
            "version_state": "absent",
            "version": None,
            "version_note": (
                "schema_version 表尚未落地（T20 迁移未执行）：本次按结构（关键表/关键列）判定；"
                "落地后此处会比较已记录版本与目标版本"
            ),
        })
        report["ok"] = ok
        return report

    try:
        if sv is not None and hasattr(sv, "schema_version_rows"):
            rows = sv.schema_version_rows(engine)
        else:
            rows = _raw_schema_versions(engine)
        versions = [int(r["version"]) for r in rows if r.get("version") is not None]
        current = max(versions) if versions else None
    except Exception as exc:  # noqa: BLE001
        report.update({"version_state": "unreadable", "error": type(exc).__name__})
        report["ok"] = False
        return report

    if current is None:
        report.update({
            "version_state": "unrecorded",
            "version": None,
            "version_note": (
                "schema_version 表存在但没有任何迁移记录；结构检查仍生效，"
                "版本门禁需 `scripts/migrate_learning_runtime.py --apply` 记录后启用"
            ),
        })
    else:
        report.update({"version_state": "present", "version": current})
        if current < target:
            ok = False
    report["ok"] = ok
    return report


def _storage_backend_name() -> str:
    mode = str(getattr(settings, "STORAGE_MODE", "auto") or "auto").lower()
    if mode in ("local", "cos"):
        return mode
    cos_configured = all([
        getattr(settings, "TENCENT_COS_SECRET_ID", None),
        getattr(settings, "TENCENT_COS_SECRET_KEY", None),
        getattr(settings, "TENCENT_COS_BUCKET", None),
    ])
    return "cos" if cos_configured else "local"


def _check_storage() -> dict:
    """存储可用性：local 检查目录可写；cos 检查必要配置存在。

    两种都**不创建目录、不写对象、不发网络请求**（/ready 会被高频探活，
    不做计费/有副作用的调用）；返回值只含后端名与布尔。
    """
    backend = _storage_backend_name()
    if backend == "local":
        root = os.path.abspath(str(getattr(settings, "LOCAL_STORAGE_ROOT", "./feclaw-storage")))
        exists = os.path.isdir(root)
        writable = os.access(root, os.W_OK) if exists else os.access(
            os.path.dirname(root) or ".", os.W_OK
        )
        return {
            "ok": bool(writable),
            "backend": "local",
            "root_present": bool(exists),
            "writable": bool(writable),
            # 明确声明 /ready 没有制造副作用
            "created": False,
        }

    config_present = {
        "TENCENT_COS_SECRET_ID": bool(getattr(settings, "TENCENT_COS_SECRET_ID", None)),
        "TENCENT_COS_SECRET_KEY": bool(getattr(settings, "TENCENT_COS_SECRET_KEY", None)),
        "TENCENT_COS_BUCKET": bool(getattr(settings, "TENCENT_COS_BUCKET", None)),
    }
    return {
        "ok": all(config_present.values()),
        "backend": "cos",
        "config_present": config_present,
        "network_probe": "skipped",
        "note": "只验证配置存在；存储连通性由独立低频巡检观察，避免每次探活都发远端请求",
    }


def collect_readiness() -> dict:
    """汇总 readiness 检查（纯只读，可在任何进程内调用/测试）。"""
    engine = _ready_engine()
    checks = {
        "database": _check_database(engine),
    }
    # DB 不通时 schema 检查必然失败，分开报告以便定位（但同样只回类名）
    checks["schema"] = _check_schema(engine) if checks["database"]["ok"] else {
        "ok": False, "version_state": "unknown", "skipped": True,
        "reason": "database-unavailable",
    }
    checks["storage"] = _check_storage()
    return checks


def _ready_payload(checks: dict) -> dict:
    ok = all(bool(c.get("ok")) for c in checks.values())
    payload = {"status": "ready" if ok else "unhealthy", "checks": checks}
    if not bool(getattr(settings, "SETUP_COMPLETE", False)):
        # 诊断提示：冷启动进程本来就不该 ready（DB/schema/存储检查也会失败）。
        # 只加字段帮助运维区分「没配置」与「配置了但依赖坏了」，不改变门禁语义。
        payload["setup_state"] = "cold-start"
    return payload


@app.get("/health")
async def health_check() -> dict:
    """轻量 liveness：不碰 DB / 存储 / 模型，只说明进程还能响应。"""
    return {"status": "healthy"}


@app.get("/ready")
async def readiness_check() -> JSONResponse:
    """/ready：真实依赖就绪检查（DB 连接 + 目标 schema + 存储配置）。

    失败返回 503，成功 200 且 body.status == "ready"。
    """
    try:
        checks = await asyncio.wait_for(
            asyncio.to_thread(collect_readiness), READY_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        payload = {
            "status": "unhealthy",
            "checks": {"readiness": {"ok": False, "error": "timeout",
                                     "budget_seconds": READY_TIMEOUT_SECONDS}},
        }
        return JSONResponse(payload, status_code=503,
                            headers={"Cache-Control": "no-store"})
    except Exception as exc:  # noqa: BLE001 —— 不把内部异常文本回给探活方
        payload = {
            "status": "unhealthy",
            "checks": {"readiness": {"ok": False, "error": type(exc).__name__}},
        }
        return JSONResponse(payload, status_code=503,
                            headers={"Cache-Control": "no-store"})

    payload = _ready_payload(checks)
    status_code = 200 if payload["status"] == "ready" else 503
    return JSONResponse(payload, status_code=status_code,
                        headers={"Cache-Control": "no-store"})


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
    app.include_router(approval_router)  # P4: 审批同意/拒绝 API
    app.include_router(organization_router)  # P4: 组织 API
    app.include_router(admin_router)  # 管理后台 API (/api/admin/*)
    app.include_router(admin_panel_router)  # 管理后台页面 + 配置 + 统计 (/admin/*)
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
        app.include_router(client_ws_router)
        logger.info("Client WS relay enabled (desktop + mobile)")
    app.include_router(metrics_internal_router)  # P1.5: 最小 metrics endpoint（admin-only），必须在 static_site_public 前注册（后者有 catch-all）
    app.include_router(zentrim_router)  # Zentrim（格物所）API — 必须在 static_site_public 前面，避免 catch-all 拦截
    app.include_router(upload_general_router)  # 通用文件上传 (P0-1)：POST /api/upload
    # P0（S8 发现）：`upload_router` 一直被 import 但**从未挂载** —— 手机端 COS 直传
    # `/api/desktop/upload_session`、`/api/desktop/upload_done` 全 405（被静态站
    # catch-all 吃掉），而日志仍打印 "registered"。这里补上挂载。
    app.include_router(upload_router)  # Desktop 上传会话：/api/desktop/upload_session|upload_done
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

    # CLI: --reset-admin —— 兼容入口；T20a 起必须显式确认，避免误触发口令重置
    if "--reset-admin" in _sys.argv:
        _idx = _sys.argv.index("--reset-admin")
        _sys.argv.pop(_idx)
        if "--confirm-reset-admin" not in _sys.argv:
            print(
                "ERROR: --reset-admin 现在必须显式确认。请改用：\n"
                "  python scripts/bootstrap_admin.py --reset --confirm-reset\n"
                "（该命令会生成新口令并只打印一次；确认已获授权后再执行）"
            )
            sys.exit(2)
        _sys.argv.pop(_sys.argv.index("--confirm-reset-admin"))
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