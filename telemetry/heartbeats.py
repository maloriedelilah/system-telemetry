"""Heartbeat files (HEARTBEATS_DIR): jobs that run now and then report through the box's telemetry.

A job that runs once a day (Chonky's 04:00 backup pull) can't keep a series alive on Pane by itself:
the pages look back a few minutes. So the job writes a small status file when it finishes, and this
collector, which runs every cycle, turns each entry into a live heartbeat line:

  heartbeat,check=<name> ok=<0|1>,age_s=<now - last_ok>,period_s=<period_s>

These are Pane's contract names (heartbeat_ok, heartbeat_age_s, heartbeat_period_s by `check`), so
the Heartbeats panels show them, and age_s keeps growing between runs, which is the point: a backup
that stops being made shows up as it ages past its period. ok is 1 only when the file says ok AND
age_s is within period_s.

Each *.json file in HEARTBEATS_DIR looks like:

  {"version": 1, "checks": [
     {"check": "backup:dbserver daily/app", "last_ok": 1794552000, "period_s": 93600, "ok": true}
  ]}

  check     the label: 1-120 printable characters
  last_ok   Unix seconds of the last good event (a backup file's time, a run's end)
  period_s  how old last_ok may get before the check is late (seconds, > 0)
  ok        optional, default true: false says the job itself failed (a pull that didn't work)

Limits: at most 64 files of at most 64 KiB, and 500 checks in all; anything that doesn't fit the
shape above is reported (by file name and entry number, never its text) and skipped. A last_ok more
than FUTURE_SLACK_S ahead of this box's clock is refused (it would read as fresh forever); within
that, it counts as age 0. A check name that appears twice keeps the first, so the folder must be
writable only by the jobs that own it (Chonky: SYSTEM and Administrators).

stdlib only.
"""

import glob
import json
import math
import os
import re
import time

from . import config

MAX_FILES = 64
MAX_BYTES = 64 * 1024
MAX_CHECKS = 500
FUTURE_SLACK_S = 300
_CHECK_RE = re.compile(r"[ -~]{1,120}")


def _number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:      # an int too big for a float (JSON allows any length)
        return None
    return f if math.isfinite(f) else None


def read_all(folder=None, now=None):
    """[{check, ok, age_s, period_s}, ...] from every *.json in folder, sorted by file name then
    file order. Never raises: problems are reported and the rest is still read."""
    folder = folder or config.HEARTBEATS_DIR
    now = time.time() if now is None else now
    if not os.path.isdir(folder):
        config.report("Heartbeat folder not found", f"looked for {folder}",
                      "the folder jobs write their status files in", "no such folder",
                      "create it, or fix HEARTBEATS_DIR in .env (empty turns this off)")
        return []
    files = sorted(glob.glob(os.path.join(glob.escape(folder), "*.json")))
    if len(files) > MAX_FILES:
        config.report("Too many heartbeat files (the rest skipped)", f"listed {folder}",
                      f"at most {MAX_FILES} *.json files", f"{len(files)}",
                      "remove old status files from that folder")
        files = files[:MAX_FILES]
    out, seen = [], set()
    for path in files:
        name = os.path.basename(path)
        try:
            if not os.path.isfile(path) or os.path.islink(path):
                raise ValueError("not a plain file")
            if os.path.getsize(path) > MAX_BYTES:
                raise ValueError(f"larger than {MAX_BYTES // 1024} KiB")
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("version") != 1 \
                    or not isinstance(data.get("checks"), list):
                raise ValueError('not {"version": 1, "checks": [...]}')
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as e:
            why = str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) \
                else type(e).__name__
            config.report("Heartbeat file skipped", f"read {name} in {folder}",
                          'a JSON file {"version": 1, "checks": [...]}', why,
                          "the job that writes it should rewrite it; until then its checks are missing")
            continue
        for i, c in enumerate(data["checks"], 1):
            if len(out) >= MAX_CHECKS:
                config.report("Too many heartbeat checks (the rest skipped)", f"read {name}",
                              f"at most {MAX_CHECKS} in all", "more", "split or trim the files")
                return out
            why = ""
            if not isinstance(c, dict):
                why = "not an object"
            else:
                check, last, period = c.get("check"), _number(c.get("last_ok")), \
                    _number(c.get("period_s"))
                ok = c.get("ok", True)
                if not isinstance(check, str) or not _CHECK_RE.fullmatch(check):
                    why = "check isn't 1-120 printable characters"
                elif last is None or period is None or period <= 0:
                    why = "last_ok or period_s isn't a number (period_s > 0)"
                elif last > now + FUTURE_SLACK_S:
                    why = f"last_ok is more than {FUTURE_SLACK_S} s in the future"
                elif not isinstance(ok, bool):
                    why = "ok isn't true or false"
                elif check in seen:
                    why = "its check name repeats an earlier one"
            if why:
                config.report("Heartbeat entry skipped", f"read {name} entry {i}",
                              "check, last_ok, period_s (and optionally ok)", why,
                              "fix the job that writes it")
                continue
            seen.add(check)
            age = max(0.0, now - last)
            out.append({"check": check, "age_s": round(age),
                        "period_s": round(period),
                        "ok": 1 if ok and age <= period else 0})
    return out
