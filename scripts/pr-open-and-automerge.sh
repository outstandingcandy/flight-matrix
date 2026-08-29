#!/usr/bin/env bash
#
# Open a PR against main and immediately flag it for auto-merge.
#
# Auto-merge is the setting that says "as soon as all required status
# checks pass, click the merge button for me." Branch protection
# (see setup-branch-protection.sh) supplies the required-checks list;
# this script hooks the PR into it.
#
# Usage:
#   scripts/pr-open-and-automerge.sh <title> [body-file]
#
# Example:
#   scripts/pr-open-and-automerge.sh "refactor: rename foo → bar" pr-body.md
#
# The title is the required positional. Body is optional; if omitted,
# ``gh pr create --fill`` populates it from the branch commits.
#
# The --merge style (a real merge commit) is used deliberately — the
# same choice each existing PR on this repo has been merged with, so
# the history stays uniform. Swap to --squash / --rebase per-PR by
# calling ``gh pr merge <n> --auto --squash`` yourself after opening.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <title> [body-file]" >&2
    exit 1
fi

title="$1"
body_file="${2:-}"

if ! command -v gh >/dev/null 2>&1; then
    echo "gh (GitHub CLI) is required — install from https://cli.github.com" >&2
    exit 1
fi

# Refuse to open PRs from main itself — that means someone forgot to
# branch. The upstream CLI errors clearly, but this earlier check
# saves a round-trip.
branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" = "main" ]; then
    echo "Refusing to open a PR from main. Create a feature branch first." >&2
    exit 1
fi

# Push the branch upstream if it isn't already there.
if ! git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    git push -u origin "$branch"
fi

if [ -n "$body_file" ]; then
    pr_url=$(gh pr create --base main --head "$branch" --title "$title" --body-file "$body_file")
else
    pr_url=$(gh pr create --base main --head "$branch" --title "$title" --fill)
fi
echo "Opened: $pr_url"

# Extract PR number from URL (last path segment).
pr_num="${pr_url##*/}"

# Enable auto-merge (merge commit style). The command exits non-zero if
# branch protection isn't configured yet — that's a hard failure, since
# the whole point is auto-merge-on-checks-green.
gh pr merge "$pr_num" --auto --merge
echo "Auto-merge enabled for PR #$pr_num — will fire on CI-green."
