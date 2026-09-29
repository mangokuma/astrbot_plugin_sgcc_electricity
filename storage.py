"""SQLite 存储：每日用电量历史、余额日志、抓取摘要缓存、提醒去重状态。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Optional


class Storage:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self._db_path = os.path.join(data_dir, "sgcc_electricity.db")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_usage (
                    date TEXT PRIMARY KEY,
                    total_usage REAL,
                    valley_usage REAL,
                    flat_usage REAL,
                    peak_usage REAL,
                    tip_usage REAL
                );
                CREATE TABLE IF NOT EXISTS balance_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    balance REAL,
                    amount_due REAL
                );
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ---- 每日用电量 ----

    def upsert_daily(self, rows: list[dict[str, Any]]):
        """写入每日用电量（按日期幂等覆盖）。"""
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO daily_usage (date, total_usage, valley_usage, flat_usage, peak_usage, tip_usage)
                   VALUES (:date, :total_usage, :valley_usage, :flat_usage, :peak_usage, :tip_usage)
                   ON CONFLICT(date) DO UPDATE SET
                     total_usage=excluded.total_usage,
                     valley_usage=excluded.valley_usage,
                     flat_usage=excluded.flat_usage,
                     peak_usage=excluded.peak_usage,
                     tip_usage=excluded.tip_usage""",
                [
                    {
                        "date": str(r.get("date", "")).strip(),
                        "total_usage": _f(r.get("total_usage")),
                        "valley_usage": _f(r.get("valley_usage")),
                        "flat_usage": _f(r.get("flat_usage")),
                        "peak_usage": _f(r.get("peak_usage")),
                        "tip_usage": _f(r.get("tip_usage")),
                    }
                    for r in rows
                    if str(r.get("date", "")).strip()
                ],
            )

    def get_daily(self, days: int = 7) -> list[dict[str, Any]]:
        """取最近 N 天的每日用电量（升序）。"""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM (SELECT * FROM daily_usage ORDER BY date DESC LIMIT ?) ORDER BY date ASC",
                (int(days),),
            )
            return [dict(row) for row in cursor.fetchall()]

    def cleanup_daily(self, retention_days: int = 180):
        """清理过期日数据（默认保留 180 天，历史图表需要）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM daily_usage WHERE date < date('now', ?)",
                (f"-{int(retention_days)} days",),
            )

    # ---- 余额日志 ----

    def insert_balance(self, balance: Optional[float], amount_due: Optional[float], ts: str):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO balance_log (ts, balance, amount_due) VALUES (?, ?, ?)",
                (ts, balance, amount_due),
            )

    def get_last_balance(self) -> Optional[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM balance_log ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None

    # ---- KV（摘要缓存 / 提醒去重）----

    def set_kv(self, key: str, value: str):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_kv(self, key: str) -> Optional[str]:
        with self._lock:
            cursor = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else None

    # ---- 抓取摘要缓存 ----

    CACHE_KEY = "last_fetch_summary"

    def save_summary(self, summary_json: str):
        self.set_kv(self.CACHE_KEY, summary_json)

    def load_summary(self) -> Optional[dict[str, Any]]:
        raw = self.get_kv(self.CACHE_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    # ---- 提醒去重 ----

    def mark_reminded(self, date_str: str, balance: float):
        self.set_kv("last_remind", json.dumps({"date": date_str, "balance": balance}))

    def reminded_today(self, date_str: str) -> bool:
        raw = self.get_kv("last_remind")
        if not raw:
            return False
        try:
            data = json.loads(raw)
            return data.get("date") == date_str
        except (json.JSONDecodeError, TypeError):
            return False


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or str(value).strip() in ("", "-", "—"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
