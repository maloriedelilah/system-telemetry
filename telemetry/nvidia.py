"""NVIDIA GPU metrics (nvidia-smi) and loaded-model-name adapters.

read_gpus()   -> list of per-GPU metric dicts (util, mem, temp, power)
read_models() -> {gpu_index: "model1, model2"} from ollama | lmstudio | docker | llamaserver | nvidia-smi
"""

import json
import os
import subprocess
import sys
import urllib.request

from . import config

MODEL_SOURCE = config.MODEL_SOURCE
MODEL_API_URL = config.MODEL_API_URL
MODEL_API_PATH = config.MODEL_API_PATH
MODEL_FILTER = config.MODEL_FILTER


def _num(s):
    """Best-effort numeric parse; returns the string back if it isn't a number."""
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except (ValueError, AttributeError):
        return s


# ---------------------------------------------------------------------------
# Hardware metrics
# ---------------------------------------------------------------------------
def read_gpus():
    """Return list of dicts, one per GPU, with hardware metrics."""
    fields = [
        "index", "name", "utilization.gpu", "memory.used",
        "memory.total", "temperature.gpu", "power.draw",
    ]
    query = ",".join(fields)
    try:
        out = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={query}",
             "--format=csv,noheader,nounits"],
            text=True, timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"nvidia-smi failed: {e}", file=sys.stderr)
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
# Loaded-model names (per-host adapter)
# ---------------------------------------------------------------------------
def read_models(gpus):
    """Return {gpu_index: 'model1, model2'} mapping. Best-effort; never raises."""
    try:
        if MODEL_SOURCE == "ollama":
            return _models_ollama(gpus)
        if MODEL_SOURCE == "lmstudio":
            return _models_lmstudio(gpus)
        if MODEL_SOURCE == "docker":
            return _models_docker(gpus)
        if MODEL_SOURCE == "llamaserver":
            return _models_llamaserver(gpus)
        return _models_from_nvidia_smi(gpus)
    except Exception as e:  # never let model lookup break the hardware metrics
        print(f"model lookup failed: {e}", file=sys.stderr)
        return {}


def _http_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _keep_local(names):
    """Drop models not belonging to this box per MODEL_FILTER (no-op if unset)."""
    if not MODEL_FILTER:
        return names
    return [n for n in names if any(sub in n.lower() for sub in MODEL_FILTER)]


def _models_ollama(gpus):
    """Ollama /api/ps lists currently-loaded models. Ollama doesn't expose which
    physical GPU index a model sits on, so on a single-GPU box we attribute all
    loaded models to GPU 0. (slimridge is single-V100, so this is correct.)"""
    data = _http_json(f"{MODEL_API_URL}/api/ps")
    names = _keep_local([m.get("name", "?") for m in data.get("models", [])])
    label = ", ".join(names) if names else "none loaded"
    return {gpus[0]["index"]: label} if gpus else {}


def _models_lmstudio(gpus):
    """LM Studio lists loaded models via its REST API. Like Ollama it doesn't map
    model->physical GPU index, so we report the full loaded set against GPU 0.
    MODEL_FILTER strips out models that `lms link` surfaces from other boxes."""
    data = _http_json(f"{MODEL_API_URL}{MODEL_API_PATH}")
    loaded = _keep_local([m.get("id", "?") for m in data.get("data", [])
                          if m.get("state") == "loaded"])
    label = ", ".join(loaded) if loaded else "none loaded"
    return {gpus[0]["index"]: label} if gpus else {}


def _models_llamaserver(gpus):
    """llama.cpp `llama-server` instances (Eighty-Eight runs two under NSSM, on
    :1234 and :1235). MODEL_API_URL is a comma-separated list of base URLs; each
    one's GET <url>/v1/models lists what it serves in data[].id. Like the
    ollama/lmstudio adapters there is no model->GPU mapping, so every label is
    reported against GPU 0. MODEL_FILTER applies to the ids.

    One instance being down must not hide the others' labels: it is reported in
    the house format and skipped. If every instance fails, returns {} (the
    callers show "unknown")."""
    urls = [u.strip().rstrip("/") for u in MODEL_API_URL.split(",") if u.strip()]
    names, answered = [], 0
    for url in urls:
        endpoint = f"{url}/v1/models"
        try:
            data = _http_json(endpoint)
            ids = [str(m.get("id", "?")) for m in data.get("data", [])]
        except Exception as e:
            config.report(
                "llama-server model lookup failed",
                f"GET {endpoint}",
                "200 with a JSON body {\"data\": [{\"id\": ...}]}",
                f"{type(e).__name__}: {e}",
                "check the llama-server is running and listening on that port "
                "(NSSM service status), and that MODEL_API_URL lists each base "
                "URL, e.g. http://127.0.0.1:1234,http://127.0.0.1:1235. "
                "The other instances' labels are still reported.")
            continue
        answered += 1
        names += ids
    if not answered:
        return {}
    names = _keep_local(names)
    label = ", ".join(names) if names else "none loaded"
    return {gpus[0]["index"]: label} if gpus else {}


def _model_arg(args):
    """The model file named by -m / --model (or --model=...) in a container's
    command line, as a short label: basename without .gguf. None if absent."""
    for i, a in enumerate(args):
        if a in ("-m", "--model") and i + 1 < len(args):
            path = args[i + 1]
            break
        if a.startswith("--model="):
            path = a.split("=", 1)[1]
            break
    else:
        return None
    name = os.path.basename(path)
    return name[:-5] if name.lower().endswith(".gguf") else name


def _models_docker(gpus):
    """Model servers running in Docker containers (llama.cpp llama-server on
    slimridge), whose APIs have no host port. Reads the running containers with
    the docker CLI — the service user must be in the `docker` group — and labels
    each one that names a model file (-m / --model) as "<model> (<service>)",
    where <service> is the compose service name (or the container name).

    GPU attribution: a container that requests specific devices
    (HostConfig.DeviceRequests[].DeviceIDs, i.e. compose `device_ids`) is shown on
    those GPU indexes; one that asks for all GPUs, or doesn't say, is shown on
    GPU 0, like the ollama/lmstudio adapters. MODEL_FILTER applies to the labels."""
    ids = subprocess.check_output(["docker", "ps", "-q"], text=True, timeout=15).split()
    if not ids:
        return {g["index"]: "none loaded" for g in gpus}
    raw = subprocess.check_output(["docker", "inspect", *ids], text=True, timeout=15)
    per_gpu = {}
    default_idx = gpus[0]["index"] if gpus else "0"
    known = {g["index"] for g in gpus}
    for c in json.loads(raw):
        cfg = c.get("Config") or {}
        args = list(c.get("Args") or []) or list(cfg.get("Cmd") or [])
        model = _model_arg(args)
        if not model:
            continue
        labels = cfg.get("Labels") or {}
        svc = labels.get("com.docker.compose.service") or c.get("Name", "").lstrip("/")
        label = f"{model} ({svc})" if svc else model
        if not _keep_local([label]):
            continue
        idxs = []
        for req in (c.get("HostConfig") or {}).get("DeviceRequests") or []:
            idxs += [str(d) for d in (req.get("DeviceIDs") or []) if str(d) in known]
        for idx in idxs or [default_idx]:
            per_gpu.setdefault(idx, []).append(label)
    return {g["index"]: ", ".join(sorted(per_gpu.get(g["index"], []))) or "none loaded"
            for g in gpus}


def _models_from_nvidia_smi(gpus):
    """Fallback: no serving API. Report VRAM-consuming compute processes per GPU.
    Filters out OS/desktop noise and permission-denied entries (common on Windows).
    For boxes like Chonky (ComfyUI loads per-job) this answers 'is something
    actually using the card'."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-compute-apps=gpu_uuid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            text=True, timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}

    try:
        uuid_out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,gpu_uuid",
             "--format=csv,noheader"],
            text=True, timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}
    uuid_to_idx = {}
    for line in uuid_out.strip().splitlines():
        idx, uuid = [p.strip() for p in line.split(",")]
        uuid_to_idx[uuid] = idx

    IGNORE = {
        "explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe",
        "shellexperiencehost.exe", "windowsterminal.exe", "phoneexperiencehost.exe",
        "dwm.exe", "chrome.exe", "msedge.exe", "code.exe", "docker desktop.exe",
        "[insufficient permissions]", "[n/a]",
    }

    per_gpu = {}
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        uuid, pname, mem = parts[0], parts[1], parts[2]
        idx = uuid_to_idx.get(uuid)
        if idx is None:
            continue
        short = os.path.basename(pname)
        if short.lower() in IGNORE:
            continue
        try:
            mem_i = int(float(mem))
        except (ValueError, TypeError):
            continue
        if mem_i <= 0:
            continue
        per_gpu.setdefault(idx, []).append(f"{short} ({mem_i}MiB)")

    result = {}
    for idx in {g["index"] for g in gpus}:
        procs = per_gpu.get(idx, [])
        result[idx] = ", ".join(procs) if procs else "idle / no compute job"
    return result
