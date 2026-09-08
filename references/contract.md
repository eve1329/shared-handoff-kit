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

`runtime/codex/bin/codex-auto` converts a guarded `resume` into a fresh session with
an explicit restore prompt, backs up and resets the guard, and supports
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
