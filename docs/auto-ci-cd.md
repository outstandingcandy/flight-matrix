# Auto CI/CD bootstrap

Everything the ``chore/auto-ci-cd`` + ``chore/deploy-via-wif`` PRs added is
now wired up to the actual GCP project. This doc is the reference for
what's running and what still needs a human.

## Pieces in place

- ``.github/dependabot.yml`` — weekly PRs bumping Python / GitHub
  Actions / Docker deps.
- ``.github/workflows/dependabot-automerge.yml`` — reads each
  Dependabot PR's update-type and calls ``gh pr merge --auto`` on it
  (CI-green → merged).
- ``.github/workflows/deploy-web.yml`` — triggered by ``workflow_run``
  after ``CI`` completes on ``main``. Uses Workload Identity Federation
  to auth against GCP as the ``flight-matrix-deploy`` service account,
  then ``gcloud compute ssh`` into the redpanda VM to pull + build + up.
- ``scripts/setup-branch-protection.sh`` — the one-shot script that
  applied the branch-protection rules + repo-level auto-merge.
- ``scripts/pr-open-and-automerge.sh`` — open a PR and flag it for
  auto-merge in one step.

## Auth: no long-lived secrets, no SSH keys

The deploy workflow authenticates to GCP via **Workload Identity
Federation**: GitHub Actions mints a short-lived OIDC token for each
run; Google's Workload Identity Pool verifies the token was minted by
GitHub Actions on this specific repo and hands back a short-lived
access token that impersonates the ``flight-matrix-deploy`` service
account. No JSON keys, no pem files, no passwords.

**Provider config on GCP** (already applied):

- Pool: ``projects/699601590181/locations/global/workloadIdentityPools/github-actions``
- Provider: ``.../providers/flight-matrix``
- Attribute condition: ``assertion.repository == 'outstandingcandy/flight-matrix'``
  — any other repo's OIDC token, even in this org, is rejected.
- Service account:
  ``flight-matrix-deploy@outstandingcandy.iam.gserviceaccount.com``
  with ``roles/compute.instanceAdmin.v1`` on the project (enough for
  ``gcloud compute ssh``) and ``roles/iam.serviceAccountUser`` on itself.

**Repo config on GitHub** (already applied):

Secrets (values redacted; ``gh secret list`` shows names + timestamps):

| Name | Points at |
|------|-----------|
| ``WIF_PROVIDER`` | The pool provider resource name above. |
| ``WIF_SERVICE_ACCOUNT`` | ``flight-matrix-deploy@outstandingcandy...`` |

Variables (non-secret):

| Name | Value |
|------|-------|
| ``GCP_PROJECT_ID`` | ``outstandingcandy`` |
| ``GCP_VM_NAME`` | ``redpanda`` |
| ``GCP_VM_ZONE`` | ``us-west1-b`` |

## Everyday flow

```bash
git checkout -b chore/foo
# ... commit changes ...
scripts/pr-open-and-automerge.sh "your title"
```

CI green → PR auto-merges → deploy-web workflow fires → gcloud
compute ssh → redpanda pulls the new commit + rebuilds + restarts.
Zero manual steps.

Dependabot every Monday morning: opens PRs, CI runs, green ones
auto-merge, deploy fires. Same flow.

## Rollback

- **Bad deploy, code is fine (image / dep issue)**: from the
  ``Actions → Deploy web`` UI, click ``Run workflow`` after resetting
  ``main`` locally to a prior commit — but force-push to main is
  blocked by protection, so:
- **Bad deploy, code is bad**: revert the offending commit via a
  ``git revert`` + ``scripts/pr-open-and-automerge.sh "revert: …"``.
  CI green → auto-merge → deploy ships the revert.
- **Emergency, bypass CI**: temporarily disable required-checks in
  Settings → Branches → Edit protection, land the fix, turn it back
  on.

## Recreating the WIF setup from scratch

If the pool / provider / SA are ever deleted, here's the exact
``gcloud`` sequence that built them:

```bash
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com \
    sts.googleapis.com --project=outstandingcandy

gcloud iam workload-identity-pools create github-actions \
    --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc flight-matrix \
    --location=global --workload-identity-pool=github-actions \
    --display-name="flight-matrix repo" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.actor=assertion.actor,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository == 'outstandingcandy/flight-matrix'"

gcloud iam service-accounts create flight-matrix-deploy \
    --display-name="Flight Matrix CI deploy"

gcloud projects add-iam-policy-binding outstandingcandy \
    --member="serviceAccount:flight-matrix-deploy@outstandingcandy.iam.gserviceaccount.com" \
    --role="roles/compute.instanceAdmin.v1"

gcloud iam service-accounts add-iam-policy-binding \
    flight-matrix-deploy@outstandingcandy.iam.gserviceaccount.com \
    --role="roles/iam.serviceAccountUser" \
    --member="serviceAccount:flight-matrix-deploy@outstandingcandy.iam.gserviceaccount.com"

gcloud iam service-accounts add-iam-policy-binding \
    flight-matrix-deploy@outstandingcandy.iam.gserviceaccount.com \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/699601590181/locations/global/workloadIdentityPools/github-actions/attribute.repository/outstandingcandy/flight-matrix"
```

Then set the four GitHub-side values back (2 secrets + 3 variables)
as shown above.

## What this still doesn't do

- **No blue/green**: ``docker compose up`` restarts ``web`` in-place;
  ~5 s request drop per deploy.
- **No image versioning**: every build produces ``flight-matrix-web:latest``;
  the previous image is garbage-collected. If you need hard rollback
  to a specific image, tag it manually on the VM before the next deploy:
  ```bash
  docker tag flight-matrix-web:latest flight-matrix-web:$(git rev-parse --short HEAD)
  ```
- **No Slack / Feishu notifications**: workflow failure surfaces via
  GitHub's default email.
