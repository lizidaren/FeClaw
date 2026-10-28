#!/usr/bin/env python3
"""管理员账号的显式引导/重置（T20a）。

正常启动（lifespan）**不再**创建或重置管理员，也不再打印密码；账号由本命令
显式创建，重置必须额外确认。这避免“重启即换密码 / 密码进日志聚合”的旧行为。

用法：

    python scripts/bootstrap_admin.py --check          # 只读：是否已有 admin
    python scripts/bootstrap_admin.py --create         # 仅在不存在时创建，随机密码打印一次
    python scripts/bootstrap_admin.py --create --password '...'
    python scripts/bootstrap_admin.py --reset --confirm-reset
                                                       # 重置既有 admin，必须显式确认

退出码：0 成功；2 拒绝（admin 已存在 / 缺少确认）；1 运行错误。
"""

from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="显式创建/重置 FeClaw admin 账号（T20a）")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="只读：报告 admin 是否存在")
    action.add_argument("--create", action="store_true", help="仅当 admin 不存在时创建")
    action.add_argument("--reset", action="store_true", help="重置既有 admin 密码")
    parser.add_argument("--username", default="admin", help="账号名（默认 admin）")
    parser.add_argument("--password", default="",
                        help="显式口令；缺省则生成随机口令并只打印一次")
    parser.add_argument("--confirm-reset", action="store_true",
                        help="与 --reset 搭配：确认覆盖既有口令")
    parser.add_argument("--database-url", default="", help="覆盖 DATABASE_URL")
    args = parser.parse_args(argv)

    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    from sqlalchemy.orm import Session

    from models.database import User, engine
    from services.setup_service import create_or_reset_admin, generate_admin_password

    with Session(engine) as db:
        admin = db.query(User).filter(User.username == args.username).first()

        if args.check:
            print(f"admin_present: {admin is not None}")
            if admin is not None:
                print(f"user_id: {admin.id}")
                print(f"is_admin: {bool(admin.is_admin)}")
            return 0

        if args.create:
            if admin is not None:
                print(f"REFUSED: 账号 {args.username!r} 已存在；如确需重置请用 "
                      f"--reset --confirm-reset", file=sys.stderr)
                return 2
            password = args.password or generate_admin_password(16)
            create_or_reset_admin(db, password)
            print(f"created: {args.username}")
            # 口令只走 stdout（不经 logger，避免进 journald/日志聚合）
            print(f"password: {password}")
            print("口令仅显示一次；请立即登录并修改。")
            return 0

        # --reset
        if admin is None:
            print(f"REFUSED: 账号 {args.username!r} 不存在；改用 --create",
                  file=sys.stderr)
            return 2
        if not args.confirm_reset:
            print("REFUSED: 重置既有管理员口令会影响线上登录，必须显式加 "
                  "--confirm-reset（并确认已获批）", file=sys.stderr)
            return 2
        password = args.password or generate_admin_password(16)
        create_or_reset_admin(db, password)
        print(f"reset: {args.username}")
        print(f"password: {password}")
        print("口令仅显示一次；旧会话请在管理后台吊销。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
