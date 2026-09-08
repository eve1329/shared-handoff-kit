#!/usr/bin/env python3
"""Claude Code SessionStart hook for restoring task-local context.

Resolution uses the Claude session mapping, then a valid current-task pointer,
then the default task ``main``. Payload files are shared with Codex.
"""

import json
import os
import pathlib
import sys
from typing import Any

from task_routing import bind_task, resolve_task_id, task_payload_rel, task_state_dir, workspace_root

PROCESS_NAME = "process.md"
AUTO_NAME = "process.auto.md"


def resolve_cwd(data: dict[str, Any]) -> pathlib.Path:
    cwd_str = str(data.get("cwd") or os.getcwd())
    return pathlib.Path(cwd_str).expanduser().resolve()


def read_context(root: pathlib.Path, task_id: str) -> tuple[str, str]:
    state = task_state_dir(root, task_id)
    process_path = state / PROCESS_NAME
    if process_path.exists():
        try:
            return (
                f"## Task Handoff (Task ID: {task_id})",
                process_path.read_text(encoding="utf-8"),
            )
        except OSError:
            pass
    auto_path = state / AUTO_NAME
    if auto_path.exists():
        try:
            return (
                f"## Task Auto Snapshot (Task ID: {task_id})",
                auto_path.read_text(encoding="utf-8"),
            )
        except OSError:
            pass
    handoff_path = task_payload_rel(task_id, PROCESS_NAME)
    return (
        f"## Task Handoff (Task ID: {task_id})",
        f"Task `{task_id}` is active.\n"
        f"Semantic task state: `{handoff_path}` (shared with Codex).\n",
    )


def main() -> int:
    raw = sys.stdin.read()
    data: dict[str, Any] = {}
    if raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            pass
    if not isinstance(data, dict):
        data = {}

    root = workspace_root(resolve_cwd(data))
    session_id = str(data.get("session_id") or "")
    task_id = resolve_task_id(root, session_id=session_id)
    bind_task(root, task_id, session_id)

    label, content = read_context(root, task_id)
    if not content:
        sys.stdout.write(json.dumps({}))
        sys.stdout.write("\n")
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"{label}\n\n{content}",
        },
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
