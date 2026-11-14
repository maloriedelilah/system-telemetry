"""Loaded-model labels found on their own (MODEL_SOURCE auto), stdlib only.
Run from the repo root:  python tests/test_models_auto.py
"""
import io, os, subprocess, sys
from contextlib import redirect_stderr

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.pop("MODEL_SOURCE", None)
os.environ.pop("TELEMETRY_NVIDIA", None)
from telemetry import nvidia  # noqa: E402

UUIDS = "0, GPU-a\n1, GPU-b\n2, GPU-c\n3, GPU-d\n"


def gpus(n=4, used=0):
    return [{"index": str(i), "mem_used": used} for i in range(n)]


def fake(apps, cmdlines, ollama=None, smi_error=None):
    """apps: [(pid, gpu_letter, process_name)]; cmdlines: {pid: [args]}; ollama: list of names,
    an Exception to raise, or None (never asked)."""
    def smi(*args):
        if smi_error:
            raise smi_error
        if args[0].startswith("--query-gpu=index,gpu_uuid"):
            return UUIDS
        if args[0].startswith("--query-compute-apps"):
            return "".join(f"{p}, GPU-{g}, {n}\n" for p, g, n in apps)
        raise AssertionError(args)
    nvidia._smi = smi
    nvidia._cmdline = lambda pid: cmdlines.get(pid)

    def http(url, timeout=5):
        assert url.endswith("/api/ps"), url
        if ollama is None:
            raise AssertionError("Ollama asked but not expected")
        if isinstance(ollama, Exception):
            raise ollama
        return {"models": [{"name": n} for n in ollama]}
    nvidia._http_json = http


def run(g):
    err = io.StringIO()
    with redirect_stderr(err):
        got = nvidia.read_models(g)
    return got, err.getvalue()


def test_redorigin_two_llama_servers_over_card_pairs():
    # Sally on 0+1 (alias), Chandra on 2+3 (model file only); the 5th card is idle.
    fake([(100, "a", "/opt/llama/llama-server"), (100, "b", "/opt/llama/llama-server"),
          (200, "c", "/opt/llama/llama-server"), (200, "d", "/opt/llama/llama-server")],
         {100: ["/opt/llama/llama-server", "-m", "/m/qwen3.8-27b-Q4_K_M.gguf", "--alias", "sally",
                "--port", "1234"],
          200: ["/opt/llama/llama-server", "--model=/m/chandra-12b-Q8_0.gguf", "--port", "1235"]})
    got, err = run(gpus(4) + [{"index": "4", "mem_used": 3}])
    assert got == {"0": "sally", "1": "sally", "2": "chandra-12b-Q8_0", "3": "chandra-12b-Q8_0",
                   "4": "idle"}, got
    assert err == "", err


def test_nothing_running_is_idle_not_missing():
    fake([], {})
    got, _ = run(gpus(2))
    assert got == {"0": "idle", "1": "idle"}, got


def test_memory_held_with_no_process_listed_is_busy():
    # WSL lists no processes; Windows can hide another session's. Don't call that card idle.
    fake([], {})
    got, _ = run([{"index": "0", "mem_used": 9000}, {"index": "1", "mem_used": 500}])
    assert got == {"0": "busy: process not visible", "1": "idle"}, got


def test_ollama_names_come_from_its_api():
    fake([(7, "a", "/usr/local/bin/ollama")],
         {7: ["/usr/local/bin/ollama", "runner", "--model", "/root/.ollama/models/blobs/sha256-ab"]},
         ollama=["qwen35-9b-q8-4200-07"])
    got, _ = run(gpus(2))
    assert got == {"0": "qwen35-9b-q8-4200-07", "1": "idle"}, got


def test_ollama_down_says_unknown_and_reports():
    fake([(7, "a", "ollama_llama_server")], {7: ["ollama_llama_server"]},
         ollama=OSError("connection refused"))
    got, err = run(gpus(1))
    assert got == {"0": "unknown: Ollama API"}, got
    assert err.startswith("!!! Ollama didn't answer"), err


def test_ollama_with_nothing_loaded():
    fake([(7, "a", "ollama")], {7: ["ollama", "serve"]}, ollama=[])
    got, _ = run(gpus(1))
    assert got == {"0": "ollama (no model loaded)"}, got


def test_comfyui_and_other_python_on_windows():
    fake([(10, "a", r"C:\ComfyUI\python_embeded\python.exe"), (11, "b", r"C:\py\python.exe")],
         {10: [r"C:\ComfyUI\python_embeded\python.exe", "-s", r"ComfyUI\main.py", "--windows-standalone-build"],
          11: [r"C:\py\python.exe", "-m", "some.module", "train.py"]})
    got, _ = run(gpus(2))
    assert got == {"0": "busy: ComfyUI", "1": "busy: python train.py"}, got


def test_vllm_and_docker_llama_server():
    fake([(20, "a", "python3"), (21, "b", "/app/llama-server")],
         {20: ["python3", "-m", "vllm.entrypoints.openai.api_server", "--model", "Qwen/Qwen3-8B"],
          21: ["/app/llama-server", "-m", "/models/bge-m3-FP16.gguf", "--embedding"]})
    got, _ = run(gpus(2))
    assert got == {"0": "Qwen3-8B", "1": "bge-m3-FP16"}, got


def test_unreadable_command_line_falls_back_to_the_process_name():
    fake([(30, "a", "/usr/bin/mystery-app"), (31, "b", "[Insufficient Permissions]")], {})
    got, _ = run(gpus(2))
    assert got == {"0": "busy: mystery-app", "1": "busy: pid 31"}, got


def test_desktop_processes_are_not_a_workload():
    fake([(40, "a", r"C:\Windows\explorer.exe"), (41, "a", r"C:\Windows\System32\dwm.exe")],
         {40: ["explorer.exe"], 41: ["dwm.exe"]})
    got, _ = run(gpus(1))
    assert got == {"0": "idle"}, got


def test_two_things_on_one_card_are_both_shown_once():
    fake([(50, "a", "llama-server"), (51, "a", "llama-server"), (52, "a", "llama-server")],
         {50: ["llama-server", "-a", "embed"], 51: ["llama-server", "-a", "rerank"],
          52: ["llama-server", "-a", "embed"]})
    got, _ = run(gpus(1))
    assert got == {"0": "embed, rerank"}, got


def test_nvidia_smi_failing_marks_every_card_unknown():
    fake([], {}, smi_error=subprocess.CalledProcessError(9, "nvidia-smi"))
    got, err = run(gpus(2))
    assert got == {"0": "unknown: nvidia-smi failed", "1": "unknown: nvidia-smi failed"}, got
    assert err.startswith("!!! can't list GPU processes"), err


def test_filter_keeps_busy_and_unknown_labels():
    nvidia.MODEL_FILTER = ["sally"]
    try:
        fake([(1, "a", "llama-server"), (2, "b", "llama-server"), (3, "b", "mystery")],
             {1: ["llama-server", "-a", "sally"], 2: ["llama-server", "-a", "other"], 3: ["mystery"]})
        got, _ = run(gpus(2))
        assert got == {"0": "sally", "1": "busy: mystery"}, got
    finally:
        nvidia.MODEL_FILTER = []


def test_off_and_no_gpus():
    nvidia.MODEL_SOURCE = "off"
    try:
        assert nvidia.read_models(gpus(1)) == {}
    finally:
        nvidia.MODEL_SOURCE = "auto"
    fake([], {})
    assert nvidia.read_models([]) == {}


def _config_in_child(env):
    code = ("from telemetry import config as c; "
            "print(c.NVIDIA_MODE, c.TELEMETRY_NVIDIA, c.MODEL_SOURCE, c.OLLAMA_URL)")
    full = {k: v for k, v in os.environ.items() if not k.startswith(("MODEL_", "TELEMETRY_"))}
    full.update(env)
    return subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, env=full, text=True).split()


def test_old_settings_now_mean_auto():
    # The host profile wrote TELEMETRY_NVIDIA=0 and MODEL_SOURCE=none: that is how redorigin lost its GPUs.
    assert _config_in_child({"TELEMETRY_NVIDIA": "0", "MODEL_SOURCE": "none"}) == \
        ["auto", "True", "auto", "http://127.0.0.1:11434"]
    assert _config_in_child({"MODEL_SOURCE": "llamaserver",
                             "MODEL_API_URL": "http://127.0.0.1:1234,http://127.0.0.1:1235"}) == \
        ["auto", "True", "auto", "http://127.0.0.1:11434"]
    assert _config_in_child({"MODEL_SOURCE": "ollama", "MODEL_API_URL": "http://127.0.0.1:11500/"}) == \
        ["auto", "True", "auto", "http://127.0.0.1:11500"]
    assert _config_in_child({"TELEMETRY_NVIDIA": "off", "MODEL_SOURCE": "off"})[:3] == ["off", "False", "off"]
    assert _config_in_child({"TELEMETRY_NVIDIA": "1", "MODEL_SOURCE": "lmstudio"})[:3] == ["on", "True", "lmstudio"]
    assert _config_in_child({})[:3] == ["auto", "True", "auto"]


def test_no_nvidia_smi_in_auto_is_silent():
    old_which, old_mode = nvidia.shutil.which, nvidia.config.NVIDIA_MODE
    nvidia.shutil.which = lambda name: None
    nvidia.config.NVIDIA_MODE = "auto"
    try:
        nvidia._smi = lambda *a: (_ for _ in ()).throw(AssertionError("must not run nvidia-smi"))
        err = io.StringIO()
        with redirect_stderr(err):
            assert nvidia.read_gpus() == []
        assert err.getvalue() == "", err.getvalue()
        # asked for explicitly (TELEMETRY_NVIDIA=1): a missing nvidia-smi is reported
        nvidia.config.NVIDIA_MODE = "on"
        nvidia._smi = lambda *a: (_ for _ in ()).throw(FileNotFoundError("nvidia-smi"))
        with redirect_stderr(err):
            assert nvidia.read_gpus() == []
        assert "!!! nvidia-smi failed" in err.getvalue(), err.getvalue()
    finally:
        nvidia.shutil.which, nvidia.config.NVIDIA_MODE = old_which, old_mode


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} passed")
