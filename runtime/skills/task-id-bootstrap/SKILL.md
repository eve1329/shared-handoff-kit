---
name: task-id-bootstrap
description: Use when the user says `新开task-id=...`, `新开 task-id=...`, `继续，task-id=...`, `开 task-id=...`, `创建 task-id`, `初始化 task-id`, or otherwise expects repo-local task state to become active for the current Codex task.
---

# Task ID Bootstrap

## Overview

Interpret `task-id` requests as filesystem and rollout state, not just
conversational scope. A task is open only when its state files exist and the
current Codex rollout is bound to it.

## Outcome Contract

Treat these as separate results:

| Result | Required proof |
|---|---|
| Task initialized | `.agents/state/tasks/<task-id>/process.md` and `process.recent.md` exist |
| Task activated | `.agents/state/current-task` contained `<task-id>` when bootstrap completed |
| Current rollout bound | `session-tasks.json` maps the exact current transcript to `<task-id>` |

**Do not say the current task was switched unless all three are verified.** A
created directory by itself is only an initialized task.

## Workflow

1. Read repo instructions first.
   Check `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, and existing `.agents/state/` files before acting.

2. Resolve the requested task id.
   Accept adjacent Chinese and punctuation forms such as
   `新开task-id=init-kmp`, `新开 task-id=init-kmp`, and
   `继续，task-id=init-kmp`. Reject values outside
   `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`.

3. Run the bundled bootstrap script in strict mode.
   It creates the task files, activates `current-task`, resolves the unique
   transcript from `CODEX_THREAD_ID`, binds it, and verifies the stored value.

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/task-id-bootstrap/scripts/bootstrap_task_id.py" \
  --repo /absolute/path/to/repo \
  --task-id init-kmp
```

   On Windows PowerShell, resolve the same home directory and use the Python Launcher:

```powershell
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
py -3 (Join-Path $codexHome "skills\task-id-bootstrap\scripts\bootstrap_task_id.py") `
  --repo C:\absolute\path\to\repo `
  --task-id init-kmp
```

   If `py` is unavailable, use `python` in place of `py -3`. The bundled state lock
   chooses `msvcrt` on Windows and `fcntl` on POSIX; do not replace it with an unlocked
   write when adapting this script.

   If the exact transcript path is available, pass it explicitly:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/task-id-bootstrap/scripts/bootstrap_task_id.py" \
  --repo /absolute/path/to/repo \
  --task-id init-kmp \
  --transcript-path /absolute/path/to/rollout.jsonl
```

   Use `--allow-unbound` only for explicitly requested offline initialization.
   That mode does not switch the current Codex rollout.

4. Require successful binding output.
   A complete switch prints all of these and exits zero:

```text
Task state: .../.agents/state/tasks/init-kmp
Current task: init-kmp
Session binding: /absolute/path/to/rollout.jsonl
Effective task: init-kmp
```

   Exit code `2` with `Session binding: NOT BOUND` is a partial result. Report
   that the files and global pointer were initialized, but the current rollout
   was not bound. Never rewrite that result as success.

5. Initialize and maintain concise task state.
   Keep `process.md` and `process.recent.md` action-oriented. Include:
   - Current Task
   - Done
   - Key Files
   - Verification
   - Current Constraints
   - Next Step

6. Keep task state isolated.
   After a task id is active, read and update only
   `.agents/state/tasks/<task-id>/process.md` and its sibling fallback files.
   Every rollout is task-local; a rollout without an explicit, mapped, or
   current task uses `.agents/state/tasks/main/`. Root `process.md`, auto,
   recent, and guard files are legacy payloads and must never be read or written.

7. Confirm each result separately.
   Report the task path and bound transcript. Verify branch creation separately.
   `current-task` is a shared last-active pointer and another Codex session may
   legitimately change it later; `session-tasks.json` is authoritative for
   restoring a particular rollout.

## State Template

Use this structure for both `process.md` and `process.recent.md` unless the repo already defines a stronger format:

```markdown
## Current Task
- <one-line task>

## Done
- <completed item>

## Key Files
- `<path>`

## Verification
- PASS: `<command or inspection>`
- NOT RUN: `<command>` -> <reason>

## Current Constraints
- <constraint>

## Next Step
- <smallest next action>
```

## Common Mistakes

| Mistake | Fix |
|---|---|
| Creating only a git branch | Treat branch creation and task-id creation as separate responsibilities. |
| Updating root `.agents/state/process.md` | Always use `.agents/state/tasks/<task-id>/`; use `main` when no task is selected. |
| Synchronizing task progress into root `process.md` | Root payload files are retired and must remain ignored. |
| Reporting success after only creating the folder | Require `Session binding` and matching `Effective task` output. |
| Treating `current-task` as a permanent session binding | Use the exact transcript entry in `session-tasks.json` for per-rollout recovery. |
| Guessing among multiple transcript matches | Stop with a partial result and request an exact `--transcript-path`. |
| Guessing the repo root from memory | Use the current workspace root or a user-provided repo path. |

## Example

User request:

```text
新开task-id=init-kmp，然后开个 init-kmp 分支
```

Expected outcome:
- Create `.agents/state/tasks/init-kmp/`
- Initialize `process.md` and `process.recent.md`
- Write `init-kmp` to `.agents/state/current-task`
- Bind the exact current rollout entry in `session-tasks.json` to `init-kmp`
- Create or switch to branch `init-kmp`
- Report state creation, rollout binding, and branch switching separately
