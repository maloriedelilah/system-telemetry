#!/usr/bin/env bash
# tests/test_update.sh — update.sh against a local repo standing in for GitHub (OPS-19).
# Needs root (update.sh refuses to run without it on Linux). CI: sudo bash tests/test_update.sh
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SRC="$PWD"
FAILS=0; CHECKS=0
fail() { printf '!!! %s\n    %s\n' "$1" "$2" >&2; FAILS=$((FAILS + 1)); }
check() { local name="$1"; shift; if "$@"; then CHECKS=$((CHECKS + 1)); else fail "$name" "ran: $*"; fi; }
has() { grep -q -- "$1" "$out"; }

if [ "$(id -u)" != 0 ]; then echo "test_update.sh needs root: sudo bash tests/test_update.sh" >&2; exit 1; fi
work="$(mktemp -d)"; out="$(mktemp)"
trap 'rm -rf "$work" "$out"' EXIT
G=(git -c user.name=test -c user.email=test@example.invalid -c init.defaultBranch=main)

# The "GitHub" repo: v1.0.0 is this checkout's update.sh; v1.1.0 changes update.sh (so the
# re-exec is visible) and release.txt (so the files show which release is checked out).
origin="$work/origin"
mkdir -p "$origin/telemetry"
cp "$SRC/update.sh" "$SRC/.gitignore" "$SRC/requirements.txt" "$origin/"
printf '"""fixture"""\n' >"$origin/telemetry/__init__.py"
printf 'release 1.0.0\n' >"$origin/release.txt"
"${G[@]}" -C "$origin" init -q
"${G[@]}" -C "$origin" add -A
"${G[@]}" -C "$origin" commit -qm v1.0.0
"${G[@]}" -C "$origin" tag -a v1.0.0 -m v1.0.0
sed -i 's/Skipping pip and the service/Skipping pip and the service (new update.sh)/' "$origin/update.sh"
printf 'release 1.1.0\n' >"$origin/release.txt"
"${G[@]}" -C "$origin" commit -qam v1.1.0
# Lightweight, as release.sh's tags are (GitHub makes them); v1.0.0 above is annotated.
"${G[@]}" -C "$origin" tag v1.1.0
# A tag whose name merely ends like a release must never be picked (Errata, OPS-19 L2).
"${G[@]}" -C "$origin" tag x/refs/tags/v99.0.0 v1.0.0
# Nor one with a leading zero, though it's on main and sorts newest (OPS-54).
"${G[@]}" -C "$origin" tag v9.0.08 v1.1.0
export TELEMETRY_REPO="$origin" TELEMETRY_SKIP_SERVICE=1

# A box from the tarball days: v1.0.0's files, no .git, plus its own config and secrets.
box="$work/box"
mkdir -p "$box"
"${G[@]}" -C "$origin" archive v1.0.0 | tar -x -C "$box"
printf 'PANE_URL=http://pane:8427\n' >"$box/.env"
mkdir -p "$box/secrets" "$box/venv" "$box/logs"
printf 'token\n' >"$box/secrets/pane_token"; touch "$box/venv/marker" "$box/logs/telemetry.log"
printf '# a hand edit from the tarball days\n' >>"$box/requirements.txt"
at() { git -c safe.directory="$box" -C "$box" describe --tags --exact-match HEAD 2>/dev/null; }

echo "== update.sh: a conversion whose fetch fails leaves no .git, and the rerun works"
bash "$box/update.sh" v9.9.9 >"$out" 2>&1 && fail "a missing tag was accepted" "" || CHECKS=$((CHECKS + 1))
check "it says it couldn't fetch" has '!!! could not fetch v9.9.9'
check "the half-made .git is removed" test ! -e "$box/.git"
check "So far says so" has 'the .git this run started was removed'

echo "== update.sh: a tarball install is converted to the newest tag, keeping its config"
if bash "$box/update.sh" >"$out" 2>&1; then CHECKS=$((CHECKS + 1)); else fail "the conversion failed" "$(tail -n 8 "$out")"; fi
check "it says it's converting" has 'converting this install to a git checkout'
check "it's at v1.1.0" test "$(at)" = v1.1.0
check "the new update.sh finished the run (the re-exec)" has '(new update.sh)'
check ".env is kept" grep -q 'PANE_URL=http://pane:8427' "$box/.env"
check "secrets/ is kept" test -f "$box/secrets/pane_token"
check "the venv and logs are kept" test -f "$box/venv/marker" -a -f "$box/logs/telemetry.log"
check "git sees no local changes" test -z "$(git -c safe.directory="$box" -C "$box" status --porcelain --untracked-files=no)"
check "the newest real tag won, not x/refs/tags/v99.0.0 or v9.0.08" test "$(at)" = v1.1.0
check "the hand-edited file is listed" has 'requirements.txt'
check "and its old copy is kept" bash -c 'grep -q "a hand edit from the tarball days" "$1"/.pre-update-*/requirements.txt' _ "$box"
check "the finish was recorded" test "$(cat "$box/.git/telemetry-finished")" = v1.1.0

echo "== update.sh: a second run changes nothing"
bash "$box/update.sh" >"$out" 2>&1 || fail "the second run failed" "$(tail -n 8 "$out")"
check "it says it's already there" has 'Already at v1.1.0'

echo "== update.sh: a release tag off main is refused, newest or named, before any checkout (OPS-54)"
"${G[@]}" -C "$origin" checkout -q -b unreviewed v1.1.0
printf 'PWNED\n' >"$origin/pwned.txt"
"${G[@]}" -C "$origin" add pwned.txt
"${G[@]}" -C "$origin" commit -qm unreviewed
"${G[@]}" -C "$origin" tag v1.2.0
"${G[@]}" -C "$origin" checkout -q main
bash "$box/update.sh" >"$out" 2>&1 && fail "the newest tag, off main, was installed" "$(tail -n 4 "$out")" || CHECKS=$((CHECKS + 1))
check "it says the tag isn't on main" has "!!! v1.2.0 isn't on main"
check "it names the commit" has "which main doesn't contain"
check "still at v1.1.0" test "$(at)" = v1.1.0
check "nothing from the branch reached the box" test ! -e "$box/pwned.txt"
check "the rogue tag isn't kept on the box" bash -c '! git -c safe.directory="$1" -C "$1" rev-parse -q --verify refs/tags/v1.2.0' _ "$box"
bash "$box/update.sh" v1.2.0 >"$out" 2>&1 && fail "a named tag off main was installed" "" || CHECKS=$((CHECKS + 1))
check "named, it's refused too" has "!!! v1.2.0 isn't on main"
check "and the box is still at v1.1.0" test "$(at)" = v1.1.0
box3="$work/box3"; mkdir -p "$box3"
"${G[@]}" -C "$origin" archive v1.0.0 | tar -x -C "$box3"
bash "$box3/update.sh" >"$out" 2>&1 && fail "a conversion installed a tag off main" "" || CHECKS=$((CHECKS + 1))
check "a conversion refuses it" has "!!! v1.2.0 isn't on main"
check "and removes the .git it started" test ! -e "$box3/.git"
check "So far says nothing was changed" has 'the .git this run started was removed'
"${G[@]}" -C "$origin" tag -d v1.2.0 >/dev/null

echo "== update.sh: a finish that didn't complete is finished on the rerun"
rm -f "$box/.git/telemetry-finished"
bash "$box/update.sh" >"$out" 2>&1 || fail "the rerun after an unfinished finish failed" "$(tail -n 8 "$out")"
check "it finishes instead of saying it's already there" has "last finish (pip, restart) didn't complete"
check "and records it" test "$(cat "$box/.git/telemetry-finished")" = v1.1.0

echo "== update.sh: a named tag"
bash "$box/update.sh" v1.0.0 >"$out" 2>&1 || fail "update.sh v1.0.0 failed" "$(tail -n 8 "$out")"
check "it's at v1.0.0" test "$(at)" = v1.0.0
check "the files are v1.0.0's" grep -qx 'release 1.0.0' "$box/release.txt"
bash "$box/update.sh" >"$out" 2>&1 || fail "back to the newest failed" "$(tail -n 8 "$out")"
check "and back to v1.1.0" test "$(at)" = v1.1.0

echo "== update.sh: local changes are refused, and nothing is changed"
printf '# a hand edit\n' >>"$box/telemetry/__init__.py"
bash "$box/update.sh" v1.0.0 >"$out" 2>&1 && fail "a dirty install was updated" "$(tail -n 4 "$out")" || CHECKS=$((CHECKS + 1))
check "it says why, in the house format" has '!!! this install has local changes'
check "it names the file" has 'telemetry/__init__.py'
check "So far says nothing was changed" has 'So far: nothing was changed'
check "still at v1.1.0" test "$(at)" = v1.1.0
check "the hand edit is still there" grep -q 'a hand edit' "$box/telemetry/__init__.py"
git -c safe.directory="$box" -C "$box" checkout -q -- telemetry/__init__.py

echo "== update.sh: a half-made .git from an interrupted conversion is resumed"
box2="$work/box2"; mkdir -p "$box2"
"${G[@]}" -C "$origin" archive v1.0.0 | tar -x -C "$box2"
printf 'X=1\n' >"$box2/.env"
git -C "$box2" init -q; git -C "$box2" remote add origin "$origin"
bash "$box2/update.sh" >"$out" 2>&1 || fail "the resume failed" "$(tail -n 8 "$out")"
check "it says it's resuming" has 'resuming it'
check "it's at v1.1.0" test "$(git -c safe.directory="$box2" -C "$box2" describe --tags --exact-match HEAD)" = v1.1.0
check ".env is kept" grep -q 'X=1' "$box2/.env"

echo "== update.sh: an install an older update.sh fetched shallow is unshallowed, so a downgrade works"
box4="$work/box4"; mkdir -p "$box4"
git -C "$box4" init -q
git -C "$box4" remote add origin "$origin"
git -C "$box4" fetch -q --depth 1 origin refs/tags/v1.1.0:refs/tags/v1.1.0
git -C "$box4" checkout -q --detach refs/tags/v1.1.0
printf 'v1.1.0\n' >"$box4/.git/telemetry-finished"
check "the old-style install is shallow" test "$(git -C "$box4" rev-parse --is-shallow-repository)" = true
bash "$box4/update.sh" v1.0.0 >"$out" 2>&1 || fail "the downgrade on a shallow install failed" "$(tail -n 8 "$out")"
check "it's at v1.0.0" test "$(git -C "$box4" describe --tags --exact-match HEAD)" = v1.0.0
check "with v1.0.0's files" grep -qx 'release 1.0.0' "$box4/release.txt"
check "and isn't shallow any more" test "$(git -C "$box4" rev-parse --is-shallow-repository)" = false

echo "== update.sh: an install dir that isn't root's is refused"
chown 65534 "$box/.git"
bash "$box/update.sh" >"$out" 2>&1 && fail "a non-root .git was accepted" "" || CHECKS=$((CHECKS + 1))
check "it says whose it is" has "!!! the install dir isn't root's"
chown 0 "$box/.git"

echo "== update.sh: bad input and a missing deploy key are refused"
bash "$box/update.sh" 1.2 >"$out" 2>&1 && fail "'1.2' was accepted" "" || CHECKS=$((CHECKS + 1))
check "a bad tag says what a tag looks like" has 'vX.Y.Z'
bash "$box/update.sh" v1.02.0 >"$out" 2>&1 && fail "'v1.02.0' was accepted" "" || CHECKS=$((CHECKS + 1))
check "a leading zero is refused too" has '!!! not a release tag'
TELEMETRY_REPO=git@github.com:example/none.git TELEMETRY_DEPLOY_KEY="$work/nope" bash "$box/update.sh" >"$out" 2>&1 \
    && fail "a missing key was accepted" "" || CHECKS=$((CHECKS + 1))
check "a missing key says how to make one" has '!!! no deploy key on this box'
check "still at v1.1.0 after both" test "$(at)" = v1.1.0

echo
if [ "$FAILS" -gt 0 ]; then echo "UPDATE CHECKS FAILED: $FAILS failure(s), $CHECKS passed" >&2; exit 1; fi
echo "UPDATE CHECKS PASSED: $CHECKS checks"
