#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# release.sh — cut a versioned release and publish it as a PUBLIC GitHub asset.
# Dev-machine tool: run from inside the repo clone. Needs git, gh, tar.
#
#   ./release.sh           # bump patch  (1.0.0 -> 1.0.1)
#   ./release.sh minor     #             (1.0.0 -> 1.1.0)
#   ./release.sh major     #             (1.0.0 -> 2.0.0)
#   ./release.sh 1.4.2      # set explicit version
#
# Bumps telemetry/__init__.py, commits, tags vX.Y.Z, pushes, builds
# system-telemetry.tar.gz, and uploads it to the GitHub release marked latest.
# Public repo => the install URL below is keyless.
# ---------------------------------------------------------------------------
set -euo pipefail

OWNER="maloriedelilah"
REPO="system-telemetry"
ASSET="system-telemetry.tar.gz"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null || die "git not found."
command -v gh  >/dev/null || die "gh (GitHub CLI) not found. Install it, then 'gh auth login'."
command -v tar >/dev/null || die "tar not found."

INIT="telemetry/__init__.py"
cur="$(grep -E "^__version__" "$INIT" | grep -oE "[0-9]+\.[0-9]+\.[0-9]+")"
[[ -n "$cur" ]] || die "Couldn't read current __version__ from $INIT."
IFS=. read -r MA MI PA <<<"$cur"

arg="${1:-patch}"
case "$arg" in
    patch) PA=$((PA + 1)) ;;
    minor) MI=$((MI + 1)); PA=0 ;;
    major) MA=$((MA + 1)); MI=0; PA=0 ;;
    [0-9]*.[0-9]*.[0-9]*)
        MA="${arg%%.*}"; rest="${arg#*.}"; MI="${rest%%.*}"; PA="${rest#*.}" ;;
    *) die "Usage: ./release.sh [patch|minor|major|X.Y.Z]" ;;
esac
NEW="$MA.$MI.$PA"
TAG="v$NEW"
say "Releasing $cur -> $NEW  ($TAG)"

git rev-parse "$TAG" >/dev/null 2>&1 && die "Tag $TAG already exists."
[[ -z "$(git status --porcelain)" ]] || die "Working tree not clean. Commit or stash first."

tmp="$INIT.tmp"
sed "s/^__version__ = \".*\"/__version__ = \"$NEW\"/" "$INIT" > "$tmp" && mv "$tmp" "$INIT"
git add "$INIT"
git commit -m "release: $TAG"
git tag -a "$TAG" -m "system-telemetry $TAG"

say "Pushing commit + tag…"
git push origin HEAD
git push origin "$TAG"

say "Building $ASSET…"
rm -f "$ASSET"
tar czf "$ASSET" \
    --exclude='./.git' --exclude='./.env' --exclude='./venv' \
    --exclude='./logs' --exclude='./__pycache__' \
    --exclude='*/__pycache__' --exclude='*.pyc' \
    --exclude="./$ASSET" \
    -C "$ROOT" .

say "Publishing GitHub release $TAG…"
gh release create "$TAG" "$ASSET" \
    --repo "$OWNER/$REPO" --title "$TAG" --notes "system-telemetry $NEW" --latest

rm -f "$ASSET"
say "Done. Keyless install URL:"
echo "  https://github.com/$OWNER/$REPO/releases/latest/download/$ASSET"
