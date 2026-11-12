#!/usr/bin/env bash
# tests/test_release.sh — release.sh against a local repo standing in for GitHub, with a fake gh.
# The fake gh answers the CI check from $FAKE_CI and makes `gh release create` tag the "GitHub"
# repo at --target, as GitHub does. Run: bash tests/test_release.sh
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SRC="$PWD"
FAILS=0; CHECKS=0
fail() { printf '!!! %s\n    %s\n' "$1" "$2" >&2; FAILS=$((FAILS + 1)); }
check() { local name="$1"; shift; if "$@"; then CHECKS=$((CHECKS + 1)); else fail "$name" "ran: $*"; fi; }
has() { grep -q -- "$1" "$out"; }

work="$(mktemp -d)"; out="$(mktemp)"
trap 'rm -rf "$work" "$out"' EXIT
G=(git -c user.name=test -c user.email=test@example.invalid -c init.defaultBranch=main)

# The "GitHub" repo: main with an annotated v1.2.0, a lightweight v1.10.0 (sort -V, not text order)
# and a decoy x/refs/tags/v99.0.0 that must never count as a release.
origin="$work/origin"
mkdir -p "$origin"
"${G[@]}" -C "$origin" init -q
echo one >"$origin/f"; "${G[@]}" -C "$origin" add f; "${G[@]}" -C "$origin" commit -qm one
"${G[@]}" -C "$origin" tag -a v1.2.0 -m v1.2.0
echo two >"$origin/f"; "${G[@]}" -C "$origin" commit -qam two
"${G[@]}" -C "$origin" tag v1.10.0
"${G[@]}" -C "$origin" tag x/refs/tags/v99.0.0
echo three >"$origin/f"; "${G[@]}" -C "$origin" commit -qam three

# The maintainer's clone, with this checkout's release.sh (untracked: release.sh never reads the tree).
clone="$work/clone"
git clone -q "$origin" "$clone"
cp "$SRC/release.sh" "$clone/"
echo "a local edit that must not matter" >>"$clone/f"

# The fake gh.
mkdir -p "$work/bin"
cat >"$work/bin/gh" <<'GH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$GH_LOG"
case "$1" in
    api) printf '%s\n' "${FAKE_CI:-success}" ;;
    release)
        tag="$3"; target=""
        shift 3
        while [ $# -gt 0 ]; do [ "$1" = --target ] && target="$2"; shift; done
        [ "${FAKE_RELEASE_FAIL:-0}" = 1 ] && { echo "HTTP 422: tag rejected by a ruleset" >&2; exit 1; }
        git -C "$FAKE_ORIGIN" tag "$tag" "$target"
        ;;
esac
GH
chmod +x "$work/bin/gh"
export PATH="$work/bin:$PATH" GH_LOG="$work/gh.log" FAKE_ORIGIN="$origin"
: >"$GH_LOG"
main() { git -C "$origin" rev-parse main; }
tag_at() { git -C "$origin" rev-parse "$1^{commit}" 2>/dev/null; }

echo "== release.sh: main's CI hasn't passed"
FAKE_CI=in_progress bash "$clone/release.sh" >"$out" 2>&1 && fail "a release with CI running was made" "" || CHECKS=$((CHECKS + 1))
check "it says CI hasn't passed" has "!!! main's CI hasn't passed"
check "and shows what CI is doing" has 'Got:      in_progress'
check "no release was asked for" bash -c '! grep -q "^release" "$1"' _ "$GH_LOG"

echo "== release.sh: GitHub refuses the release"
FAKE_RELEASE_FAIL=1 bash "$clone/release.sh" >"$out" 2>&1 && fail "a refused release was reported as made" "" || CHECKS=$((CHECKS + 1))
check "it says GitHub didn't make it" has "!!! GitHub didn't make the release"
check "it shows GitHub's reason" has 'tag rejected by a ruleset'
check "So far says nothing was released" has 'So far: nothing was released'

echo "== release.sh: a patch release tags main's commit, after the newest by version order"
if bash "$clone/release.sh" >"$out" 2>&1; then CHECKS=$((CHECKS + 1)); else fail "the patch release failed" "$(tail -n 8 "$out")"; fi
check "it's v1.10.1 (after v1.10.0, not v1.2.0 or the decoy)" has 'Releasing v1.10.1'
check "the tag names main's commit" test "$(tag_at v1.10.1)" = "$(main)"
check "gh got main's commit as the target" grep -q -- "--target $(main)" "$GH_LOG"
check "the CI check asked was 'test' on main's commit" grep -q "commits/$(main)/check-runs?check_name=test" "$GH_LOG"
check "the clone has the tag too" git -C "$clone" show-ref -q --verify refs/tags/v1.10.1
check "nothing was committed to main" test "$(git -C "$origin" log --format=%s -1 main)" = three
check "the clone's local edit is untouched" grep -q 'must not matter' "$clone/f"

echo "== release.sh: nothing new on main"
bash "$clone/release.sh" >"$out" 2>&1 && fail "main was released twice" "" || CHECKS=$((CHECKS + 1))
check "it says main is already released" has '!!! main is already released'
check "and names the release" has 'main is v1.10.1'

echo "== release.sh: minor, major and a named version"
echo four >"$origin/f"; "${G[@]}" -C "$origin" commit -qam four
bash "$clone/release.sh" minor >"$out" 2>&1 || fail "the minor release failed" "$(tail -n 8 "$out")"
check "minor is v1.11.0" test "$(tag_at v1.11.0)" = "$(main)"
echo five >"$origin/f"; "${G[@]}" -C "$origin" commit -qam five
bash "$clone/release.sh" major >"$out" 2>&1 || fail "the major release failed" "$(tail -n 8 "$out")"
check "major is v2.0.0" test "$(tag_at v2.0.0)" = "$(main)"
echo six >"$origin/f"; "${G[@]}" -C "$origin" commit -qam six
bash "$clone/release.sh" 2.3.0 >"$out" 2>&1 || fail "the named release failed" "$(tail -n 8 "$out")"
check "2.3.0 is tagged v2.3.0" test "$(tag_at v2.3.0)" = "$(main)"

echo "== release.sh: bad and old versions are refused"
echo seven >"$origin/f"; "${G[@]}" -C "$origin" commit -qam seven
bash "$clone/release.sh" v2.2.9 >"$out" 2>&1 && fail "an older version was released" "" || CHECKS=$((CHECKS + 1))
check "an older one says it isn't newer" has '!!! not newer than v2.3.0'
bash "$clone/release.sh" v2.3.0 >"$out" 2>&1 && fail "the same version was released again" "" || CHECKS=$((CHECKS + 1))
check "the same one is refused too" has '!!! not newer than v2.3.0'
for bad in 1.2 v1.02.0 latest "v1.2.3; echo hi"; do
    bash "$clone/release.sh" "$bad" >"$out" 2>&1 && fail "'$bad' was accepted" "" || CHECKS=$((CHECKS + 1))
    check "'$bad' says what a version looks like" has '!!! not a version'
done
check "none of them made a tag" test -z "$(git -C "$origin" tag --list 'v2.2.9' 'v1.2' 'v1.02.0')"

echo
if [ "$FAILS" -gt 0 ]; then echo "RELEASE CHECKS FAILED: $FAILS failure(s), $CHECKS passed" >&2; exit 1; fi
echo "RELEASE CHECKS PASSED: $CHECKS checks"
