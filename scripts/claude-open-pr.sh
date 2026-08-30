#!/usr/bin/env bash
#
# Open a PR that closes an issue, enable auto-merge, swap the issue's
# labels. Complements ``claude-take-issue.sh``.
#
# Usage:
#   scripts/claude-open-pr.sh <issue-number> "PR title"
#
# What this does:
#   1. Push the current branch upstream (with -u so future pushes work).
#   2. ``gh pr create`` with a body containing ``Closes #N`` so a
#      merge auto-closes the issue.
#   3. ``gh pr merge --auto --merge`` — waits for CI, then merges. Uses
#      the same branch-protection + auto-merge chain the rest of the
#      repo runs on.
#   4. Swap the issue's labels: remove ``in-progress``, add
#      ``queued-for-merge``. When CI merges the PR, ``Closes #N`` fires
#      and the issue closes automatically — the label is just a
#      dashboard cue between "opened PR" and "actually merged".
#
# Assumptions:
#   - We're on the branch ``claude-take-issue.sh`` created (or another
#     feature branch — the script doesn't enforce the name pattern).
#   - Branch protection on main is configured (auto-merge won't fire
#     otherwise; the PR would just sit).

set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <issue-number> <PR title>" >&2
    exit 1
fi

ISSUE_NUM="$1"
TITLE="$2"

command -v gh >/dev/null || { echo "gh CLI required" >&2; exit 1; }

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" = "main" ]; then
    echo "Refusing to open a PR from main." >&2
    exit 1
fi

# ---- 1. Push --------------------------------------------------------
if ! git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    git push -u origin "$branch"
else
    git push
fi

# ---- 2. Body with Closes-magic --------------------------------------
# GitHub matches ``Closes #N`` / ``Fixes #N`` in the PR body (or any
# commit in the PR); merging the PR then closes the issue. This is why
# we drop the ``in-progress`` label at PR open time — the merge does
# the actual close for us.
body="$(cat <<EOF
## Summary

$(git log main..HEAD --pretty=format:'- %s' 2>/dev/null || echo '- (see commits)')

Closes #$ISSUE_NUM

---
🤖 Opened by Claude Code (per issue #$ISSUE_NUM).
EOF
)"

pr_url="$(gh pr create --base main --head "$branch" --title "$TITLE" --body "$body")"
echo "Opened: $pr_url"
pr_num="${pr_url##*/}"

# ---- 3. Auto-merge --------------------------------------------------
gh pr merge "$pr_num" --auto --merge
echo "Auto-merge enabled on PR #$pr_num."

# ---- 4. Swap issue labels ------------------------------------------
gh issue edit "$ISSUE_NUM" \
    --remove-label in-progress \
    --add-label queued-for-merge >/dev/null
echo "Issue #$ISSUE_NUM: in-progress → queued-for-merge."
echo "(will auto-close on PR merge via Closes-magic)"
