"""MODEL_SOURCE=llamaserver: labels from GET <url>/v1/models on each llama-server
instance, stdlib only, real HTTP servers on 127.0.0.1 (no mocks of urllib).
Run from the repo root:  python tests/test_llamaserver_models.py
"""
import contextlib, io, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["MODEL_SOURCE"] = "llamaserver"
from telemetry import nvidia  # noqa: E402

GPUS = [{"index": "0"}, {"index": "1"}]


def serve(body, status=200, record=None):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if record is not None:
                record.append(self.path)
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def dead_url():
    s = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = s.server_address[1]
    s.server_close()                       # nothing listens here any more
    return f"http://127.0.0.1:{port}"


def models(data):
    return {"object": "list", "data": [{"id": i, "object": "model"} for i in data]}


def run(urls, filt=()):
    nvidia.MODEL_API_URL = ",".join(urls)
    nvidia.MODEL_FILTER = list(filt)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            got = nvidia.read_models(GPUS)
    finally:
        nvidia.MODEL_FILTER = []
    return got, err.getvalue()


def test_two_instances_labelled_against_gpu0():
    seen = []
    a, ua = serve(models(["qwen3-30b-a3b-q4"]), record=seen)
    b, ub = serve(models(["bge-m3-FP16"]), record=seen)
    got, err = run([ua, ub + "/"])         # trailing slash tolerated
    assert got == {"0": "qwen3-30b-a3b-q4, bge-m3-FP16"}, got
    assert seen == ["/v1/models", "/v1/models"], seen
    assert err == "", err


def test_one_down_keeps_the_others_and_says_so():
    a, ua = serve(models(["m-one"]))
    dead = dead_url()
    got, err = run([dead, ua])
    assert got == {"0": "m-one"}, got
    assert err.startswith("!!! llama-server model lookup failed\n"), err
    for field in ("    Did:      GET " + dead + "/v1/models",
                  "    Expected: ", "    Got:      ", "    Next:     "):
        assert field in err, (field, err)


def test_all_down_gives_no_labels():
    got, err = run([dead_url(), dead_url()])
    assert got == {}, got
    assert err.count("!!! llama-server model lookup failed") == 2, err


def test_bad_status_and_bad_json_are_failures():
    a, ua = serve({"error": "x"}, status=500)
    b, ub = serve(b"not json")
    c, uc = serve(models(["ok-model"]))
    got, err = run([ua, ub, uc])
    assert got == {"0": "ok-model"}, got
    assert err.count("!!! llama-server model lookup failed") == 2, err


def test_empty_list_is_none_loaded():
    a, ua = serve(models([]))
    got, _ = run([ua])
    assert got == {"0": "none loaded"}, got


def test_filter():
    a, ua = serve(models(["Qwen3-Coder", "bge-m3"]))
    got, _ = run([ua], filt=["qwen"])
    assert got == {"0": "Qwen3-Coder"}, got
    got, _ = run([ua], filt=["nomatch"])
    assert got == {"0": "none loaded"}, got


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("ALL PASS")
