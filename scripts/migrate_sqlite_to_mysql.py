"""
SQLite → MySQL 数据迁移脚本

一次性使用，迁移完成后可删除。

用法：
    python3 scripts/migrate_sqlite_to_mysql.py

前置条件：
    - MySQL 8.0 已安装并运行
    - feclaw_migrate 用户存在且有 ALL PRIVILEGES ON feclaw.*
    - .env 中 DATABASE_URL 指向 feclaw_migrate 用户
    - 后端已停止运行
"""

import os
import sys
import time
import shutil
from datetime import datetime

# 确保能找到项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 颜色输出 ────────────────────────────────────────────────────
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"


def log(msg, level="INFO"):
    color = {"OK": GREEN, "WARN": YELLOW, "ERR": RED, "STEP": CYAN}.get(level, "")
    print(f"[{level}] {color}{msg}{RESET}" if color else f"[{level}] {msg}")


# ── 第 0 步：环境检查 ──────────────────────────────────────────
log("=== SQLite → MySQL 数据迁移 ===", "STEP")
t_start = time.time()

# ── 检查后端是否已停止 ──────────────────────────────────────────
try:
    import urllib.request
    resp = urllib.request.urlopen("http://localhost:8080/", timeout=1)
    log(f"后端仍在运行（{resp.status}）！请先停止后端再执行迁移", "ERR")
    sys.exit(1)
except (urllib.error.URLError, ConnectionRefusedError):
    log("后端已停止 ✅", "OK")

BACKUP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "../data",
    f"feclaw.db.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
)

SQLITE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "../data/feclaw.db"
)

if not os.path.exists(SQLITE_PATH):
    log(f"SQLite 数据库不存在: {SQLITE_PATH}", "ERR")
    log("可能已经迁移过了？请检查 data/ 目录", "WARN")
    sys.exit(1)

# 备份
shutil.copy2(SQLITE_PATH, BACKUP_PATH)
log(f"SQLite 已备份到: {BACKUP_PATH}", "OK")

# ── 第 1 步：连接数据库 ──────────────────────────────────────────
from sqlalchemy import create_engine, text
from config import settings

# SQLite 引擎
SQLITE_PATH = os.path.abspath(SQLITE_PATH)
sqlite_url = f"sqlite:///{SQLITE_PATH}"
sqlite_engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
log(f"SQLite 已连接: {SQLITE_PATH}", "OK")

# MySQL 引擎（从 .env 读取，此时必须指向 feclaw_migrate 用户）
mysql_url = settings.DATABASE_URL
if not mysql_url.startswith("mysql"):
    log(f"DATABASE_URL 不是 MySQL: {mysql_url}", "ERR")
    log("请先在 .env 中设置 USE_MYSQL=true 且 DATABASE_URL 指向 feclaw_migrate 用户", "WARN")
    sys.exit(1)

try:
    mysql_engine = create_engine(mysql_url, pool_pre_ping=True)
    # 测试连接
    with mysql_engine.connect() as conn:
        result = conn.execute(text("SELECT VERSION()"))
        log(f"MySQL 已连接: {mysql_url.split('@')[0].split('://')[0]}//...@{mysql_url.split('@')[1]}", "OK")
        log(f"MySQL 版本: {result.scalar()}", "OK")
except Exception as e:
    log(f"MySQL 连接失败: {e}", "ERR")
    sys.exit(1)

# ── 第 2 步：检查 MySQL 是否已有数据 ──────────────────────────────
from models.database import Base, SessionLocal

with mysql_engine.connect() as conn:
    existing = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'feclaw'"
    )).scalar()
    if existing > 0:
        log(f"MySQL 中已存在 {existing} 张表！", "WARN")
        # 检查是否有数据
        for table_name in Base.metadata.tables:
            try:
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
                if count > 0:
                    log(f"  {table_name}: {count} 行数据 — 已存在，跳过迁移", "WARN")
                    log("MySQL 已有数据，迁移中止。如需重建请手动 DROP TABLE", "ERR")
                    sys.exit(1)
            except Exception:
                pass  # 表还不存在，正常
        log("MySQL 有表但无数据，继续迁移", "OK")

# ── 第 3 步：在 MySQL 创建表结构 ──────────────────────────────────
log("在 MySQL 创建表结构...", "STEP")
Base.metadata.create_all(bind=mysql_engine)
tables = list(Base.metadata.sorted_tables)
log(f"已创建 {len(tables)} 张表", "OK")

# ── 第 4 步：迁移数据 ──────────────────────────────────────────────
log("开始迁移数据...", "STEP")
total_rows = 0

# 检查哪些表在 SQLite 中存在但模型不匹配（防止 SELECT * 出错）
sqlite_tables = set()
with sqlite_engine.connect() as src:
    result = src.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    sqlite_tables = {row[0] for row in result.fetchall()}

try:
    for table in tables:
        table_name = table.name

        # 跳过 SQLite 中没有的表（如新模型尚未在 SQLite 建表）
        if table_name not in sqlite_tables:
            log(f"  {table_name:35s} — SQLite 中无此表，跳过", "WARN")
            continue

        # 从 SQLite 读取（捕获模型与旧表结构不匹配的情况）
        try:
            with sqlite_engine.connect() as src:
                rows = src.execute(table.select()).fetchall()
        except Exception as e:
            log(f"  {table_name:35s} — 读取失败 ({e}), 跳过", "WARN")
            continue

        if not rows:
            log(f"  {table_name:35s} — 0 行，跳过", "OK")
            continue

        # 将 Row 对象转为 dict，保持列名一致
        columns = [c.name for c in table.columns]
        row_dicts = [{col: getattr(row, col) for col in columns} for row in rows]

        # 修复 NULL 值和截断超长数据：
        # MySQL 的 NOT NULL/VARCHAR(n) 比 SQLite 严格，
        # SQLite 不检查 NOT NULL 也不检查 VARCHAR 长度
        for rd in row_dicts:
            for col in table.columns:
                val = rd.get(col.name)
                if val is not None:
                    # 截断：VARCHAR(n) 超长数据
                    type_name = col.type.__class__.__name__
                    if 'String' in type_name and hasattr(col.type, 'length'):
                        max_len = col.type.length
                        if isinstance(val, str) and max_len and len(val) > max_len:
                            rd[col.name] = val[:max_len]
                elif val is None and not col.nullable and col.default is None:
                    # 补默认值：NOT NULL 列的 None
                    if 'String' in type_name or 'Text' in type_name:
                        rd[col.name] = ''
                    elif 'Integer' in type_name:
                        rd[col.name] = 0

        # 写入 MySQL（使用 Table.insert() 而非 raw SQL，自动处理保留字）
        with mysql_engine.connect() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            stmt = table.insert()
            conn.execute(stmt, row_dicts)
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            conn.commit()

        log(f"  {table_name:35s} — {len(rows)} 行 → 写入成功", "OK")
        total_rows += len(rows)

except Exception as e:
    log(f"迁移失败: {e}", "ERR")
    log("回滚: MySQL 表已创建但数据不完整。请运行回滚方案还原到 SQLite", "WARN")
    raise

# ── 第 5 步：修正自增基数 ──────────────────────────────────────────
log("修正自增基数...", "STEP")
with mysql_engine.connect() as conn:
    for table in tables:
        if "id" in [c.name for c in table.columns]:
            max_id = conn.execute(
                text(f"SELECT COALESCE(MAX(id), 0) FROM {table.name}")
            ).scalar()
            if max_id > 0:
                conn.execute(
                    text(f"ALTER TABLE {table.name} AUTO_INCREMENT = {max_id + 1}")
                )
                log(f"  {table.name:35s} AUTO_INCREMENT = {max_id + 1}", "OK")
    conn.commit()

# ── 第 6 步：验证 ──────────────────────────────────────────────────
log("\n验证数据一致性...", "STEP")

errors = 0
with sqlite_engine.connect() as src, mysql_engine.connect() as dst:
    for table in tables:
        src_count = src.execute(text(f"SELECT COUNT(*) FROM {table.name}")).scalar()
        dst_count = dst.execute(text(f"SELECT COUNT(*) FROM {table.name}")).scalar()

        if src_count == dst_count:
            log(f"  {table.name:35s} {src_count:>5d} = {dst_count} ✅", "OK")
        else:
            log(f"  {table.name:35s} {src_count:>5d} != {dst_count} ❌", "ERR")
            errors += 1

elapsed = time.time() - t_start

if errors == 0:
    log(f"\n✅ 迁移完成！总行数: {total_rows}, 耗时: {elapsed:.2f}s", "STEP")
    log("操作提示:", "INFO")
    log("  1. 将 .env 中 DATABASE_URL 切回 feclaw 用户（最小权限）", "INFO")
    log("  2. 重启后端: nohup python3 -m uvicorn main:app ...", "INFO")
    log("  3. 运行 curl http://localhost:8080/ 验证", "INFO")
    log(f"  4. 备份文件保留在: {BACKUP_PATH}", "INFO")
else:
    log(f"\n❌ {errors} 张表数据不一致！请回滚到 SQLite", "ERR")
    sys.exit(1)
