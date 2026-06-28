#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bootstrap.sh — one-command install. Run this on a fresh box (Git Bash on
# Windows, or a shell on Linux):
#
#   curl -fsSL https://raw.githubusercontent.com/maloriedelilah/system-telemetry/main/bootstrap.sh | bash
#
# Detects the OS, downloads the latest release tarball, extracts it to the
# canonical install dir, and hands off to the interactive installer. No git,
# no keys — the repo is public. An existing .env is preserved (re-running
# bootstrap upgrades the code without touching your config).
#
# Override the target dir:  TELEMETRY_DIR=/custom/path curl ... | bash
# ---------------------------------------------------------------------------
set -euo pipefail

OWNER="maloriedelilah"
REPO="system-telemetry"
ASSET="system-telemetry.tar.gz"
URL="https://github.com/$OWNER/$REPO/releases/latest/download/$ASSET"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux*)               OS=linux ;;
    MINGW*|MSYS*|CYGWIN*) OS=windows ;;
    *) die "Unsupported OS: $(uname -s)" ;;
esac

SUDO=""
if [[ "$OS" == linux ]]; then
    DIR="${TELEMETRY_DIR:-/opt/telemetry}"
    [[ "$(id -u)" -ne 0 ]] && SUDO="sudo"
else
    DIR="${TELEMETRY_DIR:-/c/telemetry}"   # C:\telemetry under msys
fi

command -v curl >/dev/null || die "curl not found."
command -v tar  >/dev/null || die "tar not found."

say "Detected OS: $OS"
say "Install dir: $DIR"
$SUDO mkdir -p "$DIR"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
say "Downloading latest release…"
curl -fSL "$URL" -o "$tmp/$ASSET" || die "Download failed: $URL  (has a release been published yet?)"

say "Extracting (existing .env preserved)…"
$SUDO tar xzf "$tmp/$ASSET" -C "$DIR"

say "Launching interactive installer…"
cd "$DIR"
# Feed the real terminal in so prompts work even though we arrived via curl|bash.
exec $SUDO bash "$DIR/install.sh" </dev/tty
