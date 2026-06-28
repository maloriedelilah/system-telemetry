"""MQTT publishing with Home Assistant discovery.

publish(client, gpus, models) - GPU device ("GPUs <host>"): per-GPU util/mem/temp/
                                power + loaded-models text sensor.
publish_host(client, host)    - Host device ("Host <host>"): per-socket CPU, RAM,
                                per-disk. Socket 0 keeps bare "cpu_*" uids so
                                single-socket boxes never churn; extra sockets
                                become cpu1_*, cpu2_*, ...
Both return a list of MQTTMessageInfo (QoS 1) so the caller can confirm delivery.
"""

import json

from . import config

HOST = config.HOST
DISCOVERY_PREFIX = config.DISCOVERY_PREFIX
slug = config.slug
dbg = config.dbg


def publish(client, gpus, models):
    """Publish GPU discovery configs + states. Returns list of MQTTMessageInfo."""
    infos = []

    def pub(topic, payload):
        infos.append(client.publish(topic, payload, retain=True, qos=1))

    device_id = f"gpu_{slug(HOST)}"
    device_block = {
        "identifiers": [device_id],
        "name": f"GPUs {HOST}",
        "model": "NVIDIA GPU telemetry",
        "manufacturer": "system-telemetry",
    }

    # (key, friendly suffix, unit, device_class, state_class, icon)
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
        gpu_label = f"{HOST} GPU{idx}"

        for key, suffix, unit, dclass, sclass, icon in metrics:
            uid = f"{base_id}_{key}"
            state_topic = f"gpu_telemetry/{slug(HOST)}/gpu{idx}/{key}"
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
            pub(f"{DISCOVERY_PREFIX}/sensor/{uid}/config", json.dumps(cfg))
            pub(state_topic, gpu[key])

        # loaded-models text sensor for this GPU
        model_label = models.get(idx, "unknown")
        uid = f"{base_id}_models"
        state_topic = f"gpu_telemetry/{slug(HOST)}/gpu{idx}/models"
        cfg = {
            "name": f"{gpu_label} Loaded Models",
            "unique_id": uid,
            "state_topic": state_topic,
            "icon": "mdi:brain",
            "device": device_block,
        }
        pub(f"{DISCOVERY_PREFIX}/sensor/{uid}/config", json.dumps(cfg))
        pub(state_topic, str(model_label)[:255])  # sensor state 255-char cap

    return infos


def publish_host(client, host):
    """Publish CPU/RAM/disk stats to a separate 'Host <host>' device.
    Skips any metric whose value is None (sensor not present)."""
    infos = []

    def pub(topic, payload):
        infos.append(client.publish(topic, payload, retain=True, qos=1))

    device_id = f"host_{slug(HOST)}"
    device_block = {
        "identifiers": [device_id],
        "name": f"Host {HOST}",
        "model": "Host telemetry (LibreHardwareMonitor)",
        "manufacturer": "system-telemetry",
    }
    base = f"host_telemetry/{slug(HOST)}"

    def sensor(uid_suffix, name, unit, dclass, icon, state_topic, value):
        if value is None:
            return
        uid = f"{device_id}_{uid_suffix}"
        cfg = {
            "name": f"{HOST} {name}",
            "unique_id": uid,
            "state_topic": state_topic,
            "unit_of_measurement": unit,
            "state_class": "measurement",
            "icon": icon,
            "device": device_block,
        }
        if dclass:
            cfg["device_class"] = dclass
        pub(f"{DISCOVERY_PREFIX}/sensor/{uid}/config", json.dumps(cfg))
        pub(state_topic, value)

    # One set of CPU sensors per socket. Socket 0 keeps the original "cpu_*"
    # uids/topics (single-socket boxes don't churn); extra sockets become
    # cpu1_*, cpu2_*, ... Friendly names carry an index only when multi-socket.
    cpus = host.get("cpus") or []
    multi = len(cpus) > 1
    dbg(f"publish_host: {len(cpus)} CPU socket(s); multi={multi}; "
        f"names={[c.get('name') for c in cpus]}")
    for i, cpu in enumerate(cpus):
        pfx = "cpu" if i == 0 else f"cpu{i}"
        label = f"CPU{i}" if multi else "CPU"
        sensor(f"{pfx}_temp", f"{label} Temperature", "°C", "temperature",
               "mdi:thermometer", f"{base}/{pfx}/temp", cpu.get("temp_c"))
        sensor(f"{pfx}_load", f"{label} Load", "%", None, "mdi:gauge",
               f"{base}/{pfx}/load", cpu.get("load_pct"))
        sensor(f"{pfx}_power", f"{label} Power", "W", "power", "mdi:flash",
               f"{base}/{pfx}/power", cpu.get("power_w"))

    ram = host.get("ram") or {}
    sensor("ram_used", "RAM Used", "GB", "data_size", "mdi:memory",
           f"{base}/ram/used", ram.get("used_gb"))
    sensor("ram_pct", "RAM %", "%", None, "mdi:memory",
           f"{base}/ram/pct", ram.get("pct"))

    for d in host.get("disks") or []:
        dname = d.get("name", "disk")
        dslug = slug(dname)
        sensor(f"disk_{dslug}_temp", f"{dname} Temperature", "°C", "temperature",
               "mdi:harddisk", f"{base}/disk/{dslug}/temp", d.get("temp_c"))
        sensor(f"disk_{dslug}_used", f"{dname} Used", "%", None,
               "mdi:harddisk", f"{base}/disk/{dslug}/used", d.get("used_pct"))

    return infos
