"""SQLite storage layer.

Concurrency model (validated with the advisor):
  * WAL mode + busy_timeout so the single writer and many readers coexist.
  * Exactly ONE writer: the collector thread, holding one long-lived connection.
  * Readers (Flask requests, recommendations) open a fresh short-lived
    connection each call -- SQLite connections are cheap and this avoids a
    global lock that would serialize everything.

Time is stored as unix epoch seconds (UTC). Bucketing into local
day/week/month/year is done with SQLite's 'unixepoch','localtime' modifiers.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Iterable

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sys_samples (
    ts            INTEGER NOT NULL,
    cpu           REAL,
    ram_used      INTEGER,
    ram_total     INTEGER,
    ram_pct       REAL,
    swap_used     INTEGER,
    swap_total    INTEGER,
    disk_read_bps REAL,
    disk_write_bps REAL,
    net_sent_bps  REAL,
    net_recv_bps  REAL,
    disk_total    INTEGER,
    disk_used     INTEGER,
    disk_free     INTEGER,
    gpu_util      REAL,
    gpu_mem_used  INTEGER,
    gpu_mem_total INTEGER,
    proc_count    INTEGER,
    thread_count  INTEGER,
    cpu_freq      REAL,
    battery_pct   REAL,
    on_battery    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sys_ts ON sys_samples(ts);

-- One row per app *present in a sweep* (so AVG(cpu) == average-when-running).
CREATE TABLE IF NOT EXISTS proc_samples (
    ts        INTEGER NOT NULL,
    name      TEXT NOT NULL,
    cpu       REAL,      -- app share of total CPU, 0-100 (summed PIDs / ncores)
    ram       INTEGER,   -- summed RSS bytes across PIDs
    instances INTEGER
);
CREATE INDEX IF NOT EXISTS idx_proc_ts ON proc_samples(ts);
CREATE INDEX IF NOT EXISTS idx_proc_name_ts ON proc_samples(name, ts);

-- Daily rollups keep the year view fast and survive raw pruning.
CREATE TABLE IF NOT EXISTS proc_daily (
    day      TEXT NOT NULL,
    name     TEXT NOT NULL,
    samples  INTEGER,
    sum_cpu  REAL,
    max_cpu  REAL,
    sum_ram  REAL,
    max_ram  INTEGER,
    PRIMARY KEY (day, name)
);
CREATE INDEX IF NOT EXISTS idx_procdaily_name ON proc_daily(name);

CREATE TABLE IF NOT EXISTS app_registry (
    name       TEXT PRIMARY KEY,
    first_seen INTEGER,
    last_seen  INTEGER,
    exe_path   TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db() -> None:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        _configure(conn)
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def reader() -> sqlite3.Connection:
    """A fresh read connection for the caller's thread (close when done)."""
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
    return _configure(conn)


class Writer:
    """The single writer connection used by the collector thread."""

    def __init__(self) -> None:
        config.ensure_dirs()
        self.conn = sqlite3.connect(
            config.DB_PATH, timeout=5.0, check_same_thread=False
        )
        _configure(self.conn)
        self._lock = threading.Lock()

    # --- inserts ---------------------------------------------------------
    def insert_sys(self, row: dict[str, Any]) -> None:
        cols = (
            "ts", "cpu", "ram_used", "ram_total", "ram_pct", "swap_used",
            "swap_total", "disk_read_bps", "disk_write_bps", "net_sent_bps",
            "net_recv_bps", "disk_total", "disk_used", "disk_free", "gpu_util",
            "gpu_mem_used", "gpu_mem_total", "proc_count", "thread_count",
            "cpu_freq", "battery_pct", "on_battery",
        )
        vals = [row.get(c) for c in cols]
        placeholders = ",".join("?" * len(cols))
        with self._lock:
            self.conn.execute(
                f"INSERT INTO sys_samples ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            self.conn.commit()

    def insert_procs(self, ts: int, apps: Iterable[dict[str, Any]]) -> None:
        rows = [(ts, a["name"], a["cpu"], a["ram"], a["instances"]) for a in apps]
        if not rows:
            return
        with self._lock:
            self.conn.executemany(
                "INSERT INTO proc_samples (ts,name,cpu,ram,instances) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
            self.conn.commit()

    def touch_registry(self, ts: int, names_paths: dict[str, str | None]) -> None:
        with self._lock:
            for name, path in names_paths.items():
                self.conn.execute(
                    "INSERT INTO app_registry (name, first_seen, last_seen, exe_path) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen, "
                    "exe_path=COALESCE(app_registry.exe_path, excluded.exe_path)",
                    (name, ts, ts, path),
                )
            self.conn.commit()

    # --- maintenance -----------------------------------------------------
    def rollup_proc_daily(self) -> int:
        """Roll up completed days (local) into proc_daily. Returns days done."""
        with self._lock:
            today = self.conn.execute(
                "SELECT date('now','localtime')"
            ).fetchone()[0]
            days = [
                r[0]
                for r in self.conn.execute(
                    "SELECT DISTINCT date(ts,'unixepoch','localtime') AS d "
                    "FROM proc_samples WHERE d < ?",
                    (today,),
                ).fetchall()
            ]
            done = 0
            for day in days:
                already = self.conn.execute(
                    "SELECT 1 FROM proc_daily WHERE day=? LIMIT 1", (day,)
                ).fetchone()
                if already:
                    continue
                self.conn.execute(
                    "INSERT OR REPLACE INTO proc_daily "
                    "(day,name,samples,sum_cpu,max_cpu,sum_ram,max_ram) "
                    "SELECT date(ts,'unixepoch','localtime') AS d, name, "
                    "COUNT(*), SUM(cpu), MAX(cpu), SUM(ram), MAX(ram) "
                    "FROM proc_samples WHERE date(ts,'unixepoch','localtime')=? "
                    "GROUP BY name",
                    (day,),
                )
                done += 1
            self.conn.commit()
            return done

    def prune(self) -> None:
        now = int(time.time())
        with self._lock:
            self.conn.execute(
                "DELETE FROM proc_samples WHERE ts < ?",
                (now - config.RAW_PROC_RETENTION_DAYS * 86400,),
            )
            self.conn.execute(
                "DELETE FROM sys_samples WHERE ts < ?",
                (now - config.SYS_RETENTION_DAYS * 86400,),
            )
            self.conn.execute(
                "DELETE FROM proc_daily WHERE day < date('now','localtime',?)",
                (f"-{config.PROC_DAILY_RETENTION_DAYS} days",),
            )
            self.conn.commit()
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, value)
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
