#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# update.sh — pull the latest published release over this install and restart
# the service. No git required: re-downloads the release tarball, preserving
# your .env, venv, and logs. Run from inside the install dir:
#
#     cd /opt/telemetry        (or  cd /c/telemetry  in Git Bash)
#     ./update.sh
# ---------------------------------------------------------------------------
set -euo pipefail

OWNER="maloriedelilah"
REPO="system-telemetry"
ASSET="system-telemetry.tar.gz"
URL="https://github.com/$OWNER/$REPO/releases/latest/download/$ASSET"

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

SUDO=""
[[ "$OS" == linux && "$(id -u)" -ne 0 ]] && SUDO="sudo"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
say "Downloading latest release…"
curl -fSL "$URL" -o "$tmp/$ASSET" || die "Download failed: $URL"

# Extract to a staging dir, then copy code over (tarball excludes .env/venv/logs).
say "Applying update (.env, venv, logs preserved)…"
mkdir -p "$tmp/x"
tar xzf "$tmp/$ASSET" -C "$tmp/x"
$SUDO cp -r "$tmp/x/." "$INSTALL_DIR/"

if [[ "$OS" == linux ]]; then
    say "Refreshing deps…"
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
    say "Triggering a run (timer keeps its own cadence)…"
    $SUDO systemctl start "${SERVICE_NAME}.service"
    say "Done.  journalctl -u ${SERVICE_NAME}.service -n 20 --no-pager"
else
    say "Refreshing deps…"
    "$INSTALL_DIR/venv/Scripts/python.exe" -m pip install --quiet \
        -r "$INSTALL_DIR/requirements.txt"
    say "Restarting NSSM service…"
    nssm restart "$SERVICE_NAME"
    say "Done.  Logs: $INSTALL_DIR/logs/telemetry.err.log"
fi
