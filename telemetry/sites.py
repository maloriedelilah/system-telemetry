"""Site probes (TELEMETRY_SITES=1, pane only): is each public site up, healthy, and its certificate current?

Each line of sites.txt is an https URL, optionally followed by a check:
  health (the default)  the URL is a health endpoint: healthy means 200, a JSON object, "status"
                        is "ok" or "ok" is true, and "db" (when the key is there) is "ok"
  up                    the site has no health endpoint yet: healthy means any 2xx
Each cycle this GETs every URL in parallel and returns one result per site:

  site        the URL's host (plus port, path or query when they aren't the usual ones)
  check       health or up, so a dashboard can tell "db ok" from "answered"
  reachable   1 if any HTTP response came back, else 0 (DNS, refused, timeout, TLS failure)
  code        the HTTP status, 0 if none
  latency_ms  connect + TLS + time to the response headers
  healthy     1 if the check above passes, else 0
  db_ok       1/0 from "db"; absent if the body has no "db" key
  cert_days   days until the TLS certificate expires
  error       a short reason when it isn't healthy (printed, never pushed)

Limits, so a probe can't be pointed at or held by anything it shouldn't:
  - https only, and only to public addresses: the name is resolved first and private, loopback,
    link-local and tailnet (100.64/10) addresses are never connected to (so DNS can't steer a
    probe inside either).
  - Redirects are not followed; no proxy from the environment; the body read is capped at 64 KiB.
  - SITES_TIMEOUT bounds each socket operation, and SITES_DEADLINE bounds the whole round: each
    probe runs in a daemon thread, and one still going at the deadline is reported as a timeout
    and left behind (the process is a oneshot, so it ends with it). That is why the probes refuse
    to run in loop mode (LOOP_INTERVAL), where a stuck thread would never end.
  - The list and the control URL are fixed in the repo (sites.txt, SITES_CONTROL_URL), not .env.

The control URL is probed the same way, for reachability only: if it is down too, this box has
lost the internet and the sites may be fine.

stdlib only.
"""

import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse

from . import config

dbg = config.dbg

USER_AGENT = "pane-site-probe/1 (system-telemetry; homelab monitoring)"
MAX_BODY = 64 * 1024
CHECKS = ("health", "up")

# Tests only (never from the environment): allow http:// and loopback for the local stub servers.
_ALLOW_LOCAL_FOR_TESTS = False


class _Refused(Exception):
    pass


def _clean(text, limit=160):
    """One line, no control characters (a server's bytes can end up here), at most `limit`."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).strip()[:limit]


def load_sites(path=None):
    """[(url, check), ...] from the sites file, in order: '#' starts a comment, blank lines are
    skipped, a line is `URL [health|up]`. Bad lines and lines whose site label repeats an earlier
    one are reported (by line number; their text is never echoed) and skipped. Returns [] (after
    a report) if the file can't be read."""
    path = path or config.SITES_FILE
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError) as e:
        config.report("Site list cannot be read", f"open {path}",
                      "a readable sites.txt with one URL per line",
                      type(e).__name__, "check sites.txt in the install dir; no sites were probed")
        return []
    sites, names = [], set()
    for n, line in enumerate(raw.splitlines(), 1):
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        url, check = words[0], (words[1].lower() if len(words) > 1 else "health")
        why = (_bad_url(url) or ("more than a URL and a check" if len(words) > 2 else "")
               or ("" if check in CHECKS else "the check isn't health or up"))
        if not why:
            name = site_name(url)
            if name in names:
                why = f"its site label ({name}) repeats an earlier line"
            else:
                names.add(name)
        if why:
            config.report("Site list has a line it can't use (skipped)", f"read {path} line {n}",
                          "https://host/path [health|up], one site per label", why,
                          "fix that line in sites.txt")
            continue
        sites.append((url, check))
    return sites


def _bad_url(url):
    """'' if url is one we probe, else why not."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" and not (_ALLOW_LOCAL_FOR_TESTS and p.scheme == "http"):
        return "not an https URL"
    try:
        port = p.port
    except ValueError:
        return "a bad port"
    if not p.hostname or p.username or p.password or p.fragment or port == 0:
        return "no host, or it has a user, password or fragment"
    return ""


def site_name(url):
    """The label for a URL: its host, plus port, path and query unless they're the usual ones
    (443, /, /api/health or /health, none). load_sites refuses two lines with the same label, so
    one can never hide the other."""
    p = urllib.parse.urlsplit(url)
    name = p.hostname or url
    if p.port and p.port != 443:
        name += f":{p.port}"
    path = p.path or "/"
    if path not in ("/", "/api/health", "/health") or p.query:
        name += path + (f"?{p.query}" if p.query else "")
    return name


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
    except (UnicodeDecodeError, ValueError, RecursionError):
        return 0, None, "the body is not JSON"
    if not isinstance(data, dict):
        return 0, None, "the body is JSON but not an object"
    db_ok = (1 if _is_ok(data["db"]) else 0) if "db" in data else None
    if "status" in data:
        if not _is_ok(data["status"]):
            return 0, db_ok, f"status is {_clean(data['status'], 40)!r}, not 'ok'"
    elif "ok" in data:
        if not _is_ok(data["ok"]):
            return 0, db_ok, f"ok is {_clean(data['ok'], 40)!r}, not true"
    else:
        return 0, db_ok, 'the JSON has neither "status" nor "ok"'
    if db_ok == 0:
        return 0, db_ok, f"db is {_clean(data['db'], 40)!r}, not 'ok'"
    return 1, db_ok, ""


def _public(addr):
    return ipaddress.ip_address(addr.split("%", 1)[0]).is_global


def _connect(host, port, timeout):
    """A TCP socket to the first public address of host that answers. Private, loopback,
    link-local and tailnet addresses are refused before any connection is made."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    allowed = [i for i in infos if _ALLOW_LOCAL_FOR_TESTS or _public(i[4][0])]
    if not allowed:
        raise _Refused(f"{host} resolves only to non-public addresses")
    err = None
    for family, socktype, proto, _, addr in allowed:
        s = socket.socket(family, socktype, proto)
        s.settimeout(timeout)
        try:
            s.connect(addr)
            return s
        except OSError as e:
            err = e
            s.close()
    raise err


def probe(url, check="health", timeout=None, context=None):
    """GET one URL. Never raises; returns the result dict described above (without 'site')."""
    timeout = timeout or config.SITES_TIMEOUT
    p = urllib.parse.urlsplit(url)
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    res = {"check": check, "reachable": 0, "code": 0, "latency_ms": None, "healthy": 0,
           "db_ok": None, "cert_days": None, "error": ""}
    conn = sock = None
    t0 = time.monotonic()
    try:
        why = _bad_url(url)
        if why:
            raise _Refused(why)
        https = p.scheme == "https"
        port = p.port or (443 if https else 80)
        sock = _connect(p.hostname, port, timeout)
        if https:
            sock = (context or ssl.create_default_context()).wrap_socket(
                sock, server_hostname=p.hostname)
            try:
                not_after = sock.getpeercert().get("notAfter")
                if not_after:
                    res["cert_days"] = round(
                        (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400, 2)
            except Exception:
                pass
            conn = http.client.HTTPSConnection(p.hostname, port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(p.hostname, port, timeout=timeout)
        conn.sock = sock            # already connected and checked; http.client won't reconnect
        conn.request("GET", path, headers={"User-Agent": USER_AGENT,
                                           "Accept": "application/json"})
        r = conn.getresponse()
        res["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        res["reachable"], res["code"] = 1, r.status
        body = r.read(MAX_BODY)
        res["healthy"], res["db_ok"], res["error"] = _judge(r.status, body, check)
    except Exception as e:      # DNS, refused, timeout, TLS verify, reset, bad status line ...
        reason = (getattr(e, "verify_message", None) or getattr(e, "reason", None)
                  or getattr(e, "strerror", None) or e)
        res["error"] = f"{type(e).__name__}: {_clean(reason)}"
    finally:
        for c in (conn, sock):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
    return res


def _timed_out(check, deadline):
    return {"check": check, "reachable": 0, "code": 0, "latency_ms": None, "healthy": 0,
            "db_ok": None, "cert_days": None,
            "error": f"no answer within SITES_DEADLINE ({deadline:g} s)"}


def probe_all(sites=None, timeout=None, context=None, deadline=None):
    """(results, control) for this cycle, within `deadline` seconds (default SITES_DEADLINE).
    sites: [(url, check)] (default: load_sites()). results: one dict per site, with 'site' set,
    in list order. control: the probe result for SITES_CONTROL_URL, or None if it is empty."""
    sites = load_sites() if sites is None else list(sites)
    deadline = deadline or config.SITES_DEADLINE
    control_url = config.SITES_CONTROL_URL
    targets = sites + ([(control_url, "up")] if control_url else [])
    if not targets:
        return [], None
    slots = [None] * len(targets)    # written by the threads; read once, at the deadline

    def run(i, url, check):
        slots[i] = probe(url, check, timeout, context)

    # Daemon threads: one still stuck at the deadline is abandoned and never holds up the
    # process's exit (a ThreadPoolExecutor's workers would be joined at exit).
    threads = [threading.Thread(target=run, args=(i, u, c), daemon=True)
               for i, (u, c) in enumerate(targets)]
    end = time.monotonic() + deadline
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    out = [dict(r) if r is not None else _timed_out(c, deadline)
           for r, (_, c) in zip(list(slots), targets)]

    results = out[:len(sites)]
    for r, (url, _) in zip(results, sites):
        r["site"] = site_name(url)
        if not r["healthy"]:
            print(f"site {r['site']}: NOT healthy ({r['error'] or 'unknown'})",
                  file=sys.stderr, flush=True)
    control = out[len(sites)] if control_url else None
    if control is not None and not control["reachable"]:
        print(f"site control {control_url}: unreachable ({control['error']}); "
              "this box may have lost the internet", file=sys.stderr, flush=True)
    dbg(f"sites: {sum(r['healthy'] for r in results)}/{len(results)} healthy")
    return results, control
