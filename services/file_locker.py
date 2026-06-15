"""
基于 SQLite 的分布式文件读写锁
- 读锁共享，写锁排他
- 跨进程/跨渠道生效
- 30 秒自动过期，每 10 秒清理
"""

import sqlite3
import time
import threading
import logging

logger = logging.getLogger(__name__)


class DistributedFileLock:
    """
    分布式文件锁管理器

    使用 SQLite 作为锁存储（简单、可靠、跨进程）
    - 所有 FeClaw 进程共享同一个锁数据库
    - 同一用户的所有沙箱实例都受约束
    - 锁键: user_id + file_path
    """

    def __init__(self, db_path: str = "/tmp/feclaw_file_locks.db"):
        self._db_path = db_path
        self._local = threading.local()

        # 初始化数据库
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_locks (
                user_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                lock_type TEXT NOT NULL,  -- 'R' or 'W'
                owner TEXT NOT NULL,       -- 锁持有者（沙箱ID）
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (user_id, file_path, owner)
            )
        """)
        conn.commit()

        # 清理过期锁的线程
        self._cleaner_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleaner_thread.start()

    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地的数据库连接"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
        return self._local.conn

    def acquire_read(self, user_id: str, file_path: str,
                     owner: str, timeout: float = 30.0) -> bool:
        """
        获取读锁

        条件：没有其他写锁（可以同时有多个读锁）
        """
        self._cleanup_expired()
        conn = self._get_conn()
        now = time.time()
        expires = now + timeout

        cursor = conn.execute("""
            SELECT COUNT(*) FROM file_locks
            WHERE user_id = ? AND file_path = ?
              AND lock_type = 'W' AND expires_at > ?
        """, (user_id, file_path, now))

        if cursor.fetchone()[0] > 0:
            return False  # 有写锁，不能读

        conn.execute("""
            INSERT OR REPLACE INTO file_locks
            (user_id, file_path, lock_type, owner, acquired_at, expires_at)
            VALUES (?, ?, 'R', ?, ?, ?)
        """, (user_id, file_path, owner, now, expires))
        conn.commit()
        return True

    def acquire_write(self, user_id: str, file_path: str,
                      owner: str, timeout: float = 30.0) -> bool:
        """
        获取写锁（排他）

        条件：没有其他任何锁（读或写）
        """
        self._cleanup_expired()
        conn = self._get_conn()
        now = time.time()
        expires = now + timeout

        cursor = conn.execute("""
            SELECT COUNT(*) FROM file_locks
            WHERE user_id = ? AND file_path = ?
              AND owner != ? AND expires_at > ?
        """, (user_id, file_path, owner, now))

        if cursor.fetchone()[0] > 0:
            return False  # 有其他锁，不能写

        conn.execute("""
            INSERT OR REPLACE INTO file_locks
            (user_id, file_path, lock_type, owner, acquired_at, expires_at)
            VALUES (?, ?, 'W', ?, ?, ?)
        """, (user_id, file_path, owner, now, expires))
        conn.commit()
        return True

    def release(self, user_id: str, file_path: str, owner: str):
        """释放锁"""
        conn = self._get_conn()
        conn.execute("""
            DELETE FROM file_locks
            WHERE user_id = ? AND file_path = ? AND owner = ?
        """, (user_id, file_path, owner))
        conn.commit()

    def release_all(self, owner: str):
        """释放某个 owner 的所有锁"""
        conn = self._get_conn()
        conn.execute("DELETE FROM file_locks WHERE owner = ?", (owner,))
        conn.commit()

    def _cleanup_expired(self):
        """清理过期锁"""
        conn = self._get_conn()
        now = time.time()
        conn.execute("DELETE FROM file_locks WHERE expires_at < ?", (now,))
        conn.commit()

    def _cleanup_loop(self):
        """定期清理（每 10 秒）"""
        while True:
            time.sleep(10)
            try:
                self._cleanup_expired()
            except Exception:
                pass

    def cleanup(self):
        """公共清理方法"""
        self._cleanup_expired()
