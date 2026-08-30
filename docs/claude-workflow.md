# Issue → PR workflow via Claude Code

**Pattern.** Task lives as a GitHub issue. You tell Claude Code (in a chat
session) which issue to pick up. Claude reads it, writes the code, opens a
PR that closes the issue. CI + auto-merge + deploy handle the rest.

The point isn't automation of the *thinking* — it's giving the thinking
a persistent home (an issue with title / body / comments) instead of
losing it in a chat transcript.

## How to file a task

Use the ``Claude task`` issue template. Three fields:

- **Goal** — one line, what should be different after this ships.
- **Context / constraints** — optional; conventions to follow, files
  to touch, "don't refactor X".
- **Acceptance** — how you know it's done. Can be as loose as "I'll
  eyeball the PR".

Rough goals are fine — Claude will fill gaps with sensible defaults
rather than pester you back with clarifying questions. If a decision
is genuinely load-bearing, Claude asks; otherwise it commits and moves
on.

## How to hand a task to Claude Code

In this chat session, once the issue is filed, say any of:

- ``handle #42``
- ``开工 #42``
- just ``#42``

Claude runs:

```bash
scripts/claude-take-issue.sh 42
```

which:

- Refuses if the issue is closed or already has ``in-progress`` (a
  previous take-issue is still open somewhere — cleanup path in the
  script's error message).
- Creates a branch named ``claude/issue-42-<slug-from-title>``.
- Adds the ``in-progress`` label to the issue.
- Prints the issue body — that's Claude's task input.

## What Claude does next

- Makes the code changes on the branch.
- Runs local checks (``ruff check``, ``pytest`` for the affected
  scope; not always the full suite locally — CI will run everything).
- Commits with a message shaped like the eventual PR title.
- Opens the PR:

  ```bash
  scripts/claude-open-pr.sh 42 "your commit-shaped PR title"
  ```

  which pushes the branch, opens a PR whose body carries
  ``Closes #42``, enables auto-merge, and swaps the issue's label from
  ``in-progress`` to ``queued-for-merge``.

- After that, no more manual steps:
  - CI runs on the PR
  - CI green → auto-merge fires
  - Merge picks up ``Closes #42`` → GitHub closes the issue
  - The merge to ``main`` triggers ``Deploy web`` → redpanda gets the new
    build

## Labels

- ``claude-task`` — issue was filed with the Claude task template
- ``in-progress`` — Claude is actively on this issue (only one at a
  time; the take-issue script enforces this)
- ``queued-for-merge`` — PR is open with auto-merge on, waiting on CI

## Stopping a task in flight

Two ways:

1. Comment on the issue with anything a human should read. Claude
   watches the chat, not the issue — but if you're already in chat,
   just tell Claude "stop #42" or close the PR yourself.
2. Close the PR. Claude notices the next round-trip and won't
   re-open. Remove the ``in-progress`` label manually so the issue
   can be re-taken later.

## When NOT to file a Claude task

- **Design-heavy work** where the decisions matter more than the code.
  Plan those in chat first, *then* file an issue for the mechanical
  execution.
- **Anything touching ``config/*.yaml`` production values** or GCP
  IAM — Claude can propose changes, but a human should read the PR
  before merge. Turn off auto-merge on those PRs manually
  (``gh pr merge <n> --disable-auto``) if that's the fear.
- **Refactors with no observable outcome** — CI won't tell you if the
  refactor is *good*, only that it doesn't break tests. File as a
  chat-mode task instead.
