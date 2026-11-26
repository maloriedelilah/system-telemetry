#!/usr/bin/env bash
# tests/test_install.sh — install.sh fleet mode (--profile): the .env each profile writes, that a rerun
# restores a drifted box, the token on stdin, and that bad input changes nothing. No venv or systemd
# (TELEMETRY_SKIP_SERVICE=1). Needs root (it creates a system user): sudo bash tests/test_install.sh
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SRC="$PWD"
FAILS=0; CHECKS=0
fail() { printf '!!! %s\n    %s\n' "$1" "$2" >&2; FAILS=$((FAILS + 1)); }
check() { local name="$1"; shift; if "$@"; then CHECKS=$((CHECKS + 1)); else fail "$name" "ran: $*"; fi; }
has() { grep -q -- "$1" "$out"; }
envis() { grep -qx -- "$1" "$box/.env"; }
user_exists() { id -u "$1" >/dev/null 2>&1; }

if [ "$(id -u)" != 0 ]; then echo "test_install.sh needs root: sudo bash tests/test_install.sh" >&2; exit 1; fi
work="$(mktemp -d)"; out="$(mktemp)"
USER_T="tlmtest$$"
trap 'rm -rf "$work" "$out"; userdel "$USER_T" 2>/dev/null || true' EXIT
box="$work/box"
mkdir -p "$box"
chmod 0755 "$work" "$box"   # so the run user can reach secrets/ (as it can under /opt/telemetry)
cp -r "$SRC/install.sh" "$SRC/service" "$SRC/requirements.txt" "$SRC/autoupdate.sh" "$SRC/allowed_signers" "$box/"
export TELEMETRY_SKIP_SERVICE=1
# The updater pieces land outside the box (OPS-75); point them at the test's own folders.
mkdir -p "$work/units" "$work/sbin" "$work/etc"
export TELEMETRY_UNIT_DIR="$work/units" TELEMETRY_SBIN="$work/sbin" \
       TELEMETRY_ALLOWED_SIGNERS="$work/etc/allowed_signers" TELEMETRY_UPDATER_HEARTBEAT_DIR="$work/varlib/telemetry/heartbeat"   # parent missing on purpose (OPS-109)
URL="http://100.92.61.42:8427"
TOKEN="pw_$(head -c 30 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
run() { bash "$box/install.sh" "$@" >"$out" 2>&1; }

echo "== install.sh --profile host"
if printf '%s\n' "$TOKEN" | run --profile host --pane-url "$URL" --run-user "$USER_T" --token-stdin; then CHECKS=$((CHECKS + 1)); else fail "host profile failed" "$(tail -n 6 "$out")"; fi
check "MQTT is off" envis 'MQTT_HOST='
check "the Pane URL is set" envis "PANE_URL=$URL"
check "host stats on" envis 'TELEMETRY_HOST=1'
check "NVIDIA off" envis 'TELEMETRY_NVIDIA=0'
check "LHM off" envis 'TELEMETRY_LHM=0'
check "no model labels" envis 'MODEL_SOURCE=none'
check "the run user was created" user_exists "$USER_T"
check "the token is saved, exactly" test "$(cat "$box/secrets/pane_token")" = "$TOKEN"
check "the token file is 0400" test "$(stat -c %a "$box/secrets/pane_token")" = 400
check "and owned by the run user" test "$(stat -c %U "$box/secrets/pane_token")" = "$USER_T"
check "secrets/ is 0750, root's, the run user's group" test "$(stat -c '%a %U %G' "$box/secrets")" = "750 root $(id -gn "$USER_T")"
check "the run user can read its token" test "$(runuser -u "$USER_T" -- cat "$box/secrets/pane_token")" = "$TOKEN"
check "but can't add anything to secrets/" bash -c '! runuser -u "$1" -- touch "$2/secrets/x" 2>/dev/null' _ "$USER_T" "$box"
check "no temp files left in secrets/" test "$(ls -A "$box/secrets")" = pane_token
check ".env is 0600 and the run user's" test "$(stat -c '%a %U' "$box/.env")" = "600 $USER_T"
check "the token is not in .env" bash -c '! grep -qF "$1" "$2"' _ "$TOKEN" "$box/.env"
check "site probes off by default" envis 'TELEMETRY_SITES=0'

echo "== the token is accepted under en_US.UTF-8 too (bash's =~ ranges follow the locale)"
loc=""
if locale -a 2>/dev/null | grep -qix 'en_US.utf8'; then loc=en_US.UTF-8
elif [ -d /usr/share/i18n/locales ] && localedef -i en_US -f UTF-8 "$work/en_US.UTF-8" >/dev/null 2>&1; then
    export LOCPATH="$work"; loc=en_US.UTF-8
fi
if [ -n "$loc" ]; then
    if printf '%s\n' "$TOKEN" | LC_ALL="$loc" LANG="$loc" bash "$box/install.sh" --profile host --pane-url "$URL" --run-user "$USER_T" --token-stdin >"$out" 2>&1; then
        CHECKS=$((CHECKS + 1))
    else fail "a real token was refused under $loc" "$(tail -n 6 "$out")"; fi
    check "under $loc the token is saved" test "$(cat "$box/secrets/pane_token")" = "$TOKEN"
    unset LOCPATH
else
    echo "   (no en_US.UTF-8 locale here; skipped)"
fi

echo "== --profile gpu with model labels, and a rerun restores a drifted .env"
printf 'MQTT_HOST=192.168.68.76\nTELEMETRY_NVIDIA=0\nDEBUG=1\n' >"$box/.env"
run --profile gpu --pane-url "$URL" --run-user "$USER_T" --models llamaserver=http://127.0.0.1:1234,http://127.0.0.1:1235 \
    || fail "gpu profile failed" "$(tail -n 6 "$out")"
check "NVIDIA on" envis 'TELEMETRY_NVIDIA=1'
check "the drifted MQTT host is gone" envis 'MQTT_HOST='
check "DEBUG is back to 0" envis 'DEBUG=0'
check "llama-server labels" envis 'MODEL_SOURCE=llamaserver'
check "both URLs" envis 'MODEL_API_URL=http://127.0.0.1:1234,http://127.0.0.1:1235'
check "the earlier token is kept when none is given" test "$(cat "$box/secrets/pane_token")" = "$TOKEN"

echo "== --sites turns the site probes on; a rerun and --reconfigure keep them; --no-sites turns them off"
run --profile host --pane-url "$URL" --run-user "$USER_T" --sites || fail "--sites failed" "$(tail -n 6 "$out")"
check "--sites: TELEMETRY_SITES=1" envis 'TELEMETRY_SITES=1'
check "--sites: it says so" has 'Site probes are on'
run --profile host --pane-url "$URL" --run-user "$USER_T" || fail "rerun without --sites failed" "$(tail -n 6 "$out")"
check "a rerun without --sites keeps them on (Errata, OPS-59 Blocker)" envis 'TELEMETRY_SITES=1'
# --reconfigure (the interactive questions, answered from a file): MQTT none, Pane URL, default name,
# no NVIDIA, host stats yes, no model labels.
printf 'none\n%s\n\nn\ny\nnone\n' "$URL" >"$work/answers"
TELEMETRY_TTY_R="$work/answers" TELEMETRY_TTY_W=/dev/null run --reconfigure --run-user "$USER_T" || fail "--reconfigure failed" "$(tail -n 6 "$out")"
check "--reconfigure asked its questions (MQTT off now)" envis 'MQTT_HOST='
check "--reconfigure keeps them on" envis 'TELEMETRY_SITES=1'
run --profile host --pane-url "$URL" --run-user "$USER_T" --no-sites || fail "--no-sites failed" "$(tail -n 6 "$out")"
check "--no-sites: TELEMETRY_SITES=0" envis 'TELEMETRY_SITES=0'
run --profile host --pane-url "$URL" --run-user "$USER_T" || fail "rerun after --no-sites failed" "$(tail -n 6 "$out")"
check "and a rerun keeps them off" envis 'TELEMETRY_SITES=0'

echo "== --profile vm with ollama labels"
run --profile vm --pane-url "$URL" --run-user "$USER_T" --models ollama=http://127.0.0.1:11434 || fail "vm profile failed" "$(tail -n 6 "$out")"
check "host stats on, NVIDIA off" bash -c 'grep -qx TELEMETRY_HOST=1 "$1" && grep -qx TELEMETRY_NVIDIA=0 "$1"' _ "$box/.env"
check "ollama path" envis 'MODEL_API_PATH=/api/ps'

echo "== planted links are never followed (Errata, OPS-47 Blocker)"
victim="$work/victim"
plant_victim() { printf 'SECRET\n' >"$victim"; chown root:root "$victim"; chmod 0600 "$victim"; }
victim_intact() { test "$(stat -c '%U %G %a' "$victim")" = "root root 600" && test "$(cat "$victim")" = SECRET; }
legacy_secrets() { chown "$USER_T" "$box/secrets"; chmod 0700 "$box/secrets"; rm -f "$box/secrets/pane_token"; }
# The layout before this fix: secrets/ owned by the run user, which plants a symlink. Rerun, keeping the token.
plant_victim; legacy_secrets
runuser -u "$USER_T" -- ln -s "$victim" "$box/secrets/pane_token"
run --profile host --pane-url "$URL" --run-user "$USER_T" || fail "rerun with a planted symlink failed" "$(tail -n 6 "$out")"
check "symlink, no token: the target is untouched (owner, mode, contents)" victim_intact
check "symlink, no token: the link is gone" test ! -e "$box/secrets/pane_token" -a ! -L "$box/secrets/pane_token"
check "symlink, no token: it says so" has "!!! the saved Pane token wasn't a plain file (removed)"
check "symlink, no token: secrets/ is root's again" test "$(stat -c %U "$box/secrets")" = root
# Same, with a new token on stdin: the token replaces the link, the target is untouched.
plant_victim; legacy_secrets
runuser -u "$USER_T" -- ln -s "$victim" "$box/secrets/pane_token"
printf '%s\n' "$TOKEN" | run --profile host --pane-url "$URL" --run-user "$USER_T" --token-stdin \
    || fail "rerun with a token over a planted symlink failed" "$(tail -n 6 "$out")"
check "symlink, new token: the target is untouched" victim_intact
check "symlink, new token: the token is a plain file again" test -f "$box/secrets/pane_token" -a ! -L "$box/secrets/pane_token"
check "symlink, new token: and holds the token" test "$(cat "$box/secrets/pane_token")" = "$TOKEN"
# A hard link (protected_hardlinks stops the run user making one; this is the belt to that brace).
plant_victim; legacy_secrets
ln "$victim" "$box/secrets/pane_token"
run --profile host --pane-url "$URL" --run-user "$USER_T" || fail "rerun with a hard link failed" "$(tail -n 6 "$out")"
check "hard link: the target keeps its owner and mode" victim_intact
check "hard link: the link is gone" test ! -e "$box/secrets/pane_token"
# secrets/ itself a symlink: refused, nothing followed.
plant_victim; rm -rf "$box/secrets"; mkdir -p "$work/elsewhere"; chmod 0700 "$work/elsewhere"
ln -s "$work/elsewhere" "$box/secrets"
if run --profile host --pane-url "$URL" --run-user "$USER_T"; then fail "a symlinked secrets/ was accepted" ""; else CHECKS=$((CHECKS + 1)); fi
check "symlinked secrets/: it says why" has "!!! secrets/ isn't a plain directory"
check "symlinked secrets/: its target is untouched" test "$(stat -c '%U %a' "$work/elsewhere")" = "root 700"
rm -f "$box/secrets"
# .env a symlink (a run user that owned the install dir could plant one): replaced, not written through.
plant_victim; ln -sf "$victim" "$box/.env"
printf '%s\n' "$TOKEN" | run --profile host --pane-url "$URL" --run-user "$USER_T" --token-stdin \
    || fail "rerun with a symlinked .env failed" "$(tail -n 6 "$out")"
check ".env symlink: the target is untouched" victim_intact
check ".env symlink: .env is a plain file again" test -f "$box/.env" -a ! -L "$box/.env"
check ".env symlink: no temp files left" bash -c '! ls -A "$1" | grep -q "^\.env\."' _ "$box"

# A directory planted at secrets/pane_token (the old layout) would swallow the new token: it goes first.
legacy_secrets
runuser -u "$USER_T" -- mkdir "$box/secrets/pane_token"
printf '%s\n' "$TOKEN" | run --profile host --pane-url "$URL" --run-user "$USER_T" --token-stdin \
    || fail "a token over a planted directory failed" "$(tail -n 6 "$out")"
check "planted directory: the token is a plain file" test -f "$box/secrets/pane_token" -a ! -L "$box/secrets/pane_token"
check "planted directory: holding the token" test "$(cat "$box/secrets/pane_token")" = "$TOKEN"

echo "== the trusted release keys and the self-updater are placed outside the box (OPS-75)"
check "allowed_signers copied" cmp -s "$box/allowed_signers" "$work/etc/allowed_signers"
check "root's, 0644" test "$(stat -c '%U %a' "$work/etc/allowed_signers")" = "root 644"
check "the runner copied" cmp -s "$box/autoupdate.sh" "$work/sbin/telemetry-autoupdate"
check "root's, 0755" test "$(stat -c '%U %a' "$work/sbin/telemetry-autoupdate")" = "root 755"
check "the updater unit names this install dir" grep -qx "Environment=TELEMETRY_DIR=$box" "$work/units/telemetry-autoupdate.service"
check "and its timer is there" test -f "$work/units/telemetry-autoupdate.timer"
check "the heartbeat folder exists, root's 0755" test "$(stat -c '%U %a' "$work/varlib/telemetry/heartbeat")" = "root 755"
check "and its parent is 0755 too, so the run user can reach it (OPS-109)" test "$(stat -c '%U %a' "$work/varlib/telemetry")" = "root 755"
check "and its parent is created 0755 when missing (OPS-109)" grep -qF 'install -d -o root -g root -m 0755 "$(dirname "$hb")"' "$box/install.sh"
check "the default parent is set every time, repairing a root-only one (OPS-109)" grep -qF 'install -d -o root -g root -m 0755 /var/lib/telemetry' "$box/install.sh"
# An overridden folder's existing parent is left as it is (Errata, OPS-109 Low).
chmod 0750 "$work/varlib/telemetry"
run --profile host --pane-url "$URL" --run-user "$USER_T" || fail "rerun for the parent check failed" "$(tail -n 6 "$out")"
check "an overridden folder's existing parent is not re-owned or re-moded (OPS-109 Low)" test "$(stat -c '%a' "$work/varlib/telemetry")" = 750
chmod 0755 "$work/varlib/telemetry"

echo "== --ring sets the update ring; a rerun keeps it; a new --ring replaces it (OPS-75)"
printf '%s\n' "$TOKEN" | run --profile host --pane-url "$URL" --run-user "$USER_T" --token-stdin --ring canary \
    || fail "--ring canary failed" "$(tail -n 6 "$out")"
check "--ring canary is in .env" envis 'TELEMETRY_UPDATE_RING=canary'
check "it says so" has 'Update ring: canary'
run --profile host --pane-url "$URL" --run-user "$USER_T" || fail "a rerun failed" "$(tail -n 6 "$out")"
check "a fleet rerun keeps canary" envis 'TELEMETRY_UPDATE_RING=canary'
run --profile host --pane-url "$URL" --run-user "$USER_T" --ring off || fail "--ring off failed" "$(tail -n 6 "$out")"
check "--ring off replaces it" envis 'TELEMETRY_UPDATE_RING=off'
check "with just one ring line" test "$(grep -c '^TELEMETRY_UPDATE_RING=' "$box/.env")" = 1
check ".env is still 0600 and the run user's" test "$(stat -c '%a %U' "$box/.env")" = "600 $USER_T"
run --profile host --pane-url "$URL" --run-user "$USER_T" --ring main || fail "--ring main failed" "$(tail -n 6 "$out")"

echo "== a rerun without --run-user keeps the unit's run user (keystone, Nov 18)"
printf '[Service]\nUser=%s\n' "$USER_T" >"$work/units/system-telemetry.service"
run --profile host --pane-url "$URL" || fail "a rerun without --run-user failed" "$(tail -n 6 "$out")"
check "it says it kept the unit's user" has "Run user: $USER_T (kept from the installed unit"
check "the token stays the run user's" test "$(stat -c %U "$box/secrets/pane_token")" = "$USER_T"
check ".env stays the run user's" test "$(stat -c %U "$box/.env")" = "$USER_T"
printf '[Service]\nUser=no-such-user-%s\n' "$$" >"$work/units/system-telemetry.service"
run --profile host --pane-url "$URL" --run-user "$USER_T" || fail "a rerun with a stale unit failed" "$(tail -n 6 "$out")"
check "a unit naming a missing user isn't trusted; --run-user wins" test "$(stat -c %U "$box/secrets/pane_token")" = "$USER_T"
rm -f "$work/units/system-telemetry.service"

echo "== an install dir the run user owns is refused (as update.sh refuses it), and nothing changes"
cp "$box/.env" "$work/env.owned"
chown "$USER_T" "$box"
if run --profile host --pane-url "$URL" --run-user "$USER_T"; then fail "a run-user-owned install dir was accepted" ""; else CHECKS=$((CHECKS + 1)); fi
check "owned dir: it says why" has "!!! the install dir isn't root's"
check "owned dir: .env unchanged" cmp -s "$work/env.owned" "$box/.env"
chown root "$box"

echo "== bad input is refused and nothing changes"
cp "$box/.env" "$work/env.before"
before_tok="$(cat "$box/secrets/pane_token")"
expect_refused() { # name title-fragment [stdin] -- args...
    local name="$1" want="$2" input="$3"; shift 4
    if printf '%s' "$input" | run "$@"; then fail "$name was accepted" ""; return; fi
    CHECKS=$((CHECKS + 1))
    check "$name says why" has "$want"
    check "$name changed no .env" cmp -s "$work/env.before" "$box/.env"
    check "$name kept the token" test "$(cat "$box/secrets/pane_token")" = "$before_tok"
}
expect_refused "an unknown profile" '!!! unknown profile' '' -- --profile desktop --pane-url "$URL"
expect_refused "an unknown ring" '!!! unknown --ring' '' -- --profile host --pane-url "$URL" --ring fast
expect_refused "a URL with userinfo" '!!! not a Pane URL' '' -- --profile host --pane-url "http://$TOKEN@100.92.61.42:8427"
expect_refused "a URL with a path" '!!! not a Pane URL' '' -- --profile host --pane-url "$URL/influx/write"
expect_refused "no URL" '!!! not a Pane URL' '' -- --profile host
expect_refused "an unknown model source" '!!! unknown --models' '' -- --profile host --pane-url "$URL" --models lmstudio=http://x:1
expect_refused "a bad model URL" '!!! bad model URL' '' -- --profile host --pane-url "$URL" --models 'llamaserver=http://x:1;rm'
expect_refused "a bad user name" '!!! bad --run-user' '' -- --profile host --pane-url "$URL" --run-user 'Root;x'
expect_refused "root as the run user" "!!! --run-user can't be root" '' -- --profile host --pane-url "$URL" --run-user root
expect_refused "a token with spaces" "!!! that isn't a Pane token" "pw_abc def ghi jkl mno pqr" -- --profile host --pane-url "$URL" --token-stdin
expect_refused "an empty token" "!!! that isn't a Pane token" "" -- --profile host --pane-url "$URL" --token-stdin
expect_refused "an unknown option" '!!! unknown argument' '' -- --profile host --pane-url "$URL" --force
expect_refused "--sites without fleet mode" '!!! --sites/--no-sites are for fleet mode' '' -- --sites
expect_refused "--no-sites without fleet mode" '!!! --sites/--no-sites are for fleet mode' '' -- --no-sites
check "a refused token is never echoed" bash -c '! grep -q "pw_abc" "$1"' _ "$out"

echo
if [ "$FAILS" -gt 0 ]; then echo "INSTALL CHECKS FAILED: $FAILS failure(s), $CHECKS passed" >&2; exit 1; fi
echo "INSTALL CHECKS PASSED: $CHECKS checks"
