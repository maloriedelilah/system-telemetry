#!/usr/bin/env bash
# autoupdate.sh — the unattended updater (OPS-75).
#
# install.sh will copy this OUTSIDE the checkout (/usr/local/sbin/telemetry-autoupdate, or
# C:\ProgramData\telemetry\bin\autoupdate.sh) and a timer will run it about hourly as root (or
# SYSTEM): that's OPS-75 part B, which ships in the same release as this file. Until then nothing
# runs it.
# Living outside the checkout means a broken release can't break the thing that rolls it back.
#
# Each run:
#   1. ring (TELEMETRY_UPDATE_RING in the box's .env): canary | main (default) | off
#   2. reads main's tip: AUTOUPDATE_PAUSED there stops every box, canaries too; PROMOTED lists the
#      tags the main ring may take
#   3. picks the newest vX.Y.Z above the installed one that isn't on this box's bad list
#   4. main ring: only a PROMOTED tag, and only once SOAK_S (24 h) has passed since this box first
#      saw it; canary: at once
#   5. baseline: one telemetry run now, to know the box is healthy and how many lines it pushes
#   6. update.sh --auto <tag>  (signature, moved-tag and on-main checks all happen there)
#   7. health: the run succeeds, says "telemetry <tag>", and pushes no fewer lines than the baseline
#   8. unhealthy: back to the previous tag with THAT tag's own finish, and the new one goes on the
#      bad list until a newer tag appears
#   9. writes the telemetry-update heartbeat (ok, and when it last was) for Pane
#
# Env (tests and odd boxes): TELEMETRY_DIR, TELEMETRY_STATE_DIR, TELEMETRY_UPDATER_HEARTBEAT_DIR,
# TELEMETRY_REPO, TELEMETRY_DEPLOY_KEY, TELEMETRY_SOAK_S, TELEMETRY_HEALTH_WAIT_S,
# TELEMETRY_HEALTH_RETRY_S.
set -euo pipefail
export LC_ALL=C
umask 022

case "$(uname -s)" in
    Linux*)               OS=linux
                          DEFAULT_DIR=/opt/telemetry
                          DEFAULT_KEY=/etc/telemetry/deploy_key
                          DEFAULT_STATE=/var/lib/telemetry-update
                          DEFAULT_HB=/var/lib/telemetry/heartbeat ;;
    MINGW*|MSYS*|CYGWIN*) OS=windows
                          DEFAULT_DIR=/c/telemetry
                          DEFAULT_KEY=/c/ProgramData/telemetry/secrets/deploy_key
                          DEFAULT_STATE=/c/ProgramData/telemetry/secrets/state
                          DEFAULT_HB=/c/ProgramData/telemetry/secrets/heartbeat ;;
    *) echo "autoupdate.sh: unsupported OS $(uname -s)" >&2; exit 2 ;;
esac
INSTALL_DIR="${TELEMETRY_DIR:-$DEFAULT_DIR}"
STATE_DIR="${TELEMETRY_STATE_DIR:-$DEFAULT_STATE}"
HB_DIR="${TELEMETRY_UPDATER_HEARTBEAT_DIR:-$DEFAULT_HB}"
REPO_URL="${TELEMETRY_REPO:-git@github.com:maloriedelilah/system-telemetry.git}"
KEY="${TELEMETRY_DEPLOY_KEY:-$DEFAULT_KEY}"
SOAK_S="${TELEMETRY_SOAK_S:-86400}"
HEALTH_WAIT_S="${TELEMETRY_HEALTH_WAIT_S:-180}"
RETRY_S="${TELEMETRY_HEALTH_RETRY_S:-60}"
SERVICE_NAME="system-telemetry"
PERIOD_S=7200   # the heartbeat is late when no run has finished in 2 h (the timer runs hourly)
# GitHub's published ed25519 host key (api.github.com/meta), as update.sh pins it.
GITHUB_HOST_KEY='github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'
VER_RE='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'

GITC=(-c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.ext.allow=never -c safe.directory="$INSTALL_DIR")
g() { git "${GITC[@]}" -C "$INSTALL_DIR" "$@"; }
gx() { git "${GITC[@]}" "$@"; }
log() { printf 'autoupdate: %s\n' "$*"; }
verkey() { printf '%s\n' "$1" | awk '{ split(substr($0, 2), p, "."); printf "%012d%012d%012d\n", p[1], p[2], p[3] }'; }
now() { date +%s; }

mkdir -p "$STATE_DIR" "$HB_DIR"
[[ "$OS" == linux ]] && chmod 700 "$STATE_DIR" && chmod 755 "$HB_DIR"

# --- the heartbeat ------------------------------------------------------------
# One check, "telemetry-update". ok=false on a failure or a rollback; last_ok moves only on a good
# run, so a box whose updater stopped running shows as late on Pane's heartbeats page.
finish_run() {   # result ok detail
    local result="$1" ok="$2" detail="$3" last_ok tmpf
    last_ok="$(cat "$STATE_DIR/last_ok" 2>/dev/null || echo 0)"
    [[ "$last_ok" =~ ^[0-9]+$ ]] || last_ok=0
    if [[ "$ok" == true ]]; then last_ok="$(now)"; printf '%s\n' "$last_ok" >"$STATE_DIR/last_ok"; fi
    tmpf="$HB_DIR/.telemetry-update.json.tmp"
    printf '{"version": 1, "checks": [{"check": "telemetry-update", "last_ok": %s, "period_s": %s, "ok": %s}]}\n' \
        "$last_ok" "$PERIOD_S" "$ok" >"$tmpf"
    mv -f "$tmpf" "$HB_DIR/telemetry-update.json"
    printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$result" "$detail" >"$STATE_DIR/autoupdate.status"
    log "$result: $detail"
    rm -rf "$STATE_DIR/autoupdate.lock"
    [[ "$ok" == true ]] && exit 0
    exit 1
}

# --- one run at a time ---------------------------------------------------------------------
if ! mkdir "$STATE_DIR/autoupdate.lock" 2>/dev/null; then
    # A run killed mid-way leaves the lock; one older than 3 h is taken over.
    if [[ -n "$(find "$STATE_DIR/autoupdate.lock" -maxdepth 0 -mmin +180 2>/dev/null)" ]]; then
        rm -rf "$STATE_DIR/autoupdate.lock"; mkdir "$STATE_DIR/autoupdate.lock"
    else
        log "another run holds the lock; skipping"
        exit 0
    fi
fi

# --- ring ----------------------------------------------------------------------------------
# Read with grep, never sourced: .env belongs to the run user, so only these exact values count.
ring="$(grep -E '^TELEMETRY_UPDATE_RING=(canary|main|off)[[:space:]]*$' "$INSTALL_DIR/.env" 2>/dev/null | tail -n 1 | sed 's/^[^=]*=//; s/[[:space:]]*$//' || true)"
ring="${ring:-main}"
[[ "$ring" == off ]] && finish_run off true "TELEMETRY_UPDATE_RING=off: this box updates only by hand"

[[ -d "$INSTALL_DIR/.git" ]] || finish_run failed false "no git checkout at $INSTALL_DIR: run ./update.sh by hand once"
current="$(g describe --tags --exact-match HEAD 2>/dev/null || true)"
[[ "$current" =~ $VER_RE ]] || finish_run failed false "the checkout isn't on a release tag (${current:-none}): run ./update.sh by hand"

# --- what GitHub has -----------------------------------------------------------------------
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp" "$STATE_DIR/autoupdate.lock"' EXIT
case "$REPO_URL" in
    git@*|ssh://*)
        [[ -r "$KEY" ]] || finish_run failed false "no deploy key at $KEY"
        printf '%s\n' "$GITHUB_HOST_KEY" >"$tmp/known_hosts"
        export GIT_SSH_COMMAND="ssh -F /dev/null -i \"$KEY\" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile=\"$tmp/known_hosts\""
        ;;
esac
peek="$STATE_DIR/peek.git"
[[ -d "$peek" ]] || gx init -q --bare "$peek"
if ! gx -C "$peek" fetch -q --depth=1 "$REPO_URL" +refs/heads/main:refs/heads/main 2>"$tmp/err"; then
    finish_run failed false "could not read main: $(tr '\n' ' ' <"$tmp/err" | cut -c1-200)"
fi
# The pause stops every box, canaries too (Errata).
if gx -C "$peek" cat-file -e main:AUTOUPDATE_PAUSED 2>/dev/null; then
    finish_run paused true "AUTOUPDATE_PAUSED is on main; staying at $current"
fi
if ! tags="$(gx ls-remote --tags --refs "$REPO_URL" 'v*' 2>"$tmp/err")"; then
    finish_run failed false "could not list the releases: $(tr '\n' ' ' <"$tmp/err" | cut -c1-200)"
fi
touch "$STATE_DIR/bad" "$STATE_DIR/runner-seen"
candidate=""
best="$(verkey "$current")"
while read -r t; do
    [[ "$t" =~ $VER_RE ]] || continue
    # First sight of every newer tag is recorded, so the soak counts from when this box saw it.
    awk -v t="$t" '$1 == t { f = 1 } END { exit !f }' "$STATE_DIR/runner-seen" || printf '%s %s\n' "$t" "$(now)" >>"$STATE_DIR/runner-seen"
    grep -qxF "$t" "$STATE_DIR/bad" && continue
    k="$(verkey "$t")"
    [[ "$k" > "$best" ]] && { best="$k"; candidate="$t"; }
done < <(printf '%s\n' "$tags" | awk '$2 ~ /^refs\/tags\/v[0-9]/ { sub(/^refs\/tags\//, "", $2); print $2 }')
[[ -n "$candidate" ]] || finish_run current true "at $current, the newest release this box takes"

if [[ "$ring" == main ]]; then
    if ! gx -C "$peek" show main:PROMOTED 2>/dev/null | tr -d '\r' | grep -qxF "$candidate"; then
        finish_run waiting true "$candidate isn't in PROMOTED yet; staying at $current"
    fi
    seen="$(awk -v t="$candidate" '$1 == t { print $2; exit }' "$STATE_DIR/runner-seen")"
    if (( $(now) - seen < SOAK_S )); then
        finish_run waiting true "$candidate soaks until $(date -u -d "@$((seen + SOAK_S))" +%Y-%m-%dT%H:%MZ 2>/dev/null || echo "$((seen + SOAK_S))"); staying at $current"
    fi
fi

# --- health: one telemetry run, its exit, its version line and its line count ---------------
# Prints the number of lines pushed; fails when the run didn't succeed or wasn't $1 (an empty $1
# skips the version check).
health() {   # want_version
    local want="$1" out res n
    if [[ "$OS" == linux ]]; then
        local since; since="$(date '+%Y-%m-%d %H:%M:%S')"
        sleep 1
        timeout "$HEALTH_WAIT_S" systemctl start "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
        res="$(systemctl show -p Result --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
        out="$(journalctl -u "${SERVICE_NAME}.service" --since "$since" -o cat --no-pager 2>/dev/null || true)"
        [[ "$res" == success ]] || { echo "the run's Result is ${res:-unknown}"; return 1; }
    else
        # Windows: one telemetry cycle in the foreground, as SYSTEM like the NSSM service and with
        # the same .env, but LOOP_INTERVAL emptied (the environment wins over .env), so it runs
        # once and exits. Its own output and exit code are the evidence: no service log to tail, so
        # NSSM's log rotation and the old process's last lines can't confuse it (Errata B2, L1).
        local py="$INSTALL_DIR/venv/Scripts/python.exe" rc=0
        out="$(cd "$INSTALL_DIR" && LOOP_INTERVAL="" timeout "$HEALTH_WAIT_S" "$py" -m telemetry 2>&1 | tr -d '\r')" || rc=$?
        (( rc == 0 )) || { echo "the run exited $rc"; return 1; }
    fi
    if [[ -n "$want" ]] && ! grep -qxF "telemetry $want" <<<"$out"; then
        echo "the run didn't say 'telemetry $want'"; return 1
    fi
    # Only push lines after the version line count: an earlier run's line can't stand in (Errata L1).
    if [[ -n "$want" ]]; then out="$(awk -v v="telemetry $want" 'f; $0 == v { f = 1 }' <<<"$out")"; fi
    n="$(grep -E '^Pane: pushed [0-9]+ lines' <<<"$out" | tail -n 1 | awk '{ print $3 }')"
    [[ -n "$n" ]] || { echo "the run pushed nothing to Pane"; return 1; }
    echo "$n"
}

# Baseline first: an unhealthy box isn't updated (nothing to compare with), and the count it pushes
# now is the bar the new release has to meet. Releases before VERSION files (v1.8.0 and older)
# print no version line, so the baseline skips that part for them.
base_want="$current"
# A VERSION that doesn't match the checked-out tag is left from a release that writes one, after a
# move back to one that doesn't (v1.8.0 and older): drop it, it would fail every baseline (Errata L2).
if [[ -f "$INSTALL_DIR/VERSION" && "$(cat "$INSTALL_DIR/VERSION")" != "$current" ]]; then
    rm -f "$INSTALL_DIR/VERSION"
fi
[[ -f "$INSTALL_DIR/VERSION" ]] || base_want=""
if ! base="$(health "$base_want")"; then
    finish_run waiting false "the box isn't healthy before updating ($base); staying at $current"
fi

log "updating $current -> $candidate (ring $ring, baseline $base lines)"
if ! bash "$INSTALL_DIR/update.sh" --auto "$candidate" >"$STATE_DIR/last-update.log" 2>&1; then
    now_at="$(g describe --tags --exact-match HEAD 2>/dev/null || true)"
    if [[ "$now_at" == "$current" ]]; then
        finish_run failed false "update.sh refused or failed before changing anything: $(sed 's/\x1b\[[0-9;]*m//g' "$STATE_DIR/last-update.log" | grep -m1 '^!!!' | cut -c1-200) (log: $STATE_DIR/last-update.log)"
    fi
fi

# Healthy: the run succeeds, names $candidate, and pushes no fewer lines than the baseline. One
# retry a minute later before rolling back: a Pane blip between the two checks shouldn't cost a
# good release (Errata L3). The baseline already showed this box reaches Pane.
reason=""
for attempt in 1 2; do
    if after="$(health "$candidate")"; then
        if (( after >= base )); then
            finish_run updated true "$current -> $candidate ($after lines, baseline $base)"
        fi
        reason="it pushed $after lines, fewer than the $base before"
    else
        reason="$after"
    fi
    if [[ "$attempt" == 1 ]]; then
        log "the check after $candidate failed ($reason); checking again in ${RETRY_S}s"
        sleep "$RETRY_S"
    fi
done

# --- rollback, with the previous release's own finish ---------------------------------------
log "unhealthy after $candidate: $reason; rolling back to $current"
printf '%s\n' "$candidate" >>"$STATE_DIR/bad"
rm -f "$INSTALL_DIR/VERSION"   # the old finish writes it again if it knows how (Errata L2)
if g checkout -q --force "refs/tags/$current" 2>"$tmp/err" \
   && bash "$INSTALL_DIR/update.sh" --finish "$current" >>"$STATE_DIR/last-update.log" 2>&1; then
    finish_run rolled_back false "$candidate was unhealthy ($reason); back at $current, and $candidate is skipped until a newer release"
fi
finish_run failed false "$candidate was unhealthy ($reason) and the rollback to $current failed: $(tr '\n' ' ' <"$tmp/err" | cut -c1-200) (log: $STATE_DIR/last-update.log)"
