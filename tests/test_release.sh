#!/usr/bin/env bash
# tests/test_release.sh — release.sh against a local repo standing in for GitHub, with a fake gh
# that answers like GitHub's REST API (and applies --jq with jq). Run: bash tests/test_release.sh
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SRC="$PWD"
FAILS=0; CHECKS=0
fail() { printf '!!! %s\n    %s\n' "$1" "$2" >&2; FAILS=$((FAILS + 1)); }
check() { local name="$1"; shift; if "$@"; then CHECKS=$((CHECKS + 1)); else fail "$name" "ran: $*"; fi; }
has() { grep -q -- "$1" "$out"; }

work="$(mktemp -d)"; out="$(mktemp)"
trap 'rm -rf "$work" "$out"' EXIT
# The maintainer's own git config mustn't leak in (a global signing key would hide the "no key" case).
export GIT_CONFIG_GLOBAL="$work/gitconfig" GIT_CONFIG_NOSYSTEM=1
: >"$GIT_CONFIG_GLOBAL"
G=(git -c user.name=test -c user.email=test@example.invalid -c init.defaultBranch=main)

# The "GitHub" repo: a lightweight v1.2.0, an annotated v1.10.0 (sort -V, not text order; and
# annotated, so the "already released" check must compare the commit, not the tag object), and
# decoys that must never count as releases: x/refs/tags/v99.0.0, v9.0.08 (a leading zero), and
# v99.0.0-evil.
origin="$work/origin"
mkdir -p "$origin"
"${G[@]}" -C "$origin" init -q
# Signing keys (OPS-75): main lists "trusted" in allowed_signers; "stranger" isn't listed.
ssh-keygen -q -t ed25519 -N '' -C trusted -f "$work/trusted"
ssh-keygen -q -t ed25519 -N '' -C stranger -f "$work/stranger"
printf 'malorie namespaces="git" %s\n' "$(cat "$work/trusted.pub")" >"$origin/allowed_signers"
echo one >"$origin/f"; "${G[@]}" -C "$origin" add f allowed_signers; "${G[@]}" -C "$origin" commit -qm one
"${G[@]}" -C "$origin" tag v1.2.0
echo two >"$origin/f"; "${G[@]}" -C "$origin" commit -qam two
"${G[@]}" -C "$origin" tag -a v1.10.0 -m v1.10.0
"${G[@]}" -C "$origin" tag x/refs/tags/v99.0.0
"${G[@]}" -C "$origin" tag v9.0.08
"${G[@]}" -C "$origin" tag v99.0.0-evil

# The maintainer's clone, with this checkout's release.sh. Its origin is a stale copy at commit one:
# release.sh must read main and the tags from GitHub (the fake gh), never from the clone.
stale="$work/stale"
git clone -q "$origin" "$stale"
git -C "$stale" reset -q --hard HEAD~1
clone="$work/clone"
git clone -q "$stale" "$clone"
cp "$SRC/release.sh" "$clone/"
echo "a local edit that must not matter" >>"$clone/f"
git -C "$clone" config user.name Malorie
git -C "$clone" config user.email malorie@example.invalid
git -C "$clone" config gpg.ssh.program ssh-keygen   # plain ssh-keygen, whatever a global config says

# The fake gh: builds the JSON GitHub's REST API would return, from the "GitHub" repo, and applies
# the caller's --jq to it with jq, so release.sh's jq expressions are what the tests exercise.
# The CI check comes from $FAKE_CI (success, failure, in_progress, none). `gh release create` only
# makes the page: with --verify-tag it fails unless the tag is already on the "GitHub" repo.
mkdir -p "$work/bin"
cat >"$work/bin/gh" <<'GH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_LOG"
o="$FAKE_ORIGIN"
if [ "$1" = release ]; then
    tag="$3"; verify=0
    shift 3
    while [ $# -gt 0 ]; do [ "$1" = --verify-tag ] && verify=1; [ "$1" = --target ] && { echo "--target used" >&2; exit 1; }; shift; done
    [ "${FAKE_RELEASE_FAIL:-0}" = 1 ] && { echo "HTTP 422: release page refused" >&2; exit 1; }
    [ "$verify" = 1 ] || { echo "no --verify-tag" >&2; exit 1; }
    git -C "$o" rev-parse -q --verify "refs/tags/$tag" >/dev/null || { echo "tag $tag doesn't exist" >&2; exit 1; }
    exit 0
fi
path=""; expr="."
while [ $# -gt 0 ]; do
    case "$1" in
        --jq) expr="$2"; shift 2 ;;
        repos/*) path="$1"; shift ;;
        *) shift ;;
    esac
done
case "$path" in
    repos/maloriedelilah/system-telemetry/*) path="${path#repos/maloriedelilah/system-telemetry/}" ;;
    *) echo "HTTP 404: Not Found ($path)" >&2; exit 1 ;;
esac
case "$path" in
    commits/*/check-runs\?check_name=test)
        case "${FAKE_CI:-success}" in
            none) json='{"check_runs":[]}' ;;
            success|failure|cancelled|neutral|skipped)
                json="$(jq -n --arg c "${FAKE_CI:-success}" '{check_runs:[{name:"test",status:"completed",conclusion:$c}]}')" ;;
            *) json="$(jq -n --arg s "$FAKE_CI" '{check_runs:[{name:"test",status:$s,conclusion:null}]}')" ;;
        esac ;;
    commits/*)
        ref="${path#commits/}"
        sha="$(git -C "$o" rev-parse -q --verify "$ref^{commit}")" || { echo "HTTP 422: No commit found for SHA: $ref" >&2; exit 1; }
        json="$(jq -n --arg s "$sha" '{sha:$s}')" ;;
    git/matching-refs/tags/v)
        # Every ref starting refs/tags/v, with the object it points at (an annotated tag's own object).
        json="$(git -C "$o" for-each-ref --format='%(refname) %(objectname) %(objecttype)' refs/tags \
            | awk '$1 ~ /^refs\/tags\/v/' \
            | jq -R 'split(" ") | {ref:.[0], object:{sha:.[1], type:.[2]}}' | jq -s .)" ;;
    *) echo "HTTP 404: Not Found ($path)" >&2; exit 1 ;;
esac
printf '%s\n' "$json" | jq -r "$expr"
GH
chmod +x "$work/bin/gh"
# Windows' sort.exe ahead of GNU sort on PATH (cmd's `sh release.sh`, Nov 13): release.sh must not need sort.
cat >"$work/bin/sort" <<'SORT'
#!/usr/bin/env bash
echo "-VThe system cannot find the file specified." >&2; exit 1
SORT
chmod +x "$work/bin/sort"
export PATH="$work/bin:$PATH" GH_LOG="$work/gh.log" FAKE_ORIGIN="$origin" TELEMETRY_RELEASE_URL="$origin"
: >"$GH_LOG"
main() { git -C "$origin" rev-parse main; }
tag_at() { git -C "$origin" rev-parse "$1^{commit}" 2>/dev/null; }

echo "== release.sh: no SSH signing key set up (OPS-75)"
bash "$clone/release.sh" >"$out" 2>&1 && fail "it ran without a signing key" "" || CHECKS=$((CHECKS + 1))
check "it says how to set one up" has '!!! no SSH signing key set up'
check "with the git config lines" has 'git config --global gpg.format ssh'
git -C "$clone" config gpg.format ssh
git -C "$clone" config user.signingkey "$work/trusted.pub"

echo "== release.sh: main is the newest release, an annotated tag"
bash "$clone/release.sh" >"$out" 2>&1 && fail "main was released again over an annotated tag" "$(tail -n 4 "$out")" || CHECKS=$((CHECKS + 1))
check "it says main is already released" has '!!! main is already released'
check "and names v1.10.0, not a decoy" has 'main is v1.10.0'
check "it read main from GitHub" grep -q 'repos/maloriedelilah/system-telemetry/commits/main' "$GH_LOG"
echo three >"$origin/f"; "${G[@]}" -C "$origin" commit -qam three

echo "== release.sh: main's CI hasn't passed"
FAKE_CI=in_progress bash "$clone/release.sh" >"$out" 2>&1 && fail "a release with CI running was made" "" || CHECKS=$((CHECKS + 1))
check "it says CI hasn't passed" has "!!! main's CI hasn't passed"
check "and shows what CI is doing" has 'Got:      in_progress'
check "no release was asked for" bash -c '! grep -q "^release" "$1"' _ "$GH_LOG"
check "the error is reported once" test "$(grep -c '^.*!!! ' "$out")" = 1
FAKE_CI=failure bash "$clone/release.sh" >"$out" 2>&1 && fail "a release with CI failed was made" "" || CHECKS=$((CHECKS + 1))
check "a failed CI is refused and shown" has 'Got:      failure'
FAKE_CI=none bash "$clone/release.sh" >"$out" 2>&1 && fail "a release with no CI was made" "" || CHECKS=$((CHECKS + 1))
check "no CI at all is refused and shown" has 'Got:      none'
check "still no release asked for" bash -c '! grep -q "^release" "$1"' _ "$GH_LOG"

echo "== release.sh: a key that isn't in allowed_signers is refused before anything is pushed (OPS-75)"
git -C "$clone" config user.signingkey "$work/stranger.pub"
bash "$clone/release.sh" >"$out" 2>&1 && fail "a stranger's tag was released" "" || CHECKS=$((CHECKS + 1))
check "it says the key isn't listed" has "!!! your key isn't in allowed_signers"
check "no tag reached GitHub" test -z "$(git -C "$origin" tag --list v1.10.1)"
check "the local tag was removed" test -z "$(git -C "$clone" tag --list v1.10.1)"
check "So far says nothing was released" has 'So far: nothing was released'
git -C "$clone" config user.signingkey "$work/trusted.pub"

echo "== release.sh: GitHub refuses the release page after the tag is pushed"
FAKE_RELEASE_FAIL=1 bash "$clone/release.sh" >"$out" 2>&1 && fail "a refused release was reported as made" "" || CHECKS=$((CHECKS + 1))
check "it says GitHub didn't make the page" has "!!! GitHub didn't make the release page"
check "it shows GitHub's reason" has 'release page refused'
check "So far says the tag is pushed" has 'So far: the signed tag v1.10.1 is on GitHub'
check "and gives the command to finish" has 'gh release create v1.10.1 --repo maloriedelilah/system-telemetry --verify-tag'
git -C "$origin" tag -d v1.10.1 >/dev/null; git -C "$clone" tag -d v1.10.1 >/dev/null

echo "== release.sh: a patch release tags main's commit, after the newest by version order"
if bash "$clone/release.sh" >"$out" 2>&1; then CHECKS=$((CHECKS + 1)); else fail "the patch release failed" "$(tail -n 8 "$out")"; fi
check "it's v1.10.1 (after v1.10.0; not v1.2.0, v9.0.08 or the decoys)" has 'Releasing v1.10.1'
check "the tag names GitHub's main, not the clone's stale origin" test "$(tag_at v1.10.1)" = "$(main)"
check "the tag on GitHub is annotated and SSH-signed" bash -c 'git -C "$1" cat-file tag v1.10.1 | grep -q "BEGIN SSH SIGNATURE"' _ "$origin"
check "and verifies against main's allowed_signers" git -C "$origin" -c gpg.ssh.program=ssh-keygen -c gpg.ssh.allowedSignersFile="$origin/allowed_signers" verify-tag v1.10.1
check "gh published it with --verify-tag" grep -q -- "release create v1.10.1 .*--verify-tag" "$GH_LOG"
check "the CI check asked was 'test' on main's commit" grep -q "commits/$(main)/check-runs?check_name=test" "$GH_LOG"
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
