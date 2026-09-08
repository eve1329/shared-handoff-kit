#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook — parse task-id from prompt and inject handoff context.

Task-id patterns supported in user prompts:
  task=<id>
  task-id=<id>
  task_id=<id>
  任务=<id>

ID validation: ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$

Flow:
  1. Parse task-id from prompt
  2. Persist to .agents/state/current-task (for Stop/SessionStart hooks to read)
  3. Inject handoff context if:
     - task-id was explicitly provided in this prompt, OR
     - prompt contains resume/handoff keywords
  4. Context is read from:
     - .agents/state/tasks/<task-id>/process.md  (task-specific, preferred)
     - .agents/state/tasks/<task-id>/process.auto.md  (fallback)
     - a task-local hint when neither file exists

Legacy root process/auto/recent/guard payloads are never read or written.
"""

import json
import os
import pathlib
import re
import sys
from typing import Any

from task_routing import (
    bind_task,
    normalize_task_id,
    resolve_task_id,
    task_payload_rel,
    task_state_dir,
    workspace_root,
)

HANDOFF_KEYWORDS_RE = re.compile(
    r"(继续|直接改|接着|resume|session-handoff|handoff|new context|新上下文|新线程|clear|compact)",
    re.IGNORECASE,
)

TASK_ID_IN_PROMPT_RE = re.compile(
    r"(?:task(?:-id|_id)?|任务)\s*[=＝]\s*([a-zA-Z0-9][a-zA-Z0-9._-]{0,79})",
    re.IGNORECASE,
)

PROCESS_NAME = "process.md"
AUTO_NAME = "process.auto.md"


def resolve_cwd(data: dict[str, Any]) -> pathlib.Path:
    cwd_str = str(data.get("cwd") or os.getcwd())
    return pathlib.Path(cwd_str).expanduser().resolve()


def parse_task_id(text: str) -> str | None:
    match = TASK_ID_IN_PROMPT_RE.search(text)
    if match:
        return normalize_task_id(match.group(1))
    return None


def has_handoff_keywords(prompt: str) -> bool:
    return bool(HANDOFF_KEYWORDS_RE.search(prompt))


def read_context(root: pathlib.Path, task_id: str) -> tuple[str, str]:
    """Read handoff context. Returns (label, content)."""
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
        f"Task `{task_id}` is active but no `{handoff_path}` exists yet.\n"
        f"Semantic task state will be written to `{handoff_path}`.\n"
        f"This file is shared with Codex — both sides read/write the same file.\n",
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

    prompt = str(data.get("prompt") or "")
    root = workspace_root(resolve_cwd(data))

    parsed_task_id = parse_task_id(prompt)
    session_id = str(data.get("session_id") or "")
    task_id = resolve_task_id(root, parsed_task_id, session_id)
    bind_task(root, task_id, session_id)

    # Step 3: Check if we should inject context
    should_inject = has_handoff_keywords(prompt) or (parsed_task_id is not None)
    if not should_inject:
        # No context to inject — output empty JSON
        sys.stdout.write(json.dumps({}))
        sys.stdout.write("\n")
        return 0

    # Step 4: Read and inject context
    label, content = read_context(root, task_id)
    if not content:
        sys.stdout.write(json.dumps({}))
        sys.stdout.write("\n")
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"{label}\n\n{content}",
        },
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
