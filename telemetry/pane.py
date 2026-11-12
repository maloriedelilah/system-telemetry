"""Second output: push the collected data to Pane (VictoriaMetrics) as Influx line protocol.

to_lines(gpus, models, host) is a pure function; push(lines) does
one POST per cycle to {PANE_URL}/influx/write with a per-box bearer token.

Pane's vmauth stamps host=<box> on everything a token writes and accepts only
POST /influx/write and /api/v1/import/prometheus, so the lines carry no host tag
and no timestamp (the server stamps receipt time). VictoriaMetrics names an Influx
series <measurement>_<field>; the names below are fixed by Pane's fleet dashboard:

  gpu,gpu=<idx>,name=<gpu name>  temp_c util_percent mem_used_mb mem_total_mb mem_pct power_w
  gpu_model,gpu=<idx>,model=<label>  loaded=1     (strings are dropped, so the label is a tag)
  cpu,cpu=<name>                 temp_c load_percent power_w
  mem                            used_gb total_gb used_percent
  disk,disk=<name>               temp_c used_percent
  telemetry                      up=1 gpus=<n>    (heartbeat, every cycle)

Do not name a GPU field so that it matches .*gpu.*mem.*used.* other than
mem_used_mb: the dashboard takes the max of everything matching, so a percent
there would mix units. That is why the percentage is mem_pct.

The token is read from PANE_TOKEN_FILE each cycle and lives only in a local
variable and one request header. It is never logged, never in an error report,
and is scrubbed from anything echoed back by the server.

stdlib only.
"""

import math
import os
import re
import sys
import urllib.error
import urllib.request

from . import config

dbg = config.dbg

# "nothing is loaded" placeholders that nvidia.read_models() uses as labels; they
# are not models, so they get no gpu_model series.
_NO_MODEL = {"none loaded", "idle / no compute job", "unknown", ""}

_mode_warned = False


# ---------------------------------------------------------------------------
# Line protocol
# ---------------------------------------------------------------------------
def _tag(value):
    """Escape a tag key/value per the Influx spec (backslash, comma, space, '=').
    Control characters (newlines included) become a space; empty becomes 'unknown'."""
    s = "" if value is None else re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    if not s:
        return "unknown"
    return (s.replace("\\", "\\\\").replace(",", "\\,")
             .replace(" ", "\\ ").replace("=", "\\="))


def _number(v):
    """float(v) if v is a real, finite number, else None. Strings (e.g. nvidia-smi's
    '[N/A]'), bools, None, NaN and +-inf all give None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _fmt(f):
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


def _line(measurement, tags, fields):
    """One line, or None if no field has a usable value. tags/fields are lists of
    (key, value) pairs in output order."""
    parts = [f"{k}={_fmt(n)}" for k, v in fields
             if (n := _number(v)) is not None]
    if not parts:
        return None
    head = ",".join([measurement] + [f"{_tag(k)}={_tag(v)}" for k, v in tags])
    return f"{head} {','.join(parts)}"


def _unique(names):
    """Two sockets/drives can share a model name (dual identical Xeons in LHM), and
    identical tags would be one series. Suffix every duplicate with its position."""
    names = [str(n) if n is not None else "" for n in names]
    return [f"{n} #{i}" if names.count(n) > 1 else n
            for i, n in enumerate(names)]


def _idx_key(k):
    s = str(k)
    return (0, int(s), s) if s.isdigit() else (1, 0, s)


def to_lines(gpus, models, host):
    """Build the Influx lines for one cycle.

    gpus   - list of dicts as nvidia.read_gpus() / lhm.read_host_and_gpus() produce:
             index, name, util, mem_used, mem_total, mem_pct, temp, power
    models - {gpu_index: label} as nvidia.read_models() returns
    host   - {"cpus": [{name, temp_c, load_pct, power_w}], "ram": {used_gb,
             total_gb, pct}, "disks": [{name, temp_c, used_pct}]} or None; the
             same shape from linux_host.read_host() and lhm.read_host_and_gpus().
    """
    gpus = gpus or []
    models = models or {}
    host = host or {}
    lines = []

    def add(line):
        if line:
            lines.append(line)

    for g in gpus:
        add(_line("gpu", [("gpu", g.get("index")), ("name", g.get("name"))], [
            ("temp_c", g.get("temp")),
            ("util_percent", g.get("util")),
            ("mem_used_mb", g.get("mem_used")),
            ("mem_total_mb", g.get("mem_total")),
            ("mem_pct", g.get("mem_pct")),
            ("power_w", g.get("power")),
        ]))

    for idx in sorted(models, key=_idx_key):
        label = str(models[idx] if models[idx] is not None else "")[:255]
        if label.strip().lower() in _NO_MODEL:
            continue
        add(_line("gpu_model", [("gpu", idx), ("model", label)], [("loaded", 1)]))

    cpus = host.get("cpus") or []
    for c, name in zip(cpus, _unique([c.get("name") or "cpu" for c in cpus])):
        add(_line("cpu", [("cpu", name)], [
            ("temp_c", c.get("temp_c")),
            ("load_percent", c.get("load_pct")),
            ("power_w", c.get("power_w")),
        ]))

    ram = host.get("ram") or {}
    add(_line("mem", [], [
        ("used_gb", ram.get("used_gb")),
        ("total_gb", ram.get("total_gb")),
        ("used_percent", ram.get("pct")),
    ]))

    disks = host.get("disks") or []
    for d, name in zip(disks, _unique([d.get("name") or "disk" for d in disks])):
        add(_line("disk", [("disk", name)], [
            ("temp_c", d.get("temp_c")),
            ("used_percent", d.get("used_pct")),
        ]))

    add(_line("telemetry", [], [("up", 1), ("gpus", len(gpus))]))
    return lines


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------
def _read_token(did):
    """Read the bearer token from PANE_TOKEN_FILE. Returns it, or None after
    printing a house-format error. Nothing read from the file is ever put in a
    message (not even through an exception: decode errors quote bytes)."""
    global _mode_warned
    path = config.PANE_TOKEN_FILE
    try:
        with open(path, "rb") as f:
            raw = f.read(4096)
            mode = os.fstat(f.fileno()).st_mode & 0o777
    except OSError as e:
        config.report(
            "Pane token file cannot be read",
            f"open {path} (then: {did})",
            "a readable file holding this box's Pane token",
            f"{type(e).__name__}: {e.strerror or e}",
            f"check the file exists at {path} (or set PANE_TOKEN_FILE), is mode "
            "0400 and owned by the service user; to issue one run "
            f"`add-box.sh {config.HOST}` on pane. No request was sent.")
        return None

    if (sys.platform.startswith("linux") and mode & 0o077 and not _mode_warned):
        _mode_warned = True
        config.report(
            "Pane token file is readable by other users (continuing)",
            f"stat {path}",
            "mode 0400 or 0600",
            f"mode {mode:04o}",
            f"run `chmod 0400 {path}` as the service user's owner.")

    token = raw.strip()
    if not token or not all(0x21 <= b <= 0x7e for b in token):
        config.report(
            "Pane token file does not hold a token",
            f"read {path} (then: {did})",
            "one token: printable ASCII, no spaces or line breaks inside",
            "the file is empty or has characters a token cannot contain "
            "(contents not shown)",
            f"rewrite {path} with only the token that `add-box.sh {config.HOST}` "
            "printed on pane. No request was sent.")
        return None
    return token.decode("ascii")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the Authorization header to wherever it points.
    Returning None makes urllib raise the 3xx as an HTTPError instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    # ProxyHandler({}): never route the token through an http(s)_proxy from the
    # environment; Pane is a direct tailnet hop.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _scrub(text, token):
    return text.replace(token, "***") if token else text


def _next_for(status):
    if status == 401:
        return (f"the token is wrong or not added on Pane: check {config.PANE_TOKEN_FILE} "
                f"matches what `add-box.sh {config.HOST}` printed on pane (re-run it to "
                "issue a new one; a rotation needs no restart here)")
    if status == 403:
        return ("Pane refused the request: only POST /influx/write and "
                "/api/v1/import/prometheus are accepted; check PANE_URL points at "
                "vmauth's port with no extra path, and the box's token/grants on pane")
    if 300 <= status < 400:
        return ("the redirect was not followed (it would resend the token); check "
                "PANE_URL (http vs https, port, no path)")
    return ("check vmauth and VictoriaMetrics on pane (service status and logs); "
            "if it keeps happening, check the tailnet path and Pane's grants")


def push(lines):
    """POST the lines to Pane in one body. True on 200/204, False otherwise (the
    reason is printed in the house format). Never raises."""
    if not lines:
        return True
    base = config.PANE_URL
    # A token pasted into the URL (http://user:token@host) would land in every message below. Refuse it
    # first, and never print that part (OPS-19, Errata's OPS-18 Low).
    # Any "@" counts, scheme or not: "kiosk:TOKEN@host:8427" parses with "kiosk" as its scheme and an
    # empty netloc, and must still never be echoed (Errata, OPS-19 L1).
    if "@" in base:
        prefix = base.split("://", 1)[0] + "://" if "://" in base.split("@", 1)[0] else ""
        shown = f"{prefix}<removed>@{base.rsplit('@', 1)[1]}"
        config.report("PANE_URL has a user or token in it", f"read PANE_URL ({shown})",
                      "PANE_URL like http://100.92.61.42:8427, with the token only in PANE_TOKEN_FILE",
                      "a user:password@ part (not shown)",
                      "remove the user:token@ part from PANE_URL in .env; the token belongs in "
                      "PANE_TOKEN_FILE. No request was sent.")
        return False
    endpoint = f"{base}/influx/write"
    did = f"POST {endpoint} ({len(lines)} lines)"

    if not base.lower().startswith(("http://", "https://")):
        config.report("PANE_URL is not an http(s) URL", did,
                      "PANE_URL like http://100.92.61.42:8427",
                      f"PANE_URL={base!r}",
                      "fix PANE_URL in .env (or leave it empty to turn Pane off). "
                      "No request was sent.")
        return False

    token = _read_token(did)
    if token is None:
        return False

    try:
        req = urllib.request.Request(
            endpoint, data=("\n".join(lines) + "\n").encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "text/plain; charset=utf-8"})
        try:
            with _opener().open(req, timeout=config.PANE_TIMEOUT) as r:
                status = r.status
        except urllib.error.HTTPError as e:   # non-2xx, and 3xx (not followed)
            status = e.code
            try:
                body = e.read(1000).decode("utf-8", "replace")
            except Exception:
                body = ""
            config.report(
                "Pane rejected the push", did, "204",
                _scrub(f"{status} {' '.join(body.split())[:200]}", token).strip(),
                _next_for(status))
            return False
        if status in (200, 204):
            print(f"Pane: pushed {len(lines)} lines for {config.HOST} ({status})",
                  flush=True)
            return True
        config.report("Pane answered unexpectedly", did, "204",
                      f"{status} (no error, but not 200/204)", _next_for(status))
        return False
    except Exception as e:                    # URLError, timeout, reset, DNS, ...
        reason = getattr(e, "reason", e)
        config.report(
            "Pane unreachable", did, "204",
            _scrub(f"{type(e).__name__}: {reason}", token),
            "check this box is on the tailnet (`tailscale status`), that "
            f"{base} is right, that Pane's grants allow this box, and that "
            f"vmauth is up on pane; the timeout is PANE_TIMEOUT={config.PANE_TIMEOUT:g}s")
        return False

