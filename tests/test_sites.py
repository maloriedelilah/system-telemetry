"""Site probes: the sites file, the health judgement, real HTTP(S) probes, lines, and run_once.

Standalone script; the exit status is the verdict (CI runs every tests/test_*.py).
Real HTTP and HTTPS servers on 127.0.0.1 in threads (a self-signed certificate made with
openssl for the HTTPS cases). Nothing leaves the machine.
Run from the repo root:  python tests/test_sites.py
"""
import contextlib, importlib, io, os, socket, ssl, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from telemetry import config, pane, sites  # noqa: E402

# The stub servers are http:// on 127.0.0.1, which the probes refuse in real use; the tests that
# check that refusal turn this off again.
sites._ALLOW_LOCAL_FOR_TESTS = True

main = importlib.import_module("telemetry.__main__")   # importing it starts nothing

TMP = tempfile.mkdtemp(prefix="sites-test-")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class Capture:
    def __enter__(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        self._cms = [contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err)]
        for cm in self._cms:
            cm.__enter__()
        return self

    def __exit__(self, *exc):
        for cm in reversed(self._cms):
            cm.__exit__(*exc)
        return False


class Site:
    """A fake site. routes: {path: (status, body_bytes, content_type)} or {path: callable(h)}."""

    def __init__(self, routes, tls=None):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({"path": self.path, "ua": self.headers.get("User-Agent")})
                r = routes.get(self.path, (404, b"Not found", "text/plain"))
                if callable(r):
                    return r(self)
                status, body, ctype = r
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        scheme = "http"
        if tls:
            self.srv.socket = tls.wrap_socket(self.srv.socket, server_side=True)
            scheme = "https"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"{scheme}://localhost:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def J(body, status=200):
    return (status, body.encode(), "application/json")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sites_file(text):
    path = os.path.join(TMP, f"sites-{time.monotonic_ns()}.txt")
    with open(path, "w") as f:
        f.write(text)
    return path


_CERT = {}


def cert(days=90):
    """(server_context, client_context_that_trusts_it) for a self-signed localhost cert."""
    if days not in _CERT:
        key, crt = os.path.join(TMP, f"k{days}.pem"), os.path.join(TMP, f"c{days}.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                        "-out", crt, "-days", str(days), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost"],
                       check=True, capture_output=True)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(crt, key)
        client = ssl.create_default_context(cafile=crt)
        _CERT[days] = (server, client)
    return _CERT[days]


# ---------------------------------------------------------------------------
# the sites file
# ---------------------------------------------------------------------------
def test_load_sites_parses_checks_and_comments():
    path = sites_file("# header\n\nhttps://a.example/api/health\n"
                      "https://b.example/  up   # no route yet\n"
                      "  https://c.example/health   HEALTH\n")
    with Capture() as c:
        got = sites.load_sites(path)
    assert got == [("https://a.example/api/health", "health"), ("https://b.example/", "up"),
                   ("https://c.example/health", "health")], got
    assert c.err.getvalue() == "", c.err.getvalue()


def test_load_sites_skips_bad_lines_without_echoing_them():
    path = sites_file("ftp://x.example/\nnot a url\nhttps://ok.example/ maybe\n"
                      "https://ok.example/ up extra\nhttps:///nohost\n"
                      "https://u:pw_SECRET@x.example/\nhttps://x.example:99999/\n"
                      "https://good.example/\n")
    with Capture() as c:
        got = sites.load_sites(path)
    assert got == [("https://good.example/", "health")], got
    err = c.err.getvalue()
    assert err.count("!!! Site list has a line it can't use (skipped)") == 7, err
    for n in range(1, 8):
        assert f"line {n}" in err, err
    # A line's text is never echoed (the file could be the wrong one, holding a secret).
    assert "pw_SECRET" not in err and "nohost" not in err and "maybe" not in err, err


def test_load_sites_refuses_a_repeated_label():
    # /api/health and /health both label as the bare host, and a second identical URL repeats too:
    # whichever came second would hide the first's state.
    path = sites_file("https://a.example/api/health\nhttps://a.example/health\n"
                      "https://a.example/api/health\nhttps://a.example:8443/api/health\n"
                      "https://a.example/other\n")
    with Capture() as c:
        got = sites.load_sites(path)
    assert got == [("https://a.example/api/health", "health"),
                   ("https://a.example:8443/api/health", "health"),
                   ("https://a.example/other", "health")], got
    err = c.err.getvalue()
    assert err.count("repeats an earlier line") == 2 and "line 2" in err and "line 3" in err, err


def test_load_sites_refuses_http_in_real_use():
    sites._ALLOW_LOCAL_FOR_TESTS = False
    try:
        with Capture() as c:
            got = sites.load_sites(sites_file("http://a.example/api/health\nhttps://b.example/\n"))
        assert got == [("https://b.example/", "health")], got
        assert "not an https URL" in c.err.getvalue(), c.err.getvalue()
    finally:
        sites._ALLOW_LOCAL_FOR_TESTS = True


def test_load_sites_unreadable_file_reports_and_returns_empty():
    with Capture() as c:
        got = sites.load_sites(os.path.join(TMP, "nope.txt"))
    assert got == [] and "!!! Site list cannot be read" in c.err.getvalue(), c.err.getvalue()


def test_repo_sites_file_is_valid_and_all_https():
    sites._ALLOW_LOCAL_FOR_TESTS = False
    try:
        with Capture() as c:
            got = sites.load_sites(os.path.join(ROOT, "sites.txt"))
    finally:
        sites._ALLOW_LOCAL_FOR_TESTS = True
    assert c.err.getvalue() == "", c.err.getvalue()
    assert len(got) >= 10, got
    assert all(u.startswith("https://") for u, _ in got), got


def test_site_name():
    n = sites.site_name
    assert n("https://a.x/api/health") == "a.x"
    assert n("https://a.x/health") == "a.x"
    assert n("https://a.x/") == "a.x" and n("https://a.x") == "a.x"
    assert n("https://a.x:443/api/health") == "a.x"
    assert n("https://a.x:8443/api/health") == "a.x:8443"
    assert n("https://a.x/status") == "a.x/status"
    assert n("https://a.x/api/health?deep=1") == "a.x/api/health?deep=1"


# ---------------------------------------------------------------------------
# the judgement
# ---------------------------------------------------------------------------
def test_judge_matrix():
    j = sites._judge
    assert j(200, b'{"status":"ok","db":"ok"}') == (1, 1, "")
    assert j(200, b'{"status":"OK"}') == (1, None, "")
    assert j(200, b'{"ok":true,"service":"x"}') == (1, None, "")
    assert j(200, b'{"ok":"true"}')[0] == 1
    assert j(200, b'{"status":"ok","db":"down"}') == (0, 0, "db is 'down', not 'ok'")
    assert j(200, b'{"status":"degraded","db":"ok"}')[:2] == (0, 1)
    assert j(200, b'{"ok":false}')[0] == 0
    assert j(200, b'{"ok":1}')[0] == 0                     # only true or "ok"/"true"
    assert j(200, b'{"service":"x"}') == (0, None, 'the JSON has neither "status" nor "ok"')
    assert j(200, b'<!doctype html>') == (0, None, "the body is not JSON")
    assert j(200, b'["ok"]') == (0, None, "the body is JSON but not an object")
    assert j(200, b'\xff\xfe') == (0, None, "the body is not JSON")
    assert j(503, b'{"status":"ok"}') == (0, None, "HTTP 503")
    assert j(204, b"", "up") == (1, None, "")
    assert j(200, b"<html>", "up") == (1, None, "")
    assert j(301, b"", "up") == (0, None, "HTTP 301")
    assert j(404, b"", "up") == (0, None, "HTTP 404")


# ---------------------------------------------------------------------------
# real probes
# ---------------------------------------------------------------------------
def test_probe_healthy_and_unhealthy_over_http():
    s = Site({"/api/health": J('{"status":"ok","db":"ok"}'),
              "/bad": J('{"status":"ok","db":"error"}'),
              "/html": (200, b"<html></html>", "text/html"),
              "/boom": J('{"detail":"x"}', 500)})
    try:
        r = sites.probe(s.base + "/api/health", timeout=3)
        assert (r["reachable"], r["code"], r["healthy"], r["db_ok"], r["error"]) == \
            (1, 200, 1, 1, ""), r
        assert r["latency_ms"] is not None and r["latency_ms"] >= 0, r
        assert r["cert_days"] is None, r                  # plain http has no certificate
        r = sites.probe(s.base + "/bad", timeout=3)
        assert (r["healthy"], r["db_ok"]) == (0, 0), r
        r = sites.probe(s.base + "/html", timeout=3)
        assert (r["reachable"], r["code"], r["healthy"]) == (1, 200, 0), r
        r = sites.probe(s.base + "/html", "up", timeout=3)
        assert r["healthy"] == 1, r
        r = sites.probe(s.base + "/boom", timeout=3)
        assert (r["reachable"], r["code"], r["healthy"], r["error"]) == (1, 500, 0, "HTTP 500"), r
        assert all(q["ua"] == sites.USER_AGENT for q in s.requests), s.requests
    finally:
        s.stop()


def test_probe_keeps_query_string():
    s = Site({"/h?x=1": J('{"ok":true}')})
    try:
        assert sites.probe(s.base + "/h?x=1", timeout=3)["healthy"] == 1
    finally:
        s.stop()


def test_redirect_is_not_followed():
    def redirect(h):
        h.send_response(302)
        h.send_header("Location", "/api/health")
        h.send_header("Content-Length", "0")
        h.end_headers()
    s = Site({"/old": redirect, "/api/health": J('{"status":"ok"}')})
    try:
        r = sites.probe(s.base + "/old", timeout=3)
        assert (r["reachable"], r["code"], r["healthy"]) == (1, 302, 0), r
        assert [q["path"] for q in s.requests] == ["/old"], s.requests
    finally:
        s.stop()


def test_connection_refused_is_unreachable():
    r = sites.probe(f"http://127.0.0.1:{free_port()}/api/health", timeout=3)
    assert (r["reachable"], r["code"], r["healthy"], r["latency_ms"]) == (0, 0, 0, None), r
    assert r["error"].startswith("ConnectionRefusedError"), r


def test_timeout_is_bounded():
    def slow(h):
        time.sleep(3)
    s = Site({"/api/health": slow})
    try:
        t0 = time.monotonic()
        r = sites.probe(s.base + "/api/health", timeout=0.5)
        took = time.monotonic() - t0
        assert took < 2.0, took
        assert (r["reachable"], r["healthy"]) == (0, 0), r
        assert "timed out" in r["error"].lower() or "Timeout" in r["error"], r
    finally:
        s.stop()


def test_body_read_is_capped():
    big = b'{"status":"ok","pad":"' + b"x" * (sites.MAX_BODY * 2) + b'"}'
    s = Site({"/api/health": (200, big, "application/json")})
    try:
        r = sites.probe(s.base + "/api/health", timeout=3)
        # Only MAX_BODY bytes are read, so the JSON is cut short: reachable, but not healthy.
        assert (r["reachable"], r["code"], r["healthy"], r["error"]) == \
            (1, 200, 0, "the body is not JSON"), r
    finally:
        s.stop()


def test_env_proxy_is_not_used():
    s = Site({"/api/health": J('{"status":"ok"}')})
    dead = f"http://127.0.0.1:{free_port()}"
    old = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY")}
    try:
        for k in old:
            os.environ[k] = dead
        assert sites.probe(s.base + "/api/health", timeout=3)["healthy"] == 1
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        s.stop()


def test_https_untrusted_certificate_is_unreachable():
    server_ctx, _ = cert(90)
    s = Site({"/api/health": J('{"status":"ok"}')}, tls=server_ctx)
    try:
        r = sites.probe(s.base + "/api/health", timeout=3)       # the default (system) trust
        assert (r["reachable"], r["healthy"], r["code"]) == (0, 0, 0), r
        # The verify message says which failure it is (self-signed, expired, wrong host ...).
        assert r["error"].startswith("SSLCertVerificationError: self-signed certificate"), r
    finally:
        s.stop()


def test_https_trusted_certificate_reports_cert_days():
    server_ctx, client_ctx = cert(90)
    s = Site({"/api/health": J('{"status":"ok","db":"ok"}')}, tls=server_ctx)
    try:
        r = sites.probe(s.base + "/api/health", timeout=3, context=client_ctx)
        assert (r["reachable"], r["code"], r["healthy"], r["db_ok"]) == (1, 200, 1, 1), r
        assert 88.5 < r["cert_days"] <= 90.01, r
    finally:
        s.stop()


def test_refuses_http_and_non_public_addresses_in_real_use():
    s = Site({"/api/health": J('{"status":"ok"}')})
    sites._ALLOW_LOCAL_FOR_TESTS = False
    try:
        r = sites.probe(s.base + "/api/health", timeout=2)
        assert (r["reachable"], r["error"]) == (0, "_Refused: not an https URL"), r
        for url in ("https://127.0.0.1:1/api/health", "https://localhost:1/api/health",
                    "https://10.0.0.1/api/health", "https://192.168.68.1/", "https://169.254.169.254/",
                    "https://100.92.61.42:8427/", "https://[::1]:1/"):
            r = sites.probe(url, timeout=2)
            assert r["reachable"] == 0 and "resolves only to non-public addresses" in r["error"], (url, r)
        assert s.requests == [], s.requests                # nothing reached the stub
    finally:
        sites._ALLOW_LOCAL_FOR_TESTS = True
        s.stop()


def test_server_control_characters_never_reach_the_log():
    def bad_status(h):
        h.wfile.write(b"HTTP/1.1 2\x1b[31m00 OK\r\n\r\n")
    s = Site({"/api/health": bad_status, "/j": J('{"status":"\\u001b[2Jdown"}')})
    try:
        for path in ("/api/health", "/j"):
            r = sites.probe(s.base + path, timeout=3)
            assert r["healthy"] == 0 and r["error"], r
            assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in r["error"]), repr(r["error"])
    finally:
        s.stop()


def test_a_trickling_site_cannot_hold_up_the_round():
    def trickle(h):
        h.wfile.write(b"HTTP/1.1 200 OK\r\n")
        h.wfile.flush()
        try:
            for _ in range(100):                 # a header byte every 0.2 s: each read succeeds
                time.sleep(0.2)
                h.wfile.write(b"X")
                h.wfile.flush()
        except OSError:
            pass
    s = Site({"/slow": trickle, "/api/health": J('{"status":"ok"}')})
    old = config.SITES_CONTROL_URL
    try:
        config.SITES_CONTROL_URL = ""
        t0 = time.monotonic()
        with Capture():
            results, _ = sites.probe_all([(s.base + "/slow", "health"),
                                          (s.base + "/api/health", "health")],
                                         timeout=2, deadline=1.5)
        took = time.monotonic() - t0
        assert took < 2.5, took                             # per-socket timeouts alone never end
        slow, ok = results
        assert (slow["reachable"], slow["healthy"]) == (0, 0), slow
        assert slow["error"] == "no answer within SITES_DEADLINE (1.5 s)", slow
        assert slow["site"] == sites.site_name(s.base + "/slow") and slow["check"] == "health", slow
        assert ok["healthy"] == 1, ok                       # the healthy site isn't stuck behind it
    finally:
        config.SITES_CONTROL_URL = old
        s.stop()


def test_a_stuck_probe_does_not_keep_the_process_alive():
    # The service is a oneshot: after the deadline, the abandoned probe must not hold up exit.
    def trickle(h):
        h.wfile.write(b"HTTP/1.1 200 OK\r\n")
        try:
            for _ in range(150):
                time.sleep(0.2)
                h.wfile.write(b"X")
                h.wfile.flush()
        except OSError:
            pass
    s = Site({"/slow": trickle})
    code = ("import sys; sys.path.insert(0, %r); from telemetry import config, sites; "
            "sites._ALLOW_LOCAL_FOR_TESTS = True; config.SITES_CONTROL_URL = ''; "
            "r, _ = sites.probe_all([(%r, 'health')], timeout=5, deadline=1); "
            "print(r[0]['error'])") % (ROOT, s.base + "/slow")
    try:
        t0 = time.monotonic()
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        took = time.monotonic() - t0
        assert "no answer within SITES_DEADLINE (1 s)" in out.stdout, (out.stdout, out.stderr)
        assert took < 6, took                     # not the trickle's 30 s
    finally:
        s.stop()


def test_probe_all_order_names_control_and_reports():
    s = Site({"/api/health": J('{"status":"ok"}'), "/down": J('{"status":"down"}'),
              "/204": (204, b"", "text/plain")})
    old = config.SITES_CONTROL_URL
    try:
        config.SITES_CONTROL_URL = s.base + "/204"
        with Capture() as c:
            results, control = sites.probe_all(
                [(s.base + "/api/health", "health"), (s.base + "/down", "health")], timeout=3)
        host = s.base.split("//", 1)[1]
        assert [r["site"] for r in results] == [host, f"{host}/down"], results
        assert [r["check"] for r in results] == ["health", "health"], results
        assert [r["healthy"] for r in results] == [1, 0], results
        assert control["reachable"] == 1 and control["code"] == 204, control
        err = c.err.getvalue()
        assert f"site {host}/down: NOT healthy (status is 'down', not 'ok')" in err, err
        assert f"site {host}:" not in err, err

        config.SITES_CONTROL_URL = f"http://127.0.0.1:{free_port()}/"
        with Capture() as c:
            results, control = sites.probe_all([(s.base + "/api/health", "health")], timeout=3)
        assert control["reachable"] == 0, control
        assert "may have lost the internet" in c.err.getvalue(), c.err.getvalue()

        config.SITES_CONTROL_URL = ""
        results, control = sites.probe_all([(s.base + "/api/health", "health")], timeout=3)
        assert control is None and len(results) == 1, (results, control)
        assert sites.probe_all([], timeout=3) == ([], None)
    finally:
        config.SITES_CONTROL_URL = old
        s.stop()


def test_probe_all_runs_in_parallel():
    def slow(h):
        time.sleep(0.6)
        body = b'{"status":"ok"}'
        h.send_response(200)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    s = Site({f"/s{i}": slow for i in range(8)})
    old = config.SITES_CONTROL_URL
    try:
        config.SITES_CONTROL_URL = ""
        t0 = time.monotonic()
        results, _ = sites.probe_all([(f"{s.base}/s{i}", "health") for i in range(8)], timeout=3)
        took = time.monotonic() - t0
        assert all(r["healthy"] for r in results), results
        assert took < 2.5, took                           # serial would be 4.8 s
    finally:
        config.SITES_CONTROL_URL = old
        s.stop()


# ---------------------------------------------------------------------------
# lines
# ---------------------------------------------------------------------------
def test_site_lines_exact():
    results = [
        {"site": "write.authortools.net", "check": "health", "reachable": 1, "code": 200,
         "latency_ms": 41.5, "healthy": 1, "db_ok": 1, "cert_days": 63.25, "error": ""},
        {"site": "lens.authortools.net", "check": "up", "reachable": 1, "code": 200,
         "latency_ms": 30.0, "healthy": 1, "db_ok": None, "cert_days": 20.0, "error": ""},
        {"site": "gone.example", "check": "health", "reachable": 0, "code": 0, "latency_ms": None,
         "healthy": 0, "db_ok": None, "cert_days": None, "error": "gaierror: x"},
    ]
    control = {"reachable": 1, "code": 204}
    assert pane.site_lines(results, control) == [
        "site,site=write.authortools.net,check=health up=1,reachable=1,code=200,latency_ms=41.5,"
        "db_ok=1,cert_days=63.25",
        "site,site=lens.authortools.net,check=up up=1,reachable=1,code=200,latency_ms=30,"
        "cert_days=20",
        "site,site=gone.example,check=health up=0,reachable=0,code=0",
        "site_probe sites=3,healthy=2,control_ok=1",
    ], pane.site_lines(results, control)
    assert pane.site_lines(results[:1], None)[-1] == "site_probe sites=1,healthy=1"
    assert pane.site_lines([], None) == []
    assert pane.site_lines([], {"reachable": 0}) == ["site_probe sites=0,healthy=0,control_ok=0"]
    # Tag escaping goes through the same code as everything else.
    odd = [{"site": "a b,c=d", "check": "up", "reachable": 1, "code": 200, "healthy": 1}]
    assert pane.site_lines(odd, None)[0].startswith("site,site=a\\ b\\,c\\=d,check=up "), odd


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------
class FakePane:
    def __init__(self):
        self.bodies = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.bodies.append(self.rfile.read(n).decode())
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def _cycle_config(pane_url, sites_path, host=None):
    tok = os.path.join(TMP, f"tok-{time.monotonic_ns()}")
    with open(tok, "w") as f:
        f.write("TOKEN_abcdefghijklmnopqrstuvwxyz\n")
    os.chmod(tok, 0o400)
    config.PANE_URL, config.PANE_TOKEN_FILE, config.PANE_TIMEOUT = pane_url, tok, 3.0
    config.MQTT_HOST = ""
    config.HOST = "pane"
    config.TELEMETRY_NVIDIA, config.TELEMETRY_LHM = False, False
    config.TELEMETRY_HOST, config.IS_LINUX = host is not None, True
    main.linux_host.read_host = lambda: host
    config.TELEMETRY_SITES = True
    config.SITES_FILE = sites_path
    config.SITES_TIMEOUT = 3.0


def test_run_once_pushes_site_lines_with_host_lines():
    s = Site({"/api/health": J('{"status":"ok","db":"ok"}'), "/204": (204, b"", "text/plain")})
    p = FakePane()
    old_ctl = config.SITES_CONTROL_URL
    try:
        _cycle_config(p.url, sites_file(f"{s.base}/api/health\n"),
                      host={"ram": {"used_gb": 1.0, "total_gb": 2.0, "pct": 50.0}})
        config.SITES_CONTROL_URL = s.base + "/204"
        with Capture():
            assert main.run_once() is True
        body = p.bodies[-1]
        assert "mem used_gb=1,total_gb=2,used_percent=50\n" in body, body
        assert f"site,site={s.base.split('//', 1)[1]},check=health up=1,reachable=1,code=200," in body, body
        assert "site_probe sites=1,healthy=1,control_ok=1\n" in body, body
    finally:
        config.SITES_CONTROL_URL = old_ctl
        config.TELEMETRY_SITES = False
        s.stop()
        p.stop()


def test_run_once_sites_only_still_publishes():
    s = Site({"/api/health": J('{"status":"ok"}')})
    p = FakePane()
    old_ctl = config.SITES_CONTROL_URL
    try:
        _cycle_config(p.url, sites_file(f"{s.base}/api/health\n"), host=None)
        config.SITES_CONTROL_URL = ""
        with Capture():
            assert main.run_once() is True
        assert f"site,site={s.base.split('//', 1)[1]},check=health " in p.bodies[-1], p.bodies
    finally:
        config.SITES_CONTROL_URL = old_ctl
        config.TELEMETRY_SITES = False
        s.stop()
        p.stop()


def test_run_once_unhealthy_site_is_not_a_failed_cycle():
    p = FakePane()
    old_ctl = config.SITES_CONTROL_URL
    try:
        _cycle_config(p.url, sites_file(f"http://127.0.0.1:{free_port()}/api/health\n"),
                      host={"ram": {"used_gb": 1.0, "total_gb": 2.0, "pct": 50.0}})
        config.SITES_CONTROL_URL = ""
        with Capture():
            assert main.run_once() is True           # the push worked; the site's state is data
        assert "up=0,reachable=0,code=0" in p.bodies[-1], p.bodies
    finally:
        config.SITES_CONTROL_URL = old_ctl
        config.TELEMETRY_SITES = False
        p.stop()


def test_run_once_probe_crash_does_not_stop_host_data():
    p = FakePane()
    real = main.sites.probe_all
    try:
        _cycle_config(p.url, sites_file(""), host={"ram": {"used_gb": 1.0, "total_gb": 2.0,
                                                           "pct": 50.0}})

        def boom():
            raise RuntimeError("kaboom")
        main.sites.probe_all = boom
        with Capture() as c:
            assert main.run_once() is True
        assert "!!! Site probes crashed" in c.err.getvalue(), c.err.getvalue()
        assert "mem used_gb=1" in p.bodies[-1] and "site" not in p.bodies[-1], p.bodies
    finally:
        main.sites.probe_all = real
        config.TELEMETRY_SITES = False
        p.stop()


def test_run_once_sites_off_probes_nothing():
    p = FakePane()
    called = []
    real = main.sites.probe_all
    try:
        _cycle_config(p.url, sites_file(""), host={"ram": {"used_gb": 1.0, "total_gb": 2.0,
                                                           "pct": 50.0}})
        config.TELEMETRY_SITES = False
        main.sites.probe_all = lambda *a, **k: called.append(1) or ([], None)
        with Capture():
            assert main.run_once() is True
        assert called == [] and "site" not in p.bodies[-1], (called, p.bodies)
    finally:
        main.sites.probe_all = real
        p.stop()


def test_run_once_refuses_site_probes_in_loop_mode():
    p = FakePane()
    called = []
    real, old_loop = main.sites.probe_all, config.LOOP_INTERVAL
    try:
        _cycle_config(p.url, sites_file(""), host={"ram": {"used_gb": 1.0, "total_gb": 2.0,
                                                           "pct": 50.0}})
        config.LOOP_INTERVAL = "30"
        main.sites.probe_all = lambda *a, **k: called.append(1) or ([], None)
        with Capture() as c:
            assert main.run_once() is True               # host data still goes out
        assert called == [], called
        assert "!!! Site probes are off in loop mode" in c.err.getvalue(), c.err.getvalue()
    finally:
        main.sites.probe_all, config.LOOP_INTERVAL = real, old_loop
        config.TELEMETRY_SITES = False
        p.stop()


def test_unit_has_a_start_timeout():
    with open(os.path.join(ROOT, "service", "telemetry.service")) as f:
        unit = f.read()
    assert "\nType=oneshot\n" in unit and "\nTimeoutStartSec=60\n" in unit, unit


def test_config_defaults_and_no_env_override_of_the_list():
    code = ("import telemetry.config as c;"
            "print(c.TELEMETRY_SITES, c.SITES_FILE, c.SITES_CONTROL_URL, c.SITES_TIMEOUT, "
            "c.SITES_DEADLINE)")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SITES_", "TELEMETRY_"))}
    out = subprocess.check_output([sys.executable, "-c", code], env=env, cwd=ROOT, text=True)
    flag, path, ctl, timeout, deadline = out.split()
    assert (flag, timeout, deadline) == ("False", "8.0", "15.0"), out
    assert os.path.samefile(path, os.path.join(ROOT, "sites.txt")), out
    assert ctl == "https://www.google.com/generate_204", out
    # The service user owns .env: it can tune the timings, never the list or the control URL.
    env.update(TELEMETRY_SITES="1", SITES_FILE="/opt/telemetry/secrets/pane_token",
               SITES_CONTROL_URL="https://10.0.0.1/", SITES_TIMEOUT="2", SITES_DEADLINE="9")
    out = subprocess.check_output([sys.executable, "-c", code], env=env, cwd=ROOT, text=True)
    flag, path, ctl, timeout, deadline = out.split()
    assert (flag, timeout, deadline) == ("True", "2.0", "9.0"), out
    assert os.path.samefile(path, os.path.join(ROOT, "sites.txt")), out
    assert ctl == "https://www.google.com/generate_204", out


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok ", name)
            except Exception:
                import traceback
                failed += 1
                print("FAIL", name)
                traceback.print_exc()
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
