# Shared Handoff Contract

## Files

| Responsibility | Path |
| --- | --- |
| Semantic task state | `.agents/state/tasks/<task-id>/process.md` |
| Hook metadata | `.agents/state/tasks/<task-id>/process.auto.md` |
| Recent fallback | `.agents/state/tasks/<task-id>/process.recent.md` |
| Compact guard | `.agents/state/tasks/<task-id>/context_guard.json` |
| Last-active pointer | `.agents/state/current-task` |
| Per-rollout bindings | `.agents/state/session-tasks.json` and `.json.lock` |

Root `process.md`, `process.auto.md`, `process.recent.md`, `context_guard.json`,
and any resume payload are legacy data. The bundle leaves them untouched and never
uses them for semantic restoration.

## Codex events

`runtime/codex/hooks/context_handoff.py` is registered for:

- `SessionStart(startup|resume|clear|compact)` to inject the task-local state.
- `UserPromptSubmit` to bind task ids and inject context for continuation prompts.
- `PreCompact` and `PostCompact` to write snapshots and update the compact guard.
- `Stop` to persist the latest auto snapshot.

Codex SessionStart and UserPromptSubmit include the absolute active semantic state
path. Same-task files in linked worktrees are reported by path and mtime only
(at most three candidates, newest first); their contents are never imported.
Before cross-worktree implementation, explain which workspace/state is active
and where newer peer state exists. A recent peer mtime is a location hint, not
evidence of completed work, and does not authorize task rebinding or state copying.

Missing/template state or an mtime older than 45 minutes injects a repair
instruction even for ordinary UserPromptSubmit messages. The agent reconciles
the selected task's semantic state and fallback with current artifacts, updates
the six semantic sections, and updates again after meaningful work. Stop returns
`{"decision":"block","reason":"..."}` if repair remains necessary, asking the
agent to perform that routine update before finishing. A true `stop_hook_active`
allows the continued turn to end without a loop; unresolved state remains visible
on the next user turn. Fresh semantic state lets Stop finish normally. Hooks do
not synthesize semantic progress or merely touch process.md.

Recent fallback parses legacy response_item messages/function calls, modern
event_msg/item_completed user/agent messages, command completion (cwd/status/exit),
file changes (paths/actions), MCP status, and custom_tool_call/output. Duplicate
item IDs are collapsed. It keeps the most recent 12 usable events from a bounded
tail of 200 records, excluding reasoning and system/developer messages. Complete
file diffs and MCP argument/result payloads are not copied into that fallback.

## Session ownership and linked worktrees

Codex routing also accepts leading shorthand `继续main`, `main继续`, or
`继续 epay，...` for an existing task (`main` is always available). Explicit
Git branch commands and unknown shorthand words do not select tasks. With Codex
attachment wrappers, task selection uses the `## My request:` body; filenames,
metadata and appended image content do not select tasks. An explicit
`task-id=<id>` can create a new task. Each UserPromptSubmit resolves, writes and
reads back the exact transcript binding under one routing lock, then injects a
verified receipt before model work, even for regular follow-up prompts. Missing
or ambiguous transcript identity is reported as `NOT VERIFIED`; a unique rollout
lookup by thread ID can recover an omitted transcript path.

Binding entries retain `binding_source`, `binding_strength`, `bound_at` and
`binding_workspace` alongside `task_id`/`updated`. Explicit prompt/bootstrap
selections outrank provisional startup/default pointers and legacy mappings;
within the same strength the latest selection wins across linked worktrees of
the same Git repository. Routine hooks update `updated` without advancing
`bound_at`. Stop never reselects a task from cached prompt text. Activation
queries `--binding-status` before falling back to current-task. This mode accepts
hook JSON on stdin and does not write state. Semantic process files stay local.

`context_guard.json` keeps a top-level compatibility snapshot and a `sessions` map
keyed by normalized transcript path (or explicit thread/session ID when no path
is available). Semantic task state stays in the selected worktree's `process.md`.
`SessionStart(startup)` and `SessionStart(clear)` reset only the selected session;
`resume` and `compact` preserve its count. Another session's threshold cannot
block this one. Legacy flat guards with a matching transcript retain their budget.

When restoring the same task in a linked Git worktree, the hook reads only that
task's objective guards across the repository's `git worktree list`, selects the
newest matching session by `context_updated_at`, and writes locally under a task
guard lock with atomic replacement. It never synchronizes semantic state or
resets peer files. Goal metadata updates do not advance the context budget clock.

`task-id-bootstrap` performs a `TaskRestore` through the installed hook's
`--sync-guard` mode after binding. Its output reports session identity, count,
clear requirement and reset evidence. `--guard-status` resolves the same evidence
without writes. Both modes accept hook JSON on stdin. Missing identity is marked
unverified and does not borrow a foreign session's clear requirement.

`runtime/codex/bin/codex-auto` converts a guarded `resume` into a fresh session with
an explicit restore prompt and a guard backup. Session-aware guards require an
explicit matching resume ID; fresh launches and ambiguous `--last`/picker resumes
defer to `SessionStart` so another session cannot trigger a forced clear. The
launcher preserves old session records and lets fresh startup reset the new one.
Legacy guards without session metadata retain their previous behavior. It supports
`--auto-task <id>` and `CODEX_TASK_ID=<id>`.

## Windows Runtime

The installer selects the target platform from the current OS. On Windows it installs
`codex-auto.py`, `codex.cmd`, and `codex-auto.cmd`; Codex and Claude hook commands use
`py -3 "<absolute path>"`. The `.cmd` launchers fall back to `python` when the Python
Launcher is unavailable.

All task/session updates keep an exclusive lock while rereading and atomically replacing
the JSON mapping: POSIX uses `fcntl.flock`; Windows uses `msvcrt.locking` with bounded
retry. The state layout and task resolution order are identical across platforms.

Use `scripts/install.py --platform windows` only to validate a Windows target layout on
a non-Windows machine. Real Windows installation uses the default `--platform auto`.

## Claude events

Claude uses `task_routing.py` for the same resolution and atomic, locked session
mapping. `session_start.py` injects the task state, `task_context.py` handles prompt
markers and continuation keywords, and `context_handoff.py` writes the Stop snapshot.
Claude's Stop output is a `systemMessage`, while context injection uses
`hookSpecificOutput` only on SessionStart/UserPromptSubmit.

## Safety boundaries

- Installer writes only the selected Codex/Claude homes and creates timestamped backups.
- Existing JSON settings are parsed and merged; malformed JSON is reported instead of overwritten.
- No user credentials, model providers, proxy URLs, or project data are part of the bundle.
- A task-id must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$`.
