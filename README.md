# Rezo

A local, always-on dashboard that shows how your PC's computational resources are
used **over time** (day → week → month → year), ranks the apps consuming them,
flags trouble (resource hogs and suspicious processes), and gives **honest**
upgrade advice — recommending a purchase only when one is genuinely warranted.

It turns the invisible question *"is my machine keeping up with what I'm building,
and is anything wrong?"* into clear visuals and plain-language guidance — so you
decide **when** to upgrade, **what** to buy, and **what's slowing you down**, from
data rather than guesswork.

> **Local & private.** Everything runs on your machine. No accounts, no cloud, no
> telemetry. Data lives only in `%LOCALAPPDATA%\Rezo\rezo.db`.

Platform: **Windows 11** · Python **3.10+** (tested on 3.14)

---

## Quick start

### 1. Get the code
```powershell
git clone https://github.com/chriswu8/Rezo.git
cd Rezo
```

### 2. Install & run (recommended)
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```
This creates a local virtual environment, installs dependencies, registers Rezo
to **auto-start hidden on login**, and launches it. A **tray icon (▦)** appears —
double-click it to open the dashboard, or right-click for **Open dashboard /
Pause / Open data folder / Quit**.

Then open **http://127.0.0.1:8787/** in your browser.

Install flags:
- `-NoAutostart` — don't register it to start on login
- `-NoLaunch` — set up but don't start it now

### Alternative: run manually (no auto-start, no tray changes)
```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python run.py
```

### See every view populated immediately (optional demo data)
Week / Month / Year charts are empty until history accumulates. To preview them
right away with synthetic data:
```powershell
.\.venv\Scripts\python run.py --seed        # fills ~400 days of demo history
.\.venv\Scripts\python run.py --serve-only  # view it without collecting
```
> `--seed` **replaces** stored data with demo data. Just run normally afterwards;
> real collection takes over.

---

## Run modes

| Command | What it does |
|---|---|
| `python run.py` | Tray app + background collector + dashboard (default) |
| `python run.py --no-tray` | Same, console-attached (Ctrl+C to stop) |
| `python run.py --serve-only` | Dashboard only, against existing data (no collecting) |
| `python run.py --seed [--days N]` | Fill the database with demo history, then exit |
| `python run.py --open` | Open the dashboard in a browser |
| `…/api/alerts?demo=1` or `/?demo=1` | Preview the alert UI with **example** findings |

---

## What you'll see

- **Alerts (top of page)** — it's obvious when something's wrong:
  - **Resource contention** — flags when one app is using so much CPU/RAM it's
    starving everything else (judged on a 5-minute window-average, so brief spikes
    don't false-alarm), plus a system-level tier for "CPU saturated / memory full /
    disk thrashing" when no single app is the culprit.
  - **Suspicious processes** — heuristic, **defensive** (not antivirus): a process
    *masquerading* as a Windows system binary (e.g. `svchost.exe` running from a
    Temp folder, not `C:\Windows`), or an unsigned executable running from
    Temp/Downloads/Recycle Bin/root of `C:\`. Shows the path and verify-steps —
    never a verdict, never auto-kills. Tuned for low false alarms.
- **Live gauges** — CPU, memory, GPU, disk free, network, process/thread counts.
- **Usage over time** — CPU %, memory %, GPU %, network, and disk I/O charted
  across **Day / Week / Month / Year**.
- **Apps ranked by CPU or RAM** — average CPU *while the app was running* (not
  diluted by idle time), peak CPU, average + peak RAM, instance count, a **🟢 new**
  badge for apps first seen in the last 7 days, and ⚠️/🛡️ badges for hogs/threats.
- **Upgrade guidance** — *Recommended now* (only when a resource is sustained-
  strained; otherwise it says you're healthy) and *Planning ahead* (profile-aware
  tips for what your next builds may demand, e.g. local AI models need discrete-GPU
  VRAM; container stacks need RAM; heavy parallel builds need cores).

---

## How it works

```
 pystray tray app (main thread)
   ├─ system loop        → sys metrics every 10s ──────┐
   ├─ process loop       → top apps every 30s ─────────┤
   ├─ gpu loop           → GPU util/mem every 30s ──────┤ write
   ├─ security loop      → suspicious-process scan / 60s│ (one writer)
   ├─ maintenance        → hourly rollup/prune/checkpoint┘
   └─ Flask server       → JSON API + dashboard at 127.0.0.1:8787
                                   │  (fresh reader connection per request)
                          SQLite (WAL) at %LOCALAPPDATA%\Rezo\rezo.db
                                   ▲
                          Browser dashboard (Chart.js) — polls the API
```

A background collector samples the system with [`psutil`](https://github.com/giampaolo/psutil)
and writes to SQLite (WAL mode → one writer, many concurrent readers). A small
**Flask** server serves the dashboard (vanilla HTML/JS + a locally-vendored
**Chart.js**, so it works offline). GPU metrics use `nvidia-smi` for discrete
NVIDIA cards and fall back to Windows performance counters (so integrated Intel/AMD
GPUs report utilization too). The suspicious-process detection cores are pure,
unit-tested functions.

**Measurement care:** per-process CPU is primed via `psutil`'s cached process list
and normalized by core count (matches Task Manager); "average when run" is computed
only from ticks where the app was present; disk/network counters are stored as
deltas and clamped at reboots.

---

## Data & privacy

Everything is local — no network calls, no cloud, no telemetry. The database lives
at `%LOCALAPPDATA%\Rezo\rezo.db`. Retention is automatic: raw per-process samples
~45 days, daily app rollups ~2 years, system samples ~400 days.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1            # remove auto-start
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Purge     # also delete collected data
```
Quit the running app first via the tray icon (right-click → Quit).

## Requirements

Python 3.10+ (tested on 3.14) on Windows 11. Dependencies (installed by
`install.ps1` or `pip install -r requirements.txt`): `psutil`, `Flask`, `pystray`,
`Pillow`.

## Project layout

```
run.py            entry point (all run modes)
requirements.txt
install.ps1 / uninstall.ps1
rezo/
  config.py       paths, cadences, retention, thresholds
  storage.py      SQLite schema + writer/reader connections
  collector.py    background sampling threads
  metrics.py      hardware specs + GPU sampling
  queries.py      time-bucketed series + app rankings
  recommend.py    upgrade guidance
  analyze.py      resource-contention detection (pure core)
  security.py     suspicious-process detection (pure core)
  server.py       Flask API + static serving
  tray.py         system-tray app
  seed.py         synthetic demo data
  web/            dashboard UI (index.html, app.js, style.css, vendored Chart.js)
```
