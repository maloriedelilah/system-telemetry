#!/usr/bin/env python3
"""Linux host metrics via psutil + hwmon (lm-sensors).

The Linux counterpart to lhm.py: LibreHardwareMonitor is Windows-only, so on
Linux boxes (e.g. slimridge) host CPU/RAM/disk stats come from psutil and the
kernel hwmon sensors that psutil.sensors_temperatures() exposes.

read_host() returns the same dict shape the host half of lhm.read_host_and_gpus()
produces, so mqtt.publish_host() consumes it unchanged:

    {"cpus":  [{"name", "temp_c", "load_pct", "power_w"}],
     "ram":   {"used_gb", "total_gb", "pct"},
     "disks": [{"name", "temp_c", "used_pct"}]}

Sensor notes / gotchas:
  * CPU temp: AMD exposes "k10temp" (label Tctl/Tdie); Intel exposes "coretemp"
    with one "Package id N" per socket. NVMe temp shows up under "nvme"
    (label Composite); SATA drives need the `drivetemp` module loaded
    (`sudo modprobe drivetemp`) or they won't appear.
  * CPU power: no portable sysfs on AMD (amd_energy is gone) and Intel RAPL needs
    a timed energy delta — so power_w is left None on Linux (publish_host skips
    None) rather than publishing a bogus value.
  * Per-socket load isn't split on Linux; overall CPU% is attached to socket 0.

Standalone:  python -m telemetry.linux_host
"""

import sys

from . import config

dbg = config.dbg


def _import_psutil():
    import psutil  # imported lazily so non-host boxes don't need it installed
    return psutil


def _cpu_temps(psutil):
    """Return list of (label, temp_c), one per CPU socket. Best effort."""
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return []

    # Intel: coretemp, one "Package id N" entry per socket.
    if "coretemp" in temps:
        pkgs = [(e.label or "Package", e.current)
                for e in temps["coretemp"]
                if (e.label or "").lower().startswith("package")]
        if pkgs:
            return pkgs

    # AMD: k10temp / zenpower, single socket. Prefer Tctl/Tdie.
    for chip in ("k10temp", "zenpower", "k8temp"):
        if chip in temps:
            entries = temps[chip]
            pick = next((e.current for e in entries
                         if (e.label or "").lower() in ("tctl", "tdie", "tccd1")),
                        None)
            if pick is None and entries:
                pick = entries[0].current
            return [(chip, pick)]

    # Fallback: first available chip's first reading.
    for chip, entries in temps.items():
        if entries:
            return [(chip, entries[0].current)]
    return []


def _disk_temps(psutil):
    """Return list of (name, temp_c) for NVMe / SATA (drivetemp) sensors."""
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return []
    out = []
    for chip in ("nvme", "drivetemp"):
        for e in temps.get(chip, []):
            label = e.label or chip
            out.append((f"{chip} {label}".strip(), e.current))
    return out


def read_host():
    """Collect host metrics. Returns the host dict, or None if psutil is missing
    or nothing could be read. Never raises (mirrors lhm's contract)."""
    try:
        psutil = _import_psutil()
    except ImportError:
        print("psutil not installed — host stats unavailable on this box "
              "(re-run ./update.sh to pull the dependency).",
              file=sys.stderr, flush=True)
        return None

    try:
        # --- CPU ---------------------------------------------------------
        load = psutil.cpu_percent(interval=0.5)  # short blocking sample
        cpus = []
        for i, (name, temp) in enumerate(_cpu_temps(psutil) or [("CPU", None)]):
            cpus.append({
                "name": name,
                "temp_c": round(temp, 1) if temp is not None else None,
                "load_pct": round(load, 1) if i == 0 else None,
                "power_w": None,  # see module docstring
            })

        # --- RAM ---------------------------------------------------------
        vm = psutil.virtual_memory()
        ram = {
            "used_gb": round(vm.used / 1024**3, 1),
            "total_gb": round(vm.total / 1024**3, 1),
            "pct": round(vm.percent, 1),
        }

        # --- Disks -------------------------------------------------------
        disk_temps = _disk_temps(psutil)
        try:
            root_pct = psutil.disk_usage("/").percent
        except OSError:
            root_pct = None

        disks = []
        if disk_temps:
            # First temp sensor = boot/system drive; pair it with root usage.
            _, first_temp = disk_temps[0]
            disks.append({
                "name": "root",
                "temp_c": round(first_temp, 1) if first_temp is not None else None,
                "used_pct": round(root_pct, 1) if root_pct is not None else None,
            })
            # Any further drives: temperature only.
            for name, temp in disk_temps[1:]:
                disks.append({
                    "name": name,
                    "temp_c": round(temp, 1) if temp is not None else None,
                    "used_pct": None,
                })
        elif root_pct is not None:
            disks.append({"name": "root", "temp_c": None,
                          "used_pct": round(root_pct, 1)})

        host = {"cpus": cpus, "ram": ram, "disks": disks}
        dbg("linux_host:", host)
        return host
    except Exception as e:
        print(f"linux_host read failed: {e}", file=sys.stderr, flush=True)
        return None


if __name__ == "__main__":
    import json
    print(json.dumps(read_host(), indent=2, ensure_ascii=False))
