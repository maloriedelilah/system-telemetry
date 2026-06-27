#!/usr/bin/env python3
"""
lhm_reader.py — read host sensor stats from a LibreHardwareMonitor web server
(http://<host>:8085/data.json) and return a clean, flat metrics dict.

This is the cross-vendor companion to nvidia-smi: on Windows boxes (and anywhere
LHM runs) it supplies CPU/RAM/disk temps, and full GPU stats for cards that have
no nvidia-smi — notably the Intel Arc B580 on SilverPancake, which LHM exposes
with VRAM used/free/total, temp, power, load and fan.

Design notes / gotchas baked in:
  * Selection is by HardwareId PREFIX + sensor Type + sensor Text, NOT by the
    volatile numeric SensorId index, so it survives across machines and LHM
    versions.
  * LHM has a hardware node literally named "Virtual Memory" with HardwareId
    "/vram" — that is Windows commit charge, NOT GPU VRAM. We key RAM off "/ram"
    and GPU VRAM off the "/gpu-*" nodes, so "/vram" is ignored entirely.
  * Integrated GPUs (e.g. the 9950X's "AMD Radeon(TM) Graphics", 2 GB) show up
    alongside discrete cards. Set LHM_GPU_INCLUDE to a comma-separated list of
    name substrings to whitelist (e.g. "Arc" on SilverPancake); empty = all.

Standalone use (for testing):  python lhm_reader.py [path-to-data.json]
With no arg it fetches LHM_URL (default http://localhost:8085/data.json).
"""

import json
import os
import re
import sys
import urllib.request

LHM_URL = os.environ.get("LHM_URL", "http://localhost:8085/data.json")
# Comma-separated GPU-name substrings to include; empty = include every GPU.
GPU_INCLUDE = [s.strip().lower() for s in
               os.environ.get("LHM_GPU_INCLUDE", "").split(",") if s.strip()]

_VAL_RE = re.compile(r"^\s*(-?[\d]+(?:\.[\d]+)?)\s*(.*)$")


def parse_value(raw):
    """'76.0 °C' -> (76.0, '°C'); '1126 RPM' -> (1126.0, 'RPM'); '' -> (None,'')."""
    if raw is None:
        return None, ""
    m = _VAL_RE.match(str(raw))
    if not m:
        return None, str(raw).strip()
    return float(m.group(1)), m.group(2).strip()


def flatten(node, hw_id="", hw_name="", out=None):
    """Walk the LHM tree, returning leaf sensors as dicts:
    {hw_id, hw_name, text, type, sensor_id, value, unit}."""
    if out is None:
        out = []
    # A hardware node carries HardwareId; remember it as we descend.
    this_hw_id = node.get("HardwareId", hw_id)
    this_hw_name = node.get("Text", hw_name) if node.get("HardwareId") else hw_name
    sid = node.get("SensorId")
    if sid:  # leaf sensor
        val, unit = parse_value(node.get("RawValue", node.get("Value")))
        out.append({
            "hw_id": hw_id, "hw_name": hw_name,
            "text": node.get("Text", ""), "type": node.get("Type", ""),
            "sensor_id": sid, "value": val, "unit": unit,
        })
    for child in node.get("Children", []):
        flatten(child, this_hw_id, this_hw_name, out)
    return out


def _pick(sensors, hw_prefix, stype=None, text_any=None, text_exact=None):
    """First sensor matching the given hardware-id prefix / type / text rules."""
    for s in sensors:
        if not s["hw_id"].startswith(hw_prefix):
            continue
        if stype and s["type"] != stype:
            continue
        if text_exact is not None and s["text"] != text_exact:
            continue
        if text_any is not None and not any(t.lower() in s["text"].lower()
                                            for t in text_any):
            continue
        return s
    return None


def _hw_nodes(sensors, prefix):
    """Distinct (hw_id, hw_name) hardware nodes whose id starts with prefix,
    preserving discovery order."""
    seen, nodes = set(), []
    for s in sensors:
        if s["hw_id"].startswith(prefix) and s["hw_id"] not in seen:
            seen.add(s["hw_id"])
            nodes.append((s["hw_id"], s["hw_name"]))
    return nodes


def _v(sensor):
    return sensor["value"] if sensor else None


def collect_from_tree(tree):
    """Return structured host metrics from a parsed LHM data.json dict."""
    s = flatten(tree)
    result = {"cpu": {}, "cpus": [], "ram": {}, "gpus": [], "disks": []}

    # --- CPUs (AMD or Intel; one entry per socket) --------------------------
    # Dual-socket boards (e.g. dual Xeon) expose /intelcpu/0 AND /intelcpu/1,
    # so collect EVERY CPU node rather than just the first.
    for cpu_prefix in ("/amdcpu", "/intelcpu"):
        for hw_id, hw_name in _hw_nodes(s, cpu_prefix):
            temp = (_pick(s, hw_id, "Temperature", text_any=["Tctl/Tdie"])
                    or _pick(s, hw_id, "Temperature", text_any=["CPU Package",
                                                                "Core Max", "Core (Tctl)"])
                    or _pick(s, hw_id, "Temperature"))
            # AMD labels package power "Package"; Intel labels it "CPU Package".
            power = (_pick(s, hw_id, "Power", text_exact="Package")
                     or _pick(s, hw_id, "Power", text_any=["CPU Package"]))
            result["cpus"].append({
                "name": hw_name,
                "temp_c": _v(temp),
                "load_pct": _v(_pick(s, hw_id, "Load", text_exact="CPU Total")),
                "power_w": _v(power),
            })
    # Back-compat: keep result["cpu"] = first socket for any older caller.
    if result["cpus"]:
        result["cpu"] = result["cpus"][0]

    # --- RAM (HardwareId "/ram" — NOT "/vram") ------------------------------
    if _hw_nodes(s, "/ram"):
        used = _v(_pick(s, "/ram", "Data", text_exact="Memory Used"))
        avail = _v(_pick(s, "/ram", "Data", text_exact="Memory Available"))
        pct = _v(_pick(s, "/ram", "Load", text_exact="Memory"))
        total = round(used + avail, 1) if (used is not None and avail is not None) else None
        result["ram"] = {"used_gb": used, "total_gb": total, "pct": pct}

    # --- GPUs (nvidia / intel / amd discrete + integrated) ------------------
    for gpu_prefix in ("/gpu-nvidia", "/gpu-intel", "/gpu-amd"):
        for hw_id, hw_name in _hw_nodes(s, gpu_prefix):
            if GPU_INCLUDE and not any(t in hw_name.lower() for t in GPU_INCLUDE):
                continue
            used = _v(_pick(s, hw_id, text_exact="GPU Memory Used"))
            total = _v(_pick(s, hw_id, text_exact="GPU Memory Total"))
            temp = (_pick(s, hw_id, "Temperature", text_any=["GPU Core"])
                    or _pick(s, hw_id, "Temperature", text_any=["GPU Hot Spot",
                                                                "GPU VR SoC"])
                    or _pick(s, hw_id, "Temperature"))
            power = (_pick(s, hw_id, "Power", text_any=["GPU Package"])
                     or _pick(s, hw_id, "Power", text_any=["GPU Core", "GPU Power"])
                     or _pick(s, hw_id, "Power"))
            load = _pick(s, hw_id, "Load", text_exact="GPU Core")
            fan = _pick(s, hw_id, "Fan", text_any=["GPU Fan"])
            vram_pct = (round(100.0 * used / total, 1)
                        if used is not None and total else None)
            result["gpus"].append({
                "name": hw_name, "hw_id": hw_id,
                "temp_c": _v(temp), "power_w": _v(power), "load_pct": _v(load),
                "vram_used_mb": used, "vram_total_mb": total, "vram_pct": vram_pct,
                "fan_rpm": _v(fan),
            })

    # --- Storage (NVMe / generic) ------------------------------------------
    for disk_prefix in ("/nvme", "/hdd", "/ssd"):
        for hw_id, hw_name in _hw_nodes(s, disk_prefix):
            temp = (_pick(s, hw_id, "Temperature", text_any=["Composite"])
                    or _pick(s, hw_id, "Temperature"))
            used_pct = _pick(s, hw_id, "Load", text_any=["Used Space"])
            result["disks"].append({
                "name": hw_name, "hw_id": hw_id,
                "temp_c": _v(temp), "used_pct": _v(used_pct),
            })

    return result


def collect(url=None):
    """Fetch LHM data.json over HTTP and return structured metrics."""
    url = url or LHM_URL
    with urllib.request.urlopen(url, timeout=10) as r:
        tree = json.loads(r.read().decode())
    return collect_from_tree(tree)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = collect_from_tree(json.load(f))
    else:
        data = collect(LHM_URL)
    print(json.dumps(data, indent=2, ensure_ascii=False))
