#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# release.sh — cut a release in one command: tags main's current commit vX.Y.Z
# with a signed tag and publishes the release. Dev-machine tool; needs gh (gh auth
# login), git 2.34+ and an SSH signing key (OPS-75):
#
#   git config --global gpg.format ssh
#   git config --global user.signingkey ~/.ssh/id_ed25519.pub   # a key listed in allowed_signers
#
#   ./release.sh           # next patch  (v1.4.0 -> v1.4.1)
#   ./release.sh minor     #             (v1.4.0 -> v1.5.0)
#   ./release.sh major     #             (v1.4.0 -> v2.0.0)
#   ./release.sh v1.6.0    # a specific version, newer than the newest
#
# The tag is the version. Nothing in the code is bumped, so nothing is
# committed: the tag goes on a commit main already has. It releases GitHub's
# main as it is now (not this clone's files), and only once main's CI (the
# "test" check) has passed on that commit.
#
# Boxes install only tags signed by a key in their trusted allowed_signers
# (OPS-75), so the tag is made and signed here, checked against main's
# allowed_signers, pushed, and only then published as a release.
#
# Boxes move to the newest tag with ./update.sh, reading the private repo with
# their own deploy key (OPS-19).
# ---------------------------------------------------------------------------
set -Eeuo pipefail
# Every pattern here ([!-~], [0-9], [a-z]) means ASCII. Under a locale such as en_US.UTF-8, bash's
# =~ takes ranges from the collation order instead, and [!-~] then refused every real Pane token
# (Nov 13, the first box after pane). Assigning LC_ALL makes bash switch locale right here.
export LC_ALL=C

SLUG="maloriedelilah/system-telemetry"
# Where the signed tag is pushed and main is fetched from. HTTPS with gh as git's credential helper
# (gh auth setup-git); the tests point it at a local repo.
URL="${TELEMETRY_RELEASE_URL:-https://github.com/$SLUG.git}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SO_FAR="nothing was released"
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
# The house error format: what was tried, what was expected, what happened, what to do next.
report() {
    printf '\n\033[1;31m!!! %s\033[0m\n    Did:      %s\n    Expected: %s\n    Got:      %s\n    Next:     %s So far: %s.\n' \
        "$1" "$2" "$3" "$4" "$5" "$SO_FAR" >&2
}
# Only the main shell reports: set -E carries the trap into $( ) subshells, which printed it twice.
on_err() {
    local rc=$?
    [[ $BASHPID == "$$" ]] || return 0
    report "release.sh stopped" "line $1" "it to succeed" "exit $rc" "read the error above and run it again."
}
trap 'on_err $LINENO' ERR

if ! command -v gh >/dev/null; then
    report "gh not found" "command -v gh" "the GitHub CLI installed" "not on PATH" "install it, run gh auth login, then ./release.sh again."
    exit 1
fi

for t in git ssh-keygen; do
    if ! command -v "$t" >/dev/null; then
        report "$t not found" "command -v $t" "$t on PATH (signed tags need git 2.34+ and ssh-keygen)" "not on PATH" "install it (Git for Windows has both), then ./release.sh again."
        exit 1
    fi
done
signkey="$(git config --get user.signingkey || true)"
if [[ -z "$signkey" || "$(git config --get gpg.format || true)" != ssh ]]; then
    report "no SSH signing key set up" "git config gpg.format and user.signingkey" "gpg.format=ssh and your signing key" \
        "gpg.format=$(git config --get gpg.format || echo unset), user.signingkey=${signkey:-unset}" \
        "run: git config --global gpg.format ssh && git config --global user.signingkey ~/.ssh/id_ed25519.pub (a key listed in allowed_signers), then ./release.sh again."
    exit 1
fi
err="$(mktemp)"; work="$(mktemp -d)"; trap 'rm -rf "$err" "$work"' EXIT

# --- what GitHub has: main's commit and the release tags -------------------
# All read from $SLUG on GitHub, the repo the release is made on (not this clone's origin).
if ! main_sha="$(gh api "repos/$SLUG/commits/main" --jq '.sha' 2>"$err")" || [[ ! "$main_sha" =~ ^[0-9a-f]{40}$ ]]; then
    report "could not read main" "gh api repos/$SLUG/commits/main" "main's commit" "$(tr '\n' ' ' <"$err")${main_sha}" "check gh auth status and your access to $SLUG, then run it again."
    exit 1
fi
if ! tags="$(gh api --paginate "repos/$SLUG/git/matching-refs/tags/v" --jq '.[].ref' 2>"$err")"; then
    report "could not list the releases" "gh api repos/$SLUG/git/matching-refs/tags/v" "the vX.Y.Z tags" "$(tr '\n' ' ' <"$err")" "check gh auth status, then run it again."
    exit 1
fi
# Exactly refs/tags/vX.Y.Z with no leading zeros, as update.sh picks them.
newest="$(printf '%s\n' "$tags" | awk '$1 ~ /^refs\/tags\/v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/ { sub(/^refs\/tags\//, "", $1); print $1 }' | awk '{ split(substr($0, 2), p, "."); k = sprintf("%012d%012d%012d", p[1], p[2], p[3]); if (k > best) { best = k; t = $0 } } END { if (t != "") print t }')"

if [[ -n "$newest" ]]; then
    # The commit the newest tag names: GitHub peels an annotated tag to its commit here.
    if ! newest_sha="$(gh api "repos/$SLUG/commits/refs/tags/$newest" --jq '.sha' 2>"$err")"; then
        report "could not read $newest" "gh api repos/$SLUG/commits/refs/tags/$newest" "its commit" "$(tr '\n' ' ' <"$err")" "check gh auth status, then run it again."
        exit 1
    fi
    if [[ "$newest_sha" == "$main_sha" ]]; then
        report "main is already released" "compare main with $newest" "a commit on main since $newest" "main is $newest (${main_sha:0:7})" "merge something first; there's nothing new to release."
        exit 1
    fi
fi
newest="${newest:-v0.0.0}"

# --- the next version -------------------------------------------------------
IFS=. read -r MA MI PA <<<"${newest#v}"
arg="${1:-patch}"
case "$arg" in
    patch) PA=$((PA + 1)) ;;
    minor) MI=$((MI + 1)); PA=0 ;;
    major) MA=$((MA + 1)); MI=0; PA=0 ;;
    *)
        if [[ ! "$arg" =~ ^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
            report "not a version" "read the argument" "patch, minor, major or vX.Y.Z (e.g. v1.6.0)" "'$arg'" "run ./release.sh with one of those."
            exit 1
        fi
        MA="${BASH_REMATCH[1]}"; MI="${BASH_REMATCH[2]}"; PA="${BASH_REMATCH[3]}"
        ;;
esac
TAG="v$MA.$MI.$PA"
if [[ "$(printf '%s\n%s\n' "$newest" "$TAG" | awk '{ split(substr($0, 2), p, "."); k = sprintf("%012d%012d%012d", p[1], p[2], p[3]); if (k > best) { best = k; t = $0 } } END { if (t != "") print t }')" != "$TAG" || "$TAG" == "$newest" ]]; then
    report "not newer than $newest" "compare $TAG with the newest release" "a version after $newest" "$TAG" \
        "pick a newer version: boxes take the newest tag, so an older one would never be used."
    exit 1
fi

# --- main's CI on that commit -----------------------------------------------
if ! ci="$(gh api "repos/$SLUG/commits/$main_sha/check-runs?check_name=test" --jq '.check_runs[0].conclusion // .check_runs[0].status // "none"' 2>"$err")"; then
    report "could not read main's CI" "gh api .../commits/${main_sha:0:7}/check-runs" "the test check's result" "$(tr '\n' ' ' <"$err")" "check gh auth status, then run it again."
    exit 1
fi
if [[ "$ci" != success ]]; then
    report "main's CI hasn't passed" "read the test check on main (${main_sha:0:7})" "success" "${ci:-none}" \
        "wait for CI to finish (or fix main), then run ./release.sh again."
    exit 1
fi

# --- the signed tag (OPS-75) ------------------------------------------------
say "Releasing $TAG at main ${main_sha:0:7} (newest was $newest)"
if ! git fetch -q "$URL" refs/heads/main 2>"$err"; then
    report "could not fetch main" "git fetch $URL main" "main's commit, to tag it here" "$(tr '\n' ' ' <"$err")" \
        "check you can read the repo with git (gh auth setup-git), then run it again."
    exit 1
fi
if [[ "$(git rev-parse FETCH_HEAD)" != "$main_sha" ]]; then
    report "main moved while releasing" "compare the fetched main with GitHub's" "${main_sha:0:7}" "$(git rev-parse --short FETCH_HEAD)" "run ./release.sh again."
    exit 1
fi
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    report "$TAG already exists in this clone" "git rev-parse refs/tags/$TAG" "no local $TAG" "it exists" \
        "if it's left from a release that stopped, delete it (git tag -d $TAG) and run ./release.sh $TAG again."
    exit 1
fi
if ! git tag -s -m "$TAG" "$TAG" "$main_sha" 2>"$err"; then
    report "could not sign $TAG" "git tag -s $TAG ${main_sha:0:7}" "a tag signed with your SSH key" "$(tr '\n' ' ' <"$err")" \
        "check user.signingkey points at your key (and ssh-agent has it, if the key has a passphrase), then run it again."
    exit 1
fi
# Checked against main's own allowed_signers, as every box will check it, before anyone can see it.
git show "$main_sha:allowed_signers" >"$work/allowed_signers" 2>/dev/null || : >"$work/allowed_signers"
vt=0
git -c gpg.ssh.program=ssh-keygen -c gpg.ssh.allowedSignersFile="$work/allowed_signers" verify-tag "$TAG" >"$err" 2>&1 || vt=$?
# The same test boxes apply: exit 0, a listed signer named, and no "No principal matched".
if [[ "$vt" != 0 ]] || ! grep -q 'Good "git" signature for ' "$err" || grep -qi 'no principal matched' "$err"; then
    git tag -d "$TAG" >/dev/null
    report "your key isn't in allowed_signers" "git verify-tag $TAG against main's allowed_signers" \
        "a signature boxes will accept" "$(tr '\n' ' ' <"$err")" \
        "boxes would refuse this tag. Add your key to allowed_signers by PR first (or sign with the key that's listed), then run it again."
    exit 1
fi
if ! git push -q "$URL" "refs/tags/$TAG" 2>"$err"; then
    git tag -d "$TAG" >/dev/null
    report "could not push $TAG" "git push $URL refs/tags/$TAG" "the signed tag on GitHub" "$(tr '\n' ' ' <"$err")" \
        "fix the cause (gh auth setup-git; the v* tag ruleset allows you), then run ./release.sh $TAG again."
    exit 1
fi
SO_FAR="the signed tag $TAG is on GitHub; its release page isn't made"
# --- release ----------------------------------------------------------------
if ! gh release create "$TAG" --repo "$SLUG" --verify-tag --title "$TAG" --generate-notes --latest >"$err" 2>&1; then
    report "GitHub didn't make the release page" "gh release create $TAG --verify-tag" "the release page for the pushed tag" "$(tr '\n' ' ' <"$err")" \
        "boxes can already install $TAG. Make the page with: gh release create $TAG --repo $SLUG --verify-tag --title $TAG --generate-notes --latest"
    exit 1
fi
SO_FAR="$TAG is released"
say "Done: $TAG. On each box: sudo ./update.sh"
