# system-telemetry

Cross-platform GPU + host telemetry for the homelab fleet. Reads NVIDIA cards
(`nvidia-smi`) and Windows host sensors (LibreHardwareMonitor), publishes to
Mosquitto with Home Assistant MQTT discovery so everything auto-appears in HA,
and (optionally) pushes the same readings to [Pane](#pane).

One codebase, one `.env` schema, one install path per OS. No per-machine folder
drift, no hand-edited copies. The repo is **private** (it describes too much of the
fleet to publish); each box is a checkout of a release tag, read with its own deploy key.

## First install (once per box)

The repo is private. Each box reads it over SSH with **its own read-only deploy key**, the
same way every app reaches its box (OPS-19). Prime will do this when it provisions boxes;
until then it's a one-time setup per box.

**1. Make the box's key** (Linux, as root):

```bash
sudo install -d -m 700 /etc/telemetry
sudo ssh-keygen -q -t ed25519 -N '' -C "telemetry@$(hostname)" -f /etc/telemetry/deploy_key
sudo cat /etc/telemetry/deploy_key.pub
```

On Windows, in an **elevated Git Bash** (the key lives beside the Pane token):

```bash
mkdir -p /c/ProgramData/telemetry/secrets
icacls 'C:\ProgramData\telemetry\secrets' /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'
icacls 'C:\ProgramData\telemetry\secrets' /setowner '*S-1-5-32-544'
ssh-keygen -q -t ed25519 -N '' -C "telemetry@$(hostname)" -f /c/ProgramData/telemetry/secrets/deploy_key
cat /c/ProgramData/telemetry/secrets/deploy_key.pub
```

**2. Add the `.pub`** on GitHub: the repo's Settings → Deploy keys → Add deploy key. Title it
with the box's name, and leave **Allow write access** off.

**3. Clone and install.** GitHub's host key is pinned, so ssh never asks (fingerprint
`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`, from api.github.com/meta):

```bash
KEY=/etc/telemetry/deploy_key     # Windows: /c/ProgramData/telemetry/secrets/deploy_key
DIR=/opt/telemetry                # Windows: /c/telemetry
KH="$(mktemp)"
echo 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' > "$KH"
sudo env GIT_SSH_COMMAND="ssh -F /dev/null -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile=$KH" \
    git clone -q git@github.com:maloriedelilah/system-telemetry.git "$DIR"
cd "$DIR" && sudo ./update.sh && sudo ./install.sh; rm -f "$KH"
```

On Windows the secrets folder is locked to SYSTEM and Administrators **before** the key is made,
so the key never inherits Users:RX.

(On Windows, drop the `sudo`.) `update.sh` checks out the newest release tag, and
`install.sh` asks for the MQTT broker (or `none` for a Pane-only box), the optional Pane URL,
which collectors this box runs (NVIDIA / LHM / Arc GPUs) and the model-label source. It then
writes `.env`, builds the venv and registers the service: a **systemd timer** on Linux
(single-shot every 30 s), an **NSSM loop** on Windows, started as LocalSystem.

Re-run to reconfigure:

```bash
cd /opt/telemetry   # or  cd /c/telemetry
sudo ./install.sh --reconfigure
```

## Update

```bash
cd /opt/telemetry   # or  cd /c/telemetry (elevated Git Bash)
sudo ./update.sh            # the newest vX.Y.Z tag
sudo ./update.sh v1.4.0     # or a specific one
```

It fetches the tag with the box's deploy key, checks it out, then re-runs as the new
`update.sh` to refresh deps and restart the service. It refuses an install with local changes
to tracked files (nothing is changed). `.env`, `venv/`, `secrets/` and `logs/` are git-ignored,
so an update never touches them.

**Every `vX.Y.Z` tag is code each box runs as root** at its next update (the checkout, then
pip and the restart). Protect them: on GitHub, Settings → Rules → Rulesets → New tag ruleset,
target `v*`, restrict creations, updates and deletions to the repo admin.

If an update stops after the checkout (pip or the restart failed), fix the cause and run
`./update.sh` again: it records each finished update, so it finishes instead of saying
"already there". A conversion that fails before its checkout removes the `.git` it started,
and one interrupted midway is resumed.

**A box installed from the old release tarball** (no `.git`; slimridge, Eighty-Eight,
SilverPancake) converts itself on its first run of the new `update.sh`. Make its deploy key
(steps 1 and 2), copy the new `update.sh` into the install dir once (scp it from a
workstation), and run it. Files that differ from the release are copied to `.pre-update-<time>/` and listed, then
replaced; `.env`, the venv, `secrets/` and the logs are kept.

## Cut a release (maintainer)

One command, from any clone of the repo, with `gh` authed:

```bash
./release.sh          # next patch  (v1.4.0 -> v1.4.1)
./release.sh minor    #             (v1.4.0 -> v1.5.0)
./release.sh v1.6.0   # a specific version, newer than the newest
```

GitHub tags main's current commit `vX.Y.Z` and publishes the release, with notes listing
the merged PRs. The tag is the version: nothing in the code is bumped, so nothing is
committed or pushed (main takes changes only through PRs). It releases GitHub's main as it
is (not your clone's files), only once main's CI has passed on that commit, and refuses if
main hasn't changed since the last release. Boxes pick up the newest tag with `./update.sh`.

## Layout

```
telemetry/          the package
  config.py         env/.env + shared helpers
  nvidia.py         nvidia-smi GPUs + loaded-model labels
  lhm.py            LibreHardwareMonitor reader + host/GPU adapter
  mqtt.py           HA discovery + state publishing
  pane.py           Influx line protocol + push to Pane
  __main__.py       orchestrator (python -m telemetry)
install.sh          interactive installer (prompts -> .env, venv, service)
update.sh           move to the newest release tag (deploy key) + deps + restart
release.sh          maintainer: tag main vX.Y.Z and publish the release (one command)
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

## Pane

Pane is the fleet's VictoriaMetrics backplane on the tailnet. With `PANE_URL` set,
every cycle pushes the data it just collected to Pane **in addition to** MQTT (the
two outputs are independent: if one is down the other still goes out, and the exit
status is non-zero if either failed).

Each box has its own bearer token. Pane's vmauth stamps `host=<box>` on whatever
that token writes and accepts only `POST /influx/write` (Influx line protocol) and
`/api/v1/import/prometheus`, so a box can't write as another.

| variable | meaning |
|----------|---------|
| `PANE_URL` | Pane's vmauth base URL, e.g. `http://100.92.61.42:8427`. Empty = Pane off. |
| `PANE_TOKEN_FILE` | file holding this box's token. Default `/opt/telemetry/secrets/pane_token` (Linux), `C:\ProgramData\telemetry\secrets\pane_token` (Windows). |
| `PANE_TIMEOUT` | seconds to wait on Pane per network operation (default 10). |

A box that only reports to Pane (a Linode, say) sets `MQTT_HOST=` (present but
empty) and no MQTT user/password; MQTT is then skipped. Leaving `MQTT_HOST` out of
`.env` entirely keeps the old default broker.

### Putting the token on a box

The token is never an environment variable, never in `.env`, never on a command
line and never logged; it is read from the file every cycle, so rotating it needs
no restart. Get one by running `add-box.sh <box-name>` on pane, then on the box:

```bash
# Linux. install.sh already created /opt/telemetry/secrets (0700, service user).
# <service-user> is the account the systemd unit runs as (see User= in the unit).
sudo -u <service-user> sh -c 'umask 077; cat > /opt/telemetry/secrets/pane_token'
#   paste the token, Enter, Ctrl-D
sudo chmod 0400 /opt/telemetry/secrets/pane_token
ls -l /opt/telemetry/secrets/pane_token     # -r-------- <service-user>
```

If the file is readable by group or other, each run prints a warning (and carries
on). If the install dir isn't `/opt/telemetry`, set `PANE_TOKEN_FILE` in `.env`.

On Windows the service runs as LocalSystem. Put the token in
`C:\ProgramData\telemetry\secrets\pane_token` and restrict the folder from an
elevated prompt:

```
icacls C:\ProgramData\telemetry\secrets /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"
```

(The installer doesn't create or touch this folder on Windows.)

### What is sent

Influx line protocol, one POST per cycle to `{PANE_URL}/influx/write`, with no
timestamps and no host tag (Pane adds both). VictoriaMetrics names each series
`<measurement>_<field>`; the names are fixed by Pane's fleet dashboard:

| line | fields |
|------|--------|
| `gpu,gpu=<index>,name=<gpu name>` | `temp_c util_percent mem_used_mb mem_total_mb mem_pct power_w` |
| `gpu_model,gpu=<index>,model=<label>` | `loaded=1` |
| `cpu,cpu=<name>` | `temp_c load_percent power_w` |
| `mem` | `used_gb total_gb used_percent` |
| `disk,disk=<name>` | `temp_c used_percent` |
| `telemetry` | `up=1 gpus=<n>` (heartbeat every cycle, even with no GPU) |

Readings that are missing or not finite are left out. The GPU memory percentage is
`mem_pct`, not `mem_used_*`, on purpose: the dashboard takes the max of every series
matching `.*gpu.*mem.*used.*`, so only `gpu_mem_used_mb` may match.

### Checking it is arriving

Open the fleet dashboard on Pane: the box should appear within a cycle or two, and
`telemetry_up` should be 1. On the box, a good cycle prints
`Pane: pushed <N> lines for <box> (204)` (journal: `journalctl -u system-telemetry`
on Linux, `logs/telemetry.out.log` on Windows). Failures print a block like

```
!!! Pane rejected the push
    Did:      POST http://100.92.61.42:8427/influx/write (<N> lines)
    Expected: 204
    Got:      401 ...
    Next:     the token is wrong or not added on Pane ...
```

on stderr (`telemetry.err.log` on Windows). Redirects are never followed, since that
would resend the token to wherever they point.

## Notes

- **`.env` is gitignored** — it holds `MQTT_PASS` (but never the Pane token, which
  lives in its own file). Never commit it. The installer
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
