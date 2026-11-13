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
cp -r "$SRC/install.sh" "$SRC/service" "$SRC/requirements.txt" "$box/"
export TELEMETRY_SKIP_SERVICE=1
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
check "secrets/ is 0700, the run user's" test "$(stat -c '%a %U' "$box/secrets")" = "700 $USER_T"
check ".env is 0600 and the run user's" test "$(stat -c '%a %U' "$box/.env")" = "600 $USER_T"
check "the token is not in .env" bash -c '! grep -qF "$1" "$2"' _ "$TOKEN" "$box/.env"

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

echo "== --profile vm with ollama labels"
run --profile vm --pane-url "$URL" --run-user "$USER_T" --models ollama=http://127.0.0.1:11434 || fail "vm profile failed" "$(tail -n 6 "$out")"
check "host stats on, NVIDIA off" bash -c 'grep -qx TELEMETRY_HOST=1 "$1" && grep -qx TELEMETRY_NVIDIA=0 "$1"' _ "$box/.env"
check "ollama path" envis 'MODEL_API_PATH=/api/ps'

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
expect_refused "a URL with userinfo" '!!! not a Pane URL' '' -- --profile host --pane-url "http://$TOKEN@100.92.61.42:8427"
expect_refused "a URL with a path" '!!! not a Pane URL' '' -- --profile host --pane-url "$URL/influx/write"
expect_refused "no URL" '!!! not a Pane URL' '' -- --profile host
expect_refused "an unknown model source" '!!! unknown --models' '' -- --profile host --pane-url "$URL" --models lmstudio=http://x:1
expect_refused "a bad model URL" '!!! bad model URL' '' -- --profile host --pane-url "$URL" --models 'llamaserver=http://x:1;rm'
expect_refused "a bad user name" '!!! bad --run-user' '' -- --profile host --pane-url "$URL" --run-user 'Root;x'
expect_refused "a token with spaces" "!!! that isn't a Pane token" "pw_abc def ghi jkl mno pqr" -- --profile host --pane-url "$URL" --token-stdin
expect_refused "an empty token" "!!! that isn't a Pane token" "" -- --profile host --pane-url "$URL" --token-stdin
expect_refused "an unknown option" '!!! unknown argument' '' -- --profile host --pane-url "$URL" --force
check "a refused token is never echoed" bash -c '! grep -q "pw_abc" "$1"' _ "$out"

echo
if [ "$FAILS" -gt 0 ]; then echo "INSTALL CHECKS FAILED: $FAILS failure(s), $CHECKS passed" >&2; exit 1; fi
echo "INSTALL CHECKS PASSED: $CHECKS checks"
