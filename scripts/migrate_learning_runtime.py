#!/usr/bin/env python3
"""显式 schema 迁移入口（T20a）。

用法：

    # 只读预览（默认）：列出每条步骤的状态与将执行的 DDL，不写库
    python scripts/migrate_learning_runtime.py --plan
    python scripts/migrate_learning_runtime.py --plan --json

    # 执行缺什么补什么（幂等；重复执行不会重复 DDL）
    python scripts/migrate_learning_runtime.py --apply
    python scripts/migrate_learning_runtime.py --apply --steps users.email,users.is_active

    # 只看版本表
    python scripts/migrate_learning_runtime.py --status

退出码：0 = 没有 pending/blocked；2 = 仍有 blocked（重复数据需人工批准去重）
或执行后仍有 pending；1 = 运行错误（连不上库等）。

边界（规格 §5 T20）：
* 只做 expand（加列/放宽/加索引），不 DROP、不改既有数据；重复 share_hash 只报
  blocked，不自动去重。
* MySQL 的 DDL 可能隐式提交，**不**承诺“失败整体回滚”；中断后重跑本命令即可续跑。
* 管理员账号不在这里创建/重置（那是 `scripts/bootstrap_admin.py`，需要显式确认）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _engine_from_args(args):
    if args.database_url:
        from sqlalchemy import create_engine
        return create_engine(args.database_url, pool_pre_ping=True)
    from models.database import engine
    return engine


def _print_plan(rows) -> None:
    width = max((len(r["name"]) for r in rows), default=10)
    print(f"{'step'.ljust(width)}  {'state':<14} table.column")
    print("-" * (width + 32 + 24))
    for row in rows:
        target = f"{row['table']}.{row['column']}" if row["column"] else row["table"]
        print(f"{row['name'].ljust(width)}  {row['state']:<14} {target}")
        if row["state"] in ("pending", "blocked") and row["ddl"]:
            print(f"{'':<{width}}  {'':<14}   {row['ddl']}")
        if row["state"] == "blocked":
            print(f"{'':<{width}}  {'':<14}   ! {row['note'] or '需要人工处理'}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="FeClaw schema 增量迁移（T20a）：只读 --plan / 显式 --apply")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true",
                      help="只读预览（默认行为）")
    mode.add_argument("--apply", action="store_true",
                      help="执行 pending 步骤（幂等）")
    mode.add_argument("--status", action="store_true",
                      help="只打印 schema_version 记录")
    parser.add_argument("--steps", default="",
                        help="只处理这些步骤名（逗号分隔），用于分批/续跑")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--database-url", default="",
                        help="覆盖 DATABASE_URL（默认用 config 里的库）")
    parser.add_argument("--wechat-supersede", action="store_true",
                        help="数据修复：同一 openid 多条 active 微信绑定去重"
                             "（默认只预览；与 --apply 同用才执行）")
    args = parser.parse_args(argv)

    try:
        engine = _engine_from_args(args)
    except Exception as exc:  # pragma: no cover - 配置错误时给出可读信息
        print(f"ERROR: 无法创建数据库引擎: {exc}", file=sys.stderr)
        return 1

    from services import schema_version as sv

    if args.status:
        rows = sv.schema_version_rows(engine)
        version = sv.current_version(engine)
        if args.json:
            print(json.dumps({"current_version": version, "rows": rows},
                             ensure_ascii=False, default=str, indent=2))
        else:
            print(f"current_version: {version}")
            for row in rows:
                print(f"  v{row.get('version')}  {row.get('step_name')}  "
                      f"{row.get('applied_at')}  {row.get('note') or ''}")
        return 0

    # ── 数据修复（Q2-A/D 微信多绑定去重）：默认只读预览 ──
    if args.wechat_supersede:
        from sqlalchemy.orm import Session

        from services.wechat_binding_repair import (
            plan_supersede,
            supersede_duplicate_bindings,
        )

        with Session(engine) as db:
            candidates = plan_supersede(db)
            if args.json:
                print(json.dumps({"wechat_supersede_candidates": candidates},
                                 ensure_ascii=False, default=str, indent=2))
            else:
                print(f"wechat duplicate candidates: {len(candidates)}")
                for row in candidates[:50]:
                    print(f"  id={row['id']} user={row['user_id']} "
                          f"agent={row['agent_hash']} keep_id={row['keep_id']}")
                if len(candidates) > 50:
                    print(f"  ... 其余 {len(candidates) - 50} 条省略")
            if not args.apply:
                print("\nplan only —— 加 --apply 才会真正写入（只改 status，不删行）")
                return 0

            changed = supersede_duplicate_bindings(db, dry_run=False)
            print(f"\nwechat superseded: {len(changed)}")
        # --apply 时继续执行 schema 迁移

    only = [s for s in (args.steps or "").split(",") if s.strip()]
    selected = sv.SCHEMA_STEPS
    if only:
        wanted = set(only)
        selected = tuple(step for step in sv.SCHEMA_STEPS if step.name in wanted)
        if not selected:
            print(f"ERROR: --steps 没有匹配到任何步骤: {', '.join(only)}",
                  file=sys.stderr)
            return 1

    plan_rows = sv.plan(engine, selected)
    if not args.apply:
        if args.json:
            print(json.dumps(plan_rows, ensure_ascii=False, default=str, indent=2))
        else:
            _print_plan(plan_rows)
            counts = sv.summarize(plan_rows)
            print(f"\nsummary: {counts}")
        pending = [r for r in plan_rows if r["state"] in ("pending", "blocked")]
        return 2 if any(r["state"] == "blocked" for r in pending) else 0

    result = sv.apply(engine, selected, only=only or None)
    after = sv.plan(engine, selected)
    counts = sv.summarize(after)

    if args.json:
        print(json.dumps({"applied": result, "state": counts,
                          "current_version": sv.current_version(engine)},
                         ensure_ascii=False, default=str, indent=2))
    else:
        print(f"applied: {result['applied'] or '-'}")
        if result["skipped"]:
            print(f"already present: {len(result['skipped'])} step(s)")
        if result["blocked"]:
            print(f"BLOCKED (需人工处理): {result['blocked']}")
        if result["unsupported"]:
            print(f"unsupported on {engine.dialect.name}: {result['unsupported']}")
        if result["missing_table"]:
            print(f"missing table (先跑 init_db): {result['missing_table']}")
        print(f"current_version: {sv.current_version(engine)}")
        print(f"summary: {counts}")

    return 2 if (counts.get("blocked") or counts.get("pending")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
