"""NVIDIA GPU metrics (nvidia-smi) and loaded-model labels.

read_gpus()       -> list of per-GPU metric dicts (util, mem, temp, power)
read_models(gpus) -> {gpu_index: label} for every GPU, found on its own (see read_models)
"""

import http.client
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse

from . import config

MODEL_SOURCE = config.MODEL_SOURCE
MODEL_API_URL = config.MODEL_API_URL
MODEL_API_PATH = config.MODEL_API_PATH
MODEL_FILTER = config.MODEL_FILTER
OLLAMA_URL = config.OLLAMA_URL

# Labels for a card with nothing identifiable on it. "idle" and the "unknown: ..." forms are
# reported to Pane like any label, so a card is never silently missing from the model table.
IDLE = "idle"
# A card holding this much memory with no process listed is busy with something nvidia-smi can't
# show (WSL lists no processes; Windows can hide another session's). Below it: a desktop's share.
HIDDEN_BUSY_MB = 2048


def _num(s):
    """Best-effort numeric parse; returns the string back if it isn't a number."""
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except (ValueError, AttributeError):
        return s


def _smi(*args):
    # errors="replace": one process with a non-UTF-8 name must not turn every card into unknown.
    return subprocess.check_output(["nvidia-smi", *args], text=True, errors="replace", timeout=15)


# ---------------------------------------------------------------------------
# Hardware metrics
# ---------------------------------------------------------------------------
def read_gpus():
    """Return list of dicts, one per GPU, with hardware metrics.

    No nvidia-smi on the box: [] and nothing said, unless TELEMETRY_NVIDIA=1 asked for GPUs. Any
    other failure is reported: on a box with nvidia-smi it usually means the driver broke (a kernel
    update without its module)."""
    if config.NVIDIA_MODE == "auto" and not shutil.which("nvidia-smi"):
        return []
    fields = [
        "index", "name", "utilization.gpu", "memory.used",
        "memory.total", "temperature.gpu", "power.draw",
    ]
    try:
        out = _smi(f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits")
    except (subprocess.SubprocessError, OSError) as e:
        config.report("nvidia-smi failed", "nvidia-smi --query-gpu", "one line per GPU",
                      f"{type(e).__name__}: {e}",
                      "run nvidia-smi on this box; if it can't talk to the driver, the NVIDIA kernel "
                      "module needs rebuilding (dkms status) or the box a reboot. No GPU data this cycle.")
        return []

    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(fields):
            continue
        idx, name, util, mem_used, mem_total, temp, power = parts[:7]
        try:
            mem_used_f = float(mem_used)
            mem_total_f = float(mem_total)
            mem_pct = round(100.0 * mem_used_f / mem_total_f, 1) if mem_total_f else 0.0
        except ValueError:
            mem_pct = 0.0
        gpus.append({
            "index": idx,
            "name": name,
            "util": _num(util),
            "mem_used": _num(mem_used),
            "mem_total": _num(mem_total),
            "mem_pct": mem_pct,
            "temp": _num(temp),
            "power": _num(power),
        })
    return gpus


# ---------------------------------------------------------------------------
# Loaded-model labels
# ---------------------------------------------------------------------------
def read_models(gpus):
    """Return {gpu_index: label} for every NVIDIA GPU in gpus. Best-effort; never raises.

    auto (the default): nvidia-smi lists the processes on each card; each one is named by
    _identify(): llama-server by its --alias or model file, Ollama through its local API, vLLM and
    others by --model, ComfyUI by name, anything else as "busy: <process>". A card with nothing on
    it is "idle". When a lookup fails the card says "unknown: <why>" instead of going missing.
    lmstudio: LM Studio's API, as before. off: no labels."""
    try:
        if MODEL_SOURCE == "off":
            return {}
        if MODEL_SOURCE == "lmstudio":
            return _models_lmstudio(gpus)
        return _models_auto(gpus)
    except Exception as e:  # never let model lookup break the hardware metrics
        config.report("model lookup failed", "read the loaded models", "a label per GPU",
                      f"{type(e).__name__}: {e}", "this is a bug in nvidia.py; GPU metrics still go out.")
        return {g["index"]: f"unknown: {type(e).__name__}" for g in gpus}


HTTP_DEADLINE_S = 5         # whole request, not per read: a slow local server can't stall the cycle
HTTP_MAX_BYTES = 256 * 1024  # an /api/ps or /v1/models answer is a few KiB


def _http_json(url, timeout=HTTP_DEADLINE_S, max_bytes=HTTP_MAX_BYTES):
    """GET url and parse JSON, within one overall deadline and a size cap. Raises on either.

    The deadline covers the whole exchange: connecting, the status line and headers, and the body.
    A timer shuts the socket down when it runs out, which ends any read that is waiting, so a server
    trickling its headers can't hold the cycle (Errata L9 on #12). Plain http.client, not urlopen:
    these are local APIs, so no proxy and no redirects."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"not an http(s) URL: {url!r}")
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, u.port, timeout=timeout)
    expired = threading.Event()

    def expire():
        expired.set()
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def check():
        if expired.is_set():
            raise TimeoutError(f"no complete answer within {timeout} s")

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        conn.connect()
        check()   # the timer may have fired before the socket existed, so it couldn't shut it
        conn.request("GET", (u.path or "/") + (f"?{u.query}" if u.query else ""),
                     headers={"Accept": "application/json"})
        r = conn.getresponse()
        check()
        if r.status != 200:
            raise ValueError(f"HTTP {r.status}")
        chunks, got = [], 0
        while True:
            chunk = r.read1(min(16384, max_bytes + 1 - got))   # read1: whatever has arrived, not a full 16 KiB
            check()
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
            if got > max_bytes:
                raise ValueError(f"answer over {max_bytes} bytes")
    except (OSError, http.client.HTTPException):
        check()   # a read cut off by the timer is a timeout, not a broken server
        raise
    finally:
        timer.cancel()
        conn.close()
    return json.loads(b"".join(chunks).decode("utf-8", "replace"))


def _keep_local(names):
    """Drop models not belonging to this box per MODEL_FILTER (no-op if unset)."""
    if not MODEL_FILTER:
        return names
    return [n for n in names if any(sub in n.lower() for sub in MODEL_FILTER)]


def _models_lmstudio(gpus):
    """LM Studio lists loaded models via its REST API. It doesn't map model->physical GPU index,
    so the loaded set is reported against GPU 0. MODEL_FILTER strips out models that `lms link`
    surfaces from other boxes."""
    data = _http_json(f"{MODEL_API_URL}{MODEL_API_PATH}")
    loaded = _keep_local([m.get("id", "?") for m in data.get("data", [])
                          if m.get("state") == "loaded"])
    label = ", ".join(loaded) if loaded else IDLE
    return {gpus[0]["index"]: label} if gpus else {}


# Desktop and shell processes Windows lists on a card (WDDM). Not a workload.
_IGNORE = {
    "explorer", "searchhost", "startmenuexperiencehost", "shellexperiencehost",
    "windowsterminal", "phoneexperiencehost", "dwm", "chrome", "msedge", "firefox",
    "code", "docker desktop", "textinputhost", "widgets", "xorg", "gnome-shell",
}
_NO_NAME = {"[insufficient permissions]", "[n/a]", ""}


def _compute_apps():
    """[(pid, gpu_index, process_name)] from nvidia-smi. Raises if nvidia-smi fails."""
    uuid_to_idx = {}
    for line in _smi("--query-gpu=index,gpu_uuid", "--format=csv,noheader").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            uuid_to_idx[parts[1]] = parts[0]
    apps = []
    out = _smi("--query-compute-apps=pid,gpu_uuid,process_name", "--format=csv,noheader")
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",", 2)]   # a Windows path may hold a comma
        if len(parts) < 3:
            continue
        idx = uuid_to_idx.get(parts[1])
        try:
            pid = int(parts[0])
        except ValueError:
            pid = None
        if idx is not None:
            apps.append((pid, idx, parts[2]))
    return apps


def _cmdline(pid):
    """The process's argument list, or None when it can't be read (gone, or not ours to see).
    A process that rewrote its title into one string ("python3 server.py --flag x") is split back
    into words, so nothing after the program name ends up in a label by accident."""
    if pid is None:
        return None
    args = None
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            return None
        args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    else:
        try:
            import psutil
            args = [str(a).encode("utf-8", "replace").decode("utf-8") for a in psutil.Process(pid).cmdline()]
        except Exception:
            return None
    return _split_title(args) or None


def _split_title(args):
    """A one-string title ("python3 server.py --api-key k") back into words; anything else as is."""
    if len(args) == 1 and " " in args[0].strip():
        try:
            return shlex.split(args[0], posix=not sys.platform.startswith("win"))
        except ValueError:
            return args[0].split()
    return args


def _parent_cmdline(pid):
    """The parent's argument list (Linux), for a worker that only names itself (VLLM::EngineCore)."""
    try:
        with open(f"/proc/{pid}/status") as f:
            ppid = next(int(line.split()[1]) for line in f if line.startswith("PPid:"))
    except (OSError, StopIteration, ValueError):
        return None
    return _cmdline(ppid) if ppid > 1 else None


def _arg(args, *names):
    """The value after any of names (-m x, --model x, --model=x), or None."""
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if n.startswith("--") and a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def _short_model(path):
    """A model path, URL or repo as a short label: basename without .gguf. A URL's query string,
    fragment and user:password are dropped first; they can hold a token."""
    p = str(path)
    if "://" in p:
        u = urllib.parse.urlsplit(p)
        p = u.path or u.hostname or ""
    else:
        p = p.split("?", 1)[0].split("#", 1)[0]
    name = os.path.basename(p.rstrip("/\\")) or p
    return name[:-5] if name.lower().endswith(".gguf") else name


# vLLM options that take no value, so the word after one is the model, not its value. Only flags
# known to be valueless belong here: a flag wrongly listed would hand its value (maybe a key) to the label.
_VLLM_SWITCHES = {
    "--trust-remote-code", "--enforce-eager", "--enable-prefix-caching", "--no-enable-prefix-caching",
    "--enable-chunked-prefill", "--no-enable-chunked-prefill", "--disable-log-requests",
    "--disable-log-stats", "--enable-auto-tool-choice", "--disable-custom-all-reduce", "--enable-lora",
    "--skip-tokenizer-init", "--disable-sliding-window", "--disable-frontend-multiprocessing",
    "--enable-sleep-mode", "--headless", "--disable-fastapi-docs", "--enable-prompt-tokens-details",
    "--enable-server-load-tracking", "--return-tokens-as-token-ids", "--enable-expert-parallel",
    "--disable-mm-preprocessor-cache", "--async-scheduling",
}


def _positional_after(args, word, switches=_VLLM_SWITCHES):
    """The first argument after `word` that isn't an option (or an option's value), or None.
    `vllm serve --api-key=k --port 8000 MODEL` gives MODEL, never the key or the port, and
    `vllm serve --trust-remote-code MODEL` gives MODEL because that flag takes no value."""
    try:
        i = args.index(word) + 1
    except ValueError:
        return None
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            # --opt=value is one word; --opt value is two, unless the next word is itself an option
            if "=" not in a and a not in switches and i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
                continue
            i += 1
            continue
        return a
    return None


def _first_word(name):
    """A program name up to its first space: a spaced argv[0] can carry options (Errata L10 on #12)."""
    return (str(name).split() or ["process"])[0]


def _base(name):
    b = os.path.basename(str(name).replace("\\", "/")).lower()
    return b[:-4] if b.endswith(".exe") else b


def _identify(pname, args):
    """(kind, label) for one GPU process. kind is "ollama" (label comes from its API), "model" or
    "busy"."""
    pb = _base(pname)
    a0 = _base(args[0]) if args else ""
    argv = args or []
    if "ollama" in pb or "ollama" in a0:
        return "ollama", None
    if "llama" in pb or "llama" in a0:                       # llama-server, llama-box, llama-cli
        alias = _arg(argv, "-a", "--alias")
        model = alias or _arg(argv, "-m", "--model") or _arg(argv, "-hf", "--hf-repo")
        if model:
            return "model", alias.split(",")[0].strip() if alias else _short_model(model)
        return "busy", f"busy: {_first_word(a0 or pb)}"
    if any("vllm" in a.lower() for a in argv):
        model = _arg(argv, "--model")
        if not model or model.startswith("-"):
            model = _positional_after(argv, "serve")
        if model and not model.startswith("-"):
            return "model", _short_model(model)
    # For python, -m names a module (python -m comfy...), not a model.
    flags = ("--model", "--model-path") if a0.startswith("python") or pb.startswith("python") \
        else ("-m", "--model", "--model-path")
    model = _arg(argv, *flags)
    if model and not model.startswith("-") and not model.endswith(".py"):
        return "model", _short_model(model)
    if any("comfyui" in a.lower() for a in argv):
        return "busy", "busy: ComfyUI"
    # Only the program's own name (and a python script's file name): never its options, which can hold keys.
    script = next((os.path.basename(a) for a in argv[1:] if a.endswith(".py") and not a.startswith("-")), None)
    name = _first_word(a0 or pb)
    return "busy", f"busy: {name} {script}" if script and name.startswith("python") else f"busy: {name}"


def _ollama_models():
    """Names of the models Ollama has loaded, from its local API; raises if it doesn't answer."""
    data = _http_json(f"{OLLAMA_URL}/api/ps")
    return [str(m.get("name", "?")) for m in data.get("models", [])]


def _models_auto(gpus):
    if not gpus:
        return {}
    try:
        apps = _compute_apps()
    except (subprocess.SubprocessError, OSError) as e:
        config.report("can't list GPU processes", "nvidia-smi --query-compute-apps",
                      "the processes on each card", f"{type(e).__name__}: {e}",
                      "run nvidia-smi on this box. Each card shows as unknown until it answers.")
        return {g["index"]: "unknown: nvidia-smi failed" for g in gpus}

    per_gpu = {g["index"]: [] for g in gpus}
    ollama_on = set()
    for pid, idx, pname in apps:
        if idx not in per_gpu:
            continue
        if _base(pname) in _IGNORE or (pname.strip().lower() in _NO_NAME and pid is None):
            continue
        args = _cmdline(pid)
        if not args and pname.strip().lower() in _NO_NAME:
            per_gpu[idx].append("busy: process not readable")   # not the pid: a new series per restart
            continue
        if args and _base(args[0]).startswith("vllm::"):
            # vLLM V1's engine worker retitles itself VLLM::EngineCore; the model is on its parent's line.
            args = _parent_cmdline(pid) or args
        kind, label = _identify(pname, args)
        if kind == "ollama":
            ollama_on.add(idx)
        elif label:
            per_gpu[idx].append(label)

    if ollama_on:
        try:
            names = _ollama_models()
            labels = names or ["ollama (no model loaded)"]
        except Exception as e:
            config.report("Ollama didn't answer", f"GET {OLLAMA_URL}/api/ps",
                          "the models Ollama has loaded", f"{type(e).__name__}: {e}",
                          "check `ollama ps` on this box; if Ollama listens elsewhere, set MODEL_SOURCE=ollama "
                          "and MODEL_API_URL to its address. Its cards show as unknown meanwhile.")
            labels = ["unknown: Ollama API"]
        # Ollama doesn't say which card a model sits on: every card running Ollama gets the list.
        for idx in ollama_on:
            per_gpu[idx] += labels

    result = {}
    by_idx = {g["index"]: g for g in gpus}
    clean = lambda lb: str(lb).encode("utf-8", "replace").decode("utf-8")   # a lone surrogate can't be sent
    for idx, labels in per_gpu.items():
        labels = [lb for lb in labels if lb.startswith(("busy:", "unknown:")) or _keep_local([lb])]
        if labels:
            result[idx] = clean(", ".join(sorted(set(labels))))
            continue
        used = by_idx[idx].get("mem_used")
        if isinstance(used, (int, float)) and used >= HIDDEN_BUSY_MB:
            result[idx] = "busy: process not visible"
        else:
            result[idx] = IDLE
    return result
