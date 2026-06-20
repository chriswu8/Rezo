"""Resource-contention detection: "is one app starving the others?"

Key correctness point (per advisor): contention is about *impact over the
window*, not how hard an app runs when present. proc_samples only has rows when
an app is present, so we must divide by the TOTAL number of sweeps in the window
(absent sweeps count as 0) to get a window-average on the same time basis as the
system average. Otherwise a 2-minute burst at 80% looks like a sustained hog.

`evaluate_contention` is a pure function (table-testable). `contention()` is the
DB-backed wrapper.
"""
from __future__ import annotations

import time

from . import config
from .storage import reader

GB = 1024 ** 3


def evaluate_contention(sys_stats: dict, apps: list[dict], ram_total: int) -> list[dict]:
    """Pure core. `apps` items: {name, label, win_cpu, win_ram, max_cpu, max_ram}.

    win_cpu = window-average % of total CPU (absent sweeps = 0).
    win_ram = window-average resident bytes (absent sweeps = 0).
    """
    findings: list[dict] = []
    if not sys_stats:
        return findings
    sys_cpu = sys_stats.get("avg_cpu") or 0.0
    sys_ram = sys_stats.get("avg_ram") or 0.0
    cpu_pressure = sys_cpu >= config.SYS_CPU_PRESSURE
    ram_pressure = sys_ram >= config.SYS_RAM_PRESSURE
    blamed_cpu = blamed_ram = False

    for a in apps:
        win_cpu = a.get("win_cpu") or 0.0
        win_ram = a.get("win_ram") or 0.0
        ram_share = (win_ram / ram_total * 100) if ram_total else 0.0
        sev = None
        kinds = []
        bits = []

        # --- CPU ---
        if win_cpu >= config.APP_CPU_HOG_HIGH:
            sev = "high"; kinds.append("CPU")
            bits.append(f"averaging {win_cpu:.0f}% of total CPU")
            blamed_cpu = True
        elif cpu_pressure and win_cpu >= config.APP_CPU_DOMINANT:
            sev = "high"; kinds.append("CPU")
            bits.append(f"using {win_cpu:.0f}% of total CPU while the system is "
                        f"saturated ({sys_cpu:.0f}% avg)")
            blamed_cpu = True
        elif win_cpu >= config.APP_CPU_DOMINANT:
            sev = _max(sev, "medium"); kinds.append("CPU")
            bits.append(f"averaging {win_cpu:.0f}% of total CPU")

        # --- RAM ---
        if ram_share >= config.APP_RAM_HOG_PCT:
            sev = "high"; kinds.append("RAM")
            bits.append(f"holding {ram_share:.0f}% of memory (~{win_ram/GB:.1f} GB)")
            blamed_ram = True
        elif ram_pressure and ram_share >= config.APP_RAM_DOMINANT_PCT:
            sev = "high"; kinds.append("RAM")
            bits.append(f"holding {ram_share:.0f}% of memory (~{win_ram/GB:.1f} GB) "
                        f"while RAM is {sys_ram:.0f}% full")
            blamed_ram = True
        elif ram_share >= config.APP_RAM_DOMINANT_PCT:
            sev = _max(sev, "medium"); kinds.append("RAM")
            bits.append(f"holding {ram_share:.0f}% of memory (~{win_ram/GB:.1f} GB)")

        if not sev:
            continue
        kind = "+".join(dict.fromkeys(kinds))
        impact = ("starving other processes of CPU time" if "CPU" in kinds and "RAM" not in kinds
                  else "forcing other apps to swap to disk and slow down" if "RAM" in kinds and "CPU" not in kinds
                  else "competing hard for CPU and memory")
        detail = (f"{a['label']} is " + " and ".join(bits) +
                  f" — likely {impact}.")
        findings.append({
            "name": a["name"], "label": a["label"], "severity": sev,
            "kind": kind, "detail": detail,
            "win_cpu": round(win_cpu, 1), "ram_share": round(ram_share, 1),
            "suggestion": ("If this is a tool you're intentionally running (a "
                           "build, a model, a VM, an export), heavy use is normal. "
                           "If it's unexpected, close it or investigate why it's busy."),
        })

    # --- system-level pressure with no single culprit (honest disk/RAM cover) ---
    if cpu_pressure and not blamed_cpu:
        findings.append(_sys_finding(
            "medium", "CPU",
            f"Overall CPU is saturated (~{sys_cpu:.0f}% avg) with load spread "
            "across many processes, not one — everything will feel slower.",
            "Close apps you don't need, or reduce parallel builds/tasks."))
    if ram_pressure and not blamed_ram:
        findings.append(_sys_finding(
            "medium", "RAM",
            f"Memory is nearly full (~{sys_ram:.0f}% avg) without one dominant "
            "app — the system may be paging to disk, which slows everything.",
            "Close some apps/tabs, or see the upgrade guidance below."))
    disk_bps = (sys_stats.get("avg_disk") or 0.0)
    if disk_bps >= config.DISK_BUSY_BPS:
        findings.append(_sys_finding(
            "info", "Disk",
            f"Sustained heavy disk activity (~{disk_bps/1024/1024:.0f} MB/s) — "
            "disk-bound work can make the whole system feel sluggish even when "
            "CPU and RAM look fine.",
            "Expected during big copies, indexing, or builds; otherwise check "
            "what's reading/writing so much."))

    order = {"high": 0, "medium": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return findings


def _sys_finding(sev, kind, detail, suggestion):
    return {"name": None, "label": "System", "severity": sev, "kind": kind,
            "detail": detail, "suggestion": suggestion, "system": True}


def _max(cur, new):
    rank = {None: -1, "info": 0, "medium": 1, "high": 2}
    return new if rank[new] > rank[cur] else cur


def contention(minutes: int | None = None) -> list[dict]:
    minutes = minutes or config.CONTENTION_WINDOW_MIN
    start = int(time.time()) - minutes * 60
    conn = reader()
    try:
        s = conn.execute(
            "SELECT AVG(cpu) avg_cpu, MAX(cpu) max_cpu, AVG(ram_pct) avg_ram, "
            "MAX(ram_total) ram_total, "
            "AVG(disk_read_bps + disk_write_bps) avg_disk, COUNT(*) n "
            "FROM sys_samples WHERE ts >= ?", (start,)
        ).fetchone()
        # Need a few samples before calling anything "sustained" — otherwise a
        # single high reading right after launch would false-alarm as a hog.
        if not s or not s["n"] or s["n"] < 3:
            return []
        sys_stats = {"avg_cpu": s["avg_cpu"], "max_cpu": s["max_cpu"],
                     "avg_ram": s["avg_ram"], "avg_disk": s["avg_disk"]}
        ram_total = s["ram_total"] or 0

        sweeps_row = conn.execute(
            "SELECT COUNT(DISTINCT ts) n FROM proc_samples WHERE ts >= ?", (start,)
        ).fetchone()
        total_sweeps = sweeps_row["n"] or 0
        apps = []
        if total_sweeps >= 3:  # same "sustained, not a blip" guard for per-app
            rows = conn.execute(
                "SELECT name, SUM(cpu) sum_cpu, SUM(ram) sum_ram, "
                "MAX(cpu) max_cpu, MAX(ram) max_ram "
                "FROM proc_samples WHERE ts >= ? GROUP BY name", (start,)
            ).fetchall()
            for r in rows:
                apps.append({
                    "name": r["name"], "label": _label(r["name"]),
                    "win_cpu": (r["sum_cpu"] or 0) / total_sweeps,
                    "win_ram": (r["sum_ram"] or 0) / total_sweeps,
                    "max_cpu": r["max_cpu"] or 0, "max_ram": r["max_ram"] or 0,
                })
    finally:
        conn.close()
    return evaluate_contention(sys_stats, apps, ram_total)


def _label(name: str) -> str:
    base = name[:-4] if name.lower().endswith(".exe") else name
    return base.replace("_", " ").strip().title() or name
