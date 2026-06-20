"""Read-side queries: time-bucketed series and ranked app usage.

Each call opens its own short-lived reader connection (WAL allows this to run
concurrently with the single collector writer).
"""
from __future__ import annotations

import time

from . import config
from .storage import reader

# range -> (window_seconds, bucket_seconds or None for month-string)
RANGES = {
    "day": (24 * 3600, 900),        # 15-min buckets
    "week": (7 * 86400, 3600),      # hourly buckets
    "month": (30 * 86400, 86400),   # daily buckets
    "year": (366 * 86400, None),    # monthly buckets (calendar months)
}

_SERIES_COLS = (
    "cpu", "ram_pct", "gpu_util",
    "net_recv_bps", "net_sent_bps",
    "disk_read_bps", "disk_write_bps",
)


def timeseries(rng: str) -> dict:
    window, bucket = RANGES.get(rng, RANGES["day"])
    now = int(time.time())
    start = now - window
    avg_cols = ", ".join(f"AVG({c}) AS {c}" for c in _SERIES_COLS)

    conn = reader()
    try:
        if bucket is not None:
            sql = (
                f"SELECT (ts/{bucket})*{bucket} AS t, {avg_cols}, "
                f"MAX(ram_used) AS ram_used, MAX(ram_total) AS ram_total "
                f"FROM sys_samples WHERE ts >= ? GROUP BY t ORDER BY t"
            )
        else:
            # calendar-month buckets; t = first-of-month epoch (for labelling)
            sql = (
                "SELECT CAST(strftime('%s', strftime('%Y-%m-01 00:00:00', ts, "
                "'unixepoch','localtime')) AS INTEGER) AS t, "
                f"{avg_cols}, MAX(ram_used) AS ram_used, MAX(ram_total) AS ram_total "
                "FROM sys_samples WHERE ts >= ? GROUP BY t ORDER BY t"
            )
        rows = conn.execute(sql, (start,)).fetchall()
    finally:
        conn.close()

    points = []
    for r in rows:
        d = {"t": int(r["t"])}
        for c in _SERIES_COLS:
            v = r[c]
            d[c] = round(v, 2) if v is not None else None
        d["ram_used"] = r["ram_used"]
        d["ram_total"] = r["ram_total"]
        points.append(d)
    return {"range": rng, "points": points}


def app_rankings(rng: str) -> list[dict]:
    window, _ = RANGES.get(rng, RANGES["day"])
    now = int(time.time())
    start = now - window
    new_cutoff = now - config.NEW_APP_DAYS * 86400

    conn = reader()
    try:
        # Year view (and anything beyond raw retention) uses daily rollups.
        if rng == "year" or window > config.RAW_PROC_RETENTION_DAYS * 86400:
            day_cutoff_rows = conn.execute(
                "SELECT date(?, 'unixepoch','localtime')", (start,)
            ).fetchone()
            day_cutoff = day_cutoff_rows[0]
            rows = conn.execute(
                "SELECT d.name AS name, "
                "  SUM(d.sum_cpu)/NULLIF(SUM(d.samples),0) AS avg_cpu, "
                "  MAX(d.max_cpu) AS max_cpu, "
                "  SUM(d.sum_ram)/NULLIF(SUM(d.samples),0) AS avg_ram, "
                "  MAX(d.max_ram) AS max_ram, "
                "  SUM(d.samples) AS samples, "
                "  r.first_seen AS first_seen, r.last_seen AS last_seen, "
                "  r.exe_path AS exe_path "
                "FROM proc_daily d LEFT JOIN app_registry r ON r.name = d.name "
                "WHERE d.day >= ? GROUP BY d.name",
                (day_cutoff,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT p.name AS name, AVG(p.cpu) AS avg_cpu, MAX(p.cpu) AS max_cpu, "
                "  AVG(p.ram) AS avg_ram, MAX(p.ram) AS max_ram, COUNT(*) AS samples, "
                "  r.first_seen AS first_seen, MAX(p.ts) AS last_seen, "
                "  r.exe_path AS exe_path "
                "FROM proc_samples p LEFT JOIN app_registry r ON r.name = p.name "
                "WHERE p.ts >= ? GROUP BY p.name",
                (start,),
            ).fetchall()
    finally:
        conn.close()

    apps = []
    for r in rows:
        first_seen = r["first_seen"]
        apps.append(
            {
                "name": r["name"],
                "label": _label(r["name"]),
                "avg_cpu": round(r["avg_cpu"] or 0, 1),
                "max_cpu": round(r["max_cpu"] or 0, 1),
                "avg_ram": int(r["avg_ram"] or 0),
                "max_ram": int(r["max_ram"] or 0),
                "samples": int(r["samples"] or 0),
                "first_seen": first_seen,
                "last_seen": r["last_seen"],
                "exe_path": r["exe_path"],
                "is_new": bool(first_seen and first_seen >= new_cutoff),
            }
        )
    apps.sort(key=lambda a: a["avg_cpu"], reverse=True)
    return apps


def latest_sys() -> dict | None:
    conn = reader()
    try:
        r = conn.execute(
            "SELECT * FROM sys_samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def column_values(column: str, days: int) -> list[float]:
    """Non-null values of a sys_samples column over the last `days` (for stats)."""
    if column not in {
        "cpu", "ram_pct", "gpu_util", "swap_used", "swap_total",
        "disk_free", "disk_total",
    }:
        raise ValueError(f"unsafe column: {column}")
    start = int(time.time()) - days * 86400
    conn = reader()
    try:
        rows = conn.execute(
            f"SELECT {column} AS v FROM sys_samples "
            f"WHERE ts >= ? AND {column} IS NOT NULL",
            (start,),
        ).fetchall()
        return [r["v"] for r in rows]
    finally:
        conn.close()


def data_span_days() -> float:
    conn = reader()
    try:
        r = conn.execute(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM sys_samples"
        ).fetchone()
        if not r or r["lo"] is None:
            return 0.0
        return round((r["hi"] - r["lo"]) / 86400.0, 2)
    finally:
        conn.close()


def _label(name: str) -> str:
    """A friendlier display name (chrome.exe -> Chrome)."""
    base = name[:-4] if name.lower().endswith(".exe") else name
    return base.replace("_", " ").strip().title() or name
