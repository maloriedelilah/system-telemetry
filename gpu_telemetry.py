#!/usr/bin/env python3
"""
gpu_telemetry.py — read NVIDIA GPU stats + loaded-model names, publish to MQTT
with Home Assistant MQTT discovery so sensors auto-appear in HA.

Per GPU it publishes: utilization %, memory used (MiB) + memory %, temperature (C),
power draw (W). Plus a per-GPU text sensor listing loaded model names.

Model-name source is pluggable per host:
  - "ollama"   -> queries Ollama /api/ps              (slimridge)
  - "lmstudio" -> queries LM Studio /api/v0/models    (eighty-eight)
  - "none"     -> falls back to nvidia-smi process VRAM only (chonky)

Designed to be run once per interval by a systemd timer (not a long-running loop),
so it's simple and crash-resilient. Config via environment variables (see the
systemd unit / .env).
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Config (from environment)
# ---------------------------------------------------------------------------
MQTT_HOST = os.environ.get("MQTT_HOST", "homehub.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

# Friendly host name used in entity names/ids. Defaults to the actual hostname.
HOST = os.environ.get("GPU_HOST_NAME", socket.gethostname()).lower()

# Where to get loaded-model names: ollama | lmstudio | none
MODEL_SOURCE = os.environ.get("MODEL_SOURCE", "none").lower()
# Base URL for that serving API (only used if MODEL_SOURCE != none)
MODEL_API_URL = os.environ.get("MODEL_API_URL", "http://127.0.0.1:11434")

DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")

# Set DEBUG=1 in the environment for verbose output.
DEBUG = os.environ.get("DEBUG", "0") not in ("0", "", "false", "False")


def dbg(*a):
    if DEBUG:
        print("[debug]", *a, file=sys.stderr, flush=True)

# ---------------------------------------------------------------------------
# Read GPU hardware stats from nvidia-smi
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
            mem_used_f = mem_total_f = mem_pct = 0.0
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


def _num(s):
    """Best-effort numeric parse; returns the string back if it isn't a number."""
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except (ValueError, AttributeError):
        return s


# ---------------------------------------------------------------------------
# Read loaded model names per GPU (per-host adapter)
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


def _models_ollama(gpus):
    """Ollama /api/ps lists currently-loaded models. Ollama doesn't expose which
    physical GPU index a model sits on, so on a single-GPU box we attribute all
    loaded models to GPU 0. (slimridge is single-V100, so this is correct.)"""
    data = _http_json(f"{MODEL_API_URL}/api/ps")
    names = [m.get("name", "?") for m in data.get("models", [])]
    label = ", ".join(names) if names else "none loaded"
    # single-GPU attribution
    return {gpus[0]["index"]: label} if gpus else {}


def _models_lmstudio(gpus):
    """LM Studio lists loaded models via its REST API. Like Ollama it doesn't map
    model->physical GPU index, so we report the full loaded set against GPU 0 and
    note it's box-wide. (Good enough for 'is it loaded at all'.)
    Path is configurable via MODEL_API_PATH (LM Studio default differs from Ollama)."""
    path = os.environ.get("MODEL_API_PATH", "/api/v0/models")
    data = _http_json(f"{MODEL_API_URL}{path}")
    loaded = [m.get("id", "?") for m in data.get("data", [])
              if m.get("state") == "loaded"]
    label = ", ".join(loaded) if loaded else "none loaded"
    return {gpus[0]["index"]: label} if gpus else {}


def _models_from_nvidia_smi(gpus):
    """Fallback: no serving API. Report VRAM-consuming compute processes per GPU.
    Filters out OS/desktop noise and permission-denied entries (common on Windows,
    where the whole desktop touches the GPU). For boxes like Chonky (ComfyUI loads
    per-job) this answers 'is something actually using the card'."""
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

    # ignore desktop/OS processes and permission-denied placeholders
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
        # only keep entries with a real VRAM number
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


# ---------------------------------------------------------------------------
# MQTT publish with HA discovery
# ---------------------------------------------------------------------------
def slug(s):
    return "".join(c if c.isalnum() else "_" for c in str(s).lower()).strip("_")


def publish(client, gpus, models):
    """Publish discovery configs + states. Returns list of MQTTMessageInfo so the
    caller can wait for delivery before disconnecting (QoS 1)."""
    infos = []

    def pub(topic, payload):
        infos.append(client.publish(topic, payload, retain=True, qos=1))

    device_id = f"gpu_{slug(HOST)}"
    device_block = {
        "identifiers": [device_id],
        "name": f"GPUs {HOST}",
        "model": "NVIDIA GPU telemetry",
        "manufacturer": "gpu_telemetry.py",
    }

    # Each metric: (key, friendly suffix, unit, device_class, state_class, icon)
    metrics = [
        ("util",     "Utilization", "%",   None,          "measurement", "mdi:gauge"),
        ("mem_used", "Memory Used", "MiB", None,          "measurement", "mdi:memory"),
        ("mem_pct",  "Memory %",    "%",   None,          "measurement", "mdi:memory"),
        ("temp",     "Temperature", "°C",  "temperature", "measurement", "mdi:thermometer"),
        ("power",    "Power",       "W",   "power",       "measurement", "mdi:flash"),
    ]

    for gpu in gpus:
        idx = gpu["index"]
        base_id = f"{device_id}_gpu{idx}"
        # human label like "slimridge GPU0 (Tesla V100-SXM2-16GB)"
        gpu_label = f"{HOST} GPU{idx}"

        for key, suffix, unit, dclass, sclass, icon in metrics:
            uid = f"{base_id}_{key}"
            state_topic = f"gpu_telemetry/{slug(HOST)}/gpu{idx}/{key}"
            disc_topic = f"{DISCOVERY_PREFIX}/sensor/{uid}/config"
            cfg = {
                "name": f"{gpu_label} {suffix}",
                "unique_id": uid,
                "state_topic": state_topic,
                "unit_of_measurement": unit,
                "state_class": sclass,
                "icon": icon,
                "device": device_block,
            }
            if dclass:
                cfg["device_class"] = dclass
            pub(disc_topic, json.dumps(cfg))
            pub(state_topic, gpu[key])

        # loaded-models text sensor for this GPU
        model_label = models.get(idx, "unknown")
        uid = f"{base_id}_models"
        state_topic = f"gpu_telemetry/{slug(HOST)}/gpu{idx}/models"
        disc_topic = f"{DISCOVERY_PREFIX}/sensor/{uid}/config"
        cfg = {
            "name": f"{gpu_label} Loaded Models",
            "unique_id": uid,
            "state_topic": state_topic,
            "icon": "mdi:brain",
            "device": device_block,
        }
        pub(disc_topic, json.dumps(cfg))
        # state payloads have a 255-char limit for sensors; truncate to be safe
        pub(state_topic, model_label[:255])

    return infos


def run_once():
    """One publish cycle. Returns True on full success, False otherwise."""
    gpus = read_gpus()
    if not gpus:
        print("No GPUs found; nothing to publish.", file=sys.stderr, flush=True)
        return False
    dbg(f"found {len(gpus)} GPU(s):", [g["name"] for g in gpus])

    models = read_models(gpus)
    dbg("model labels:", models)
    dbg(f"connecting to MQTT {MQTT_HOST}:{MQTT_PORT} as user "
        f"'{MQTT_USER or '(anonymous)'}'")

    conn_result = {"rc": None}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        code = getattr(reason_code, "value", reason_code)
        conn_result["rc"] = code
        if code == 0:
            dbg("MQTT connected OK")
        else:
            print(f"MQTT connect FAILED, reason code {code} "
                  f"({reason_code})", file=sys.stderr, flush=True)

    def on_disconnect(client, userdata, *args):
        dbg("MQTT disconnected")

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        client = mqtt.Client()
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    if DEBUG:
        client.enable_logger()

    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    except Exception as e:
        print(f"MQTT connect() raised: {e}", file=sys.stderr, flush=True)
        return False

    client.loop_start()

    for _ in range(50):
        if conn_result["rc"] is not None:
            break
        time.sleep(0.1)
    if conn_result["rc"] is None:
        print("MQTT: no CONNACK within 5s (broker unreachable / dropping).",
              file=sys.stderr, flush=True)
    elif conn_result["rc"] != 0:
        print("MQTT: connection refused (bad user/pass?). Aborting cycle.",
              file=sys.stderr, flush=True)
        client.loop_stop()
        return False

    infos = publish(client, gpus, models)
    delivered = failed = 0
    for info in infos:
        try:
            info.wait_for_publish(timeout=5)
            if info.is_published():
                delivered += 1
            else:
                failed += 1
        except (ValueError, RuntimeError) as e:
            failed += 1
            dbg("wait_for_publish error:", e)

    client.loop_stop()
    client.disconnect()
    print(f"Published {len(gpus)} GPU(s) for {HOST}: "
          f"{delivered}/{len(infos)} confirmed"
          + (f", {failed} FAILED" if failed else ""), flush=True)
    return failed == 0


def main():
    # LOOP_INTERVAL set (seconds) -> run forever (for NSSM/Windows service).
    # Unset -> single shot (for systemd timer on Linux).
    loop_interval = os.environ.get("LOOP_INTERVAL", "").strip()
    if loop_interval:
        interval = float(loop_interval)
        print(f"Loop mode: publishing every {interval}s. Ctrl-C to stop.",
              flush=True)
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"cycle error (continuing): {e}", file=sys.stderr, flush=True)
            time.sleep(interval)
    else:
        ok = run_once()
        sys.exit(0 if ok else 4)


if __name__ == "__main__":
    main()
