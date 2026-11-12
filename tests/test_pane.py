"""Pane output: line protocol, push over HTTP, token hygiene, independent outputs.

Standalone script; the exit status is the verdict (CI runs every tests/test_*.py).
Real HTTP servers on 127.0.0.1 in threads; the MQTT client is faked (no broker).
Run from the repo root:  python tests/test_pane.py
"""
import contextlib, importlib, io, os, re, stat, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from telemetry import config, pane  # noqa: E402

main = importlib.import_module("telemetry.__main__")   # importing it starts nothing

TOKEN = "TOKEN_s3cr3t-Abc123_XYZ.789"
TMP = tempfile.mkdtemp(prefix="pane-test-")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class Capture:
    """Capture stdout+stderr and assert the token never shows up in either."""

    def __enter__(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        self._cms = [contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err)]
        for cm in self._cms:
            cm.__enter__()
        return self

    def __exit__(self, *exc):
        for cm in reversed(self._cms):
            cm.__exit__(*exc)
        assert TOKEN not in self.out.getvalue(), "token leaked to stdout"
        assert TOKEN not in self.err.getvalue(), "token leaked to stderr"
        assert TOKEN[8:20] not in self.out.getvalue() + self.err.getvalue()
        return False


class Pane:
    """A fake Pane. behavior(handler) -> None; it must send the response itself."""

    def __init__(self, behavior=None):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({
                    "path": self.path, "auth": self.headers.get("Authorization"),
                    "ctype": self.headers.get("Content-Type"),
                    "body": self.rfile.read(n).decode()})
                (behavior or Pane.ok)(self)

            do_GET = do_POST

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    @staticmethod
    def ok(h):
        h.send_response(204)
        h.end_headers()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def respond(status, body=b"", headers=()):
    def b(h):
        h.send_response(status)
        for k, v in headers:
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    return b


def token_file(content=TOKEN + "\n", mode=0o400):
    path = os.path.join(TMP, f"tok-{time.monotonic_ns()}")
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, mode)
    return path


def setup(url, tokfile=None, timeout=5.0):
    config.PANE_URL = url
    config.PANE_TOKEN_FILE = tokfile or token_file()
    config.PANE_TIMEOUT = timeout
    config.HOST = "slimridge"
    pane._mode_warned = False


def free_url():
    s = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = s.server_address[1]
    s.server_close()
    return f"http://127.0.0.1:{port}"


def assert_house(err, title_part, status_part=None):
    assert err.startswith("!!! "), err
    lines = err.rstrip("\n").split("\n")
    assert title_part in lines[0], err
    assert [l[:14] for l in lines[1:]] == [
        "    Did:      ", "    Expected: ", "    Got:      ", "    Next:     "], err
    if status_part:
        assert status_part in lines[3], err


# Realistic collector shapes ---------------------------------------------------
LINUX_GPUS = [{"index": "0", "name": "Tesla V100-PCIE-16GB", "util": 12, "mem_used": 3021,
               "mem_total": 16384, "mem_pct": 18.4, "temp": 47, "power": 58.31}]
LINUX_MODELS = {"0": "bge-m3-FP16 (embed), bge-reranker-v2-m3-FP16 (rerank)"}
LINUX_HOST = {
    "cpus": [{"name": "k10temp", "temp_c": 52.4, "load_pct": 6.3, "power_w": None}],
    "ram": {"used_gb": 21.7, "total_gb": 62.7, "pct": 34.6},
    "disks": [{"name": "root", "temp_c": 38.0, "used_pct": 41.2},
              {"name": "nvme Composite", "temp_c": None, "used_pct": None}],
}
# Windows / LibreHardwareMonitor: dual identical Xeons, names with odd characters,
# an Arc card whose indices were offset by the orchestrator, missing readings.
LHM_GPUS = [
    {"index": "0", "name": "NVIDIA Tesla V100-SXM2-16GB", "util": 0, "mem_used": 0,
     "mem_total": 16384, "mem_pct": 0.0, "temp": 33, "power": 40.1},
    {"index": "3", "name": "Intel Arc B580 Graphics", "util": None, "mem_used": 812.5,
     "mem_total": 12288.0, "mem_pct": 6.6, "temp": None, "power": 11.0},
]
LHM_HOST = {
    "cpus": [{"name": "Intel Xeon E5-2690 v3", "temp_c": 61.0, "load_pct": 22.5, "power_w": 88.0},
             {"name": "Intel Xeon E5-2690 v3", "temp_c": 58.0, "load_pct": None, "power_w": 79.5}],
    "ram": {"used_gb": 101.2, "total_gb": 255.9, "pct": 39.5},
    "disks": [{"name": "Samsung SSD 990 PRO 2TB", "hw_id": "/nvme/0", "temp_c": 40.0, "used_pct": 55.0},
              {"name": "Samsung SSD 990 PRO 2TB", "hw_id": "/nvme/1", "temp_c": 41.0, "used_pct": None}],
}


# ---------------------------------------------------------------------------
# to_lines
# ---------------------------------------------------------------------------
def test_to_lines_linux_exact():
    assert pane.to_lines(LINUX_GPUS, LINUX_MODELS, LINUX_HOST) == [
        r"gpu,gpu=0,name=Tesla\ V100-PCIE-16GB temp_c=47,util_percent=12,mem_used_mb=3021,"
        r"mem_total_mb=16384,mem_pct=18.4,power_w=58.31",
        r"gpu_model,gpu=0,model=bge-m3-FP16\ (embed)\,\ bge-reranker-v2-m3-FP16\ (rerank) loaded=1",
        "cpu,cpu=k10temp temp_c=52.4,load_percent=6.3",
        "mem used_gb=21.7,total_gb=62.7,used_percent=34.6",
        "disk,disk=root temp_c=38,used_percent=41.2",
        "telemetry up=1,gpus=1",
    ]


def test_to_lines_lhm_shape():
    lines = pane.to_lines(LHM_GPUS, {"0": "none loaded", "3": "qwen3-8b"}, LHM_HOST)
    assert lines == [
        r"gpu,gpu=0,name=NVIDIA\ Tesla\ V100-SXM2-16GB temp_c=33,util_percent=0,mem_used_mb=0,"
        r"mem_total_mb=16384,mem_pct=0,power_w=40.1",
        r"gpu,gpu=3,name=Intel\ Arc\ B580\ Graphics mem_used_mb=812.5,mem_total_mb=12288,"
        "mem_pct=6.6,power_w=11",
        "gpu_model,gpu=3,model=qwen3-8b loaded=1",           # "none loaded" is not a model
        r"cpu,cpu=Intel\ Xeon\ E5-2690\ v3\ #0 temp_c=61,load_percent=22.5,power_w=88",
        r"cpu,cpu=Intel\ Xeon\ E5-2690\ v3\ #1 temp_c=58,power_w=79.5",
        "mem used_gb=101.2,total_gb=255.9,used_percent=39.5",
        r"disk,disk=Samsung\ SSD\ 990\ PRO\ 2TB\ #0 temp_c=40,used_percent=55",
        r"disk,disk=Samsung\ SSD\ 990\ PRO\ 2TB\ #1 temp_c=41",
        "telemetry up=1,gpus=2",
    ], lines


def test_escaping_of_awkward_names():
    gpus = [{"index": "0", "name": "NVIDIA GeForce RTX 3090", "temp": 50}]
    host = {"cpus": [{"name": "Core (Tctl/Tdie)", "temp_c": 60},
                     {"name": "weird, name=x\\y\nz", "temp_c": 61}]}
    lines = pane.to_lines(gpus, {}, host)
    assert lines[0] == r"gpu,gpu=0,name=NVIDIA\ GeForce\ RTX\ 3090 temp_c=50", lines[0]
    assert lines[1] == r"cpu,cpu=Core\ (Tctl/Tdie) temp_c=60", lines[1]
    assert lines[2] == r"cpu,cpu=weird\,\ name\=x\\y\ z temp_c=61", lines[2]
    assert all("\n" not in l for l in lines)


def test_bad_values_skipped_and_never_nan_inf():
    nan, inf = float("nan"), float("inf")
    gpus = [{"index": "0", "name": "G", "util": nan, "mem_used": inf, "mem_total": -inf,
             "mem_pct": "[N/A]", "temp": True, "power": None}]      # nothing usable
    host = {"cpus": [{"name": "c", "temp_c": "n/a", "load_pct": None, "power_w": nan}],
            "ram": {"used_gb": None, "total_gb": None, "pct": None},
            "disks": [{"name": "d", "temp_c": None, "used_pct": inf}]}
    lines = pane.to_lines(gpus, {}, host)
    assert lines == ["telemetry up=1,gpus=1"], lines
    mixed = pane.to_lines([{"index": "0", "name": "G", "util": nan, "temp": 40,
                            "mem_pct": "[Not Supported]"}], {}, None)
    assert mixed[0] == "gpu,gpu=0,name=G temp_c=40", mixed
    for l in mixed + lines:
        assert not re.search(r"(?i)\b(nan|inf)\b", l), l


def test_empty_tag_values_become_unknown():
    lines = pane.to_lines([{"index": "", "name": "   ", "temp": 1}],
                          {"": "m"}, {"cpus": [{"name": "", "temp_c": 2}]})
    assert lines[0] == "gpu,gpu=unknown,name=unknown temp_c=1", lines[0]
    assert lines[1] == "gpu_model,gpu=unknown,model=m loaded=1", lines[1]
    assert lines[2] == "cpu,cpu=cpu temp_c=2", lines[2]        # missing name -> "cpu"
    assert pane.to_lines([{"index": None, "name": None, "temp": 1}], {}, None)[0] == \
        "gpu,gpu=unknown,name=unknown temp_c=1"


def test_heartbeat_with_no_gpus_and_no_timestamp_or_host_tag():
    assert pane.to_lines([], {}, None) == ["telemetry up=1,gpus=0"]
    assert pane.to_lines([], {}, {"cpus": [], "ram": {}, "disks": []}) == ["telemetry up=1,gpus=0"]
    for l in pane.to_lines(LHM_GPUS, {"0": "m"}, LHM_HOST) + pane.to_lines(LINUX_GPUS, LINUX_MODELS, LINUX_HOST):
        assert ",host=" not in l.split(" ")[0], l
        assert len(re.split(r"(?<!\\) ", l)) == 2, l             # head + fields; no timestamp


def test_dashboard_regex_collision():
    """Only gpu_mem_used_mb may match .*gpu.*mem.*used.* (the dashboard takes the max)."""
    names = set()
    for l in pane.to_lines(LHM_GPUS, {"0": "m"}, LHM_HOST) + pane.to_lines(LINUX_GPUS, LINUX_MODELS, LINUX_HOST):
        head, fields = re.split(r"(?<!\\) ", l)
        m = head.split(",")[0]
        names |= {f"{m}_{f.split('=')[0]}" for f in fields.split(",")}
    hits = {n for n in names if re.fullmatch(r".*gpu.*mem.*used.*", n)}
    assert hits == {"gpu_mem_used_mb"}, hits
    assert {"gpu_temp_c", "gpu_util_percent", "gpu_mem_total_mb", "gpu_mem_pct", "gpu_power_w",
            "gpu_model_loaded", "cpu_temp_c", "cpu_load_percent", "cpu_power_w",
            "mem_used_gb", "mem_total_gb", "mem_used_percent", "disk_temp_c",
            "disk_used_percent", "telemetry_up", "telemetry_gpus"} <= names, names
    assert re.fullmatch(r"(?i).*(cpu|coretemp|package).*temp.*", "cpu_temp_c")


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------
def test_push_ok_sends_path_header_body():
    p = Pane()
    try:
        setup(p.url)
        lines = pane.to_lines(LINUX_GPUS, LINUX_MODELS, LINUX_HOST)
        with Capture() as c:
            assert pane.push(lines) is True
        assert len(p.requests) == 1
        r = p.requests[0]
        assert r["path"] == "/influx/write", r
        assert r["auth"] == f"Bearer {TOKEN}", "wrong Authorization header"
        assert r["ctype"].startswith("text/plain"), r
        assert r["body"].rstrip("\n").split("\n") == lines
        assert c.out.getvalue() == "Pane: pushed 6 lines for slimridge (204)\n", c.out.getvalue()
        assert c.err.getvalue() == ""
    finally:
        p.stop()


def test_push_200_is_ok_too():
    p = Pane(respond(200))
    try:
        setup(p.url)
        with Capture():
            assert pane.push(["telemetry up=1,gpus=0"]) is True
    finally:
        p.stop()


def test_token_whitespace_stripped_and_reread_each_cycle():
    p = Pane()
    try:
        path = token_file("  " + TOKEN + " \r\n\n")
        setup(p.url, path)
        with Capture():
            assert pane.push(["telemetry up=1,gpus=0"])
            # Rotation, as it's done on a box: a new 0400 file moved over the old one
            # (the old one is 0400, so writing it in place fails for a non-root user).
            os.replace(token_file("rotated-token-XYZ\n"), path)   # no restart needed
            assert pane.push(["telemetry up=1,gpus=0"])
        assert [r["auth"] for r in p.requests] == [f"Bearer {TOKEN}", "Bearer rotated-token-XYZ"]
    finally:
        p.stop()


def test_401_false_with_house_format_and_echoed_token_scrubbed():
    # A hostile/buggy server that echoes the Authorization header back in its body.
    def echo(h):
        respond(401, ("unauthorized: " + (h.headers.get("Authorization") or "")).encode())(h)
    p = Pane(echo)
    try:
        setup(p.url)
        with Capture() as c:
            assert pane.push(["telemetry up=1,gpus=0"]) is False
        err = c.err.getvalue()
        assert_house(err, "Pane rejected the push", "401 unauthorized: Bearer ***")
        assert "add-box.sh slimridge" in err and "wrong or not added" in err, err
        assert f"POST {p.url}/influx/write (1 lines)" in err, err
        assert c.out.getvalue() == ""
    finally:
        p.stop()


def test_500_false_with_house_format_and_body_truncated():
    p = Pane(respond(500, b"x" * 1000))
    try:
        setup(p.url)
        with Capture() as c:
            assert pane.push(["telemetry up=1,gpus=0"]) is False
        err = c.err.getvalue()
        assert_house(err, "Pane rejected the push", "500 " + "x" * 200)
        got = err.split("    Got:      ")[1].split("\n")[0]
        assert got == "500 " + "x" * 200, len(got)
    finally:
        p.stop()


def test_redirect_not_followed():
    target = Pane()
    p = Pane(respond(302, headers=[("Location", target.url + "/influx/write")]))
    try:
        setup(p.url)
        with Capture() as c:
            assert pane.push(["telemetry up=1,gpus=0"]) is False
        assert len(p.requests) == 1
        assert target.requests == [], "the redirect was followed (token would have been resent)"
        assert_house(c.err.getvalue(), "Pane rejected the push", "302")
        assert "not followed" in c.err.getvalue()
    finally:
        p.stop()
        target.stop()


def test_timeout_returns_false_within_timeout():
    p = Pane(lambda h: time.sleep(4))
    try:
        setup(p.url, timeout=0.6)
        t0 = time.monotonic()
        with Capture() as c:
            assert pane.push(["telemetry up=1,gpus=0"]) is False
        assert time.monotonic() - t0 < 2.5, time.monotonic() - t0
        assert_house(c.err.getvalue(), "Pane unreachable", "timed out")
        assert "tailnet" in c.err.getvalue()
    finally:
        p.stop()


def test_connection_refused_house_format():
    setup(free_url())
    with Capture() as c:
        assert pane.push(["telemetry up=1,gpus=0"]) is False
    assert_house(c.err.getvalue(), "Pane unreachable", "URLError")
    assert "Pane's grants" in c.err.getvalue()


def test_env_proxy_is_not_used():
    p = Pane()
    old = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
    try:
        os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"   # dead
        os.environ.pop("no_proxy", None), os.environ.pop("NO_PROXY", None)
        setup(p.url)
        with Capture():
            assert pane.push(["telemetry up=1,gpus=0"]) is True
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        p.stop()


def test_missing_token_file_no_request():
    p = Pane()
    try:
        setup(p.url, os.path.join(TMP, "does-not-exist"))
        with Capture() as c:
            assert pane.push(["telemetry up=1,gpus=0"]) is False
        assert p.requests == []
        assert_house(c.err.getvalue(), "Pane token file cannot be read", "FileNotFoundError")
        assert "does-not-exist" in c.err.getvalue() and "add-box.sh slimridge" in c.err.getvalue()
    finally:
        p.stop()


def test_bad_token_contents_no_request_and_no_leak():
    p = Pane()
    try:
        for content in ("", "  \n", "two words", TOKEN + "\n" + TOKEN + "\n", TOKEN[:10] + "\x00" + TOKEN[10:],
                        TOKEN + "\u00e9"):
            path = os.path.join(TMP, f"bad-{time.monotonic_ns()}")
            with open(path, "wb") as f:
                f.write(content.encode("utf-8"))
            os.chmod(path, 0o400)
            setup(p.url, path)
            with Capture() as c:
                assert pane.push(["telemetry up=1,gpus=0"]) is False, repr(content)
            assert_house(c.err.getvalue(), "does not hold a token")
        assert p.requests == []
    finally:
        p.stop()


def test_group_readable_token_file_warns_but_pushes():
    if not sys.platform.startswith("linux"):
        return
    p = Pane()
    try:
        setup(p.url, token_file(mode=0o640))
        with Capture() as c:
            assert pane.push(["telemetry up=1,gpus=0"]) is True
            assert pane.push(["telemetry up=1,gpus=0"]) is True     # warns once per process
        err = c.err.getvalue()
        assert_house(err, "readable by other users (continuing)", "mode 0640")
        assert err.count("!!! ") == 1, err
        assert len(p.requests) == 2
        setup(p.url, token_file(mode=0o400))
        with Capture() as c:
            assert pane.push(["telemetry up=1,gpus=0"]) is True
        assert c.err.getvalue() == ""
    finally:
        p.stop()


def test_non_http_url_rejected_without_request():
    setup("file:///etc/passwd")
    with Capture() as c:
        assert pane.push(["telemetry up=1,gpus=0"]) is False
    assert_house(c.err.getvalue(), "PANE_URL is not an http(s) URL")


def test_url_with_userinfo_rejected_without_printing_it():
    for url in ("http://kiosk:s3cr3t-token@100.92.61.42:8427", "https://s3cr3t-token@pane:8427",
                "file://u:s3cr3t-token@x/"):
        setup(url)
        with Capture() as c:
            assert pane.push(["telemetry up=1,gpus=0"]) is False
        assert_house(c.err.getvalue(), "PANE_URL has a user or token in it")
        assert "s3cr3t-token" not in c.err.getvalue() + c.out.getvalue()


# ---------------------------------------------------------------------------
# run_once: independent outputs
# ---------------------------------------------------------------------------
class FakeInfo:
    def wait_for_publish(self, timeout=None):
        pass

    def is_published(self):
        return True


class FakeMQTT:
    """Stands in for paho's Client. refuse=True: connect() raises like a dead broker."""
    refuse = False
    published = 0

    def __init__(self, *a, **k):
        pass

    def enable_logger(self):
        pass

    def username_pw_set(self, *a):
        pass

    def connect(self, host, port, keepalive=30):
        if FakeMQTT.refuse:
            raise ConnectionRefusedError(111, "Connection refused")
        self.on_connect(self, None, {}, 0)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, retain=False, qos=0):
        FakeMQTT.published += 1
        return FakeInfo()


class FakeCallbackAPI:
    VERSION2 = 2


def patch_cycle(mqtt_host="broker.test", refuse=False):
    FakeMQTT.refuse, FakeMQTT.published = refuse, 0
    main.mqtt_client.Client = FakeMQTT
    main.mqtt_client.CallbackAPIVersion = FakeCallbackAPI
    main.nvidia.read_gpus = lambda: [dict(g) for g in LINUX_GPUS]
    main.nvidia.read_models = lambda gpus: dict(LINUX_MODELS)
    main.linux_host.read_host = lambda: LINUX_HOST
    config.TELEMETRY_NVIDIA, config.TELEMETRY_LHM = True, False
    config.TELEMETRY_HOST, config.IS_LINUX = True, True
    config.MQTT_HOST, config.MQTT_PORT, config.MQTT_USER = mqtt_host, 1883, ""


def test_run_once_both_ok():
    p = Pane()
    try:
        setup(p.url)
        patch_cycle()
        with Capture() as c:
            assert main.run_once() is True
        assert len(p.requests) == 1 and FakeMQTT.published > 0
        out = c.out.getvalue().splitlines()
        assert out[0] == "Pane: pushed 6 lines for slimridge (204)", out     # Pane first
        assert out[1].startswith("Published 1 GPU(s) + host stats for slimridge:"), out
    finally:
        p.stop()


def test_run_once_mqtt_failing_still_pushes_to_pane():
    p = Pane()
    try:
        setup(p.url)
        patch_cycle(refuse=True)
        with Capture() as c:
            assert main.run_once() is False
        assert len(p.requests) == 1, "Pane must still be pushed when MQTT is down"
        assert "Pane: pushed 6 lines" in c.out.getvalue()
        assert "MQTT connect() raised" in c.err.getvalue()
    finally:
        p.stop()


def test_run_once_pane_failing_still_publishes_to_mqtt():
    for behavior, url in ((respond(500, b"boom"), None), (None, "dead")):
        p = Pane(behavior)
        try:
            setup(free_url() if url == "dead" else p.url)
            patch_cycle()
            with Capture() as c:
                assert main.run_once() is False
            assert FakeMQTT.published > 0, "MQTT must still be published when Pane is down"
            assert "!!! Pane" in c.err.getvalue()
            assert "Published 1 GPU(s) + host stats for slimridge:" in c.out.getvalue()
        finally:
            p.stop()


def test_run_once_pane_crash_does_not_stop_mqtt():
    p = Pane()
    real = main.pane.to_lines
    try:
        setup(p.url)
        patch_cycle()
        main.pane.to_lines = lambda *a: 1 / 0
        with Capture() as c:
            assert main.run_once() is False
        assert FakeMQTT.published > 0
        assert_house(c.err.getvalue(), "Pane output crashed", "ZeroDivisionError")
    finally:
        main.pane.to_lines = real
        p.stop()


def test_run_once_pane_only_when_mqtt_host_empty():
    p = Pane()
    try:
        setup(p.url)
        patch_cycle(mqtt_host="")
        with Capture() as c:
            assert main.run_once() is True
        assert len(p.requests) == 1 and FakeMQTT.published == 0
        assert "MQTT" not in c.out.getvalue() + c.err.getvalue()
    finally:
        p.stop()


def test_run_once_mqtt_only_when_pane_url_empty():
    setup("")
    patch_cycle()
    with Capture() as c:
        assert main.run_once() is True
    assert FakeMQTT.published > 0 and "Pane" not in c.out.getvalue() + c.err.getvalue()


def test_run_once_no_output_configured():
    setup("")
    patch_cycle(mqtt_host="")
    with Capture() as c:
        assert main.run_once() is False
    assert_house(c.err.getvalue(), "No output is configured")


def test_run_once_missing_token_file_sends_nothing_but_mqtt_goes_on():
    p = Pane()
    try:
        setup(p.url, os.path.join(TMP, "nope"))
        patch_cycle()
        with Capture() as c:
            assert main.run_once() is False
        assert p.requests == [] and FakeMQTT.published > 0
        assert "Pane token file cannot be read" in c.err.getvalue()
    finally:
        p.stop()


def test_config_defaults():
    # Fresh interpreter so the module-level env parsing is what's tested.
    import subprocess
    code = ("import telemetry.config as c;"
            "print(repr(c.PANE_URL), c.PANE_TOKEN_FILE, c.PANE_TIMEOUT, repr(c.MQTT_HOST))")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PANE_", "MQTT_"))}
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    out = subprocess.check_output([sys.executable, "-c", code], env=env, cwd=root, text=True).strip()
    want_tok = r"C:\ProgramData\telemetry\secrets\pane_token" if os.name == "nt" \
        else "/opt/telemetry/secrets/pane_token"
    assert out == f"'' {want_tok} 10.0 'homehub.local'", out
    env.update(MQTT_HOST="", PANE_URL=" http://100.92.61.42:8427/ ", PANE_TIMEOUT="2.5")
    out = subprocess.check_output([sys.executable, "-c", code], env=env, cwd=root, text=True).strip()
    assert out == f"'http://100.92.61.42:8427' {want_tok} 2.5 ''", out
    env.update(PANE_TIMEOUT="soon")
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=root, text=True, capture_output=True)
    assert " 10.0 " in r.stdout and "PANE_TIMEOUT" in r.stderr, (r.stdout, r.stderr)


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
