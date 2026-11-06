# system-telemetry

Cross-platform GPU + host telemetry for the homelab fleet. Reads NVIDIA cards
(`nvidia-smi`) and Windows host sensors (LibreHardwareMonitor), publishes to
Mosquitto with Home Assistant MQTT discovery so everything auto-appears in HA.

One codebase, one `.env` schema, one install path per OS. No per-machine folder
drift, no hand-edited copies, no keys — the repo is public and ships versioned
releases.

## Install (one command)

In a shell on Linux, or **Git Bash** on Windows:

```bash
curl -fsSL https://raw.githubusercontent.com/maloriedelilah/system-telemetry/main/bootstrap.sh | bash
```

That downloads the latest release, drops it at `/opt/telemetry` (Linux) or
`C:\telemetry` (Windows), and runs the interactive installer. It asks for the
MQTT broker, which collectors this box runs (NVIDIA / LHM / Arc GPUs), and the
model-label source, then writes `.env`, builds the venv, and registers the
service — **systemd timer** on Linux (single-shot every 30s), **NSSM loop** on
Windows.

On Windows the installer registers the NSSM service and starts it as LocalSystem
— no password step, the service is live when the installer finishes. (Run the
installer from an elevated Git Bash so NSSM can register the service.)

Re-run to reconfigure:

```bash
cd /opt/telemetry   # or  cd /c/telemetry
./install.sh --reconfigure
```

## Update

```bash
cd /opt/telemetry   # or  cd /c/telemetry
./update.sh
```

Pulls the latest published release over the install (your `.env`, venv, and logs
are preserved), refreshes deps, and restarts/triggers the service. No git on the
box — it just re-fetches the release tarball.

## Cut a release (maintainer)

From a clone of the repo, with `gh` authed:

```bash
./release.sh          # bump patch  (1.0.0 -> 1.0.1)
./release.sh minor    #             (1.0.0 -> 1.1.0)
./release.sh 1.4.2    # explicit version
```

Bumps `telemetry/__init__.py`, tags `vX.Y.Z`, pushes, and publishes
`system-telemetry.tar.gz` as the release asset marked *latest* — which is what
`bootstrap.sh` and `update.sh` pull, keylessly, on every box.

## Layout

```
telemetry/          the package
  config.py         env/.env + shared helpers
  nvidia.py         nvidia-smi GPUs + loaded-model labels
  lhm.py            LibreHardwareMonitor reader + host/GPU adapter
  mqtt.py           HA discovery + state publishing
  __main__.py       orchestrator (python -m telemetry)
bootstrap.sh        one-command remote installer (curl | bash entry point)
install.sh          interactive installer (prompts -> .env, venv, service)
update.sh           pull latest release + deps + restart
release.sh          maintainer: bump, tag, publish release asset
.env.example        config schema reference (the installer writes the real .env)
service/            systemd unit/timer + NSSM reference
```

## Roles (per-machine, set during install)

Each box sets what it runs. The collectors are independent flags:

| machine       | NVIDIA | LHM | LHM_GPUS | MODEL_SOURCE | notes |
|---------------|:------:|:---:|:--------:|--------------|-------|
| slimridge     |   1    |  0  |    0     | docker       | Linux, single V100; llama.cpp embed + rerank in Docker (user in `docker` group) |
| Chonky        |   1    |  0* |    0     | none         | RTX 3090; LHM optional for host temps |
| Eighty-Eight  |   1    |  1  |    0     | lmstudio     | dual Xeon + 3×V100 + K2200; LHM = host temps only (`llamaserver` fits its two llama-server instances on :1234/:1235) |
| SilverPancake |   0    |  1  |    1     | lmstudio     | Arc B580 has no nvidia-smi; LHM supplies the GPU |

`*` flip to 1 whenever you want Chonky's CPU/RAM/disk temps too.

## Model-label sources

`MODEL_SOURCE` picks where the "loaded models" label comes from:

| source        | reads | `MODEL_API_URL` |
|---------------|-------|-----------------|
| `ollama`      | `GET /api/ps` | the Ollama base URL |
| `lmstudio`    | `GET <MODEL_API_PATH>` (LM Studio's REST API) | the LM Studio base URL |
| `docker`      | `docker ps` / `docker inspect` (service user in the `docker` group) | unused |
| `llamaserver` | `GET <url>/v1/models` on each llama.cpp `llama-server` instance, using each `data[].id` | comma-separated base URLs, e.g. `http://127.0.0.1:1234,http://127.0.0.1:1235` |
| `none`        | VRAM-using compute processes from `nvidia-smi` | unused |

The API-based sources can't tell which card a model sits on, so they report the
full set against GPU 0. `MODEL_FILTER` (comma-separated substrings) keeps only
matching labels. With `llamaserver`, an instance that doesn't answer is reported
on stderr and skipped; the other instances' labels still go out.

## Notes

- **`.env` is gitignored** — it holds `MQTT_PASS`. Never commit it. The installer
  writes it with `umask 077` (owner-readable only).
- **Single- vs dual-socket** is automatic: socket 0 keeps bare `cpu_*` entities
  (no churn), extra sockets become `cpu1_*`, `cpu2_*`.
- **HA discovery prefix is `homeassistant`** (the default) even though the HA
  instance is named cooperhome.
- **Windows service runs as LocalSystem** — the venv bundles its own deps, and
  the LHM (`localhost:8085`) and LM Studio (`localhost:1234`) endpoints are
  reachable from any account, so no run-as-user step is needed. Bonus: it
  survives Windows password changes.
- LHM `/vram` is Windows commit charge, not GPU VRAM; the reader keys RAM off
  `/ram` and GPU VRAM off `/gpu-*`.
