"""Heartbeat files: reading status files from occasional jobs, the lines, and run_once wiring.

Standalone script; the exit status is the verdict (CI runs every tests/test_*.py).
Run from the repo root:  python tests/test_heartbeats.py
"""
import contextlib, importlib, io, json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from telemetry import config, heartbeats, pane  # noqa: E402

main = importlib.import_module("telemetry.__main__")   # importing it starts nothing

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
NOW = 1_800_000_000.0


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


def folder(files):
    """A temp folder holding {name: object-or-raw-string}."""
    d = tempfile.mkdtemp(prefix="hb-test-")
    for name, content in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
    return d


def doc(*checks):
    return {"version": 1, "checks": list(checks)}


def test_reads_checks_and_computes_age_and_ok():
    d = folder({
        "b.json": doc({"check": "backup:keystone", "last_ok": NOW - 3600, "period_s": 93600}),
        "a.json": doc({"check": "backup:dbserver daily/app", "last_ok": NOW - 100000, "period_s": 93600},
                      {"check": "backup-run:pull-backups", "last_ok": NOW - 60, "period_s": 93600,
                       "ok": False}),
        "ignored.txt": "not json",
    })
    with Capture() as c:
        got = heartbeats.read_all(d, now=NOW)
    assert got == [
        {"check": "backup:dbserver daily/app", "age_s": 100000, "period_s": 93600, "ok": 0},  # late
        {"check": "backup-run:pull-backups", "age_s": 60, "period_s": 93600, "ok": 0},         # said failed
        {"check": "backup:keystone", "age_s": 3600, "period_s": 93600, "ok": 1},
    ], got
    assert c.err.getvalue() == "", c.err.getvalue()


def test_future_last_ok_is_age_zero_and_period_boundary_is_ok():
    d = folder({"x.json": doc({"check": "a", "last_ok": NOW + 500, "period_s": 10},
                              {"check": "b", "last_ok": NOW - 10, "period_s": 10})})
    got = heartbeats.read_all(d, now=NOW)
    assert [(g["age_s"], g["ok"]) for g in got] == [(0, 1), (10, 1)], got


def test_bad_files_are_skipped_without_echoing_their_text():
    d = folder({
        "1-not-json.json": "pw_SECRET_in_a_broken_file {",
        "2-wrong-shape.json": {"checks": [{"check": "x", "last_ok": 1, "period_s": 1}]},
        "3-list.json": [1, 2],
        "4-good.json": doc({"check": "good", "last_ok": NOW, "period_s": 60}),
        "5-big.json": "x" * (heartbeats.MAX_BYTES + 1),
    })
    os.symlink(os.path.join(d, "4-good.json"), os.path.join(d, "6-link.json"))
    with Capture() as c:
        got = heartbeats.read_all(d, now=NOW)
    assert [g["check"] for g in got] == ["good"], got
    err = c.err.getvalue()
    assert err.count("!!! Heartbeat file skipped") == 5, err
    for name in ("1-not-json", "2-wrong-shape", "3-list", "5-big", "6-link"):
        assert name in err, (name, err)
    assert "pw_SECRET" not in err, err


def test_bad_entries_are_skipped_one_by_one():
    d = folder({"x.json": doc(
        "nope",
        {"check": "", "last_ok": NOW, "period_s": 60},
        {"check": "a\nb", "last_ok": NOW, "period_s": 60},
        {"check": "x" * 121, "last_ok": NOW, "period_s": 60},
        {"check": "nums", "last_ok": "yesterday", "period_s": 60},
        {"check": "nums2", "last_ok": NOW, "period_s": 0},
        {"check": "nums3", "last_ok": True, "period_s": 60},
        {"check": "okflag", "last_ok": NOW, "period_s": 60, "ok": "yes"},
        {"check": "fine", "last_ok": NOW, "period_s": 60},
        {"check": "fine", "last_ok": NOW - 999, "period_s": 60},
    )})
    with Capture() as c:
        got = heartbeats.read_all(d, now=NOW)
    assert got == [{"check": "fine", "age_s": 0, "period_s": 60, "ok": 1}], got
    err = c.err.getvalue()
    assert err.count("!!! Heartbeat entry skipped") == 9, err
    assert "repeats an earlier one" in err and "entry 10" in err, err


def test_limits_on_files_and_checks():
    many = {f"f{i:03}.json": doc({"check": f"c{i}", "last_ok": NOW, "period_s": 60})
            for i in range(heartbeats.MAX_FILES + 5)}
    with Capture() as c:
        got = heartbeats.read_all(folder(many), now=NOW)
    assert len(got) == heartbeats.MAX_FILES, len(got)
    assert "!!! Too many heartbeat files" in c.err.getvalue(), c.err.getvalue()
    big = {"x.json": doc(*[{"check": f"c{i}", "last_ok": NOW, "period_s": 60}
                           for i in range(heartbeats.MAX_CHECKS + 3)])}
    with Capture() as c:
        got = heartbeats.read_all(folder(big), now=NOW)
    assert len(got) == heartbeats.MAX_CHECKS, len(got)
    assert "!!! Too many heartbeat checks" in c.err.getvalue(), c.err.getvalue()


def test_missing_folder_reports_and_returns_empty():
    with Capture() as c:
        assert heartbeats.read_all("/nonexistent/hb", now=NOW) == []
    assert "!!! Heartbeat folder not found" in c.err.getvalue(), c.err.getvalue()


def test_bom_written_by_windows_powershell_is_accepted():
    d = tempfile.mkdtemp(prefix="hb-bom-")
    with open(os.path.join(d, "x.json"), "w", encoding="utf-8-sig") as f:
        json.dump(doc({"check": "bom", "last_ok": NOW, "period_s": 60}), f)
    assert [g["check"] for g in heartbeats.read_all(d, now=NOW)] == ["bom"]


def test_heartbeat_lines_exact():
    got = pane.heartbeat_lines([
        {"check": "backup:dbserver daily/app", "age_s": 3600, "period_s": 93600, "ok": 1},
        {"check": "backup-run:pull-backups", "age_s": 100000, "period_s": 93600, "ok": 0},
    ])
    assert got == [
        "heartbeat,check=backup:dbserver\\ daily/app ok=1,age_s=3600,period_s=93600",
        "heartbeat,check=backup-run:pull-backups ok=0,age_s=100000,period_s=93600",
    ], got
    assert pane.heartbeat_lines([]) == []


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


def _cycle(pane_url, hb_dir, host):
    tok = os.path.join(tempfile.mkdtemp(), "tok")
    with open(tok, "w") as f:
        f.write("TOKEN_abcdefghijklmnopqrstuvwxyz\n")
    os.chmod(tok, 0o400)
    config.PANE_URL, config.PANE_TOKEN_FILE, config.PANE_TIMEOUT = pane_url, tok, 3.0
    config.MQTT_HOST, config.HOST = "", "chonky"
    config.TELEMETRY_NVIDIA, config.TELEMETRY_LHM = False, False
    config.TELEMETRY_HOST, config.IS_LINUX = host is not None, True
    main.linux_host.read_host = lambda: host
    config.HEARTBEATS_DIR = hb_dir


def test_run_once_pushes_heartbeats_with_host_lines():
    p = FakePane()
    try:
        d = folder({"x.json": doc({"check": "backup:keystone", "last_ok": time.time() - 30,
                                   "period_s": 93600})})
        _cycle(p.url, d, {"ram": {"used_gb": 1.0, "total_gb": 2.0, "pct": 50.0}})
        with Capture():
            assert main.run_once() is True
        body = p.bodies[-1]
        assert "mem used_gb=1,total_gb=2,used_percent=50\n" in body, body
        assert "heartbeat,check=backup:keystone ok=1,age_s=" in body and ",period_s=93600\n" in body, body
    finally:
        config.HEARTBEATS_DIR = ""
        p.stop()


def test_run_once_heartbeats_only_still_publishes():
    p = FakePane()
    try:
        d = folder({"x.json": doc({"check": "only", "last_ok": time.time(), "period_s": 60})})
        _cycle(p.url, d, None)
        with Capture():
            assert main.run_once() is True
        assert "heartbeat,check=only ok=1" in p.bodies[-1], p.bodies
    finally:
        config.HEARTBEATS_DIR = ""
        p.stop()


def test_run_once_crash_in_reader_does_not_stop_host_data():
    p = FakePane()
    real = main.heartbeats.read_all
    try:
        _cycle(p.url, "/whatever", {"ram": {"used_gb": 1.0, "total_gb": 2.0, "pct": 50.0}})

        def boom():
            raise RuntimeError("kaboom")
        main.heartbeats.read_all = boom
        with Capture() as c:
            assert main.run_once() is True
        assert "!!! Heartbeat files crashed" in c.err.getvalue(), c.err.getvalue()
        assert "mem used_gb=1" in p.bodies[-1] and "heartbeat" not in p.bodies[-1], p.bodies
    finally:
        main.heartbeats.read_all = real
        config.HEARTBEATS_DIR = ""
        p.stop()


def test_off_by_default_reads_nothing():
    code = "import telemetry.config as c; print(repr(c.HEARTBEATS_DIR))"
    env = {k: v for k, v in os.environ.items() if k != "HEARTBEATS_DIR"}
    out = subprocess.check_output([sys.executable, "-c", code], env=env, cwd=ROOT, text=True).strip()
    assert out == "''", out
    p = FakePane()
    called = []
    real = main.heartbeats.read_all
    try:
        _cycle(p.url, "", {"ram": {"used_gb": 1.0, "total_gb": 2.0, "pct": 50.0}})
        main.heartbeats.read_all = lambda *a, **k: called.append(1) or []
        with Capture():
            assert main.run_once() is True
        assert called == [] and "heartbeat" not in p.bodies[-1], (called, p.bodies)
    finally:
        main.heartbeats.read_all = real
        p.stop()


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
