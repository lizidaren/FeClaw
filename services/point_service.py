"""
积分服务 - 用户每日配额管理

提供每日积分自动初始化、重置和原子扣减功能。
每次对话消耗 1 积分，次日自动重置。
"""

import logging
from datetime import date

from sqlalchemy import text
from models.database import SessionLocal, UserPoints
from config import settings

logger = logging.getLogger(__name__)


class PointService:
    """用户积分配额服务"""

    @staticmethod
    def try_deduct(user_id: str) -> bool:
        """原子扣减，返回是否扣减成功。

        自动初始化新用户、重置每日配额、并原子扣减。
        """
        db = SessionLocal()
        try:
            today = date.today()

            # 先尝试原子 UPDATE（已存在用户 + 跨日重置），消除 SELECT-then-INSERT 竞态
            result = db.execute(
                text(
                    """
                    UPDATE user_points
                    SET
                        used_today = CASE
                            WHEN last_reset IS NULL OR last_reset < :today THEN 1
                            ELSE used_today + 1
                        END,
                        last_reset = CASE
                            WHEN last_reset IS NULL OR last_reset < :today THEN :today
                            ELSE last_reset
                        END,
                        daily_points = :daily_free_points
                    WHERE user_id = :uid
                        AND (
                            (last_reset IS NULL OR last_reset < :today)
                            OR used_today < daily_points
                        )
                    """
                ),
                {
                    "uid": user_id,
                    "today": today,
                    "daily_free_points": settings.DAILY_FREE_POINTS,
                },
            )
            db.commit()

            if result.rowcount > 0:
                return True

            # UPDATE 未命中：可能是新用户或已用完配额。确认是否存在记录
            points = db.query(UserPoints).filter(UserPoints.user_id == user_id).first()

            if points is None:
                # 新用户：尝试 INSERT（唯一约束下并发安全）
                from sqlalchemy.exc import IntegrityError
                points = UserPoints(
                    user_id=user_id,
                    daily_points=settings.DAILY_FREE_POINTS,
                    used_today=1,
                    last_reset=today,
                )
                db.add(points)
                try:
                    db.commit()
                    return True
                except IntegrityError:
                    db.rollback()
                    # 并发INSERT已存在，重试UPDATE
                    return PointService.try_deduct(user_id)

            # 已有记录但 UPDATE 没命中 => 配额用完
            return False
        except Exception as e:
            db.rollback()
            logger.error(f"[PointService] try_deduct error for user {user_id}: {e}")
            return False
        finally:
            db.close()
