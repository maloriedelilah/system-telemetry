#!/usr/bin/env bash
# tests/test_autoupdate.sh — the unattended updater (OPS-75) against a local "GitHub", with signed
# tags, a trusted signers file, and fake systemctl/journalctl standing in for the telemetry run.
# Needs root (update.sh refuses to run without it on Linux). CI: sudo bash tests/test_autoupdate.sh
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SRC="$PWD"
FAILS=0; CHECKS=0
fail() { printf '!!! %s\n    %s\n' "$1" "$2" >&2; FAILS=$((FAILS + 1)); }
check() { local name="$1"; shift; if "$@"; then CHECKS=$((CHECKS + 1)); else fail "$name" "ran: $*"; fi; }
has() { grep -q -- "$1" "$out"; }

if [ "$(id -u)" != 0 ]; then echo "test_autoupdate.sh needs root: sudo bash tests/test_autoupdate.sh" >&2; exit 1; fi
work="$(mktemp -d)"; out="$(mktemp)"
trap 'rm -rf "$work" "$out"' EXIT
export GIT_CONFIG_GLOBAL="$work/gitconfig" GIT_CONFIG_NOSYSTEM=1
: >"$GIT_CONFIG_GLOBAL"
G=(git -c user.name=test -c user.email=test@example.invalid -c init.defaultBranch=main)
S=("${G[@]}" -c gpg.format=ssh -c gpg.ssh.program=ssh-keygen -c user.signingkey="$work/key")
ssh-keygen -q -t ed25519 -N '' -C release -f "$work/key"
printf 'malorie namespaces="git" %s\n' "$(cat "$work/key.pub")" >"$work/allowed_signers"
chmod 644 "$work/allowed_signers"

# "GitHub": v1.0.0 is this checkout's update.sh; each release changes release.txt.
origin="$work/origin"
mkdir -p "$origin/telemetry"
cp "$SRC/update.sh" "$SRC/.gitignore" "$SRC/requirements.txt" "$origin/"
printf '"""fixture"""\n' >"$origin/telemetry/__init__.py"
release() {   # version [unsigned]
    printf 'release %s\n' "$1" >"$origin/release.txt"
    "${G[@]}" -C "$origin" add -A
    "${G[@]}" -C "$origin" commit -qm "v$1"
    if [ "${2:-}" = unsigned ]; then "${G[@]}" -C "$origin" tag -a "v$1" -m "v$1"; else "${S[@]}" -C "$origin" tag -s "v$1" -m "v$1"; fi
}
main_file() {   # name [content]: put a file on main (or remove it with content "-")
    if [ "${2:-}" = - ]; then rm -f "$origin/$1"; else printf '%s\n' "${2:-}" >"$origin/$1"; fi
    "${G[@]}" -C "$origin" add -A
    "${G[@]}" -C "$origin" commit -qm "$1" --allow-empty
}
"${G[@]}" -C "$origin" init -q
release 1.0.0

export TELEMETRY_REPO="$origin" TELEMETRY_SKIP_SERVICE=1
export TELEMETRY_STATE_DIR="$work/state" TELEMETRY_ALLOWED_SIGNERS="$work/allowed_signers"
export TELEMETRY_UPDATER_HEARTBEAT_DIR="$work/hb" TELEMETRY_HEALTH_WAIT_S=10 TELEMETRY_HEALTH_RETRY_S=0

# The box: a checkout at v1.0.0, put there by update.sh itself (so it's verified and has VERSION).
box="$work/box"
"${G[@]}" clone -q "$origin" "$box"
"${G[@]}" -C "$box" checkout -q v1.0.0
bash "$box/update.sh" --finish v1.0.0 >"$out" 2>&1
printf 'PANE_URL=http://pane:8427\n' >"$box/.env"
export TELEMETRY_DIR="$box"
at() { git -c safe.directory="$box" -C "$box" describe --tags --exact-match HEAD 2>/dev/null; }
ring() { sed -i '/^TELEMETRY_UPDATE_RING=/d' "$box/.env"; [ -z "${1:-}" ] || printf 'TELEMETRY_UPDATE_RING=%s\n' "$1" >>"$box/.env"; }
hb_ok() { grep -q "\"ok\": $1" "$work/hb/telemetry-update.json"; }

# Fake systemd: a "run" prints the checkout's VERSION and pushes $FAKE_LINES (default 9), and
# Result is $FAKE_RESULT (default success). $FAKE_NEW_LINES / $FAKE_NEW_RESULT apply once the
# box is past v1.0.0, so a bad release can be told from a good baseline.
mkdir -p "$work/bin"
cat >"$work/bin/systemctl" <<'EOF'
#!/bin/bash
v="$(cat "$TELEMETRY_DIR/VERSION" 2>/dev/null || echo unknown)"
lines="${FAKE_LINES:-9}"; result="${FAKE_RESULT:-success}"
if [ "$v" != v1.0.0 ] && [ "$v" != "${FAKE_GOOD:-v1.0.0}" ]; then
    lines="${FAKE_NEW_LINES:-$lines}"; result="${FAKE_NEW_RESULT:-$result}"
fi
case "$1" in
    start) printf 'telemetry %s\nPane: pushed %s lines for box (204)\n' "$v" "$lines" >"$FAKE_JOURNAL"
           printf '%s\n' "$result" >"$FAKE_JOURNAL.result" ;;
    show)  cat "$FAKE_JOURNAL.result" 2>/dev/null || echo success ;;
esac
exit 0
EOF
cat >"$work/bin/journalctl" <<'EOF'
#!/bin/bash
cat "$FAKE_JOURNAL" 2>/dev/null
EOF
chmod +x "$work/bin/systemctl" "$work/bin/journalctl"
export PATH="$work/bin:$PATH" FAKE_JOURNAL="$work/journal"
run() { bash "$SRC/autoupdate.sh" >"$out" 2>&1; }

echo "== autoupdate: ring off never moves"
ring off; release 1.1.0
run || fail "ring off failed" "$(tail -n 5 "$out")"
check "it says off" has 'autoupdate: off'
check "still v1.0.0" test "$(at)" = v1.0.0
check "the heartbeat is ok" hb_ok true

echo "== autoupdate: main ring waits for PROMOTED, then for the soak"
ring
run || fail "an unpromoted wait failed" "$(tail -n 5 "$out")"
check "an unpromoted tag waits" has "v1.1.0 isn't in PROMOTED yet"
check "still v1.0.0" test "$(at)" = v1.0.0
main_file PROMOTED v1.1.0
run || fail "a soaking wait failed" "$(tail -n 5 "$out")"
check "a promoted tag soaks 24 h first" has 'v1.1.0 soaks until'
check "still v1.0.0" test "$(at)" = v1.0.0

echo "== autoupdate: the pause file stops every ring"
main_file AUTOUPDATE_PAUSED
TELEMETRY_SOAK_S=0 run || fail "paused failed" "$(tail -n 5 "$out")"
check "main pauses" has 'autoupdate: paused'
ring canary
run || true
check "canaries pause too" has 'autoupdate: paused'
check "still v1.0.0" test "$(at)" = v1.0.0
main_file AUTOUPDATE_PAUSED -

echo "== autoupdate: after the soak, a promoted, signed, healthy release installs"
ring
TELEMETRY_SOAK_S=0 run || fail "the update failed" "$(tail -n 8 "$out")"
check "it says updated" has 'autoupdate: updated: v1.0.0 -> v1.1.0 (9 lines, baseline 9)'
check "at v1.1.0" test "$(at)" = v1.1.0
check "VERSION says v1.1.0" test "$(cat "$box/VERSION")" = v1.1.0
check "the heartbeat is ok" hb_ok true
check "the heartbeat file has telemetry's shape" grep -q '"check": "telemetry-update"' "$work/hb/telemetry-update.json"
run || fail "a current run failed" "$(tail -n 5 "$out")"
check "then it's current" has 'autoupdate: current: at v1.1.0'

echo "== autoupdate: a canary takes a release at once, no PROMOTED needed"
ring canary; release 1.2.0
FAKE_GOOD=v1.2.0 run || fail "the canary update failed" "$(tail -n 8 "$out")"
check "the canary updated" has 'updated: v1.1.0 -> v1.2.0'
check "at v1.2.0" test "$(at)" = v1.2.0

echo "== autoupdate: a release that pushes fewer lines is rolled back with the old finish"
release 1.3.0
FAKE_GOOD=v1.2.0 FAKE_NEW_LINES=5 run && fail "an unhealthy release was kept" "" || CHECKS=$((CHECKS + 1))
check "it says rolled back" has 'autoupdate: rolled_back: v1.3.0 was unhealthy (it pushed 5 lines, fewer than the 9 before)'
check "back at v1.2.0" test "$(at)" = v1.2.0
check "VERSION is back too (the old finish ran)" test "$(cat "$box/VERSION")" = v1.2.0
check "v1.3.0 is on the bad list" grep -qx v1.3.0 "$work/state/bad"
check "the heartbeat says not ok" hb_ok false
FAKE_GOOD=v1.2.0 run || fail "a run after the rollback failed" "$(tail -n 5 "$out")"
check "the bad release is skipped next time" has 'autoupdate: current: at v1.2.0'
check "and the heartbeat is ok again" hb_ok true

echo "== autoupdate: a failed run (Result) is rolled back too"
release 1.4.0
FAKE_GOOD=v1.2.0 FAKE_NEW_RESULT=exit-code run && fail "a failing release was kept" "" || CHECKS=$((CHECKS + 1))
check "it names the Result" has "the run's Result is exit-code"
check "back at v1.2.0" test "$(at)" = v1.2.0

echo "== autoupdate: update.sh refusing (unsigned tag) changes nothing and isn't marked bad"
release 1.5.0 unsigned
FAKE_GOOD=v1.2.0 run && fail "an unsigned tag was reported fine" "" || CHECKS=$((CHECKS + 1))
check "it says update.sh refused" has "autoupdate: failed: update.sh refused or failed before changing anything: !!! v1.5.0 isn't signed"
check "still v1.2.0" test "$(at)" = v1.2.0
check "not on the bad list (it tries again, and keeps saying so)" bash -c '! grep -qx v1.5.0 "$1"' _ "$work/state/bad"
check "the heartbeat says not ok" hb_ok false

echo "== autoupdate: an unhealthy box isn't updated at all"
"${G[@]}" -C "$origin" tag -d v1.5.0 >/dev/null; release 1.6.0
FAKE_RESULT=exit-code run && fail "an unhealthy box was updated" "" || CHECKS=$((CHECKS + 1))
check "it says the baseline failed" has "isn't healthy before updating"
check "still v1.2.0" test "$(at)" = v1.2.0

echo "== autoupdate: a stale VERSION (from a release that writes one) is dropped, not trusted (Errata L2)"
printf 'v9.9.9\n' >"$box/VERSION"
FAKE_RESULT=success run || true
check "the stale VERSION is gone or rewritten" bash -c '! grep -qx v9.9.9 "$1/VERSION" 2>/dev/null' _ "$box"
check "and the baseline didn't fail on it" bash -c '! grep -q "isn.t healthy before updating" "$1"' _ "$out"

echo "== autoupdate: only push lines after the version line count (Errata L1)"
release 1.7.0
cat >"$work/bin/systemctl" <<'EOF'
#!/bin/bash
v="$(cat "$TELEMETRY_DIR/VERSION" 2>/dev/null || echo unknown)"
case "$1" in
    start) if [ "$v" = v1.7.0 ]; then
               printf 'Pane: pushed 9 lines for box (204)\ntelemetry %s\ncycle error\n' "$v" >"$FAKE_JOURNAL"
           else
               printf 'telemetry %s\nPane: pushed 9 lines for box (204)\n' "$v" >"$FAKE_JOURNAL"
           fi ;;
    show)  echo success ;;
esac
EOF
FAKE_GOOD=v1.2.0 run && fail "an old push line stood in for the new release" "" || CHECKS=$((CHECKS + 1))
check "a push before the version line doesn't count" has 'v1.7.0 was unhealthy (the run pushed nothing to Pane)'
check "back at the previous release" bash -c '[ "$(git -c safe.directory="$1" -C "$1" describe --tags --exact-match HEAD)" != v1.7.0 ]' _ "$box"

echo "== autoupdate: a failed first check is retried once before rolling back (Errata L3)"
release 1.8.0
cat >"$work/bin/systemctl" <<'EOF'
#!/bin/bash
v="$(cat "$TELEMETRY_DIR/VERSION" 2>/dev/null || echo unknown)"
n=9
if [ "$v" = v1.8.0 ] && [ ! -e "$FAKE_JOURNAL.blipped" ]; then n=0; touch "$FAKE_JOURNAL.blipped"; fi
case "$1" in
    start) if [ "$n" = 0 ]; then printf 'telemetry %s\nPane unreachable\n' "$v"; else printf 'telemetry %s\nPane: pushed %s lines for box (204)\n' "$v" "$n"; fi >"$FAKE_JOURNAL" ;;
    show)  echo success ;;
esac
EOF
run || fail "a blip cost a good release" "$(tail -n 6 "$out")"
check "it says it checks again" has 'checking again in 0s'
check "and keeps the release" has 'updated: .* -> v1.8.0'

echo "== autoupdate: one run at a time"
mkdir "$work/state/autoupdate.lock"
run || fail "a locked run failed" "$(tail -n 5 "$out")"
check "a held lock skips" has 'another run holds the lock'
rmdir "$work/state/autoupdate.lock"

echo "== autoupdate: .env can only say canary, main or off"
release 1.9.0
ring; printf 'TELEMETRY_UPDATE_RING=canary; rm -rf /\n' >>"$box/.env"
run || true
check "a junk value counts as main (the default)" has "v1.9.0 isn't in PROMOTED yet"

echo
if [ "$FAILS" -gt 0 ]; then echo "AUTOUPDATE CHECKS FAILED: $FAILS of $((CHECKS + FAILS))"; exit 1; fi
echo "AUTOUPDATE CHECKS PASSED: $CHECKS checks"
