#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# system-telemetry updater. Pulls the latest from GitHub, refreshes deps, and
# restarts/triggers the service. Run from inside the install dir:
#
#     cd /opt/telemetry   (or  cd "C:\telemetry"  in msys bash)
#     ./update.sh
#
# Authenticates via the deploy key configured for this clone's git remote.
# ---------------------------------------------------------------------------
set -euo pipefail

SERVICE_NAME="system-telemetry"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INSTALL_DIR"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux*)               OS=linux ;;
    MINGW*|MSYS*|CYGWIN*) OS=windows ;;
    *) die "Unsupported OS: $(uname -s)" ;;
esac

say "Pulling latest…"
git pull --ff-only

if [[ "$OS" == "linux" ]]; then
    say "Refreshing deps…"
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
    say "Triggering a run (timer continues on its own cadence)…"
    sudo systemctl start "${SERVICE_NAME}.service"
    say "Done.  journalctl -u ${SERVICE_NAME}.service -n 20 --no-pager"
else
    say "Refreshing deps…"
    "$INSTALL_DIR/venv/Scripts/python.exe" -m pip install --quiet \
        -r "$INSTALL_DIR/requirements.txt"
    say "Restarting NSSM service…"
    nssm restart "$SERVICE_NAME"
    say "Done.  Logs: $INSTALL_DIR/logs/telemetry.err.log"
fi
