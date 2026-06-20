"""Central configuration: paths, cadences, retention, thresholds.

Everything tunable lives here so the rest of the code reads cleanly.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Rezo"

# --- Storage location (persists independently of the code folder) ---
_local = os.environ.get("LOCALAPPDATA") or str(Path.home())
DATA_DIR = Path(_local) / APP_NAME
DB_PATH = DATA_DIR / "rezo.db"

# --- Web server ---
HOST = "127.0.0.1"
PORT = 8787
URL = f"http://{HOST}:{PORT}/"

# --- Sampling cadences (seconds) ---
SYS_INTERVAL = 10        # full system snapshot
PROC_INTERVAL = 30       # per-process rollup
GPU_INTERVAL = 30        # GPU is read out-of-band (spawns PowerShell), keep slow

# --- Per-process storage shaping ---
# Each proc sweep stores the union of the top-K apps by CPU and the top-K by RAM.
PROC_TOP_K = 15

# --- Retention (a maintenance pass enforces these) ---
RAW_PROC_RETENTION_DAYS = 45      # fine-grained day/week/month app rankings
SYS_RETENTION_DAYS = 400          # raw system samples (indexed; bucketed on the fly)
PROC_DAILY_RETENTION_DAYS = 800   # daily app rollups power the year view

# --- "New app" window ---
NEW_APP_DAYS = 7

# --- Recommendation thresholds (only flag a purchase when sustained) ---
RAM_P90_WARN = 80.0      # % used
RAM_P90_HIGH = 90.0
CPU_P90_WARN = 85.0      # % used
GPU_P90_WARN = 80.0      # % used
DISK_FREE_PCT_WARN = 12.0
DISK_FREE_GB_WARN = 25.0
SWAP_PRESSURE_PCT = 40.0  # swap used as % of swap total, sustained -> RAM pressure

# Window used to evaluate sustained pressure for recommendations.
RECO_WINDOW_DAYS = 14

# --- Resource contention ("an app is starving others") ---
# Short window so a "happening now" alert clears soon after the hog stops.
CONTENTION_WINDOW_MIN = 5
SYS_CPU_PRESSURE = 85.0        # system avg CPU over window -> saturated
SYS_RAM_PRESSURE = 88.0        # system avg RAM% over window -> memory pressure
APP_CPU_DOMINANT = 50.0        # app's window-avg % of total CPU -> dominant
APP_CPU_HOG_HIGH = 70.0        # app alone this high (sustained) -> high severity
APP_RAM_DOMINANT_PCT = 25.0    # app's window-avg RAM as % of total RAM
APP_RAM_HOG_PCT = 40.0         # app alone holding this much RAM -> high severity
DISK_BUSY_BPS = 120 * 1024 * 1024   # sustained read+write avg -> "disk busy" (info)

# --- Suspicious-process scan (heuristic, defensive; NOT antivirus) ---
SEC_INTERVAL = 60              # background scan cadence (seconds)
SEC_MAX_SIG_CHECK = 16         # cap signature checks per scan (only on candidates)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
