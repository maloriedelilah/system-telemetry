#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# system-telemetry installer. Runs identically on Linux and on Windows via
# msys/mingw bash. Run it from inside the cloned repo:
#
#     git clone git@github.com:maloriedelilah/system-telemetry.git
#     cd system-telemetry
#     ./install.sh
#
# Linux  -> venv + systemd service & timer (single-shot every 30s).
# Windows-> venv + NSSM service (long-running loop). Prints the run-as-user
#           + start commands at the end (those need your password).
# ---------------------------------------------------------------------------
set -euo pipefail

SERVICE_NAME="system-telemetry"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux*)                       OS=linux ;;
    MINGW*|MSYS*|CYGWIN*)         OS=windows ;;
    *) die "Unsupported OS: $(uname -s)" ;;
esac
say "Detected OS: $OS"
say "Install dir: $INSTALL_DIR"

# ---------------------------------------------------------------------------
# .env — create from example on first install, then require editing.
# ---------------------------------------------------------------------------
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn "Created .env from .env.example — edit it (MQTT_PASS, roles) before the"
    warn "service will work. Re-run install.sh afterward, or just start the service."
fi

# ===========================================================================
# LINUX
# ===========================================================================
if [[ "$OS" == "linux" ]]; then
    # /opt and systemd need root; re-exec under sudo if necessary.
    if [[ "$INSTALL_DIR" == /opt/* && "$(id -u)" -ne 0 ]]; then
        say "Re-executing under sudo for /opt + systemd…"
        exec sudo -E bash "$INSTALL_DIR/install.sh" "$@"
    fi
    RUN_USER="${SUDO_USER:-$USER}"

    command -v python3 >/dev/null || die "python3 not found."
    say "Creating venv…"
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
    # venv owned by root after sudo; let the run-user read/execute it.
    chown -R "$RUN_USER" "$INSTALL_DIR/venv" 2>/dev/null || true

    say "Installing systemd unit + timer (run user: $RUN_USER)…"
    sed -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" -e "s#__RUN_USER__#$RUN_USER#g" \
        "$INSTALL_DIR/service/telemetry.service" \
        > "/etc/systemd/system/${SERVICE_NAME}.service"
    cp "$INSTALL_DIR/service/telemetry.timer" \
        "/etc/systemd/system/${SERVICE_NAME}.timer"
    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}.timer"
    say "Done. Timer active. Run now with:  sudo systemctl start ${SERVICE_NAME}.service"
    say "Logs:  journalctl -u ${SERVICE_NAME}.service -f"
    exit 0
fi

# ===========================================================================
# WINDOWS (msys/mingw bash)
# ===========================================================================
if [[ "$OS" == "windows" ]]; then
    PY="$(command -v python || command -v py || true)"
    [[ -n "$PY" ]] || die "python not found on PATH (need a system Python to build the venv)."
    say "Creating venv…"
    "$PY" -m venv "$INSTALL_DIR/venv"
    VENV_PY="$INSTALL_DIR/venv/Scripts/python.exe"
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"

    mkdir -p "$INSTALL_DIR/logs"

    # Ensure LOOP_INTERVAL is set on Windows (NSSM runs a long-lived loop).
    if ! grep -qE '^LOOP_INTERVAL=[0-9]' "$INSTALL_DIR/.env"; then
        warn "LOOP_INTERVAL is blank in .env — setting it to 30 (Windows loop mode)."
        # portable in-place edit without sed -i quirks on msys
        tmp="$INSTALL_DIR/.env.tmp"
        sed 's/^LOOP_INTERVAL=.*/LOOP_INTERVAL=30/' "$INSTALL_DIR/.env" > "$tmp"
        mv "$tmp" "$INSTALL_DIR/.env"
    fi

    command -v nssm >/dev/null || die "nssm not found on PATH. Install NSSM, then re-run."

    # Windows-style paths for NSSM.
    win() { command -v cygpath >/dev/null && cygpath -w "$1" || echo "$1"; }
    WDIR="$(win "$INSTALL_DIR")"
    WVENV_PY="$(win "$VENV_PY")"
    WOUT="$(win "$INSTALL_DIR/logs/telemetry.out.log")"
    WERR="$(win "$INSTALL_DIR/logs/telemetry.err.log")"

    say "Registering NSSM service '$SERVICE_NAME'…"
    nssm install "$SERVICE_NAME" "$WVENV_PY" "-m" "telemetry" || \
        nssm set "$SERVICE_NAME" Application "$WVENV_PY"
    nssm set "$SERVICE_NAME" AppDirectory "$WDIR"
    nssm set "$SERVICE_NAME" AppParameters "-m telemetry"
    nssm set "$SERVICE_NAME" AppStdout "$WOUT"
    nssm set "$SERVICE_NAME" AppStderr "$WERR"
    nssm set "$SERVICE_NAME" Start SERVICE_AUTO_START

    say "Service registered. Two steps need YOUR password, so finish them by hand:"
    cat <<EOF

  nssm set $SERVICE_NAME ObjectName ".\\$USERNAME" "<YourPassword>"
  nssm start $SERVICE_NAME

(Run-as-user is the documented gotcha — LocalSystem can't reach the venv/LHM.)
Logs: $INSTALL_DIR/logs/telemetry.{out,err}.log
EOF
    exit 0
fi
