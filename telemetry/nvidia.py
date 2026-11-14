"""NVIDIA GPU metrics (nvidia-smi) and loaded-model labels.

read_gpus()       -> list of per-GPU metric dicts (util, mem, temp, power)
read_models(gpus) -> {gpu_index: label} for every GPU, found on its own (see read_models)
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request

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
    return subprocess.check_output(["nvidia-smi", *args], text=True, timeout=15)


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


def _http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


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
    """The process's argument list, or None when it can't be read (gone, or not ours to see)."""
    if pid is None:
        return None
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            return None
        return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a] or None
    try:
        import psutil
        return psutil.Process(pid).cmdline() or None
    except Exception:
        return None


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
    """A model path or repo as a short label: basename, without .gguf."""
    name = os.path.basename(str(path).rstrip("/\\")) or str(path)
    return name[:-5] if name.lower().endswith(".gguf") else name


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
        return "busy", f"busy: {a0 or pb}"
    if any("vllm" in a.lower() for a in argv):
        model = _arg(argv, "serve") or _arg(argv, "--model")
        if model:
            return "model", _short_model(model)
    # For python, -m names a module (python -m comfy...), not a model.
    flags = ("--model", "--model-path") if a0.startswith("python") or pb.startswith("python") \
        else ("-m", "--model", "--model-path")
    model = _arg(argv, *flags)
    if model and not model.startswith("-") and not model.endswith(".py"):
        return "model", _short_model(model)
    if any("comfyui" in a.lower() for a in argv):
        return "busy", "busy: ComfyUI"
    script = next((os.path.basename(a) for a in argv[1:] if a.endswith(".py")), None)
    name = a0 or pb or "process"
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
            per_gpu[idx].append(f"busy: pid {pid}")
            continue
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
    for idx, labels in per_gpu.items():
        labels = [lb for lb in labels if lb.startswith(("busy:", "unknown:")) or _keep_local([lb])]
        if labels:
            result[idx] = ", ".join(sorted(set(labels)))
            continue
        used = by_idx[idx].get("mem_used")
        if isinstance(used, (int, float)) and used >= HIDDEN_BUSY_MB:
            result[idx] = "busy: process not visible"
        else:
            result[idx] = IDLE
    return result
