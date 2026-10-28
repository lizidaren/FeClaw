"""微信多绑定去重迁移（Q2-A/D，T20a 起从启动期移出）。

背景：iLink「一个微信同时只能跟一个聊天」，用户「取消绑定 → 绑新的」时解绑旧的是
**无通知的**，库里因此会积累多条同 openid 的 active 行；查询按 id desc 会命中已死的
旧行，把入站/出站路由到错误的 Agent。

策略（与历史实现一致，只做数据修复、不删行、不动凭证）：
* 同一 `wx_openid` 的多条 active 只保留 `(last_msg_at desc, id desc)` 最新的那条；
* 其余置 `superseded`（回退 = 一条 UPDATE 改回 active）；
* 幂等：无重复直接跳过。

T20a：正常启动不再自动执行；由 `scripts/migrate_learning_runtime.py
--wechat-supersede [--apply]` 显式触发（默认 --plan 只读）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def find_duplicate_active_bindings(db) -> Dict[Tuple[str, str], List[Any]]:
    """返回 {openid: [active 绑定行, ...]} 中重复（>=2 条）的分组。只读。"""
    from models.database import WeChatBinding as _WCB

    actives = db.query(_WCB).filter(_WCB.status == "active").all()
    groups: Dict[Tuple[str, str], List[Any]] = {}
    for binding in actives:
        key = ((binding.wx_openid or "").strip(), "")
        if not key[0]:
            continue
        groups.setdefault(key, []).append(binding)
    return {key: rows for key, rows in groups.items() if len(rows) > 1}


def plan_supersede(db) -> List[Dict[str, Any]]:
    """只读预览：会被置为 superseded 的行。"""
    from datetime import datetime as _dt

    rows: List[Dict[str, Any]] = []
    for (_openid, _), bindings in find_duplicate_active_bindings(db).items():
        bindings.sort(key=lambda r: (r.last_msg_at is not None,
                                     r.last_msg_at or _dt.min, r.id), reverse=True)
        keep = bindings[0]
        for loser in bindings[1:]:
            rows.append({
                "id": loser.id,
                "user_id": loser.user_id,
                "agent_hash": loser.agent_hash,
                "openid_suffix": (_openid or "")[-6:],
                "keep_id": keep.id,
            })
    return rows


def supersede_duplicate_bindings(db, dry_run: bool = True) -> List[Tuple[int, Optional[int], str]]:
    """执行（或预览）去重；返回被取代的 [(id, user_id, agent_hash), ...]。"""
    from datetime import datetime as _dt

    changed: List[Tuple[int, Optional[int], str]] = []
    try:
        for (_openid, _), bindings in find_duplicate_active_bindings(db).items():
            bindings.sort(key=lambda r: (r.last_msg_at is not None,
                                         r.last_msg_at or _dt.min, r.id), reverse=True)
            for loser in bindings[1:]:
                changed.append((loser.id, loser.user_id, loser.agent_hash))
                if not dry_run:
                    loser.status = "superseded"
        if changed and not dry_run:
            db.commit()
            logger.warning(
                "Q2-A: superseded %s duplicate active wechat binding(s) "
                "(id, user_id, agent_hash)=%s", len(changed), changed)
    except Exception as exc:  # pragma: no cover - 数据修复失败不应吞掉
        if not dry_run:
            db.rollback()
        logger.error("Q2-A: wechat binding supersede failed: %s", exc)
        raise
    return changed
