---
name: Claude task
about: A task you want Claude Code (running in a local chat session) to pick up
title: "[claude] "
labels: ["claude-task"]
---

<!--
The three fields below become the whole prompt Claude Code reads
when you say "handle #N" in chat. Keep them tight — Claude will
fill gaps with sensible defaults rather than pester you with
clarifying questions.
-->

## Goal
<!-- One line: what should be different in the world after this ships. -->

## Context / constraints
<!--
Optional. Anything Claude should know that isn't obvious from the codebase:
- specific files / functions to touch
- conventions to follow or avoid
- links to related PRs / issues
- "don't refactor X, that's stable"
Leave blank if there's nothing to add.
-->

## Acceptance
<!--
How do you know it's done? A test that passes, a curl that returns
X, a page that renders Y. If verification is "I'll eyeball the PR",
say that — it's a valid answer.
-->
