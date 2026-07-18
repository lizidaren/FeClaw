"""
迁移脚本：把 User.platform_user_id 数据复制到 UserLink 表

一次性的，生产上先跑这个再升级代码。
- 只处理 platform_user_id 非空的 User
- 已存在对应 UserLink 的跳过（幂等，可重复执行）
- 不删除 User.platform_user_id 列（保持向后兼容）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import SessionLocal, User, UserLink


def migrate():
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.platform_user_id.isnot(None)).all()
        count = 0
        skipped = 0
        for user in users:
            existing = db.query(UserLink).filter(
                UserLink.provider == "platform",
                UserLink.provider_user_id == user.platform_user_id,
            ).first()
            if not existing:
                link = UserLink(
                    user_id=user.id,
                    provider="platform",
                    provider_user_id=user.platform_user_id,
                    provider_username=user.username,
                )
                db.add(link)
                count += 1
            else:
                skipped += 1
        db.commit()
        print(f"Migrated {count} user links (skipped {skipped} already-linked)")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
