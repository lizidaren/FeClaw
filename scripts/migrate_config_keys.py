#!/usr/bin/env python3
"""
Config Key 格式迁移脚本

将旧的 config key 格式迁移到新的扁平化格式：

旧格式 → 新格式
───────────────────────────    ─────────────────
config.global.xxx               global/xxx
config.feishu.xxx               channels/feishu/xxx
config.wechat.xxx               channels/wechat/xxx
config.web.xxx                  channels/web/xxx
{hash}:{name}                   agents/{hash}/{name}

安全特性：
- 迁移前自动备份数据库
- 保留旧数据（只更新 key 字段）
- 检查唯一约束冲突
"""

import sqlite3
import shutil
import sys
import os
from datetime import datetime


def migrate(db_path: str):
    """执行 config key 迁移"""
    if not os.path.exists(db_path):
        print(f"Error: Database not found: {db_path}")
        sys.exit(1)

    # 备份
    backup_path = f"{db_path}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(db_path, backup_path)
    print(f"Backup created: {backup_path}")

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    try:
        # 1. config.global.xxx → global/xxx
        c.execute("""
            UPDATE agent_config
            SET key = 'global/' || substr(key, 15)
            WHERE key LIKE 'config.global.%'
        """)
        global_count = c.rowcount
        print(f"Migrated config.global.* → global/* : {global_count} rows")

        # 2. config.{channel}.xxx → channels/{channel}/xxx
        channel_count = 0
        for channel in ("feishu", "wechat", "web"):
            prefix = f"config.{channel}."
            c.execute("""
                UPDATE agent_config
                SET key = 'channels/""" + channel + """/' || substr(key, """ + str(len(prefix) + 1) + """)
                WHERE key LIKE '""" + prefix + """%'
            """)
            count = c.rowcount
            channel_count += count
            if count:
                print(f"Migrated config.{channel}.* → channels/{channel}/* : {count} rows")

        # 3. {hash}:{name} → agents/{hash}/{name}
        #    agent_hash 固定为 8 字符十六进制字符串（secrets.token_hex(4)）
        #    匹配 8 位 hex hash 后跟冒号的格式
        c.execute("""
            UPDATE agent_config
            SET key = 'agents/' || substr(key, 1, 8) || '/' || substr(key, 10)
            WHERE key GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]:*'
        """)
        agent_count = c.rowcount
        print(f"Migrated {{hash}}:{{name}} → agents/{{hash}}/{{name}} : {agent_count} rows")

        conn.commit()
        print(f"\nMigration complete. Total rows updated: {global_count + channel_count + agent_count}")

    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        print(f"Restore from backup: {backup_path}")
        sys.exit(1)
    finally:
        conn.close()

    # 默认配置预填：为所有已有 Agent 补填默认配置
    _backfill_default_configs(db_path)


def _backfill_default_configs(db_path: str):
    """为所有已有 Agent 补填默认配置"""
    # 添加项目根目录到 path 以便导入
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from services.agent_tools_service import DEFAULT_CONFIG
    from models.database import SessionLocal, AgentConfig, AgentProfile

    db = SessionLocal()
    try:
        agents = db.query(AgentProfile).all()
        count = 0
        for agent in agents:
            for key, value in DEFAULT_CONFIG.items():
                config_key = f"agents/{agent.hash}/{key}"
                existing = db.query(AgentConfig).filter(
                    AgentConfig.key == config_key,
                ).first()
                if not existing:
                    db.add(AgentConfig(
                        key=config_key,
                        value=str(value),
                        agent_hash=agent.hash,
                        permission="readwrite",
                        description=f"{key}",
                    ))
                    count += 1
        db.commit()
        print(f"\nDefault config backfill: filled {count} default configs for {len(agents)} agents")

        # 显示迁移+回填后的快照
        conn = sqlite3.connect(db_path)
        try:
            c = conn.cursor()
            c.execute("SELECT key FROM agent_config ORDER BY key")
            rows = c.fetchall()
            print(f"\nCurrent config keys ({len(rows)} total):")
            for row in rows:
                print(f"  {row[0]}")
        finally:
            conn.close()

    except Exception as e:
        db.rollback()
        print(f"Default config backfill failed: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/feclaw.db"
    migrate(db_path)
