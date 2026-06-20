"""Heuristic suspicious-process detection (defensive, for your own machine).

This is NOT antivirus. It surfaces *signals worth checking* — it never makes a
verdict, never kills anything. The detection core, `evaluate_process`, is a pure
function so it can be table-tested with crafted inputs (you can't summon malware
on a clean box to test against).

High-confidence, low-false-positive signals only:
  * Masquerade  — a process named like a Windows system binary (svchost, lsass,
                  csrss, …) running from somewhere other than C:\\Windows. The
                  PATH check is the primary gate (works even if Rezo runs
                  elevated and can read the real system process's path).
  * Bad location — an executable running from Temp / Downloads / Recycle Bin /
                  the root of C:\\ . Signed binaries in these spots (e.g. signed
                  installers) are dropped to avoid false alarms; unsigned ones,
                  especially using CPU, are flagged.

Limitation (stated to the user): a periodic scan can miss processes that run
briefly and exit — a common malware pattern. This complements, not replaces,
Microsoft Defender.
"""
from __future__ import annotations

import os
import re
import time

import psutil

from . import config
from .metrics import _ps

# Names that are *exclusively* Windows system processes (all legit copies live
# under C:\Windows). A copy anywhere else is the classic masquerade technique.
SYSTEM_NAMES = {
    "svchost.exe", "lsass.exe", "csrss.exe", "services.exe", "winlogon.exe",
    "wininit.exe", "smss.exe", "spoolsv.exe", "dwm.exe", "conhost.exe",
    "taskhostw.exe", "sihost.exe", "ctfmon.exe", "fontdrvhost.exe",
    "runtimebroker.exe", "dllhost.exe", "audiodg.exe", "searchindexer.exe",
}

# (substring in lowercased path, human label). Deliberately NOT including
# AppData\Local\<app> or Roaming — far too many legitimate apps live there.
_SUS_DIRS = [
    ("\\appdata\\local\\temp\\", "a temporary folder (AppData\\Local\\Temp)"),
    ("\\windows\\temp\\", "the Windows Temp folder"),
    ("\\downloads\\", "your Downloads folder"),
    ("$recycle.bin", "the Recycle Bin"),
    ("\\users\\public\\", "the Public user folder"),
]
_C_ROOT = re.compile(r"^c:\\[^\\]+\.exe$")

_VERIFY = ("Verify it: right-click the .exe → Properties → Digital Signatures, "
           "search the name online, and run a Microsoft Defender scan. "
           "If you don't recognise it, treat it as suspect.")


def suspicious_location(path_lower: str) -> str | None:
    for frag, label in _SUS_DIRS:
        if frag in path_lower:
            return label
    if _C_ROOT.match(path_lower):
        return "the root of C:\\"
    return None


def evaluate_process(name: str, exe_path: str | None, signed: str | None,
                     cpu: float | None = None) -> dict | None:
    """Pure detector. `signed` in {'valid','unsigned','invalid','unknown',None}.

    Returns a finding dict or None. No I/O — table-testable.
    """
    if not exe_path:
        return None  # can't assess location -> don't guess
    name_l = name.lower()
    path_l = exe_path.lower()
    in_windows = path_l.startswith("c:\\windows")
    bad_sig = signed in ("unsigned", "invalid")
    cpu_note = (f" It is currently using about {cpu:.0f}% of total CPU."
                if cpu and cpu >= 20 else "")

    # 1) Masquerade — primary gate is the PATH, not signature.
    if name_l in SYSTEM_NAMES and not in_windows:
        reasons = [f"Named like the Windows system process '{name}', but running "
                   f"from {exe_path} instead of the Windows system folder."]
        if bad_sig:
            reasons.append("It is not validly digitally signed.")
        elif signed == "valid":
            reasons.append("(It is signed, but the location is still wrong for "
                           "this name.)")
        return {
            "name": name, "label": _label(name), "exe_path": exe_path,
            "severity": "high", "kind": "Masquerade",
            "reasons": reasons, "cpu": cpu, "action": _VERIFY,
        }

    # 2) Bad location.
    loc = suspicious_location(path_l)
    if loc:
        if signed == "valid":
            return None  # signed installer/updater in temp etc. — not alarming
        reasons = [f"Running from {loc} — unusual for a normally-installed app "
                   f"({exe_path})."]
        if bad_sig:
            reasons.append("It is not digitally signed.")
        elif signed in (None, "unknown"):
            reasons.append("Its digital signature could not be confirmed.")
        sev = "high" if (bad_sig and (cpu_note or loc in (
            "the Recycle Bin", "a temporary folder (AppData\\Local\\Temp)"))) else "medium"
        if cpu_note:
            reasons.append(cpu_note.strip())
        return {
            "name": name, "label": _label(name), "exe_path": exe_path,
            "severity": sev, "kind": "Unusual location",
            "reasons": reasons, "cpu": cpu, "action": _VERIFY,
        }

    return None


def _label(name: str) -> str:
    base = name[:-4] if name.lower().endswith(".exe") else name
    return base.replace("_", " ").strip().title() or name


# --------------------------------------------------------------------------
# Live plumbing (feeds the pure function); produces zero PowerShell on a clean
# machine because signatures are only checked for already-flagged candidates.
# --------------------------------------------------------------------------
# cache definitive signature results by path -> (mtime, status) so repeated
# scans of the same unchanged file don't re-spawn PowerShell every 60s.
_sig_cache: dict[str, tuple[float | None, str]] = {}


def _mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _check_signatures(paths: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    need: list[str] = []
    for p in paths:
        key = p.lower()
        cached = _sig_cache.get(key)
        if cached and cached[0] == _mtime(p):
            result[key] = cached[1]
        else:
            need.append(p)
    if not need:
        return result

    arr = ",".join("'" + p.replace("'", "''") + "'" for p in need)
    script = (
        f"$p=@({arr}); Get-AuthenticodeSignature -LiteralPath $p "
        "-ErrorAction SilentlyContinue | "
        "ForEach-Object { $_.Path + '|' + $_.Status }"
    )
    out = _ps(script, timeout=20.0)
    if out:
        for line in out.strip().splitlines():
            if "|" not in line:
                continue
            path, _, status = line.rpartition("|")
            s = status.strip().lower()
            key = path.strip().lower()
            mapped = "valid" if s == "valid" else "unsigned" if s == "notsigned" else "invalid"
            result[key] = mapped
            _sig_cache[key] = (_mtime(path.strip()), mapped)  # remember definitive result
    return result


def scan_processes(cpu_by_name: dict[str, float] | None = None) -> dict:
    cpu_by_name = cpu_by_name or {}
    scanned = 0
    candidates = []  # (name, exe, cpu)
    for p in psutil.process_iter(["name"]):
        scanned += 1
        try:
            name = (p.info.get("name") or "").strip()
            if not name:
                continue
            exe = p.exe()  # AccessDenied on protected/system procs -> skip (legit)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
        if not exe:
            continue
        # prelim: would this even be a candidate (ignoring signature)?
        if evaluate_process(name, exe, "unknown", cpu_by_name.get(name)) is None:
            continue
        candidates.append((name, exe, cpu_by_name.get(name)))

    # signature-check only the flagged candidates (cap; dedupe by path)
    uniq = []
    seen = set()
    for _, exe, _c in candidates:
        k = exe.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(exe)
    sigs = _check_signatures(uniq[: config.SEC_MAX_SIG_CHECK])

    findings = []
    emitted = set()
    for name, exe, cpu in candidates:
        signed = sigs.get(exe.lower(), "unknown")
        f = evaluate_process(name, exe, signed, cpu)
        if not f:
            continue
        key = (f["name"], f["exe_path"])
        if key in emitted:
            continue
        emitted.add(key)
        findings.append(f)

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return {
        "suspicious": findings,
        "scanned_at": int(time.time()),
        "scanned_count": scanned,
        "signature_checked": bool(uniq),
    }
