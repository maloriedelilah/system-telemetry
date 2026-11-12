#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# release.sh — cut a release in one command: GitHub tags main's current commit
# vX.Y.Z and publishes the release. Dev-machine tool; needs git and gh
# (gh auth login).
#
#   ./release.sh           # next patch  (v1.4.0 -> v1.4.1)
#   ./release.sh minor     #             (v1.4.0 -> v1.5.0)
#   ./release.sh major     #             (v1.4.0 -> v2.0.0)
#   ./release.sh v1.6.0    # a specific version, newer than the newest
#
# The tag is the version. Nothing in the code is bumped, so nothing is
# committed or pushed: GitHub makes the tag on a commit main already has.
# It releases GitHub's main as it is now (not this clone's files), and only
# once main's CI (the "test" check) has passed on that commit.
#
# Boxes move to the newest tag with ./update.sh, reading the private repo with
# their own deploy key (OPS-19).
# ---------------------------------------------------------------------------
set -Eeuo pipefail

SLUG="maloriedelilah/system-telemetry"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SO_FAR="nothing was released"
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
# The house error format: what was tried, what was expected, what happened, what to do next.
report() {
    printf '\n\033[1;31m!!! %s\033[0m\n    Did:      %s\n    Expected: %s\n    Got:      %s\n    Next:     %s So far: %s.\n' \
        "$1" "$2" "$3" "$4" "$5" "$SO_FAR" >&2
}
trap 'report "release.sh stopped" "line $LINENO" "it to succeed" "exit $?" "read the error above and run it again."' ERR

for tool in git gh; do
    if ! command -v "$tool" >/dev/null; then
        report "$tool not found" "command -v $tool" "$tool installed" "not on PATH" "install it (gh: then gh auth login) and run ./release.sh again."
        exit 1
    fi
done

err="$(mktemp)"; trap 'rm -f "$err"' EXIT

# --- what GitHub has: main's commit and the release tags -------------------
if ! main_sha="$(git ls-remote origin refs/heads/main 2>"$err" | awk '{ print $1 }')" || [[ -z "$main_sha" ]]; then
    report "could not read main" "git ls-remote origin refs/heads/main" "main's commit" "$(tr '\n' ' ' <"$err")" "check this clone's origin and your access, then run it again."
    exit 1
fi
if ! tags="$(git ls-remote --tags origin 'v*' 2>"$err")"; then
    report "could not list the releases" "git ls-remote --tags origin" "the vX.Y.Z tags" "$(tr '\n' ' ' <"$err")" "check this clone's origin and your access, then run it again."
    exit 1
fi
# Exactly refs/tags/vX.Y.Z, as update.sh picks them (a tag named like x/refs/tags/v99.0.0 is ignored).
newest="$(printf '%s\n' "$tags" | awk '$2 ~ /^refs\/tags\/v[0-9]+\.[0-9]+\.[0-9]+$/ { sub(/^refs\/tags\//, "", $2); print $2 }' | sort -V | tail -n 1)"
newest="${newest:-v0.0.0}"

# The commit the newest tag names (an annotated tag's peeled ^{} line wins over the tag object).
newest_sha="$(printf '%s\n' "$tags" | awk -v t="refs/tags/$newest" '$2 == t && !c { c = $1 } $2 == t "^{}" { p = $1 } END { print (p ? p : c) }')"
if [[ "$newest_sha" == "$main_sha" ]]; then
    report "main is already released" "compare main with $newest" "a commit on main since $newest" "main is $newest (${main_sha:0:7})" "merge something first; there's nothing new to release."
    exit 1
fi

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
if [[ "$(printf '%s\n%s\n' "$newest" "$TAG" | sort -V | tail -n 1)" != "$TAG" || "$TAG" == "$newest" ]]; then
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

# --- release ----------------------------------------------------------------
say "Releasing $TAG at main ${main_sha:0:7} (newest was $newest)"
if ! gh release create "$TAG" --repo "$SLUG" --target "$main_sha" --title "$TAG" --generate-notes --latest >"$err" 2>&1; then
    report "GitHub didn't make the release" "gh release create $TAG --target ${main_sha:0:7}" "the tag and release" "$(tr '\n' ' ' <"$err")" \
        "fix the cause (gh auth status; the v* tag ruleset allows you), then run ./release.sh $TAG again."
    exit 1
fi
SO_FAR="$TAG is released"
git fetch -q origin "refs/tags/$TAG:refs/tags/$TAG" 2>/dev/null || true
say "Done: $TAG. On each box: sudo ./update.sh"
