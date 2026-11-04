"""MODEL_SOURCE=docker: labels from `docker ps` / `docker inspect`, stdlib only.
Run from the repo root:  python tests/test_docker_models.py
"""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["MODEL_SOURCE"] = "docker"
from telemetry import nvidia  # noqa: E402

GPUS = [{"index": "0"}, {"index": "1"}]


def container(name, svc, args, device_ids=None):
    req = [{"Driver": "nvidia", "DeviceIDs": device_ids}] if device_ids is not None else []
    return {"Name": "/" + name, "Args": args,
            "Config": {"Cmd": args, "Labels": {"com.docker.compose.service": svc} if svc else {}},
            "HostConfig": {"DeviceRequests": req}}


def fake(ps, inspect):
    def check_output(cmd, **kw):
        if cmd[:2] == ["docker", "ps"]:
            return ps
        if cmd[:2] == ["docker", "inspect"]:
            return json.dumps(inspect)
        raise AssertionError(cmd)
    nvidia.subprocess.check_output = check_output


def test_labels_and_gpu_attribution():
    fake("a\nb\nc\nd\n", [
        container("chunkinator-embed-1", "embed", ["-m", "/models/bge-m3-FP16.gguf", "--embedding"], ["0"]),
        container("chunkinator-rerank-1", "rerank", ["--model=/models/bge-reranker-v2-m3-FP16.gguf"], ["1"]),
        container("chunkinator-qdrant-1", "qdrant", ["./entrypoint.sh"]),           # no model: skipped
        container("loose", None, ["-m", "/m/other.gguf"]),                           # no device request: GPU 0
    ])
    got = nvidia.read_models(GPUS)
    assert got == {"0": "bge-m3-FP16 (embed), other (loose)",
                   "1": "bge-reranker-v2-m3-FP16 (rerank)"}, got


def test_no_containers():
    fake("", [])
    assert nvidia.read_models(GPUS) == {"0": "none loaded", "1": "none loaded"}


def test_unknown_device_id_falls_back_to_gpu0():
    fake("a\n", [container("x", "embed", ["-m", "/models/m.gguf"], ["7"])])
    assert nvidia.read_models(GPUS) == {"0": "m (embed)", "1": "none loaded"}


def test_docker_missing_never_raises():
    def boom(cmd, **kw):
        raise FileNotFoundError("docker")
    nvidia.subprocess.check_output = boom
    assert nvidia.read_models(GPUS) == {}


def test_filter():
    nvidia.MODEL_FILTER = ["rerank"]
    try:
        fake("a\nb\n", [container("e", "embed", ["-m", "/models/bge-m3.gguf"], ["0"]),
                        container("r", "rerank", ["-m", "/models/rr.gguf"], ["0"])])
        assert nvidia.read_models(GPUS) == {"0": "rr (rerank)", "1": "none loaded"}
    finally:
        nvidia.MODEL_FILTER = []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("ALL PASS")
