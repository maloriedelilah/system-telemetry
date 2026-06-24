# GPU Telemetry → MQTT → Home Assistant

Per-GPU telemetry (utilization, VRAM, temperature, power) plus a "loaded models"
text sensor, pushed from each GPU box to a Mosquitto broker and auto-discovered
by Home Assistant. One pane of glass over a whole GPU fleet — including boxes at
other physical locations, reached over Tailscale.

**Deployed across:**
- **slimridge** — V100 (single), Ollama, Ubuntu → systemd timer
- **chonky** — RTX 3090, ComfyUI, Windows → NSSM service
- **eighty-eight** — 3× V100 + Quadro K2200, LM Studio, Windows, **remote (Tailscale)** → NSSM service

---

## Architecture

```
[each GPU box]
  gpu_telemetry.py
    ├─ nvidia-smi  → util / mem / temp / power per card
    └─ model API   → loaded model names (Ollama | LM Studio | process-view)
         │
         │  MQTT publish (HA discovery + state), QoS 1, retained configs
         ▼
[Dell / homehub]  Mosquitto broker  ──►  Home Assistant MQTT integration
                                          auto-creates "GPUs <host>" devices
```

Same script everywhere; behaviour differs only via environment variables.

---

## The script: `gpu_telemetry.py`

Cross-platform Python (Linux + Windows). Reads `nvidia-smi`, optionally queries a
model-serving API, publishes to MQTT with **Home Assistant MQTT discovery** so
sensors appear with no manual HA config.

Key env vars:

| Var | Purpose | Example |
|-----|---------|---------|
| `MQTT_HOST` | broker IP (LAN IP, or **Tailscale IP** for remote boxes) | `192.168.68.76` / `100.91.90.124` |
| `MQTT_PORT` | broker port | `1883` |
| `MQTT_USER` / `MQTT_PASS` | broker creds (an HA user works) | `gpu-telemetry` |
| `GPU_HOST_NAME` | friendly host label in HA | `slimridge` |
| `MODEL_SOURCE` | `ollama` \| `lmstudio` \| `none` | `lmstudio` |
| `MODEL_API_URL` | serving API base URL | `http://127.0.0.1:1234` |
| `MODEL_API_PATH` | model-list path (LM Studio differs from Ollama) | `/api/v0/models` |
| `MQTT_DISCOVERY_PREFIX` | **must match HA's discovery prefix** (default `homeassistant`) | `homeassistant` |
| `LOOP_INTERVAL` | if set (seconds) → loop forever (Windows); unset → one-shot (systemd) | `15` |
| `DEBUG` | `1` for verbose connection/delivery logging | `1` |

**Model source per box:**
- `ollama` → queries `{MODEL_API_URL}/api/ps` (slimridge)
- `lmstudio` → queries `{MODEL_API_URL}{MODEL_API_PATH}` for `state=loaded` (eighty-eight)
- `none` → nvidia-smi process-view, filtered (chonky / ComfyUI loads per-job)

Multi-GPU: hardware metrics are **per-card** (GPU0, GPU1, …). Model names can't be
mapped to a physical card by Ollama/LM Studio, so the model label is attributed to
GPU0 as a box-wide "what's loaded" indicator. Fine for "is it loaded at all".

---

## Deployment A — Linux / systemd (slimridge)

```bash
sudo apt install -y python3-pip
pip install paho-mqtt --break-system-packages

sudo mkdir -p /opt/gpu-telemetry
sudo cp gpu_telemetry.py /opt/gpu-telemetry/

sudo nano /etc/gpu-telemetry.env        # fill in ALL fields (see env template)
sudo chmod 600 /etc/gpu-telemetry.env
sudo chown root:root /etc/gpu-telemetry.env

# test by hand first (proves the whole pipeline)
sudo bash -c 'set -a; source /etc/gpu-telemetry.env; set +a; DEBUG=1 python3 /opt/gpu-telemetry/gpu_telemetry.py'
# want: "MQTT connected OK" and "N/N confirmed"

sudo cp gpu-telemetry.service gpu-telemetry.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-telemetry.timer
systemctl list-timers gpu-telemetry.timer   # confirm it's ticking (every 15s)
```

One-shot mode (no `LOOP_INTERVAL`): the timer fires the script every 15s.

## Deployment B — Windows / NSSM (chonky, eighty-eight)

1. Put `gpu_telemetry.py` + a `run-*.bat` (sets env, `LOOP_INTERVAL=15`) in `C:\gpu-telemetry\`.
2. `pip install paho-mqtt`
3. **Test the .bat by hand** in cmd first — want `Published N GPU(s): X/X confirmed`, no traceback.
4. Install as a service (admin cmd) — **run as your user, log from the start**:

```bat
nssm install GPUTelemetry "C:\gpu-telemetry\run-gpu-telemetry-<box>.bat"
nssm set GPUTelemetry AppDirectory "C:\gpu-telemetry"
nssm set GPUTelemetry Start SERVICE_AUTO_START
nssm set GPUTelemetry ObjectName ".\<WindowsUser>" "<password>"
nssm set GPUTelemetry AppStdout "C:\gpu-telemetry\service.log"
nssm set GPUTelemetry AppStderr "C:\gpu-telemetry\service.log"
nssm start GPUTelemetry
nssm status GPUTelemetry          # want SERVICE_RUNNING
```

Loop mode (`LOOP_INTERVAL=15`): the service runs the script continuously.

## Deployment C — Remote box over Tailscale (eighty-eight)

For a GPU box on a different network/location:
1. Put the **HA/broker box on the tailnet** via the **Tailscale ADD-ON** (HAOS appliance —
   not a normal install). Repo: `https://github.com/hassio-addons/repository`.
   ⚠️ In Tailscale admin → Machines → **disable key expiry** for the HA device, or it
   drops off the tailnet in 90 days and telemetry silently stops.
2. Set the remote box's `MQTT_HOST` to the **broker's Tailscale IP** (100.x.x.x), not its LAN IP.
3. Otherwise identical to Deployment B.

(This is separate from the Tailscale *integration*, which only monitors the tailnet read-only.)

---

## ⚠️ Hard-won gotchas (read before debugging)

- **Discovery prefix MUST match HA's.** HA auto-creates devices only from configs on
  the prefix it watches. With no prefix configured in HA, the default is the literal
  string `homeassistant` (NOT your instance name, even if you renamed HA to something
  else). Publishing to the wrong prefix = configs land on the broker but HA ignores
  them. Symptom: state data flows, but no device appears, and **no MQTT errors in the
  HA log** (it's not even trying). Fix: `MQTT_DISCOVERY_PREFIX=homeassistant`.
- **Fill in the WHOLE env file.** Missing `MQTT_USER` → connects anonymously → broker
  refuses (reason 135 "Not authorized"). Missing `MODEL_SOURCE` → wrong/empty model data.
- **NSSM: run as your user, not LocalSystem.** `pip install` lands in your *user*
  site-packages; LocalSystem can't see them → `ModuleNotFoundError: No module named
  'paho'` → service pauses. Fix: `nssm set ... ObjectName ".\User" "pass"`.
- **NEVER `taskkill /F /IM python.exe`** on a box with running Python. It can catch the
  interpreter mid-write and **zero the python.exe binary** (0 bytes → "this app can't
  run on your PC" / "access denied"). Recovery: python.org installer → Repair (needs the
  *matching* version). Stop the specific *service* cleanly instead.
- **Confirm publishing, not just "running."** A service/timer can report running while
  the script crashes each cycle. Check the log for `N/N confirmed` AND that HA sensors
  show fresh values.
- **paho-mqtt 2.x** changed `Client()` — script handles both 1.x/2.x. Fresh installs get 2.x.
- **Publish race:** publishes use QoS 1 + `wait_for_publish` before disconnect. Without
  that, fire-and-forget QoS 0 + immediate disconnect silently drops messages (script
  reports "published" but nothing arrives).
- **LM Studio:** its local server is OFF by default (Developer/Server tab → Start), and
  its model path differs from Ollama (`/api/v0/models`). Set `MODEL_API_URL` to LM
  Studio's port (default 1234) and `MODEL_API_PATH=/api/v0/models`.
- **Windows process-view (`MODEL_SOURCE=none`)** is noisy (whole desktop touches the GPU)
  and permission-limited; the script filters OS/desktop processes and shows
  "idle / no compute job" when nothing real is loaded.

---

## Debugging tools

- **DEBUG=1** on the script → connection result, CONNACK, per-message delivery count.
- **HA → Settings → Devices & Services → MQTT → Configure → "Listen to a topic":**
  - `gpu_telemetry/#` → are state messages arriving?
  - `<prefix>/sensor/gpu_<host>_gpu0_util/config` → is the discovery config there + retained?
- If config is present + retained but no device: prefix mismatch, or `unique_id` collision.
- If even a hand-published minimal config on `<prefix>/sensor/test/config` doesn't create
  an entity → HA isn't watching that prefix.

---

## Files

- `gpu_telemetry.py` — the cross-platform script
- `gpu-telemetry.env` — Linux env template (slimridge)
- `gpu-telemetry.service` / `gpu-telemetry.timer` — systemd units
- `run-gpu-telemetry-chonky.bat` — Windows env+launch (model_source=none)
- (make a `run-gpu-telemetry-eightyeight.bat` from chonky's: set host name,
  `MODEL_SOURCE=lmstudio`, `MODEL_API_URL=http://127.0.0.1:1234`,
  `MODEL_API_PATH=/api/v0/models`, and `MQTT_HOST=<broker tailscale IP>`)
