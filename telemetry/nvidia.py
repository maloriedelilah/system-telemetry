"""NVIDIA GPU metrics (nvidia-smi) and loaded-model-name adapters.

read_gpus()   -> list of per-GPU metric dicts (util, mem, temp, power)
read_models() -> {gpu_index: "model1, model2"} from ollama | lmstudio | nvidia-smi
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
