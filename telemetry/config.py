"""Configuration: environment variables, optional .env file, shared helpers.

The same .env schema is used on Linux and Windows (msys/mingw bash). The service
manager (systemd EnvironmentFile / NSSM AppEnvironmentExtra) injects these into
the process environment; for manual runs we also parse a .env sitting next to the
installed package so `python -m telemetry` behaves identically from a shell.
"""

import os
import socket
import sys


# ---------------------------------------------------------------------------
# .env loader (no external dependency). Service-provided env always wins.
# ---------------------------------------------------------------------------
def _load_dotenv():
    """Parse a .env next to the package root (one dir up) if present.

    Only sets keys not already in os.environ, so values injected by systemd/NSSM
    take precedence over the file. Silent if the file is missing or malformed.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass
    except Exception as e:  # never let config parsing crash the service
        print(f"[config] .env parse warning: {e}", file=sys.stderr, flush=True)


_load_dotenv()


def _flag(name, default="0"):
    return os.environ.get(name, default) not in ("0", "", "false", "False")


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
MQTT_HOST = os.environ.get("MQTT_HOST", "homehub.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
# Friendly host name used in entity names/ids. Defaults to the actual hostname.
HOST = os.environ.get("TELEMETRY_HOST_NAME",
                      os.environ.get("GPU_HOST_NAME",
                                     socket.gethostname())).lower()

# ---------------------------------------------------------------------------
# Roles — explicit per machine (replaces inferring LHM from LHM_URL presence)
# ---------------------------------------------------------------------------
TELEMETRY_NVIDIA = _flag("TELEMETRY_NVIDIA", "1")   # run nvidia-smi collector
TELEMETRY_LHM = _flag("TELEMETRY_LHM", "0")         # run LibreHardwareMonitor reader
LHM_GPUS = _flag("LHM_GPUS", "0")                   # include GPUs from LHM (Arc boxes)

# ---------------------------------------------------------------------------
# Model-name source: ollama | lmstudio | none
# ---------------------------------------------------------------------------
MODEL_SOURCE = os.environ.get("MODEL_SOURCE", "none").lower()
MODEL_API_URL = os.environ.get("MODEL_API_URL", "http://127.0.0.1:11434")
MODEL_API_PATH = os.environ.get("MODEL_API_PATH", "/api/v0/models")
# Locality filter: comma-separated, case-insensitive substrings. When set, only
# loaded models whose id contains one of these are reported (LM Studio's `lms
# link` surfaces models from other linked boxes with no host field). Empty = all.
MODEL_FILTER = [s.strip().lower() for s in
                os.environ.get("MODEL_FILTER", "").split(",") if s.strip()]

# ---------------------------------------------------------------------------
# LibreHardwareMonitor
# ---------------------------------------------------------------------------
# Web-server JSON endpoint, e.g. http://localhost:8085/data.json
LHM_URL = os.environ.get("LHM_URL", "http://localhost:8085/data.json").strip()
# Optional include-filter for LHM GPU names (e.g. "Arc" to pick only the B580).
LHM_GPU_INCLUDE = os.environ.get("LHM_GPU_INCLUDE", "").strip()

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
# LOOP_INTERVAL set (seconds) -> run forever (NSSM/Windows). Unset -> single shot
# (systemd timer on Linux).
LOOP_INTERVAL = os.environ.get("LOOP_INTERVAL", "").strip()
DEBUG = _flag("DEBUG", "0")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def slug(s):
    return "".join(c if c.isalnum() else "_" for c in str(s).lower()).strip("_")


def dbg(*a):
    if DEBUG:
        print("[debug]", *a, file=sys.stderr, flush=True)
