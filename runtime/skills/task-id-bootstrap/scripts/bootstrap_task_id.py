#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from shared_handoff_lock import exclusive_file_lock


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SESSION_TASKS_REL = Path(".agents/state/session-tasks.json")
CURRENT_TASK_REL = Path(".agents/state/current-task")


PROCESS_TEMPLATE = """## Current Task
- {current_task}

## Done
- Created task state directory at `.agents/state/tasks/{task_id}/`.

## Key Files
- `.agents/state/tasks/{task_id}/process.md`
- `.agents/state/tasks/{task_id}/process.recent.md`

## Verification
- PASS: task state directory initialized for `{task_id}`

## Current Constraints
- Keep this file concise and update it after meaningful progress.

## Next Step
- {next_step}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize repo-local .agents/state/tasks/<task-id>/ state files."
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Absolute or relative path to the repository root.",
    )
    parser.add_argument(
        "--task-id",
        required=True,
        help="Task identifier to initialize under .agents/state/tasks/.",
    )
    parser.add_argument(
        "--current-task",
        default="New task initialized.",
        help="Initial Current Task line.",
    )
    parser.add_argument(
        "--next-step",
        default="Continue implementing the requested task work.",
        help="Initial Next Step line.",
    )
    parser.add_argument(
        "--transcript-path",
        help="Current rollout transcript path to bind to this task.",
    )
    parser.add_argument(
        "--thread-id",
        help="Current Codex thread id. Defaults to CODEX_THREAD_ID.",
    )
    parser.add_argument(
        "--allow-unbound",
        action="store_true",
        help="Initialize task files without requiring a current-rollout binding.",
    )
    return parser.parse_args()


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    if value:
        return Path(value).expanduser().absolute()
    return Path.home() / ".codex"


def resolve_transcript_path(
    explicit_path: str | None,
    explicit_thread_id: str | None,
) -> tuple[str | None, str]:
    if explicit_path:
        transcript = Path(explicit_path).expanduser().absolute()
        if transcript.is_file():
            return str(transcript), ""
        return None, f"transcript does not exist: {transcript}"

    thread_id = (explicit_thread_id or os.environ.get("CODEX_THREAD_ID") or "").strip()
    if not thread_id:
        return None, "no --transcript-path or CODEX_THREAD_ID was available"
    if not TASK_ID_RE.fullmatch(thread_id):
        return None, f"invalid Codex thread id: {thread_id!r}"

    sessions_root = codex_home() / "sessions"
    matches = sorted(
        str(path)
        for path in sessions_root.rglob(f"rollout-*-{thread_id}.jsonl")
        if path.is_file()
    ) if sessions_root.is_dir() else []
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        return None, f"no rollout transcript matched CODEX_THREAD_ID={thread_id}"
    return None, f"multiple rollout transcripts matched CODEX_THREAD_ID={thread_id}"


def read_session_tasks(path: Path) -> dict:
    if not path.exists():
        return {"sessions": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid session mapping object in {path}")
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise RuntimeError(f"invalid sessions map in {path}")
    return value


def bind_task_session(repo_root: Path, transcript: str, task_id: str) -> str:
    sessions_path = repo_root / SESSION_TASKS_REL
    lock_path = sessions_path.with_suffix(".json.lock")
    with exclusive_file_lock(lock_path):
        session_tasks = read_session_tasks(sessions_path)
        session_tasks["sessions"][transcript] = {
            "task_id": task_id,
            "updated": now(),
        }
        atomic_write_text(
            sessions_path,
            json.dumps(session_tasks, ensure_ascii=False, indent=2) + "\n",
        )

    effective = read_session_tasks(sessions_path)["sessions"].get(transcript)
    if not isinstance(effective, dict) or effective.get("task_id") != task_id:
        raise RuntimeError(f"session binding verification failed for {transcript}")
    return str(effective["task_id"])


def write_if_missing_or_empty(path: Path, content: str) -> None:
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return
    path.write_text(content, encoding="utf-8")


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo).expanduser().resolve()
    task_id = args.task_id.strip()

    if not TASK_ID_RE.fullmatch(task_id):
        raise SystemExit(
            "invalid task-id: use 1-80 ASCII letters, digits, dots, underscores, or hyphens"
        )
    if not repo_root.is_dir():
        raise SystemExit(f"repository path is not a directory: {repo_root}")

    task_dir = repo_root / ".agents" / "state" / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    content = PROCESS_TEMPLATE.format(
        task_id=task_id,
        current_task=args.current_task,
        next_step=args.next_step,
    )

    write_if_missing_or_empty(task_dir / "process.md", content)
    write_if_missing_or_empty(task_dir / "process.recent.md", content)
    atomic_write_text(repo_root / CURRENT_TASK_REL, f"{task_id}\n")

    print(f"Task state: {task_dir}")
    print(f"Current task: {task_id}")

    transcript, resolution_error = resolve_transcript_path(
        args.transcript_path,
        args.thread_id,
    )
    if transcript:
        try:
            effective_task = bind_task_session(repo_root, transcript, task_id)
        except RuntimeError as error:
            resolution_error = str(error)
        else:
            print(f"Session binding: {transcript}")
            print(f"Effective task: {effective_task}")
            return 0

    message = f"Session binding: NOT BOUND ({resolution_error})"
    if args.allow_unbound:
        print(message)
        print("Effective task: NOT VERIFIED")
        return 0

    print(message, file=sys.stderr)
    print(
        "Task files and current-task were initialized, but the current rollout was not bound.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
