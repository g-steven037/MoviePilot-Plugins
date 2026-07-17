from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional


class ShowCache:
    """持久化 TMDB 响应，减少重复联网查询。"""

    def __init__(self, path: Path, ttl_hours: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = ttl_hours * 3600
        self._connection = sqlite3.connect(str(path), timeout=10)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tmdb_show_cache (
                cache_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
            """
        )
        self._connection.commit()

    def get(self, cache_key: str) -> Optional[dict]:
        """读取仍在有效期内的缓存。"""
        row = self._connection.execute(
            "SELECT payload, fetched_at FROM tmdb_show_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row or time.time() - float(row[1]) >= self._ttl_seconds:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, json.JSONDecodeError):
            self.delete(cache_key)
            return None

    def set(self, cache_key: str, payload: dict) -> None:
        """写入或更新一条缓存。"""
        self._connection.execute(
            """
            INSERT INTO tmdb_show_cache (cache_key, payload, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload = excluded.payload,
                fetched_at = excluded.fetched_at
            """,
            (cache_key, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self._connection.commit()

    def delete(self, cache_key: str) -> None:
        """删除指定缓存。"""
        self._connection.execute(
            "DELETE FROM tmdb_show_cache WHERE cache_key = ?", (cache_key,)
        )
        self._connection.commit()

    def prune(self) -> int:
        """清除长期过期的缓存记录。"""
        cursor = self._connection.execute(
            "DELETE FROM tmdb_show_cache WHERE fetched_at < ?",
            (time.time() - self._ttl_seconds * 7,),
        )
        self._connection.commit()
        return max(int(cursor.rowcount or 0), 0)

    def close(self) -> None:
        """关闭SQLite连接。"""
        self._connection.close()
