#!/usr/bin/env python3
"""Orchestrator / service entrypoint.

Reads role flags from config and runs the enabled collectors:
  TELEMETRY_NVIDIA -> nvidia.read_gpus + nvidia.read_models (auto: whenever nvidia-smi exists)
  TELEMETRY_LHM    -> lhm.read_host_and_gpus (host temps; +GPUs if LHM_GPUS)
  TELEMETRY_HOST   -> linux_host.read_host (Linux psutil/hwmon)
  TELEMETRY_SITES  -> sites.probe_all (public sites' /api/health; Pane output only)
  HEARTBEATS_DIR   -> heartbeats.read_all (status files from occasional jobs; Pane output only)

Data is collected once, then sent to each enabled output independently:
  PANE_URL set  -> pane.push   (Influx line protocol to Pane)
  MQTT_HOST set -> HA discovery + states over MQTT

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
from . import linux_host
from . import mqtt as publisher
from . import pane
from . import sites
from . import heartbeats

dbg = config.dbg


def run_once():
    """One publish cycle. Returns True only if every enabled output succeeded."""
    gpus = nvidia.read_gpus() if config.TELEMETRY_NVIDIA else []
    nvidia_gpus = list(gpus)   # model labels are looked up for these; LHM's GPUs have none

    lhm_gpus, host = ([], None)
    if config.TELEMETRY_LHM:
        lhm_gpus, host = lhm.read_host_and_gpus()
    # Offset LHM GPU indices so they don't collide with nvidia indices.
    for i, g in enumerate(lhm_gpus):
        g["index"] = str(len(gpus) + i)
    gpus = gpus + lhm_gpus

    # Linux host stats (psutil/lm-sensors) — LHM is Windows-only, so this is how
    # Linux boxes populate the "Host <host>" device.
    if host is None and config.TELEMETRY_HOST and config.IS_LINUX:
        host = linux_host.read_host()

    # Site probes go to Pane only (Home Assistant has no use for them). A probe never raises,
    # but a bug here must not cost the host and GPU data, so it is fenced off like an output.
    site_results, site_control = [], None
    if config.TELEMETRY_SITES and config.LOOP_INTERVAL:
        # A probe stuck past its deadline is abandoned in a daemon thread, which only ends when
        # the process does: fine for the oneshot timer, a slow leak in loop mode (Errata, OPS-59).
        config.report("Site probes are off in loop mode", "read TELEMETRY_SITES and LOOP_INTERVAL",
                      "TELEMETRY_SITES=1 only with the systemd timer (LOOP_INTERVAL empty)",
                      f"LOOP_INTERVAL={config.LOOP_INTERVAL}",
                      "run them on a Linux box with the timer (pane), or set TELEMETRY_SITES=0")
    elif config.TELEMETRY_SITES:
        try:
            site_results, site_control = sites.probe_all()
        except Exception as e:
            config.report("Site probes crashed", "probe the sites in SITES_FILE",
                          "one result per site", f"{type(e).__name__}: {e}",
                          "this is a bug in telemetry/sites.py; host and GPU data still go out")

    # Status files from occasional jobs go to Pane only. Fenced off like an output: a bug here must
    # not cost the host and GPU data.
    beats = []
    if config.HEARTBEATS_DIR:
        try:
            beats = heartbeats.read_all()
        except Exception as e:
            config.report("Heartbeat files crashed", f"read {config.HEARTBEATS_DIR}",
                          "one entry per check", f"{type(e).__name__}: {e}",
                          "this is a bug in telemetry/heartbeats.py; host and GPU data still go out")

    if not gpus and not host and not site_results and site_control is None and not beats:
        print("No GPUs, no host data, no site probes and no heartbeat files; nothing to publish "
              "(check TELEMETRY_NVIDIA / TELEMETRY_LHM / TELEMETRY_HOST / TELEMETRY_SITES / "
              "HEARTBEATS_DIR).",
              file=sys.stderr, flush=True)
        return False
    dbg(f"found {len(gpus)} GPU(s):", [g["name"] for g in gpus],
        "| host stats:", bool(host))

    models = nvidia.read_models(nvidia_gpus) if nvidia_gpus else {}
    dbg("model labels:", models)

    if not config.PANE_URL and not config.MQTT_HOST:
        config.report(
            "No output is configured",
            "looked for PANE_URL and MQTT_HOST",
            "at least one of them set",
            "both are empty",
            "set PANE_URL (push to Pane) and/or MQTT_HOST (Home Assistant) in .env")
        return False

    # Outputs are independent: each runs in its own try/except so one failing
    # (broker refused, Pane down, timeout) never stops the other. Pane goes first;
    # it is quick and doesn't depend on the broker.
    results = []
    if config.PANE_URL:
        try:
            results.append(pane.push(pane.to_lines(gpus, models, host)
                                     + pane.site_lines(site_results, site_control)
                                     + pane.heartbeat_lines(beats)))
        except Exception as e:
            config.report("Pane output crashed", "build and push the Pane lines",
                          "a push attempt", f"{type(e).__name__}: {e}",
                          "this is a bug in telemetry/pane.py; the MQTT output is unaffected")
            results.append(False)
    if config.MQTT_HOST:
        try:
            results.append(publish_mqtt(gpus, models, host))
        except Exception as e:
            config.report("MQTT output crashed",
                          f"publish to {config.MQTT_HOST}:{config.MQTT_PORT}",
                          "published states", f"{type(e).__name__}: {e}",
                          "check the broker is reachable; the Pane output is unaffected")
            results.append(False)
    return all(results)


def publish_mqtt(gpus, models, host):
    """The MQTT output: HA discovery + states. True if everything was confirmed."""
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
