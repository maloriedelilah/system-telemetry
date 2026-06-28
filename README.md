# system-telemetry

Cross-platform GPU + host telemetry for the homelab fleet. Reads NVIDIA cards
(`nvidia-smi`) and Windows host sensors (LibreHardwareMonitor), publishes to
Mosquitto with Home Assistant MQTT discovery so everything auto-appears in HA.

One codebase, one `.env` schema, one install path per OS. No more per-machine
folder drift or hand-edited copies.

## Layout

```
telemetry/          the package
  config.py         env/.env + shared helpers
  nvidia.py         nvidia-smi GPUs + loaded-model labels
  lhm.py            LibreHardwareMonitor reader + host/GPU adapter
  mqtt.py           HA discovery + state publishing
  __main__.py       orchestrator (python -m telemetry)
install.sh          OS-detecting installer (venv + service)
update.sh           git pull + deps + restart
.env.example        canonical config schema
service/            systemd unit/timer + NSSM reference
```

## Install

```bash
git clone git@github.com:maloriedelilah/system-telemetry.git
# Linux:
sudo mv system-telemetry /opt/telemetry && cd /opt/telemetry
# Windows (msys bash):  put it at C:\telemetry, then cd /c/telemetry
./install.sh
```

`install.sh` makes the venv, installs deps, creates `.env` from the example, and
registers the service — **systemd timer** on Linux (single-shot every 30s),
**NSSM loop** on Windows. Edit `.env`, then start the service (Linux: the timer
is already enabled; Windows: run the two printed `nssm` commands — they need your
account password).

## Update

```bash
cd /opt/telemetry   # or  cd /c/telemetry
./update.sh
```

Pulls latest, refreshes deps, triggers/restarts the service. Auth is via the
clone's deploy key.

## Roles (per-machine `.env`)

Each box sets what it runs. The collectors are independent flags:

| machine       | NVIDIA | LHM | LHM_GPUS | MODEL_SOURCE | notes |
|---------------|:------:|:---:|:--------:|--------------|-------|
| slimridge     |   1    |  0  |    0     | ollama       | Linux, single V100 |
| Chonky        |   1    |  0* |    0     | none         | RTX 3090; LHM optional for host temps |
| Eighty-Eight  |   1    |  1  |    0     | lmstudio     | dual Xeon + 3×V100 + K2200; LHM = host temps only |
| SilverPancake |   0    |  1  |    1     | lmstudio     | Arc B580 has no nvidia-smi; LHM supplies the GPU |

`*` flip to 1 whenever you want Chonky's CPU/RAM/disk temps too.

## Notes

- **`.env` is gitignored** — it holds `MQTT_PASS`. Never commit it.
- **Single- vs dual-socket** is automatic: socket 0 keeps bare `cpu_*` entities
  (no churn), extra sockets become `cpu1_*`, `cpu2_*`.
- **HA discovery prefix is `homeassistant`** (the default) even though the HA
  instance is named cooperhome.
- **Windows service must run as your user account**, not LocalSystem — the venv
  and `localhost:8085` LHM endpoint aren't reachable otherwise.
- LHM `/vram` is Windows commit charge, not GPU VRAM; the reader keys RAM off
  `/ram` and GPU VRAM off `/gpu-*`.
