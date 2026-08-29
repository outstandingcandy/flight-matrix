# Auto CI/CD bootstrap

Everything the ``chore/auto-ci-cd`` PR adds is dormant until the repo
admin performs a one-time setup on GitHub. This doc is the runbook for
that.

## What lands with the PR (no admin action needed)

- ``.github/dependabot.yml`` — weekly PRs bumping Python / GitHub
  Actions / Docker deps. Fires automatically once merged; no config
  needed on the GitHub side.
- ``.github/workflows/deploy-web.yml`` — deploy pipeline. Present but
  will fail the first run until secrets are added (step 3 below).
- ``scripts/setup-branch-protection.sh`` — the one-shot script that
  applies branch protection + auto-merge repo settings. Run it once
  from a workstation with ``gh auth login`` done (step 1 below).
- ``scripts/pr-open-and-automerge.sh`` — helper for opening a PR and
  flagging it for auto-merge in a single command.

## Step 1 — Branch protection + auto-merge (once)

```bash
gh auth login                       # if not already logged in
scripts/setup-branch-protection.sh  # applies the rules
```

Verify at `https://github.com/outstandingcandy/flight-matrix/settings/branches` —
should show one rule for ``main`` with 5 required checks.

## Step 2 — GitHub secrets for deploy (once)

Under `Settings → Secrets and variables → Actions`, add these four:

| Name | Value | Notes |
|------|-------|-------|
| ``REDPANDA_SSH_KEY`` | Private key contents (whole file, including header + footer) | Read-only key that can pull + ``docker compose`` on the box; do NOT reuse your admin login key. |
| ``REDPANDA_HOST`` | IP or hostname of the VM | e.g. ``136.109.216.214`` or ``redpanda.your-domain.com`` |
| ``REDPANDA_USER`` | SSH username | Usually ``panda`` on the current setup. |
| ``REDPANDA_KNOWN_HOSTS`` | Output of ``ssh-keyscan -H $REDPANDA_HOST`` | Multi-line value; paste as-is. |

## Step 3 — Deploy dry-run

- Merge one no-op PR to main (e.g. a doc typo) and watch
  `Actions → Deploy web` — the SSH session logs should show
  ``git fetch → build → up -d → migration → smoke``.
- If it fails on ``ssh-keygen -F``, the ``REDPANDA_KNOWN_HOSTS`` value
  is stale. Re-run ``ssh-keyscan -H <host>`` locally, paste the whole
  output back into the secret.

## Step 4 — Opening PRs after that

Use ``scripts/pr-open-and-automerge.sh "your title"`` instead of ``gh pr
create``. It opens the PR and immediately marks it for auto-merge —
CI green → merged → deploy fires → new build lives on redpanda.

## Rollback

- **Rollback deploy only** (image is bad, code is fine):
  from the ``Actions → Deploy web`` UI, click ``Run workflow``
  after ``git reset --hard`` main locally to a prior commit and
  ``git push --force``. (Force-push to main is blocked by protection,
  so you'll have to open a "revert" PR.)
- **Rollback code**: revert the offending commit with ``gh pr create``
  from a revert branch, wait for CI, let auto-merge fire, deploy
  workflow ships it.
- **Emergency** (bypass CI): `Settings → Branches → Edit protection`
  toggles off the required-checks rule for a minute. Turn it back on
  after the emergency deploy.

## What the deploy workflow does NOT do

- No blue/green — the ``docker compose up`` restarts the ``web``
  service in-place, so there's a ~5-second window during which
  requests may drop.
- No image versioning — ``docker compose build`` produces
  ``flight-matrix-web:latest`` every time. The previous build is
  garbage-collected next time ``docker image prune`` runs. If you need
  hard rollback to a specific image, tag it manually on the VM before
  the next deploy:
  ```bash
  docker tag flight-matrix-web:latest flight-matrix-web:$(git rev-parse --short HEAD)
  ```
