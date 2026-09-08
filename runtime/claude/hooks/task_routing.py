#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import tempfile
from typing import Any

from shared_handoff_lock import exclusive_file_lock


DEFAULT_TASK_ID = "main"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
STATE_REL = pathlib.Path(".agents/state")
TASKS_REL = STATE_REL / "tasks"
CURRENT_TASK_REL = STATE_REL / "current-task"
SESSION_TASKS_REL = STATE_REL / "session-tasks.json"


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_task_id(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    return candidate if TASK_ID_RE.fullmatch(candidate) else None


def workspace_root(cwd: pathlib.Path) -> pathlib.Path:
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        return cwd
    return pathlib.Path(value).resolve() if value else cwd


def task_state_dir(root: pathlib.Path, task_id: str | None) -> pathlib.Path:
    return root / TASKS_REL / (normalize_task_id(task_id) or DEFAULT_TASK_ID)


def task_payload_rel(task_id: str | None, filename: str) -> str:
    effective_task = normalize_task_id(task_id) or DEFAULT_TASK_ID
    return str(TASKS_REL / effective_task / pathlib.Path(filename).name)


def current_task_path(root: pathlib.Path) -> pathlib.Path:
    return root / CURRENT_TASK_REL


def session_tasks_path(root: pathlib.Path) -> pathlib.Path:
    return root / SESSION_TASKS_REL


def session_key(session_id: str | None) -> str | None:
    value = (session_id or "").strip()
    return f"claude:{value}" if value else None


def read_current_task(root: pathlib.Path) -> str | None:
    try:
        return normalize_task_id(current_task_path(root).read_text(encoding="utf-8"))
    except OSError:
        return None


def read_session_tasks(root: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(session_tasks_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sessions": {}}
    if not isinstance(value, dict) or not isinstance(value.get("sessions"), dict):
        return {"sessions": {}}
    return value


def read_session_task(root: pathlib.Path, session_id: str | None) -> str | None:
    key = session_key(session_id)
    if not key:
        return None
    entry = read_session_tasks(root)["sessions"].get(key)
    if not isinstance(entry, dict):
        return None
    return normalize_task_id(str(entry.get("task_id") or ""))


def resolve_task_id(
    root: pathlib.Path,
    explicit_task_id: str | None = None,
    session_id: str | None = None,
) -> str:
    return (
        normalize_task_id(explicit_task_id)
        or read_session_task(root, session_id)
        or read_current_task(root)
        or DEFAULT_TASK_ID
    )


def atomic_write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        text=True,
    )
    temporary_path = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def bind_task(root: pathlib.Path, task_id: str | None, session_id: str | None = None) -> str:
    effective_task = normalize_task_id(task_id) or DEFAULT_TASK_ID
    atomic_write_text(current_task_path(root), f"{effective_task}\n")

    key = session_key(session_id)
    if not key:
        return effective_task

    path = session_tasks_path(root)
    lock_path = path.with_suffix(".json.lock")
    with exclusive_file_lock(lock_path):
        value = read_session_tasks(root)
        value["sessions"][key] = {
            "task_id": effective_task,
            "updated": now(),
        }
        atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return effective_task
