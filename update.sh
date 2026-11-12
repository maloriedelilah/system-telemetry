#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# update.sh — move this install to the newest release tag (vX.Y.Z) and restart.
#
# The install dir is a git checkout of the private repo, fetched over SSH with
# this box's own read-only deploy key (OPS-19; how every app reaches its box).
# Run from inside the install dir, as root (sudo) on Linux or from an elevated
# Git Bash on Windows:
#
#     ./update.sh            # the newest vX.Y.Z tag
#     ./update.sh v1.4.0     # a specific tag
#
# - A checkout with local changes to tracked files is refused, and nothing is
#   changed (commit them upstream or discard them first).
# - An install from the tarball days (no .git) is converted in place: tracked
#   files are replaced, and .env, venv, secrets/ and logs (all git-ignored)
#   are kept.
# - After checking out the new tag it re-runs as the NEW update.sh to finish
#   (pip install, restart), so a change to this script takes effect at once.
#
# Env (odd boxes and tests):
#   TELEMETRY_DEPLOY_KEY   the private key (default /etc/telemetry/deploy_key,
#                          or C:\ProgramData\telemetry\secrets\deploy_key)
#   TELEMETRY_REPO         the remote (default git@github.com:maloriedelilah/system-telemetry.git)
#   TELEMETRY_SKIP_SERVICE=1   don't pip install or restart (tests)
# ---------------------------------------------------------------------------
set -Eeuo pipefail

SERVICE_NAME="system-telemetry"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="${TELEMETRY_REPO:-git@github.com:maloriedelilah/system-telemetry.git}"
# GitHub's published ed25519 host key (api.github.com/meta; SHA256
# +DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU). Pinned: ssh never asks.
GITHUB_HOST_KEY='github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'

SO_FAR="nothing was changed"
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
# The house error format: what was tried, what was expected, what happened, what to do next.
report() {
    printf '\n\033[1;31m!!! %s\033[0m\n    Did:      %s\n    Expected: %s\n    Got:      %s\n    Next:     %s So far: %s.\n' \
        "$1" "$2" "$3" "$4" "$5" "$SO_FAR" >&2
}
trap 'report "update.sh stopped unexpectedly" "line $LINENO: $BASH_COMMAND" "every step to succeed" "exit $?" "read the error above, fix it, and run ./update.sh again."' ERR

case "$(uname -s)" in
    Linux*)               OS=linux;   DEFAULT_KEY=/etc/telemetry/deploy_key ;;
    MINGW*|MSYS*|CYGWIN*) OS=windows; DEFAULT_KEY=/c/ProgramData/telemetry/secrets/deploy_key ;;
    *) report "unsupported OS" "uname -s" "Linux or Git Bash on Windows" "$(uname -s)" "run it on a supported box."; exit 2 ;;
esac
KEY="${TELEMETRY_DEPLOY_KEY:-$DEFAULT_KEY}"

# Repository hooks, fsmonitor and ext:: never run in an update (it runs as root / admin).
g() { git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.ext.allow=never \
          -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" "$@"; }

# --- finish: runs as the NEW update.sh, after the checkout ------------------
finish() {
    local version
    version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INSTALL_DIR/telemetry/__init__.py")"
    SO_FAR="checked out $1 (telemetry $version)"
    if [[ "${TELEMETRY_SKIP_SERVICE:-0}" == 1 ]]; then
        say "At $1 (telemetry $version). Skipping pip and the service (TELEMETRY_SKIP_SERVICE=1)."
        return 0
    fi
    local py
    if [[ "$OS" == linux ]]; then py="$INSTALL_DIR/venv/bin/python"; else py="$INSTALL_DIR/venv/Scripts/python.exe"; fi
    if [[ ! -x "$py" ]]; then
        say "At $1 (telemetry $version). No venv yet: run ./install.sh to finish the first install."
        return 0
    fi
    say "Refreshing deps…"
    "$py" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"
    SO_FAR="$SO_FAR; pip install done"
    if [[ "$OS" == linux ]]; then
        systemctl start "${SERVICE_NAME}.service"
        say "Done: telemetry $version. Check: journalctl -u ${SERVICE_NAME}.service -n 20 --no-pager"
    else
        nssm restart "$SERVICE_NAME"
        say "Done: telemetry $version. Logs: $INSTALL_DIR/logs/telemetry.err.log"
    fi
}

if [[ "${1:-}" == "--finish" ]]; then
    finish "${2:?--finish needs the tag}"
    exit 0
fi

# --- preflight --------------------------------------------------------------
if [[ "$OS" == linux && "$(id -u)" -ne 0 ]]; then
    report "update.sh needs root" "checked the user" "root (it writes the install dir and restarts the service)" "uid $(id -u)" "run: sudo ./update.sh"
    exit 2
fi
if [[ "$OS" == windows ]] && ! reg query 'HKU\S-1-5-19' >/dev/null 2>&1; then
    report "update.sh needs an elevated Git Bash" "checked for admin rights" "Administrator (NSSM and C:\telemetry need it)" "not elevated" "right-click Git Bash, Run as administrator, then ./update.sh again."
    exit 2
fi
command -v git >/dev/null || { report "git is not installed" "looked for git" "git on PATH" "not found" "install git (apt install git, or Git for Windows), then run ./update.sh again."; exit 2; }

target="${1:-}"
if [[ -n "$target" && ! "$target" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    report "not a release tag" "read the argument" "vX.Y.Z, e.g. v1.4.0" "'$target'" "pass a release tag, or nothing for the newest."
    exit 2
fi

# SSH with this box's deploy key and GitHub's pinned host key. Not needed for a
# local (file:// or path) remote, which is what the tests use.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
case "$REPO_URL" in
    git@*|ssh://*)
        if [[ ! -r "$KEY" ]]; then
            report "no deploy key on this box" "looked for $KEY" "this box's read-only deploy key" "missing or unreadable" \
                "make one and add its .pub to the repo's Deploy keys (README, First install), or set TELEMETRY_DEPLOY_KEY."
            exit 2
        fi
        printf '%s\n' "$GITHUB_HOST_KEY" >"$tmp/known_hosts"
        export GIT_SSH_COMMAND="ssh -i \"$KEY\" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=\"$tmp/known_hosts\""
        ;;
esac

converting=0
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    converting=1
    say "No .git here: converting this install to a git checkout (.env, venv, secrets/ and logs are kept)."
fi

if [[ "$converting" == 0 ]]; then
    dirty="$(g status --porcelain --untracked-files=no)"
    if [[ -n "$dirty" ]]; then
        report "this install has local changes" "git status (tracked files only)" "no changes: the install is exactly a release" \
            "$(printf '%s' "$dirty" | tr '\n' ',' | sed 's/,$//')" \
            "a fix worth keeping goes upstream (ask Hopper); otherwise discard it with: git -C $INSTALL_DIR checkout -- <file>. Then run ./update.sh again."
        exit 1
    fi
fi

# --- which tag --------------------------------------------------------------
if [[ -z "$target" ]]; then
    if ! tags="$(git ls-remote --tags --refs "$REPO_URL" 'v*' 2>"$tmp/err")"; then
        report "could not list the releases" "git ls-remote --tags $REPO_URL" "the repo's vX.Y.Z tags" "$(tr '\n' ' ' <"$tmp/err")" \
            "check the deploy key is added to the repo (read-only is enough) and the box can reach github.com on port 22."
        exit 1
    fi
    target="$(printf '%s\n' "$tags" | sed -n 's#.*refs/tags/\(v[0-9]\{1,\}\.[0-9]\{1,\}\.[0-9]\{1,\}\)$#\1#p' | sort -V | tail -n 1)"
    if [[ -z "$target" ]]; then
        report "no release tags found" "git ls-remote --tags $REPO_URL" "at least one vX.Y.Z tag" "none" "cut a release with ./release.sh on a workstation."
        exit 1
    fi
fi

if [[ "$converting" == 0 ]]; then
    current="$(g describe --tags --exact-match HEAD 2>/dev/null || true)"
    if [[ "$current" == "$target" ]]; then
        say "Already at $target."
        exit 0
    fi
fi

# --- fetch and check out ----------------------------------------------------
if [[ "$converting" == 1 ]]; then
    g init -q
    g remote add origin "$REPO_URL"
    SO_FAR="made $INSTALL_DIR a git checkout (nothing replaced yet)"
else
    g remote set-url origin "$REPO_URL"
fi
say "Fetching $target…"
if ! g fetch -q --depth 1 origin "refs/tags/$target:refs/tags/$target" 2>"$tmp/err"; then
    report "could not fetch $target" "git fetch origin $target" "the tag's commit" "$(tr '\n' ' ' <"$tmp/err")" \
        "check the tag exists and the deploy key works, then run ./update.sh again."
    exit 1
fi
if [[ "$converting" == 1 ]]; then
    # -f replaces the old tarball's files; git-ignored ones (.env, venv, secrets/, logs) aren't touched.
    g checkout -q -f --detach "refs/tags/$target"
else
    g checkout -q --detach "refs/tags/$target"
fi
SO_FAR="checked out $target"

# Finish as the new update.sh (bash reads a script as it runs; the old one may differ).
trap - ERR
rm -rf "$tmp"   # exec skips the EXIT trap
exec bash "$INSTALL_DIR/update.sh" --finish "$target"
