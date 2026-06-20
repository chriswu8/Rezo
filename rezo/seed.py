"""Synthetic data backfill for development / first-run demo.

Week / month / year views can't be verified the day you install (no history
exists yet). `python run.py --seed` fills the DB with ~400 days of plausible
data so every timeframe and the app rankings render immediately.

This is clearly-labelled TEST data and wipes existing rows. Real collection
takes over from here; the seed just primes the visuals.
"""
from __future__ import annotations

import math
import random
import sqlite3
import time

from . import config, storage

# name, cpu_mean%, cpu_sd, ram_mean_bytes, ram_sd, present_prob, first_seen_days
_APPS = [
    ("chrome.exe",          7.0, 4.0, 3.2e9, 0.6e9, 0.98, 320),
    ("Code.exe",            4.5, 3.0, 1.7e9, 0.4e9, 0.92, 280),
    ("node.exe",            9.0, 6.0, 0.9e9, 0.3e9, 0.62, 210),
    ("python.exe",         13.0, 9.0, 0.8e9, 0.4e9, 0.55, 160),
    ("Docker Desktop.exe",  3.0, 2.0, 1.3e9, 0.3e9, 0.42,  95),
    ("pwsh.exe",            2.0, 1.5, 0.25e9, 0.1e9, 0.7, 250),
    ("explorer.exe",        1.2, 0.8, 0.28e9, 0.05e9, 1.0, 365),
    ("MsMpEng.exe",         3.5, 2.5, 0.3e9, 0.1e9, 1.0, 365),
    ("Spotify.exe",         1.5, 1.0, 0.42e9, 0.1e9, 0.5, 200),
    ("WindowsTerminal.exe", 1.0, 0.7, 0.22e9, 0.05e9, 0.6, 200),
    ("claude.exe",          5.5, 3.5, 0.55e9, 0.2e9, 0.55,   6),   # NEW
    ("ollama.exe",         22.0, 14.0, 5.0e9, 1.2e9, 0.18,   3),   # NEW + heavy
]


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _sys_row(ts: int, ram_total: int, rng: random.Random) -> tuple:
    dt = time.localtime(ts)
    hour = dt.tm_hour + dt.tm_min / 60.0
    weekend = dt.tm_wday >= 5
    # diurnal: busy ~10:00-22:00, calmer overnight; lighter on weekends
    diurnal = math.sin(max(0.0, (hour - 7) / 17.0) * math.pi)
    load = (0.85 if not weekend else 0.5) * diurnal
    cpu = _clip(8 + 55 * load + rng.gauss(0, 7), 0.5, 99)
    ram_pct = _clip(38 + 32 * load + rng.gauss(0, 5), 18, 96)
    ram_used = int(ram_total * ram_pct / 100)
    gpu = _clip(rng.gauss(6, 6) + (28 * load if rng.random() < 0.15 else 0), 0, 100)
    busy = max(0.0, load) + 0.05
    return (
        ts, round(cpu, 1), ram_used, ram_total, round(ram_pct, 1),
        int(rng.uniform(0, 2.5e9)), int(8e9),                 # swap used / total
        round(rng.uniform(0, 80e6) * busy, 1), round(rng.uniform(0, 40e6) * busy, 1),
        round(rng.uniform(0, 6e6) * busy, 1), round(rng.uniform(0, 20e6) * busy, 1),
        int(1.0e12), int(0.62e12), int(0.38e12),              # disk total/used/free
        round(gpu, 1), int(rng.uniform(0.2e9, 1.4e9)), None,  # gpu util/mem
        int(rng.uniform(180, 320)), int(rng.uniform(2200, 4200)),
        round(rng.uniform(3000, 4400), 0), None, None,
    )


def generate(days: int = 400) -> dict:
    storage.init_db()
    rng = random.Random(20260614)
    vm_total = 0
    try:
        import psutil
        vm_total = psutil.virtual_memory().total
    except Exception:
        pass
    ram_total = vm_total or 64 * 1024 ** 3

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    now = int(time.time())
    cur = conn.cursor()
    cur.execute("BEGIN")
    for t in ("sys_samples", "proc_samples", "proc_daily", "app_registry"):
        cur.execute(f"DELETE FROM {t}")

    # ---- system samples: finer near now, coarser further back ----
    sys_rows = []
    start = now - days * 86400
    t = start
    while t <= now:
        age_days = (now - t) / 86400
        step = 300 if age_days <= 2 else (1800 if age_days <= 30 else 21600)
        sys_rows.append(_sys_row(t, ram_total, rng))
        t += step
    cur.executemany(
        "INSERT INTO sys_samples (ts,cpu,ram_used,ram_total,ram_pct,swap_used,"
        "swap_total,disk_read_bps,disk_write_bps,net_sent_bps,net_recv_bps,"
        "disk_total,disk_used,disk_free,gpu_util,gpu_mem_used,gpu_mem_total,"
        "proc_count,thread_count,cpu_freq,battery_pct,on_battery) "
        "VALUES (" + ",".join("?" * 22) + ")",
        sys_rows,
    )

    # ---- app registry ----
    for name, *_rest in _APPS:
        first = now - int(_rest[-1]) * 86400
        cur.execute(
            "INSERT INTO app_registry (name,first_seen,last_seen,exe_path) VALUES (?,?,?,?)",
            (name, first, now - rng.randint(0, 3600), f"C:\\Path\\{name}"),
        )

    # ---- raw proc samples for the last 45 days (hourly) ----
    proc_rows = []
    horizon = min(days, config.RAW_PROC_RETENTION_DAYS)
    t = now - horizon * 86400
    while t <= now:
        for name, cpu_m, cpu_sd, ram_m, ram_sd, prob, fseen in _APPS:
            if (now - t) / 86400 > fseen:
                continue
            if rng.random() > prob:
                continue
            cpu = _clip(rng.gauss(cpu_m, cpu_sd), 0.1, 100)
            ram = int(_clip(rng.gauss(ram_m, ram_sd), 5e7, 5e10))
            proc_rows.append((t, name, round(cpu, 1), ram, rng.randint(1, 12)))
        t += 3600
    cur.executemany(
        "INSERT INTO proc_samples (ts,name,cpu,ram,instances) VALUES (?,?,?,?,?)",
        proc_rows,
    )

    # ---- daily rollups for every completed day (matches the hourly rollup job
    #      in production, which covers all days; the year view reads these) ----
    daily_rows = []
    for d in range(1, days):
        day = time.strftime("%Y-%m-%d", time.localtime(now - d * 86400))
        for name, cpu_m, cpu_sd, ram_m, ram_sd, prob, fseen in _APPS:
            if d > fseen:
                continue
            samples = int(24 * prob)
            if samples <= 0:
                continue
            cpu_avg = _clip(rng.gauss(cpu_m, cpu_sd * 0.4), 0.1, 100)
            ram_avg = _clip(rng.gauss(ram_m, ram_sd * 0.4), 5e7, 5e10)
            daily_rows.append((
                day, name, samples, round(cpu_avg * samples, 1),
                round(_clip(cpu_avg * 1.8, 0.1, 100), 1),
                round(ram_avg * samples, 1), int(ram_avg * 1.4),
            ))
    cur.executemany(
        "INSERT OR REPLACE INTO proc_daily (day,name,samples,sum_cpu,max_cpu,sum_ram,max_ram) "
        "VALUES (?,?,?,?,?,?,?)",
        daily_rows,
    )

    conn.commit()
    conn.close()
    return {"sys": len(sys_rows), "proc": len(proc_rows), "daily": len(daily_rows)}
