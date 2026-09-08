# Global Context Handoff

For long-running work, keep one task-local handoff file current. Every rollout
must resolve to a task id before reading or writing handoff state:

- Prefer an explicit task id, then the current session mapping, then a valid
  `.agents/state/current-task` pointer.
- If none exists, use the default task id `main`.
- Use `.agents/state/tasks/<task-id>/process.md` exclusively.

Task-local files are the sole semantic state. Never read, create, update, or
synchronize root payload files such as `.agents/state/process.md`,
`process.auto.md`, `process.recent.md`, or `context_guard.json`; existing root
payload files are legacy data. Root `current-task`, `session-tasks.json`, and
their lock may remain as routing metadata only.

Keep the resolved `process.md` as a concise rolling task state, not a chat
transcript. After any meaningful work turn that changes task state, repo state,
decisions, verification, or the next step, update it briefly. Prefer a small
2-6KB state file over copying logs or conversation history.

Before compact, clear, handoff, or any likely context switch, update that file
with:

- Current Task
- Done
- Key Files
- Verification
- Current Constraints
- Next Step

After resume, compact, or clear, resolve and read the same `process.md` before
continuing. Do not rely on previous chat history after a context switch. Use
the sibling `process.auto.md` only as supporting evidence from hooks; semantic
task state belongs in the resolved `process.md`.

If the resolved `process.md` is missing, stale, or still a TODO template, also
inspect its sibling `process.auto.md` and `process.recent.md` when present.
Treat `process.recent.md` as a lightweight fallback for the last gap before an
automatic compact, not as authoritative state.

For very long tasks, prefer automatic compact for ordinary continuity, but do a
controlled clear after roughly two automatic compacts, after a major phase
change, or when the model starts repeating stale assumptions. A controlled clear
means: update the resolved `process.md`, record verification and next step, then
clear/resume and restore from that same task scope first.

When the user says `继续`, `直接改`, `session-handoff`, `handoff`, or asks to
switch context, restore state from files first, then continue with one concrete
next action.
