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
    """Q2-A/D 幂等迁移：同一微信号（openid）只保留最近活跃的一条 active，其余置 superseded。

    为什么：iLink「一个微信同时只能跟一个聊天」，用户「取消绑定 → 绑新的」时
    解绑旧的是**无通知的**，库里因此会积累多条同 openid 的 active 行；
    查询按 id desc 会命中已死的旧行，把入站/出站路由到错误的 Agent
    （§AA「没有消息可存档」· §AC「出站 session timeout」· §AH「招呼被 e45a 回了」的根因）。

    幂等：无重复直接跳过；不删行、不动凭证（回退 = 一条 UPDATE 改回 active）。
    选保留者：(last_msg_at desc, id desc) —— 最近真正活跃的那条才是当前会话。

    Returns: 被取代的 [(id, user_id, agent_hash), ...]（便于自证脚本断言）
    """
    from datetime import datetime as _dt
    from models.database import WeChatBinding as _WCB
    changed = []
    db = SessionLocal()
    try:
        actives = db.query(_WCB).filter(_WCB.status == "active").all()
        groups = {}
        for b in actives:
            key = (b.wx_openid or "").strip()
            if not key:
                continue
            groups.setdefault(key, []).append(b)
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            rows.sort(key=lambda r: (r.last_msg_at is not None, r.last_msg_at or _dt.min, r.id), reverse=True)
            for loser in rows[1:]:
                changed.append((loser.id, loser.user_id, loser.agent_hash))
                loser.status = "superseded"
        if changed:
            db.commit()
            logger.warning(
                "Q2-A: superseded {} duplicate active wechat binding(s) "
                "(id, user_id, agent_hash)={}".format(len(changed), changed)
            )
    except Exception as e:
        db.rollback()
        logger.error(f"Q2-A: wechat binding supersede migration failed: {e}")
    finally:
        db.close()
    return changed


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
    from models.organization import Organization  # noqa: F401  (P4: 组织)
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

        # Q1 登出吊销：users.jwt_version（登出 +1 ⇒ 旧 token 立即失效）
        # 幂等：先查 information_schema，已存在则跳过。默认 0 ⇒ 不强制登出现有用户。
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'jwt_version' AND TABLE_SCHEMA = DATABASE()"
        ))
        if not result.fetchone():
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN jwt_version INT NOT NULL DEFAULT 0"
            ))
            conn.commit()
            logger.info("Added jwt_version column to users table (Q1 logout revocation)")

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

        # FIX-E/P0-1：group_messages.sender_hash 曾为 VARCHAR(4)，写入方可能传 8 位
        # agent hash（agent_profiles.hash 允许 4 或 8 位），MySQL 静默截断 ⇒ 群消息
        # 归属错人。扩到 VARCHAR(8)（无损扩列，幂等可重复跑）。
        try:
            conn.execute(text(
                "ALTER TABLE group_messages MODIFY COLUMN sender_hash VARCHAR(8) NULL"
            ))
            logger.info("FIX-E: widened group_messages.sender_hash to VARCHAR(8)")
        except Exception as _e:
            logger.debug(f"FIX-E: group_messages.sender_hash alter skipped: {_e}")

        # FIX-E/P0-1：share_mappings.share_hash 模型已声明 unique=True（Q20/H19），但
        # create_all 不会给已存在的表补唯一索引 ⇒ 补迁移。⚠️ 加唯一索引前先查重复值，
        # 有重复则只告警不硬加（避免迁移在脏数据上失败 / 掩盖既有跨租户解析串味）。
        try:
            _dup = conn.execute(text(
                "SELECT share_hash FROM share_mappings "
                "GROUP BY share_hash HAVING COUNT(*) > 1 LIMIT 1"
            )).fetchone()
            _has_unique = conn.execute(text(
                "SELECT INDEX_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_NAME='share_mappings' AND TABLE_SCHEMA=DATABASE() "
                "AND COLUMN_NAME='share_hash' AND NON_UNIQUE=0 LIMIT 1"
            )).fetchone()
            if _dup is not None:
                logger.warning(
                    "FIX-E: share_mappings.share_hash has duplicate value(s) "
                    f"(e.g. {_dup[0]}); skipping unique index — needs manual dedup"
                )
            elif _has_unique is None:
                conn.execute(text(
                    "ALTER TABLE share_mappings "
                    "ADD UNIQUE INDEX uq_share_mappings_share_hash (share_hash)"
                ))
                logger.info("FIX-E: added unique index uq_share_mappings_share_hash")
            else:
                logger.debug("FIX-E: share_mappings.share_hash unique index already present")
        except Exception as _e:
            logger.debug(f"FIX-E: share_mappings.share_hash unique index migration skipped: {_e}")
        try:
            conn.commit()
        except Exception:
            pass

        # P4: groups.organization_id 列 + 索引（外键 use_alter 避免循环依赖）
        try:
            org_cols = [r[0] for r in conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_NAME='groups' AND TABLE_SCHEMA=DATABASE()"
            )).fetchall()]
            if 'organization_id' not in org_cols:
                conn.execute(text(
                    "ALTER TABLE groups ADD COLUMN organization_id INT NULL"
                ))
                conn.execute(text(
                    "CREATE INDEX idx_groups_organization_id ON groups(organization_id)"
                ))
                conn.commit()
                logger.info("P4: added groups.organization_id column + index")
        except Exception as _e:
            logger.debug(f"P4: groups.organization_id migration skipped: {_e}")

        # P1.x: group_members 新增 3 列（job_description/status/allow_dm）
        # 与 models/group.py 的 GroupMember 对齐；缺列会导致群成员相关接口 500。
        try:
            gm_cols = [r[0] for r in conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_NAME='group_members' AND TABLE_SCHEMA=DATABASE()"
            )).fetchall()]
            _gm_added = []
            if 'job_description' not in gm_cols:
                conn.execute(text("ALTER TABLE group_members ADD COLUMN job_description TEXT NULL"))
                _gm_added.append('job_description')
            if 'status' not in gm_cols:
                conn.execute(text("ALTER TABLE group_members ADD COLUMN status VARCHAR(16) DEFAULT 'dormant'"))
                _gm_added.append('status')
            if 'allow_dm' not in gm_cols:
                conn.execute(text("ALTER TABLE group_members ADD COLUMN allow_dm BOOLEAN DEFAULT 1"))
                _gm_added.append('allow_dm')
            if _gm_added:
                conn.commit()
                logger.info(f"P1.x: added group_members columns: {', '.join(_gm_added)}")
        except Exception as _e:
            logger.debug(f"P1.x: group_members columns migration skipped: {_e}")

        # Q7/DEPLOY-GATE: 补缺列幂等迁移 —— 与 models/database.py 对齐
        # conversation_sessions.channel + chat_history.tool_call_id/tool_name/tool_args
        # 旧库升级时 create_all 不会给已存在的表加列，缺列会导致相关接口 500。
        try:
            _missing_cols = {
                "conversation_sessions": {"channel": "VARCHAR(32) NULL"},
                "chat_history": {
                    "tool_call_id": "VARCHAR(64) NULL",
                    "tool_name": "VARCHAR(64) NULL",
                    "tool_args": "JSON NULL",
                },
            }
            for _tbl, _cols in _missing_cols.items():
                _existing = [r[0] for r in conn.execute(text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    f"WHERE TABLE_NAME='{_tbl}' AND TABLE_SCHEMA=DATABASE()"
                )).fetchall()]
                _added = [c for c in _cols if c not in _existing]
                for _col in _added:
                    conn.execute(text(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_cols[_col]}"))
                if _added:
                    conn.commit()
                    logger.info(f"Q7: added {_tbl} columns: {', '.join(_added)}")
        except Exception as _e:
            logger.debug(f"Q7: missing-column migration skipped: {_e}")

    # Q2-A/D：微信多绑定根治（逻辑见 migrate_wechat_binding_supersede，便于自证脚本复用它）
    migrate_wechat_binding_supersede()

    # 创建默认管理员用户（如果不存在）
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            import os as _os, secrets
            _random_pwd = secrets.token_hex(12)
            _final_pwd = _os.environ.get("FECLAW_ADMIN_PASSWORD", _random_pwd)
            admin = User(
                username="admin",
                password_hash=hash_password(_final_pwd),
                salt=None,
                password_version=2,
                is_admin=True
            )
            db.add(admin)
            db.commit()
            logger.info("=" * 60)
            logger.info("  🚀 初始管理员账户已创建")
            logger.info(f"  用户名: admin")
            # Q21/L4：口令不再写入应用日志（日志聚合/告警系统会长期留存凭据），
            # 改走 stdout 打印一次（与 print_admin_banner 同源，仅供终端查看）。
            print(f"  [FeClaw] 初始管理员密码: {_final_pwd}")
            logger.info("  ⚠️ 密码仅在终端显示一次，请立即登录并修改！")
            logger.info("=" * 60)

        # Q19/H12：不再自动创建 `test` 账号 —— 口令由源码决定（sha256("test")），
        # 任何部署都会有一个可推导凭据的账号，构成在野风险。已删除该创建逻辑。
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
app = FastAPI(
    title="FeClaw Gateway",
    description="FeClaw 智能体网关平台",
    version="1.0.0",
    lifespan=lifespan
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