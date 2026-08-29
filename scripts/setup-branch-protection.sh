#!/usr/bin/env bash
#
# One-shot: apply repo settings + branch-protection rules for main.
#
# Idempotent — running twice makes no additional change; the payload is
# the same. Uses ``gh api`` which reads ~/.config/gh/hosts.yml auth,
# so ``gh auth login`` must have been done first.
#
# What this sets, and why:
#
# 1) Repo settings (``PATCH /repos/{owner}/{repo}``):
#    - ``allow_auto_merge: true``       — enables the "Enable auto-merge"
#                                          button on every PR. Auto-merge
#                                          waits for required checks to
#                                          pass, then merges without
#                                          human intervention.
#    - ``delete_branch_on_merge: true`` — cleans up the source branch
#                                          right after merge (matches the
#                                          manual ``git push --delete``
#                                          I've been running by hand).
#    - Keep all three merge styles enabled; the picker on the PR chooses
#      per-PR.
#
# 2) Branch protection on ``main``
#    (``PUT /repos/{owner}/{repo}/branches/main/protection``):
#    - Required status checks (strict mode → branch must be up-to-date
#      with main before merge):
#          Lint & format (ruff)
#          Type-check (mypy)
#          Tests (pytest) (3.11)
#          Tests (pytest) (3.12)
#          gitleaks
#    - No approving reviews required — solo maintainer, CI is the gate.
#    - Force pushes and deletions blocked.
#    - Conversation-resolution required (comments must be resolved).
#    - ``enforce_admins: false`` — leaves an escape hatch for a
#      genuine hotfix that needs to skip CI. Don't use it casually.

set -euo pipefail

REPO="${GITHUB_REPOSITORY:-outstandingcandy/flight-matrix}"

if ! command -v gh >/dev/null 2>&1; then
    echo "gh (GitHub CLI) is required — install from https://cli.github.com" >&2
    exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
    echo "Run 'gh auth login' first." >&2
    exit 1
fi

echo "==> Repo-level settings for $REPO"
gh api \
    --method PATCH \
    -H "Accept: application/vnd.github+json" \
    "/repos/$REPO" \
    -f "allow_auto_merge=true" \
    -f "delete_branch_on_merge=true" \
    -f "allow_merge_commit=true" \
    -f "allow_squash_merge=true" \
    -f "allow_rebase_merge=true" \
    >/dev/null
echo "    allow_auto_merge, delete_branch_on_merge → true"

echo "==> Branch protection on main"
# The API takes an entire object in one PUT — no partial patches. Every
# field below is required or defaults to a value we don't want.
gh api \
    --method PUT \
    -H "Accept: application/vnd.github+json" \
    "/repos/$REPO/branches/main/protection" \
    --input - <<'JSON' >/dev/null
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "Lint & format (ruff)",
      "Type-check (mypy)",
      "Tests (pytest) (3.11)",
      "Tests (pytest) (3.12)",
      "Tests (pytest) (3.14)",
      "gitleaks"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON
echo "    5 required checks + no-force-push + no-delete + conversation-resolution"

echo
echo "Done. Verify at:"
echo "  https://github.com/$REPO/settings/branches"
