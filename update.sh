#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# update.sh — move this install to the newest release tag (vX.Y.Z) and restart.
#
# The install dir is a git checkout of the private repo, fetched over SSH with
# this box's own read-only deploy key (OPS-19; how every app reaches its box).
# Run from inside the install dir, as root (sudo) on Linux or from an elevated
# Git Bash on Windows:
#
#     ./update.sh                     # the newest vX.Y.Z tag
#     ./update.sh v1.4.0              # a specific tag (not older than the one installed)
#     ./update.sh --rollback v1.4.0   # back to an older tag, on purpose
#     ./update.sh --auto              # unattended (OPS-75): the trusted signers file is required
#
# Signed releases (OPS-75): when this box has a trusted allowed-signers file (install.sh puts it at
# /etc/telemetry/allowed_signers, or C:\ProgramData\telemetry\secrets\allowed_signers), a tag is installed
# only if it carries an SSH signature by a key in that file. The copy inside the checkout is never
# used to judge a tag. A tag that has moved since this box first saw it is refused, and so is an
# older tag than the one installed, unless --rollback says so.
#
# - A checkout with local changes to tracked files is refused, and nothing is
#   changed (commit them upstream or discard them first).
# - An install from the tarball days (no .git) is converted in place: files
#   that differ from the release are copied to .pre-update-<time>/ first, then
#   replaced; .env, venv, secrets/ and logs (all git-ignored) are kept. A
#   conversion that fails before its checkout removes the .git it started, and
#   a half-made one left by an interrupted run is resumed.
# - After checking out the tag it re-runs as the NEW update.sh to finish (pip
#   install as the venv's owner, restart). Finishing is recorded, so a rerun
#   after a failed finish finishes instead of saying "already there".
# - Only a tag whose commit is on main is installed (main takes only reviewed,
#   CI-green PRs); one off main is refused before anything is checked out. Also
#   protect v* tags on GitHub (README, Update).
#
# Env (odd boxes and tests):
#   TELEMETRY_DEPLOY_KEY   the private key (default /etc/telemetry/deploy_key,
#                          or C:\ProgramData\telemetry\secrets\deploy_key)
#   TELEMETRY_REPO         the remote (default git@github.com:maloriedelilah/system-telemetry.git)
#   TELEMETRY_SKIP_SERVICE=1   don't pip install or restart (tests)
#   TELEMETRY_ALLOWED_SIGNERS  the trusted signers file (defaults above)
#   TELEMETRY_STATE_DIR        where first-seen tag records live (default /var/lib/telemetry-update,
#                              or C:\ProgramData\telemetry\secrets\state)
# ---------------------------------------------------------------------------
set -Eeuo pipefail

SERVICE_NAME="system-telemetry"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="${TELEMETRY_REPO:-git@github.com:maloriedelilah/system-telemetry.git}"
# GitHub's published ed25519 host key (api.github.com/meta; SHA256
# +DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU). Pinned: ssh never asks.
GITHUB_HOST_KEY='github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'
FINISHED="$INSTALL_DIR/.git/telemetry-finished"   # the tag whose finish (pip, restart) last completed

SO_FAR="nothing was changed"
STEP="starting"
NEXT_ON_ERR="read the error above, fix it, and run ./update.sh again."
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
# The house error format: what was tried, what was expected, what happened, what to do next.
report() {
    printf '\n\033[1;31m!!! %s\033[0m\n    Did:      %s\n    Expected: %s\n    Got:      %s\n    Next:     %s So far: %s.\n' \
        "$1" "$2" "$3" "$4" "$5" "$SO_FAR" >&2
}
# Only the main shell reports: set -E carries the trap into $( ) subshells.
on_err() {
    local rc=$?
    [[ $BASHPID == "$$" ]] || return 0
    report "update.sh stopped: $STEP failed" "$STEP (update.sh line $1)" "it to succeed" "exit $rc" "$NEXT_ON_ERR"
}
trap 'on_err $LINENO' ERR

case "$(uname -s)" in
    Linux*)               OS=linux;   DEFAULT_KEY=/etc/telemetry/deploy_key
                          DEFAULT_SIGNERS=/etc/telemetry/allowed_signers
                          DEFAULT_STATE=/var/lib/telemetry-update ;;
    MINGW*|MSYS*|CYGWIN*) OS=windows; DEFAULT_KEY=/c/ProgramData/telemetry/secrets/deploy_key
                          # In secrets\, which only SYSTEM and Administrators can write: ProgramData's
                          # default ACL lets any user create files beside it (Errata, OPS-75 Low).
                          DEFAULT_SIGNERS=/c/ProgramData/telemetry/secrets/allowed_signers
                          DEFAULT_STATE=/c/ProgramData/telemetry/secrets/state ;;
    *) report "unsupported OS" "uname -s" "Linux or Git Bash on Windows" "$(uname -s)" "run it on a supported box."; exit 2 ;;
esac
KEY="${TELEMETRY_DEPLOY_KEY:-$DEFAULT_KEY}"
SIGNERS="${TELEMETRY_ALLOWED_SIGNERS:-$DEFAULT_SIGNERS}"
STATE_DIR="${TELEMETRY_STATE_DIR:-$DEFAULT_STATE}"
# A release's sort key: v1.10.0 after v1.9.0 (by number, not text).
verkey() { printf '%s\n' "$1" | awk '{ split(substr($0, 2), p, "."); printf "%012d%012d%012d\n", p[1], p[2], p[3] }'; }

# Repository hooks, fsmonitor and ext:: never run in an update (it runs as root / admin). On Linux the
# install dir must be root's (checked below), so git's own ownership check stays on; Git for Windows
# can't compare an Administrators-owned folder with an admin user, so there it's marked safe.
GITC=(-c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.ext.allow=never)
[[ "$OS" == windows ]] && GITC+=(-c "safe.directory=$INSTALL_DIR")
gx() { git "${GITC[@]}" "$@"; }
g() { git "${GITC[@]}" -C "$INSTALL_DIR" "$@"; }

# --- finish: runs as the NEW update.sh, after the checkout ------------------
finish() {
    local tag="$1" py owner
    SO_FAR="checked out $tag; the deps and the service aren't refreshed yet"
    NEXT_ON_ERR="fix the cause, then run ./update.sh again: it finishes $tag (it knows the last finish didn't complete)."
    if [[ "${TELEMETRY_SKIP_SERVICE:-0}" == 1 ]]; then
        printf '%s\n' "$tag" >"$FINISHED"
        say "At $tag. Skipping pip and the service (TELEMETRY_SKIP_SERVICE=1)."
        return 0
    fi
    if [[ "$OS" == linux ]]; then py="$INSTALL_DIR/venv/bin/python"; else py="$INSTALL_DIR/venv/Scripts/python.exe"; fi
    if [[ ! -x "$py" ]]; then
        say "At $tag. No venv yet: run ./install.sh to finish the first install."
        return 0
    fi
    say "Refreshing deps…"
    # The hashed lock when the release has one (OPS-75): exact versions, exact bytes, no extra
    # dependencies, so an unattended update installs only what was reviewed.
    local req=(-r "$INSTALL_DIR/requirements.txt")
    [[ -f "$INSTALL_DIR/requirements.lock" ]] && req=(--require-hashes --no-deps -r "$INSTALL_DIR/requirements.lock")
    STEP="pip install ${req[*]}"
    if [[ "$OS" == linux ]]; then
        # As the venv's owner, never as root through a venv another user can write (Errata, OPS-19 L5).
        owner="$(stat -c %U "$INSTALL_DIR/venv")"
        if [[ "$owner" == root ]]; then
            "$py" -m pip install --quiet --no-cache-dir "${req[@]}"
        else
            runuser -u "$owner" -- "$py" -m pip install --quiet --no-cache-dir "${req[@]}"
        fi
    else
        "$py" -m pip install --quiet --no-cache-dir "${req[@]}"
    fi
    SO_FAR="checked out $tag; deps refreshed; the service isn't restarted yet"
    if [[ "$OS" == linux ]]; then
        # --no-block: the unit is a oneshot run by a timer; a failing telemetry run is the service's
        # business (journalctl), not a failed update.
        STEP="systemctl start --no-block ${SERVICE_NAME}.service"
        systemctl start --no-block "${SERVICE_NAME}.service"
        printf '%s\n' "$tag" >"$FINISHED"
        say "Done: telemetry $tag. Check: journalctl -u ${SERVICE_NAME}.service -n 20 --no-pager"
    else
        STEP="nssm restart $SERVICE_NAME"
        nssm restart "$SERVICE_NAME"
        printf '%s\n' "$tag" >"$FINISHED"
        say "Done: telemetry $tag. Logs: $INSTALL_DIR/logs/telemetry.err.log"
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
    report "update.sh needs an elevated Git Bash" "checked for admin rights" "Administrator (NSSM and C:\\telemetry need it)" "not elevated" "right-click Git Bash, Run as administrator, then ./update.sh again."
    exit 2
fi
if [[ "$OS" == linux ]]; then
    for p in "$INSTALL_DIR" "$INSTALL_DIR/.git"; do
        if [[ -e "$p" && "$(stat -c %u "$p")" != 0 ]]; then
            report "the install dir isn't root's" "stat -c %U $p" "owned by root (git runs here as root)" "owned by $(stat -c %U "$p")" \
                "if nobody changed it on purpose, find out who did; then: chown root:root $p, and run ./update.sh again."
            exit 2
        fi
    done
fi
command -v git >/dev/null || { report "git is not installed" "looked for git" "git on PATH" "not found" "install git (apt install git, or Git for Windows), then run ./update.sh again."; exit 2; }

ROLLBACK=0; AUTO=0
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --rollback) ROLLBACK=1 ;;
        --auto)     AUTO=1 ;;
        *) report "unknown option" "read the arguments" "--rollback, --auto, or a tag" "'$1'" "run ./update.sh [--rollback] [vX.Y.Z]."; exit 2 ;;
    esac
    shift
done
target="${1:-}"
if [[ "$ROLLBACK" == 1 && -z "$target" ]]; then
    report "--rollback needs a tag" "read the arguments" "--rollback vX.Y.Z" "no tag" "name the release to go back to, e.g. ./update.sh --rollback v1.4.0."
    exit 2
fi
if [[ -n "$target" && ! "$target" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    report "not a release tag" "read the argument" "vX.Y.Z, e.g. v1.4.0" "'$target'" "pass a release tag, or nothing for the newest."
    exit 2
fi

# The trusted signers file decides what runs here as root, so only root (or Administrators) may
# change it. Unattended updates (--auto) need it. A manual run without it warns and goes on, but only
# on a box that has never installed a verified release: once one has, a missing file means it was
# removed, and nothing installs until ./install.sh puts it back (Errata, OPS-75).
VERIFIED_MARK="$STATE_DIR/verified"
if [[ -e "$SIGNERS" ]]; then
    if [[ "$OS" == linux ]] && { [[ "$(stat -c %u "$SIGNERS")" != 0 ]] || [[ $(( 0$(stat -c %a "$SIGNERS") & 022 )) != 0 ]]; }; then
        report "the trusted signers file isn't locked down" "stat $SIGNERS" "owned by root, writable only by root" \
            "owner $(stat -c %U "$SIGNERS"), mode $(stat -c %a "$SIGNERS")" \
            "if nobody changed it on purpose, find out who did; then: chown root:root $SIGNERS && chmod 644 $SIGNERS, and run ./update.sh again."
        exit 2
    fi
    if [[ "$OS" == windows ]]; then
        acl="$(icacls "$(cygpath -w "$(dirname "$SIGNERS")")" 2>&1 || true)"
        if grep -Eqi '\\Users:|Everyone:|Authenticated Users:' <<<"$acl"; then
            report "the trusted signers folder isn't locked down" "icacls $(dirname "$SIGNERS")" "SYSTEM and Administrators only" \
                "$(grep -Ei '\\Users:|Everyone:|Authenticated Users:' <<<"$acl" | tr -s ' ' | tr '\n' ';')" \
                "lock it as the README's First install does (icacls ... /inheritance:r /grant:r SYSTEM and Administrators), then run ./update.sh again."
            exit 2
        fi
    fi
elif [[ -e "$VERIFIED_MARK" ]]; then
    report "the trusted signers file is gone" "looked for $SIGNERS" "the allowed-signers file (this box has installed verified releases since $(cat "$VERIFIED_MARK" 2>/dev/null))" \
        "missing" \
        "nothing installs without it, --rollback included. If nobody removed it on purpose, find out who did; then run ./install.sh by hand to put it back."
    exit 2
elif [[ "$AUTO" == 1 ]]; then
    report "no trusted signers file" "looked for $SIGNERS" "the allowed-signers file install.sh puts there" "missing" \
        "run ./install.sh by hand once (it installs the file), then unattended updates can run."
    exit 2
fi
# SSH with this box's deploy key and GitHub's pinned host key, and nothing else: no ssh_config, no
# system known_hosts. Not needed for a local (file:// or path) remote, which is what the tests use.
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
        export GIT_SSH_COMMAND="ssh -F /dev/null -i \"$KEY\" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile=\"$tmp/known_hosts\""
        ;;
esac

# converting: no usable checkout yet. Either no .git (a tarball install) or a half-made .git with no
# HEAD, left by a conversion that was interrupted (resumed here, not refused: Errata, OPS-19 B1).
converting=0
fresh_git=0
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    converting=1; fresh_git=1
    say "No .git here: converting this install to a git checkout (.env, venv, secrets/ and logs are kept)."
elif ! g rev-parse -q --verify HEAD >/dev/null 2>&1; then
    converting=1
    say "A half-made .git (no commit checked out) from an interrupted conversion: resuming it."
fi

if [[ "$converting" == 0 ]]; then
    STEP="git status"
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
    if ! tags="$(gx ls-remote --tags --refs "$REPO_URL" 'v*' 2>"$tmp/err")"; then
        report "could not list the releases" "git ls-remote --tags $REPO_URL" "the repo's vX.Y.Z tags" "$(tr '\n' ' ' <"$tmp/err")" \
            "check the deploy key is added to the repo (read-only is enough), the box reaches github.com on port 22, and, if ssh says the host key doesn't match, compare GitHub's key at api.github.com/meta with the one pinned in update.sh."
        exit 1
    fi
    # Exactly refs/tags/vX.Y.Z with no leading zeros: a tag named like x/refs/tags/v99.0.0 or v9.0.08
    # is ignored (Errata, OPS-19 L2; OPS-54).
    target="$(printf '%s\n' "$tags" | awk '$2 ~ /^refs\/tags\/v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/ { sub(/^refs\/tags\//, "", $2); print $2 }' | awk '{ split(substr($0, 2), p, "."); k = sprintf("%012d%012d%012d", p[1], p[2], p[3]); if (k > best) { best = k; t = $0 } } END { if (t != "") print t }')"
    if [[ -z "$target" ]]; then
        report "no release tags found" "git ls-remote --tags $REPO_URL" "at least one vX.Y.Z tag" "none" "cut a release with ./release.sh on a workstation."
        exit 1
    fi
fi

current=""
[[ "$converting" == 0 ]] && current="$(g describe --tags --exact-match HEAD 2>/dev/null || true)"
# Never to an older release by accident: a deleted tag on GitHub, or a mistyped one (Errata, OPS-75).
if [[ "$current" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ && "$ROLLBACK" == 0 ]] \
   && [[ "$(verkey "$target")" < "$(verkey "$current")" ]]; then
    report "$target is older than this box's $current" "compare $target with the installed release" "a release no older than $current" "$target" \
        "to go back on purpose: ./update.sh --rollback $target. If you didn't ask for $target, a newer tag may have been deleted on GitHub: tell Hopper."
    exit 1
fi
if [[ "$converting" == 0 ]]; then
    if [[ "$current" == "$target" ]]; then
        if [[ "$(cat "$FINISHED" 2>/dev/null || true)" == "$target" ]]; then
            say "Already at $target."
            exit 0
        fi
        say "At $target, but its last finish (pip, restart) didn't complete: finishing now."
        trap - ERR
        rm -rf "$tmp"
        exec bash "$INSTALL_DIR/update.sh" --finish "$target"
    fi
fi

# --- fetch and check out ----------------------------------------------------
if [[ "$fresh_git" == 1 ]]; then
    STEP="git init"
    g init -q
    g remote add origin "$REPO_URL"
    SO_FAR="started a .git in $INSTALL_DIR (no files replaced)"
else
    STEP="git remote set-url"
    g remote set-url origin "$REPO_URL" 2>/dev/null || g remote add origin "$REPO_URL"
fi
say "Fetching $target and main…"
# main in full, with the tag: the check below needs main's history. An install an older update.sh
# fetched with --depth 1 is unshallowed, or its shallow boundary would hide a downgrade's commit.
FETCH=(fetch -q)
[[ "$(g rev-parse --is-shallow-repository 2>/dev/null || true)" == true ]] && FETCH+=(--unshallow)
if ! g "${FETCH[@]}" origin "refs/tags/$target:refs/tags/$target" "+refs/heads/main:refs/remotes/origin/main" 2>"$tmp/err"; then
    got="$(tr '\n' ' ' <"$tmp/err")"
    if [[ "$fresh_git" == 1 ]]; then
        rm -rf "$INSTALL_DIR/.git"
        SO_FAR="nothing was changed (the .git this run started was removed)"
    fi
    report "could not fetch $target" "git fetch origin refs/tags/$target and main" "the tag's commit and main" "$got" \
        "check the tag exists (git ls-remote --tags) and the deploy key works, then run ./update.sh again."
    exit 1
fi

# Only a tag on main is installed: main takes only reviewed, CI-green PRs, so this ties what runs here
# as root to reviewed code whatever the tag rules on GitHub say. It runs in this box's own update.sh,
# before the checkout, so a rogue tag's update.sh never gets to skip it (Errata, OPS-54).
STEP="checking $target is on main"
onmain=0
g merge-base --is-ancestor "refs/tags/$target^{commit}" refs/remotes/origin/main 2>"$tmp/err" || onmain=$?
if [[ "$onmain" != 0 ]]; then
    sha="$(g rev-parse --short "refs/tags/$target^{commit}" 2>/dev/null || echo '?')"
    g tag -d "$target" >/dev/null 2>&1 || true
    if [[ "$fresh_git" == 1 ]]; then
        rm -rf "$INSTALL_DIR/.git"
        SO_FAR="nothing was changed (the .git this run started was removed)"
    fi
    if [[ "$onmain" == 1 ]]; then
        got="$target is commit $sha, which main doesn't contain"
    else
        got="the check itself failed: $(tr '\n' ' ' <"$tmp/err")"
    fi
    report "$target isn't on main" "git merge-base --is-ancestor $target origin/main" "a release tag on a commit of main (reviewed, CI-green code)" "$got" \
        "don't install it. A release tag off main is a mistake or code nobody reviewed: tell Malorie and Hopper. To stay put, do nothing; ./update.sh vX.Y.Z installs a release that is on main."
    exit 1
fi

# --- signed by a trusted key, and not moved (OPS-75) ------------------------------
# Like the main check, this runs in the box's own update.sh before the checkout, against the box's
# own copy of the signers, so a rogue tag can't vouch for itself.
signed_ok=0
refuse_tag() {   # what, got, next
    g tag -d "$target" >/dev/null 2>&1 || true
    if [[ "$fresh_git" == 1 ]]; then
        rm -rf "$INSTALL_DIR/.git"
        SO_FAR="nothing was changed (the .git this run started was removed)"
    fi
    report "$1" "$2" "$3" "$4" "$5"
    exit 1
}
if [[ -f "$SIGNERS" ]]; then
    STEP="verifying $target's signature"
    sig="$SIGNERS"
    [[ "$OS" == windows ]] && sig="$(cygpath -m "$SIGNERS")"
    # Only SSH signatures count: gpg and x509 are pointed at false, so a tag signed with some key in
    # root's GPG keyring can't pass. A lightweight tag has no signature at all and fails too.
    if ! g cat-file tag "refs/tags/$target" 2>/dev/null | grep -q -- '-----BEGIN SSH SIGNATURE-----'; then
        refuse_tag "$target isn't signed" "git cat-file tag $target" "an annotated tag with an SSH signature (release.sh makes them)" \
            "no SSH signature on the tag" \
            "don't install it: releases are signed now, so an unsigned tag wasn't made by release.sh. Tell Malorie and Hopper."
    fi
    # ssh-keygen itself, whatever a global gpg.ssh.program says.
    # git prints "Good signature" for a key that isn't in the file and then "No principal matched",
    # exiting 1; the exit code alone is the guarantee, so also require the line naming a listed
    # signer ('signature for <name>') and the absence of that refusal.
    vt=0
    g -c gpg.format=ssh -c gpg.ssh.program=ssh-keygen -c gpg.ssh.allowedSignersFile="$sig" \
        -c gpg.program=false -c gpg.x509.program=false \
        verify-tag "$target" >"$tmp/verify" 2>&1 || vt=$?
    if [[ "$vt" != 0 ]] || ! grep -q 'Good "git" signature for ' "$tmp/verify" || grep -qi 'no principal matched' "$tmp/verify"; then
        refuse_tag "$target isn't signed by a trusted key" "git verify-tag $target (signers: $SIGNERS)" \
            "a signature by a key in this box's trusted signers file" "$(tr '\n' ' ' <"$tmp/verify")" \
            "don't install it. If a new signer was added, the box learns of it from a release signed by a key it already trusts, or from ./install.sh. Otherwise tell Malorie and Hopper."
    fi
    say "$target is signed by $(sed -n 's/.*Good "git" signature for \([^ ]*\).*/\1/p' "$tmp/verify" | head -n 1)'s trusted key."
    signed_ok=1
else
    say "WARNING: no trusted signers file ($SIGNERS), so $target's signature isn't checked. ./install.sh puts it there."
fi
STEP="checking $target hasn't moved"
sha="$(g rev-parse "refs/tags/$target^{commit}")"
mkdir -p "$STATE_DIR"
[[ "$OS" == linux ]] && chmod 700 "$STATE_DIR"
seen="$(awk -v t="$target" '$1 == t { print $2; exit }' "$STATE_DIR/tags" 2>/dev/null || true)"
if [[ -n "$seen" && "$seen" != "$sha" ]]; then
    refuse_tag "$target has moved" "compare $target with this box's first sight of it ($STATE_DIR/tags)" \
        "the same commit as when this box first saw it (${seen:0:7})" "now ${sha:0:7}" \
        "don't install it: a release tag never moves. Tell Malorie and Hopper."
fi
[[ -n "$seen" ]] || printf '%s %s %s\n' "$target" "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$STATE_DIR/tags"
# From the first verified release on, a missing signers file is a refusal (see the preflight).
[[ "$signed_ok" == 1 && ! -e "$VERIFIED_MARK" ]] && printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$target" >"$VERIFIED_MARK"

if [[ "$converting" == 1 ]]; then
    # Files that differ from the release are kept a copy of, then replaced; git-ignored ones (.env,
    # venv, secrets/, logs) aren't touched (Errata, OPS-19 L7).
    STEP="comparing the install with $target"
    backup="$INSTALL_DIR/.pre-update-$(date +%Y%m%d-%H%M%S)"
    changed=()
    while IFS= read -r f; do
        [[ -f "$INSTALL_DIR/$f" ]] || continue
        if [[ "$(g hash-object -- "$f")" != "$(g rev-parse "refs/tags/$target:$f")" ]]; then
            changed+=("$f")
            mkdir -p "$backup/$(dirname "$f")"
            cp -p -- "$INSTALL_DIR/$f" "$backup/$f"
        fi
    done < <(g ls-tree -r --name-only "refs/tags/$target")
    if [[ ${#changed[@]} -gt 0 ]]; then
        say "These differ from $target and are replaced; the old copies are in $backup: ${changed[*]}"
        SO_FAR="started a .git; copied ${#changed[@]} differing file(s) to $backup"
    fi
    STEP="git checkout -f $target"
    g checkout -q -f --detach "refs/tags/$target"
else
    STEP="git checkout $target"
    g checkout -q --detach "refs/tags/$target"
fi
SO_FAR="checked out $target"

# Finish as the new update.sh (bash reads a script as it runs; the old one may differ).
trap - ERR
rm -rf "$tmp"   # exec skips the EXIT trap
exec bash "$INSTALL_DIR/update.sh" --finish "$target"
