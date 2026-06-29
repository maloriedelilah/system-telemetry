#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install.sh — interactive installer. Runs identically on Linux and on Windows
# via Git Bash / msys. Normally invoked by bootstrap.sh, but also works directly:
#
#     cd /opt/telemetry        (or  cd /c/telemetry  in Git Bash)
#     ./install.sh             # first run: prompts and writes .env
#     ./install.sh --reconfigure   # re-prompt and overwrite .env
#
# First run with no .env: prompts for MQTT, roles, and model source, writes
# .env, builds the venv, and registers the service. Re-running with an existing
# .env keeps your config and just (re)builds the venv + service.
#
# Linux  -> venv + systemd service & timer (single-shot every 30s).
# Windows-> venv + NSSM service (long-running loop), started as LocalSystem.
#           The venv makes a run-as-user account unnecessary.
# ---------------------------------------------------------------------------
set -euo pipefail

SERVICE_NAME="system-telemetry"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TTY_R="${TELEMETRY_TTY_R:-/dev/tty}"      # terminal for reading answers (overridable for tests)
TTY_W="${TELEMETRY_TTY_W:-/dev/tty}"      # terminal for writing prompts
RECONFIGURE=0
[[ "${1:-}" == "--reconfigure" ]] && RECONFIGURE=1

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux*)               OS=linux ;;
    MINGW*|MSYS*|CYGWIN*) OS=windows ;;
    *) die "Unsupported OS: $(uname -s)" ;;
esac

# ---------------------------------------------------------------------------
# Linux: /opt + systemd need root. Re-exec under sudo, keeping the tty so
# prompts still work.
# ---------------------------------------------------------------------------
if [[ "$OS" == linux && "$INSTALL_DIR" == /opt/* && "$(id -u)" -ne 0 ]]; then
    say "Re-executing under sudo for /opt + systemd…"
    exec sudo -E bash "$INSTALL_DIR/install.sh" "$@" </dev/tty
fi

say "Detected OS: $OS"
say "Install dir: $INSTALL_DIR"

# ---------------------------------------------------------------------------
# Interactive prompt helpers (read the real terminal, not piped stdin).
# ---------------------------------------------------------------------------
# Prompts are written to FD 4, answers read from FD 3 — both bound to the
# terminal once (see write_env). Opening once and reading sequentially is the
# correct behavior for /dev/tty and keeps the flow testable.
ask() {  # ask <prompt> [default] -> echoes answer (or default)
    local prompt="$1" default="${2:-}" ans
    if [[ -n "$default" ]]; then printf '%s [%s]: ' "$prompt" "$default" >&4
    else printf '%s: ' "$prompt" >&4; fi
    read -r ans <&3 || ans=""
    echo "${ans:-$default}"
}
ask_secret() {  # ask_secret <prompt> -> echoes answer, no echo to terminal
    local prompt="$1" ans
    printf '%s: ' "$prompt" >&4
    read -rs ans <&3 || ans=""
    printf '\n' >&4
    echo "$ans"
}
ask_yn() {  # ask_yn <prompt> [y|n] -> returns 0 for yes
    local prompt="$1" default="${2:-y}" ans hint="[Y/n]"
    [[ "$default" == n ]] && hint="[y/N]"
    printf '%s %s: ' "$prompt" "$hint" >&4
    read -r ans <&3 || ans=""
    ans="${ans:-$default}"
    [[ "$ans" =~ ^[Yy] ]]
}

write_env() {
    exec 3<"$TTY_R" 4>"$TTY_W"     # bind terminal once for the whole prompt flow
    say "Let's configure this box. Press Enter to accept [defaults]."
    printf '\n' >&4

    local MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS HOSTNAME_FRIENDLY
    MQTT_HOST="$(ask 'MQTT broker host' '192.168.68.76')"
    MQTT_PORT="$(ask 'MQTT broker port' '1883')"
    MQTT_USER="$(ask 'MQTT username' 'gpu-telemetry')"
    while :; do
        MQTT_PASS="$(ask_secret 'MQTT password')"
        [[ -n "$MQTT_PASS" ]] && break
        printf '  password cannot be empty\n' >&4
    done
    HOSTNAME_FRIENDLY="$(ask 'Friendly host name (blank = OS hostname)' '')"

    # Roles
    local NVIDIA=0 LHM=0 LHM_GPUS=0 LHM_URL="http://localhost:8085/data.json" LHM_INC=""
    if ask_yn 'Collect NVIDIA GPU metrics (nvidia-smi)?' y; then NVIDIA=1; fi
    if [[ "$OS" == windows ]]; then
        if ask_yn 'Collect host stats via LibreHardwareMonitor?' y; then
            LHM=1
            LHM_URL="$(ask 'LibreHardwareMonitor JSON URL' "$LHM_URL")"
            if ask_yn 'Include non-NVIDIA GPUs from LHM (e.g. Arc)?' n; then
                LHM_GPUS=1
                LHM_INC="$(ask 'GPU-name substring to include (blank = all)' 'Arc')"
            fi
        fi
    else
        warn "LibreHardwareMonitor is Windows-only — host stats disabled on Linux."
    fi

    # Model source
    local MODEL_SOURCE MODEL_URL MODEL_PATH="/api/v0/models" MODEL_FILTER=""
    while :; do
        MODEL_SOURCE="$(ask 'Loaded-model labels source — ollama / lmstudio / none' 'none')"
        case "$MODEL_SOURCE" in ollama|lmstudio|none) break ;; esac
        printf '  enter one of: ollama, lmstudio, none\n' >&4
    done
    case "$MODEL_SOURCE" in
        ollama)
            MODEL_URL="$(ask 'Ollama API URL' 'http://127.0.0.1:11434')"
            MODEL_PATH="/api/ps" ;;
        lmstudio)
            MODEL_URL="$(ask 'LM Studio API URL' 'http://127.0.0.1:1234')"
            MODEL_PATH="/api/v0/models"
            MODEL_FILTER="$(ask 'Model-name filter, keeps only local models (blank = all)' '')" ;;
        none) MODEL_URL="http://127.0.0.1:11434" ;;
    esac

    # Loop cadence: Windows service loops; Linux timer handles cadence.
    local LOOP=""
    [[ "$OS" == windows ]] && LOOP=30

    exec 3<&- 4>&-                  # release the terminal
    umask 077   # .env holds the MQTT password
    cat > "$INSTALL_DIR/.env" <<ENV
# system-telemetry config — generated by install.sh. Never commit this file.
MQTT_HOST=$MQTT_HOST
MQTT_PORT=$MQTT_PORT
MQTT_USER=$MQTT_USER
MQTT_PASS=$MQTT_PASS
MQTT_DISCOVERY_PREFIX=homeassistant

TELEMETRY_HOST_NAME=$HOSTNAME_FRIENDLY

TELEMETRY_NVIDIA=$NVIDIA
TELEMETRY_LHM=$LHM
LHM_GPUS=$LHM_GPUS
LHM_URL=$LHM_URL
LHM_GPU_INCLUDE=$LHM_INC

MODEL_SOURCE=$MODEL_SOURCE
MODEL_API_URL=$MODEL_URL
MODEL_API_PATH=$MODEL_PATH
MODEL_FILTER=$MODEL_FILTER

LOOP_INTERVAL=$LOOP
DEBUG=0
ENV
    say "Wrote $INSTALL_DIR/.env"
}

# ---------------------------------------------------------------------------
# .env: prompt on first run or --reconfigure; otherwise keep existing.
# ---------------------------------------------------------------------------
if [[ ! -f "$INSTALL_DIR/.env" || "$RECONFIGURE" -eq 1 ]]; then
    write_env
else
    say "Existing .env found — keeping it (run ./install.sh --reconfigure to change)."
    # Safety: Windows loop service needs LOOP_INTERVAL set.
    if [[ "$OS" == windows ]] && ! grep -qE '^LOOP_INTERVAL=[0-9]' "$INSTALL_DIR/.env"; then
        warn "LOOP_INTERVAL blank in .env — setting 30 (Windows loop mode)."
        tmp="$INSTALL_DIR/.env.tmp"
        sed 's/^LOOP_INTERVAL=.*/LOOP_INTERVAL=30/' "$INSTALL_DIR/.env" > "$tmp"
        mv "$tmp" "$INSTALL_DIR/.env"
    fi
fi

# ===========================================================================
# LINUX — venv + systemd
# ===========================================================================
if [[ "$OS" == linux ]]; then
    RUN_USER="${SUDO_USER:-$USER}"
    command -v python3 >/dev/null || die "python3 not found."
    say "Creating venv…"
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
    chown -R "$RUN_USER" "$INSTALL_DIR/venv" 2>/dev/null || true
    # .env is written root-owned (umask 077 under the sudo re-exec), but the
    # service runs as $RUN_USER. Hand it over so config.py can read it; mode
    # stays 0600 so the MQTT password remains owner-only.
    chown "$RUN_USER" "$INSTALL_DIR/.env" 2>/dev/null || true

    say "Installing systemd unit + timer (run user: $RUN_USER)…"
    sed -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" -e "s#__RUN_USER__#$RUN_USER#g" \
        "$INSTALL_DIR/service/telemetry.service" \
        > "/etc/systemd/system/${SERVICE_NAME}.service"
    cp "$INSTALL_DIR/service/telemetry.timer" \
        "/etc/systemd/system/${SERVICE_NAME}.timer"
    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}.timer"
    say "Done. Timer active. Run now:  sudo systemctl start ${SERVICE_NAME}.service"
    say "Logs:  journalctl -u ${SERVICE_NAME}.service -f"
    exit 0
fi

# ===========================================================================
# WINDOWS — venv + NSSM
# ===========================================================================
if [[ "$OS" == windows ]]; then
    PY="$(command -v python || command -v py || true)"
    [[ -n "$PY" ]] || die "python not found on PATH (need a system Python to build the venv)."
    say "Creating venv…"
    "$PY" -m venv "$INSTALL_DIR/venv"
    VENV_PY="$INSTALL_DIR/venv/Scripts/python.exe"
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"
    mkdir -p "$INSTALL_DIR/logs"

    command -v nssm >/dev/null || die "nssm not found on PATH. Install NSSM, then re-run."

    win() { command -v cygpath >/dev/null && cygpath -w "$1" || echo "$1"; }
    WDIR="$(win "$INSTALL_DIR")"
    WVENV_PY="$(win "$VENV_PY")"
    WOUT="$(win "$INSTALL_DIR/logs/telemetry.out.log")"
    WERR="$(win "$INSTALL_DIR/logs/telemetry.err.log")"

    say "Registering NSSM service '$SERVICE_NAME'…"
    nssm install "$SERVICE_NAME" "$WVENV_PY" "-m" "telemetry" 2>/dev/null || \
        nssm set "$SERVICE_NAME" Application "$WVENV_PY"
    nssm set "$SERVICE_NAME" AppDirectory "$WDIR"
    nssm set "$SERVICE_NAME" AppParameters "-m telemetry"
    nssm set "$SERVICE_NAME" AppStdout "$WOUT"
    nssm set "$SERVICE_NAME" AppStderr "$WERR"
    nssm set "$SERVICE_NAME" Start SERVICE_AUTO_START

    say "Starting service (runs as LocalSystem — the venv makes that sufficient)…"
    nssm start "$SERVICE_NAME" 2>/dev/null || nssm restart "$SERVICE_NAME"

    say "Done."
    cat <<EOF
Logs:               $INSTALL_DIR/logs/telemetry.{out,err}.log
Reconfigure later:  ./install.sh --reconfigure
EOF
    exit 0
fi
