#!/usr/bin/env bash
#
# Take an issue: start a branch, mark the issue as being worked on,
# print the issue's contents so the chat-session Claude can read it.
#
# Usage:
#   scripts/claude-take-issue.sh <issue-number>
#
# What this does, in order:
#   1. Fetch the issue via ``gh issue view`` — title + body + labels + url.
#   2. Sanity-check: refuse if the issue already has ``in-progress`` (a
#      previous take-issue is still open somewhere).
#   3. Slugify the title → ``claude/issue-<n>-<slug>``.
#   4. ``git checkout main && git pull && git checkout -b <slug>``.
#   5. Add ``in-progress`` label to the issue.
#   6. Print the issue body (this is Claude's task input).
#
# Failure modes handled:
#   - Issue is closed → refuse.
#   - Working tree has uncommitted changes → refuse (would drag them
#     onto the new branch).
#   - Local main doesn't fast-forward → refuse (rebase / stash first).

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <issue-number>" >&2
    exit 1
fi

ISSUE_NUM="$1"

command -v gh >/dev/null || { echo "gh CLI required" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run 'gh auth login' first." >&2; exit 1; }

# ---- 1. Fetch issue ---------------------------------------------------
issue_json="$(gh issue view "$ISSUE_NUM" \
    --json number,title,body,labels,state,url,author)"

state="$(printf '%s' "$issue_json" | jq -r '.state')"
if [ "$state" != "OPEN" ]; then
    echo "Issue #$ISSUE_NUM is $state, refusing to take it." >&2
    exit 1
fi

# ---- 2. Guard against double-taking -----------------------------------
if printf '%s' "$issue_json" | jq -e '.labels[] | select(.name == "in-progress")' >/dev/null; then
    echo "Issue #$ISSUE_NUM already has 'in-progress' label." >&2
    echo "If a previous branch was abandoned, remove the label manually:" >&2
    echo "  gh issue edit $ISSUE_NUM --remove-label in-progress" >&2
    exit 1
fi

# ---- 3. Slugify the title into a branch name --------------------------
title="$(printf '%s' "$issue_json" | jq -r '.title')"
# Strip a leading "[claude] " if the issue template's prefix survived.
# Then lower-case, keep alnum + dashes, collapse dashes, trim.
slug="$(printf '%s' "$title" \
    | sed -E 's/^\[[a-z0-9]+\][[:space:]]*//i' \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' \
    | cut -c1-40 \
    | sed -E 's/-+$//')"
branch="claude/issue-${ISSUE_NUM}-${slug}"

# ---- 4. Working-tree guard + branch setup -----------------------------
if ! git diff --quiet HEAD 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    echo "Working tree has uncommitted changes. Stash or commit first." >&2
    git status --short >&2
    exit 1
fi

echo "==> git checkout main + fast-forward"
git checkout main
git pull --ff-only origin main

echo "==> new branch: $branch"
git checkout -b "$branch"

# ---- 5. Mark the issue ------------------------------------------------
echo "==> labelling issue #$ISSUE_NUM with 'in-progress'"
gh issue edit "$ISSUE_NUM" --add-label in-progress >/dev/null

# ---- 6. Print the task ------------------------------------------------
cat <<EOF

========================================
Issue #$ISSUE_NUM taken.
$(printf '%s' "$issue_json" | jq -r '.url')

Branch: $branch
Title:  $title
Author: $(printf '%s' "$issue_json" | jq -r '.author.login')
Labels: $(printf '%s' "$issue_json" | jq -r '.labels | map(.name) | join(", ")')

---- BODY ----
$(printf '%s' "$issue_json" | jq -r '.body')
--------------

Next: make the code changes, commit, then run:
  scripts/claude-open-pr.sh $ISSUE_NUM "your commit-shaped PR title"
========================================
EOF
