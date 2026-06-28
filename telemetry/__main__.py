#!/usr/bin/env python3
"""Orchestrator / service entrypoint.

Reads role flags from config and runs the enabled collectors:
  TELEMETRY_NVIDIA -> nvidia.read_gpus + nvidia.read_models
  TELEMETRY_LHM    -> lhm.read_host_and_gpus (host temps; +GPUs if LHM_GPUS)

LOOP_INTERVAL set (seconds) -> run forever (NSSM/Windows service).
Unset -> single shot (systemd timer on Linux).

Run:  python -m telemetry
"""

import sys
import time

import paho.mqtt.client as mqtt_client

from . import config
from . import nvidia
from . import lhm
from . import mqtt as publisher

dbg = config.dbg


def run_once():
    """One publish cycle. Returns True on full success, False otherwise."""
    gpus = nvidia.read_gpus() if config.TELEMETRY_NVIDIA else []

    lhm_gpus, host = ([], None)
    if config.TELEMETRY_LHM:
        lhm_gpus, host = lhm.read_host_and_gpus()
    # Offset LHM GPU indices so they don't collide with nvidia indices.
    for i, g in enumerate(lhm_gpus):
        g["index"] = str(len(gpus) + i)
    gpus = gpus + lhm_gpus

    if not gpus and not host:
        print("No GPUs and no host data; nothing to publish "
              "(check TELEMETRY_NVIDIA / TELEMETRY_LHM).",
              file=sys.stderr, flush=True)
        return False
    dbg(f"found {len(gpus)} GPU(s):", [g["name"] for g in gpus],
        "| host stats:", bool(host))

    models = nvidia.read_models(gpus) if gpus else {}
    dbg("model labels:", models)
    dbg(f"connecting to MQTT {config.MQTT_HOST}:{config.MQTT_PORT} as user "
        f"'{config.MQTT_USER or '(anonymous)'}'")

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
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        client = mqtt_client.Client()
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    if config.DEBUG:
        client.enable_logger()

    if config.MQTT_USER:
        client.username_pw_set(config.MQTT_USER, config.MQTT_PASS)

    try:
        client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
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

    infos = []
    if gpus:
        infos += publisher.publish(client, gpus, models)
    if host:
        infos += publisher.publish_host(client, host)
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
    print(f"Published {len(gpus)} GPU(s)"
          + (" + host stats" if host else "")
          + f" for {config.HOST}: "
          f"{delivered}/{len(infos)} confirmed"
          + (f", {failed} FAILED" if failed else ""), flush=True)
    return failed == 0


def main():
    if config.LOOP_INTERVAL:
        interval = float(config.LOOP_INTERVAL)
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
