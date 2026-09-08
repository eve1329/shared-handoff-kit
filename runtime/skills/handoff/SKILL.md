---
name: handoff
description: Use when the user asks to hand off ongoing work to a new thread or session, continue from a previous handoff, restore project state from files instead of chat history, or mentions handoff, 交接, 新线程继续, resume, session-handoff, or context switching.
---

# Handoff

## Overview

Create a compact, evidence-driven handoff so a fresh agent can continue the work without relying on chat history.

**Core principle:** transfer state, proof, and the next decision; do not transfer a vague story.

## When to Use

- The user asks for `handoff`, `交接`, `新开线程继续`, `继续上次`, `resume`, or `session-handoff`.
- The current thread is getting long and the next work should continue in a fresh thread or later session.
- The user wants a "next-thread prompt" that can be pasted into a new conversation.
- A task is partially complete and another agent or future session must pick it up safely.
- The repo has explicit state files such as `progress.md`, `session-state.md`, `todo.md`, `verification.md`, or a project-specific resume prompt.

Do not use this skill for a normal completion summary when the work is already finished and nobody needs to continue it.

## Two Modes

### 1. Export Handoff

Use when ending the current round and preparing another thread or session to continue.

### 2. Resume From Handoff

Use when starting from a prior handoff and reconstructing state in a fresh context.

## Export Workflow

### 1. Rebuild the real state from artifacts

Prefer current artifacts over memory:

- Current workspace path
- Current branch and dirty-worktree state
- Files actually changed
- Commands actually run
- Verification that passed, failed, or was not run
- Existing state docs such as `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `progress.md`, `session-state.md`, `todo.md`, `verification.md`, `spec.md`, `evaluation.md`
- Task-relevant docs and touched files only

### 2. Compress into decision-useful facts

A good handoff contains:

- What the current task is
- What is already done and should not be redone
- Which files matter most
- What has been verified, with exact commands
- What is still blocked or unverified
- The next smallest meaningful step
- Important constraints such as dirty worktree, missing tools, or "do not revert unrelated changes"

### 3. Separate facts from assumptions

Mark each uncertain item clearly:

- `Verified`: confirmed by current files, command output, or tests
- `Not verified`: not run in this session or blocked by environment
- `Assumption`: reasoned guess that the next session should confirm quickly

Never blur these together.

### 4. Update state files when the project uses them

If the repo already keeps handoff-friendly state, update it before ending the round:

- `progress.md`
- `session-state.md`
- `todo.md`
- `verification.md`
- `evaluation.md`
- project-specific resume or handoff prompts

Do not invent a parallel state system when one already exists.

### 5. Produce the handoff in the smallest useful format

Prefer one of these:

- A **minimal next-thread prompt** for straightforward continuation
- A **full handoff package** for complex or risky work

Do not dump every explored branch. Keep only the branches that affect the next move.

## Resume Workflow

### 1. Restore from files, not chat

In a fresh thread, prefer:

- Project instructions: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`
- State files: `progress.md`, `session-state.md`, `todo.md`, `verification.md`, `spec.md`, `evaluation.md`
- Git state: branch, status, and relevant diffs
- Task-specific docs and touched files

If the project has a reset or bootstrap script, run that first.

### 2. Reconstruct four things quickly

Before changing code, establish:

1. What is the goal right now?
2. What work is already done?
3. What proof exists that it works or fails?
4. What is the next smallest step?

### 3. Trust current repo state over stale handoff text

If the handoff says one thing but the files or git state say another, trust the current repo state and call out the mismatch explicitly.

### 4. Continue with one concrete next action

Do not spend the first turn re-summarizing everything. Restore the minimum needed context, tell the user what you reconstructed, then take the next step.

## Required Contents

| Section | Must include |
|---|---|
| Workspace | Absolute path and branch |
| Task | Current task or target outcome |
| Done | What is complete and should not be repeated |
| Key files | The files or modules that matter most |
| Verification | Exact commands and current status |
| Risks | Blockers, missing tools, or open questions |
| Next step | One recommended next move |

## Minimal Next-Thread Prompt

Use this when the continuation is straightforward.

```text
Workspace: /absolute/path/to/repo
Branch: branch-name
Current date: YYYY-MM-DD

Working rules:
- Do not revert unrelated dirty-worktree changes.
- Prefer minimal changes and immediate re-verification.

Current task:
- <one clear task>

Already completed:
- <completed item 1>
- <completed item 2>

Key files:
- /absolute/path/to/file1
- /absolute/path/to/file2

Verification:
- PASS: `<exact command>`
- FAIL: `<exact command>` -> <short reason>
- NOT RUN: `<exact command>` -> <why not>

Important constraints:
- <tooling, environment, or repo constraints>

Next step:
1. <one concrete next action>

Please continue from the current workspace state. Do not redo the completed fixes unless verification shows they are broken.
```

## Full Handoff Package

Use this when the task is risky, multi-step, or easy to misread.

```text
Workspace: /absolute/path/to/repo
Branch: branch-name
Dirty worktree: yes|no
Current date: YYYY-MM-DD

Mission:
- <what outcome this work is trying to achieve>

Current task:
- <what the next session should be working on now>

Completed and verified:
- <fact 1>
- <fact 2>

Completed but not yet fully verified:
- <item 1>

Key files and why they matter:
- /absolute/path/to/file1 — <why>
- /absolute/path/to/file2 — <why>

Verification evidence:
- PASS: `<command>`
- PASS: `<command>`
- FAIL: `<command>` -> <reason>
- BLOCKED: `<command>` -> <missing tool or environment issue>

Known failed approaches:
- <approach> -> <why it failed or why not to retry blindly>

Open risks or assumptions:
- Verified: <fact>
- Not verified: <fact>
- Assumption: <fact>

Recommended next step:
1. <first action>
2. <second action only if the first succeeds>
```

## Hard Rules

- Never write "continue from above" or "see previous chat".
- Never omit the verification status.
- Never hide blockers behind optimistic language.
- Never present guesses as completed facts.
- Never tell the next session to "inspect everything" when you already know the likely next step.
- Never forget to mention dirty-worktree constraints when they matter.
- Never ask the next session to redo known-good work without a concrete reason.

## Good Handoff Characteristics

- Short enough to scan quickly
- Specific enough to act on immediately
- Anchored in current files and commands
- Honest about unknowns
- Ends with one recommended next action

## Common Mistakes

### Too vague

Bad:

```text
I fixed a lot of things. Please continue the remaining work.
```

Good:

```text
Current task: make `:example:sample:compileKotlinOhosArm64` pass after the generic data-class export changes.
Next step: rerun `./gradlew :example:sample:compileKotlinOhosArm64 --stacktrace` and inspect generated `ServiceProvider.kt` if it fails.
```

### Too narrative

Do not write a long story of every attempt. Keep only:

- successful changes
- currently relevant failures
- the next move

### Missing proof

Bad:

```text
This should work now.
```

Good:

```text
Verified: `./gradlew :module:test` passed in the current workspace.
Not verified: device-side manual test was not run because `hdc` is unavailable.
```

## Output Style

When the user asks for a handoff, default to:

1. A compact handoff summary
2. A paste-ready next-thread prompt

If the user explicitly wants only one of them, return only that.
