"""Site probes (TELEMETRY_SITES=1): is each public site up, healthy, and its certificate current?

Each line of SITES_FILE is a URL, optionally followed by a check:
  health (the default)  the URL is a health endpoint: healthy means 200, a JSON object, "status"
                        is "ok" or "ok" is true, and "db" (when the key is there) is "ok"
  up                    the site has no health endpoint yet: healthy means any 2xx
Each cycle this GETs every URL (in parallel) and returns one result per site:

  site        the URL's host (host + path if two entries share a host)
  reachable   1 if any HTTP response came back, else 0 (DNS, refused, timeout, TLS failure)
  code        the HTTP status, 0 if none
  latency_ms  connect + TLS + time to the response headers
  healthy     1 if the check above passes, else 0
  db_ok       1/0 from "db"; absent if the body has no "db" key
  cert_days   days until the TLS certificate expires (https only)
  error       a short reason when it isn't healthy (printed, never pushed)

Redirects are not followed (a health URL that redirects is misconfigured, and is reported with
its 3xx code). No proxy from the environment is used. The body read is capped at 64 KiB.

SITES_CONTROL_URL is probed the same way, for reachability only: if it is down too, this box has
lost the internet and the sites may be fine.

stdlib only.
"""

import http.client
import json
import socket
import ssl
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import config

dbg = config.dbg

USER_AGENT = "pane-site-probe/1 (system-telemetry; homelab monitoring)"
MAX_BODY = 64 * 1024
MAX_WORKERS = 16


CHECKS = ("health", "up")


def load_sites(path=None):
    """[(url, check), ...] from the sites file, in order: '#' starts a comment, blank lines are
    skipped, a line is `URL [health|up]`. Bad lines are reported and skipped. Returns [] (after a
    report) if the file can't be read."""
    path = path or config.SITES_FILE
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        config.report("Site list cannot be read", f"open {path}",
                      "a readable sites.txt with one URL per line",
                      f"{type(e).__name__}: {e.strerror or e}",
                      "check SITES_FILE in .env (empty means the repo's sites.txt); "
                      "no sites were probed")
        return []
    sites, seen = [], set()
    for n, line in enumerate(raw.splitlines(), 1):
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        url, check = words[0], (words[1].lower() if len(words) > 1 else "health")
        p = urllib.parse.urlsplit(url)
        if (p.scheme not in ("http", "https") or not p.hostname or len(words) > 2
                or check not in CHECKS):
            config.report("Site list has a line it can't read (skipped)",
                          f"read {path} line {n}", "http(s)://host/path [health|up]",
                          repr(line.strip()[:120]), "fix that line in sites.txt")
            continue
        if url in seen:
            continue
        seen.add(url)
        sites.append((url, check))
    return sites


def site_names(urls):
    """The host of each URL; host + path where two URLs share a host."""
    hosts = [urllib.parse.urlsplit(u).hostname or u for u in urls]
    out = []
    for u, h in zip(urls, hosts):
        if hosts.count(h) > 1:
            p = urllib.parse.urlsplit(u)
            out.append(h + (p.path or "/"))
        else:
            out.append(h)
    return out


def _is_ok(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("ok", "true"))


def _judge(code, body, check="health"):
    """(healthy, db_ok, error) for a response."""
    if check == "up":
        return (1, None, "") if 200 <= code < 300 else (0, None, f"HTTP {code}")
    if code != 200:
        return 0, None, f"HTTP {code}"
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 0, None, "the body is not JSON"
    if not isinstance(data, dict):
        return 0, None, "the body is JSON but not an object"
    db_ok = (1 if _is_ok(data["db"]) else 0) if "db" in data else None
    if "status" in data:
        if not _is_ok(data["status"]):
            return 0, db_ok, f"status is {str(data['status'])[:40]!r}, not 'ok'"
    elif "ok" in data:
        if not _is_ok(data["ok"]):
            return 0, db_ok, f"ok is {str(data['ok'])[:40]!r}, not true"
    else:
        return 0, db_ok, "the JSON has neither \"status\" nor \"ok\""
    if db_ok == 0:
        return 0, db_ok, f"db is {str(data['db'])[:40]!r}, not 'ok'"
    return 1, db_ok, ""


def probe(url, check="health", timeout=None, context=None):
    """GET one URL. Never raises; returns the result dict described above (without 'site')."""
    timeout = timeout or config.SITES_TIMEOUT
    p = urllib.parse.urlsplit(url)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    res = {"reachable": 0, "code": 0, "latency_ms": None, "healthy": 0,
           "db_ok": None, "cert_days": None, "error": ""}
    conn = None
    t0 = time.monotonic()
    try:
        if p.scheme == "https":
            conn = http.client.HTTPSConnection(
                p.hostname, p.port or 443, timeout=timeout,
                context=context or ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=timeout)
        conn.request("GET", path, headers={"User-Agent": USER_AGENT,
                                           "Accept": "application/json"})
        if p.scheme == "https":
            try:
                not_after = conn.sock.getpeercert().get("notAfter")
                if not_after:
                    res["cert_days"] = round(
                        (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400, 2)
            except Exception:
                pass
        r = conn.getresponse()
        res["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        res["reachable"], res["code"] = 1, r.status
        body = r.read(MAX_BODY)
        res["healthy"], res["db_ok"], res["error"] = _judge(r.status, body, check)
    except Exception as e:      # DNS, refused, timeout, TLS verify, reset, bad status line ...
        reason = getattr(e, "reason", None) or getattr(e, "strerror", None) or e
        res["error"] = f"{type(e).__name__}: {' '.join(str(reason).split())[:160]}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return res


def probe_all(sites=None, timeout=None, context=None):
    """(results, control) for this cycle. sites: [(url, check)] (default: load_sites()).
    results: one dict per site, with 'site' set, in list order. control: the probe result for
    SITES_CONTROL_URL, or None if it is empty."""
    sites = load_sites() if sites is None else list(sites)
    control_url = config.SITES_CONTROL_URL
    targets = sites + ([(control_url, "up")] if control_url else [])
    if not targets:
        return [], None
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(targets))) as ex:
        out = list(ex.map(lambda t: probe(t[0], t[1], timeout, context), targets))
    results = out[:len(sites)]
    for r, name in zip(results, site_names([u for u, _ in sites])):
        r["site"] = name
        if not r["healthy"]:
            print(f"site {name}: NOT healthy ({r['error'] or 'unknown'})",
                  file=sys.stderr, flush=True)
    control = out[len(sites)] if control_url else None
    if control is not None and not control["reachable"]:
        print(f"site control {control_url}: unreachable ({control['error']}); "
              "this box may have lost the internet", file=sys.stderr, flush=True)
    healthy = sum(r["healthy"] for r in results)
    dbg(f"sites: {healthy}/{len(results)} healthy")
    return results, control
