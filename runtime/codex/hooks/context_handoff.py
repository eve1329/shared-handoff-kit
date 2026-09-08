#!/usr/bin/env python3
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import sqlite3
import re
import subprocess
import sys
import tempfile
from collections import deque
from typing import Any

from shared_handoff_lock import exclusive_file_lock


PROCESS_REL = ".agents/state/process.md"
AUTO_REL = ".agents/state/process.auto.md"
RECENT_REL = ".agents/state/process.recent.md"
GUARD_REL = ".agents/state/context_guard.json"
STATE_REL = ".agents/state"
TASKS_REL = ".agents/state/tasks"
CURRENT_TASK_REL = ".agents/state/current-task"
SESSION_TASKS_REL = ".agents/state/session-tasks.json"
DEFAULT_TASK_ID = "main"
PROCESS_STALE_MINUTES = 45
RECENT_EVENT_LIMIT = 12
DEFAULT_AUTO_COMPACT_CLEAR_THRESHOLD = 3
GOAL_DB_RELATIVE_PATHS = (
    pathlib.Path("sqlite/goals_1.sqlite"),
    pathlib.Path("goals_1.sqlite"),
)

CONTINUATION_RE = re.compile(
    r"(继续|直接改|接着|resume|session-handoff|handoff|new context|新上下文|新线程|clear|compact)",
    re.IGNORECASE,
)

PROCESS_TEMPLATE_MARKERS = (
    "TODO: keep the active task here before compaction, clear, or handoff.",
    "TODO: record completed work that should not be repeated.",
    "TODO: record task-relevant files with one-line reasons.",
    "TODO: record exact commands and whether they passed, failed, or were not run.",
    "TODO: record the next smallest meaningful action.",
)

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
TASK_ID_PROMPT_RE = re.compile(
    r"(?:(?<![A-Za-z0-9._-])(?:task|task_id|task-id|codex_task|codex-task)|(?:^|\s)任务)"
    r"\s*[:=]\s*"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,79})(?=[^A-Za-z0-9._-]|$)",
    re.IGNORECASE,
)
TASK_ID_LINE_RE = re.compile(
    r"^\s*(?:Task ID|Task|任务)\s*:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,79})\s*$",
    re.IGNORECASE | re.MULTILINE,
)
TRANSCRIPT_SESSION_ID_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,79})\.jsonl$"
)


def read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw_stdin": raw}
    return data if isinstance(data, dict) else {"_raw_stdin": raw}


def run(cmd: list[str], cwd: pathlib.Path) -> str:
    try:
        return subprocess.check_output(
            cmd,
            cwd=str(cwd),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        return ""


def session_cwd(data: dict[str, Any]) -> pathlib.Path:
    value = data.get("cwd")
    if isinstance(value, str) and value:
        return pathlib.Path(value).expanduser().resolve()
    return pathlib.Path(os.getcwd()).resolve()


def session_transcript_path(data: dict[str, Any]) -> str:
    return str(data.get("transcript_path") or "")


def workspace_root(cwd: pathlib.Path) -> pathlib.Path:
    root = run(["git", "rev-parse", "--show-toplevel"], cwd)
    if root:
        return pathlib.Path(root).resolve()
    return cwd


def effective_task_id(task_id: str | None) -> str:
    return normalize_task_id(task_id) or DEFAULT_TASK_ID


def state_root(root: pathlib.Path, task_id: str | None = None) -> pathlib.Path:
    return root / TASKS_REL / effective_task_id(task_id)


def task_state_path(root: pathlib.Path, relative_path: str, task_id: str | None = None) -> pathlib.Path:
    return state_root(root, task_id) / pathlib.Path(relative_path).name


def task_process_path(root: pathlib.Path, task_id: str | None = None) -> pathlib.Path:
    return task_state_path(root, PROCESS_REL, task_id)


def task_auto_path(root: pathlib.Path, task_id: str | None = None) -> pathlib.Path:
    return task_state_path(root, AUTO_REL, task_id)


def task_recent_path(root: pathlib.Path, task_id: str | None = None) -> pathlib.Path:
    return task_state_path(root, RECENT_REL, task_id)


def task_guard_path(root: pathlib.Path, task_id: str | None = None) -> pathlib.Path:
    return task_state_path(root, GUARD_REL, task_id)


def task_current_path(root: pathlib.Path) -> pathlib.Path:
    return root / CURRENT_TASK_REL


def task_sessions_path(root: pathlib.Path) -> pathlib.Path:
    return root / SESSION_TASKS_REL


def task_sessions_lock_path(root: pathlib.Path) -> pathlib.Path:
    return task_sessions_path(root).with_suffix(".json.lock")


def normalize_task_id(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate or not TASK_ID_RE.fullmatch(candidate):
        return None
    return candidate


def codex_home() -> pathlib.Path:
    value = os.environ.get("CODEX_HOME")
    if value:
        return pathlib.Path(value).expanduser().resolve()
    return pathlib.Path.home() / ".codex"


def goal_db_paths() -> list[pathlib.Path]:
    root = codex_home()
    paths = [root / relative for relative in GOAL_DB_RELATIVE_PATHS]
    return [path for path in paths if path.exists()]


def transcript_session_id(transcript_path: str) -> str | None:
    if not transcript_path:
        return None
    path = pathlib.Path(transcript_path).expanduser()
    if not path.exists() or not path.is_file():
        return None

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or str(record.get("type") or "") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                candidate = normalize_task_id(str(payload.get("session_id") or payload.get("id") or ""))
                if candidate:
                    return candidate
    except OSError:
        return None

    match = TRANSCRIPT_SESSION_ID_RE.search(path.name)
    if match:
        return normalize_task_id(match.group(1))
    return None


def resolve_goal_thread_id(data: dict[str, Any]) -> str | None:
    explicit = normalize_task_id(
        str(
            data.get("thread_id")
            or data.get("threadId")
            or data.get("session_id")
            or data.get("sessionId")
            or ""
        )
    )
    if explicit:
        return explicit

    transcript = session_transcript_path(data)
    return transcript_session_id(transcript)


def goal_row_for_thread(thread_id: str | None) -> dict[str, Any] | None:
    if not thread_id:
        return None

    for db_path in goal_db_paths():
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
            try:
                row = connection.execute(
                    "select goal_id, objective, status from thread_goals where thread_id = ?",
                    (thread_id,),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            continue

        if row and isinstance(row[0], str) and isinstance(row[2], str):
            return {
                "thread_id": thread_id,
                "goal_id": row[0],
                "objective": row[1] if isinstance(row[1], str) else "",
                "status": row[2],
            }
    return None


def goal_status_for_thread(thread_id: str | None) -> str | None:
    row = goal_row_for_thread(thread_id)
    if row:
        status = str(row.get("status") or "").strip()
        return status or None
    return None


def read_task_goal_thread_id(root: pathlib.Path, task_id: str | None) -> str | None:
    if not task_id:
        return None
    guard = read_guard(root, task_id)
    return normalize_task_id(str(guard.get("goal_thread_id") or ""))


def write_task_goal_thread_id(root: pathlib.Path, task_id: str, goal_thread_id: str) -> pathlib.Path:
    guard = read_guard(root, task_id)
    guard["goal_thread_id"] = goal_thread_id
    guard["goal_last_seen_at"] = now()
    return write_guard(root, guard, task_id)


def resolve_goal_context(root: pathlib.Path, data: dict[str, Any], task_id: str | None) -> dict[str, Any]:
    current_goal_thread_id = resolve_goal_thread_id(data)
    current_goal_active = goal_status_for_thread(current_goal_thread_id) == "active"
    if task_id and current_goal_active and current_goal_thread_id:
        write_task_goal_thread_id(root, task_id, current_goal_thread_id)

    task_goal_thread_id = read_task_goal_thread_id(root, task_id)
    task_goal_active = goal_status_for_thread(task_goal_thread_id) == "active"

    if current_goal_active:
        return {
            "active": True,
            "source": "thread",
            "thread_id": current_goal_thread_id,
            "current_thread_id": current_goal_thread_id,
            "task_thread_id": task_goal_thread_id,
        }

    if task_goal_active:
        return {
            "active": True,
            "source": "task",
            "thread_id": task_goal_thread_id,
            "current_thread_id": current_goal_thread_id,
            "task_thread_id": task_goal_thread_id,
        }

    return {
        "active": False,
        "source": "none",
        "thread_id": current_goal_thread_id,
        "current_thread_id": current_goal_thread_id,
        "task_thread_id": task_goal_thread_id,
    }


def clear_required_is_effective(guard: dict[str, Any], goal_active: bool) -> bool:
    return bool(guard.get("clear_required")) and not goal_active


def prompt_task_id(prompt: str) -> str | None:
    if not prompt:
        return None
    match = TASK_ID_PROMPT_RE.search(prompt)
    if match:
        return normalize_task_id(match.group(1))
    match = TASK_ID_LINE_RE.search(prompt)
    if match:
        return normalize_task_id(match.group(1))
    return None


def read_task_sessions(root: pathlib.Path) -> dict[str, Any]:
    path = task_sessions_path(root)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sessions": {}}
    if not isinstance(loaded, dict):
        return {"sessions": {}}
    sessions = loaded.get("sessions")
    if not isinstance(sessions, dict):
        loaded["sessions"] = {}
    return loaded


def write_task_sessions(root: pathlib.Path, value: dict[str, Any]) -> pathlib.Path:
    path = task_sessions_path(root)
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return path


def resolve_task_id(root: pathlib.Path, data: dict[str, Any]) -> str:
    explicit = normalize_task_id(str(data.get("task_id") or data.get("taskId") or ""))
    if explicit:
        return explicit

    prompt = str(data.get("prompt") or "")
    prompt_task = prompt_task_id(prompt)
    if prompt_task:
        return prompt_task

    transcript = session_transcript_path(data)
    if transcript:
        sessions = read_task_sessions(root)
        session_entry = sessions.get("sessions", {}).get(transcript)
        if isinstance(session_entry, dict):
            mapped = normalize_task_id(str(session_entry.get("task_id") or ""))
            if mapped:
                return mapped

    current = task_current_path(root)
    if current.exists():
        try:
            mapped = normalize_task_id(current.read_text(encoding="utf-8").strip())
            if mapped:
                return mapped
        except OSError:
            pass
    return DEFAULT_TASK_ID


def write_task_current(root: pathlib.Path, task_id: str) -> pathlib.Path:
    path = task_current_path(root)
    atomic_write_text(path, f"{task_id}\n")
    return path


def register_task_session(root: pathlib.Path, transcript: str, task_id: str) -> pathlib.Path:
    if not transcript:
        return task_sessions_path(root)
    lock_path = task_sessions_lock_path(root)
    with exclusive_file_lock(lock_path):
        sessions = read_task_sessions(root)
        sessions.setdefault("sessions", {})
        sessions["sessions"][transcript] = {
            "task_id": task_id,
            "updated": now(),
        }
        return write_task_sessions(root, sessions)


def task_relative_prefix(task_id: str | None) -> str:
    return f"{TASKS_REL}/{effective_task_id(task_id)}"


def task_relative_path(relative_path: str, task_id: str | None) -> str:
    return f"{task_relative_prefix(task_id)}/{pathlib.Path(relative_path).name}"


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


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


def current_time() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def process_template(root: pathlib.Path, branch: str, task_id: str | None = None) -> str:
    task_id = effective_task_id(task_id)
    return f"""# Process State

Updated: {now()}
Workspace: {root}
Branch: {branch or "(none)"}
Task ID: {task_id}

## Current Task
- TODO: keep the active task here before compaction, clear, or handoff.

## Done
- TODO: record completed work that should not be repeated.

## Key Files
- TODO: record task-relevant files with one-line reasons.

## Verification
- TODO: record exact commands and whether they passed, failed, or were not run.

## Current Constraints
- Do not rely on previous chat history after compact/clear/resume.
- Do not revert unrelated user changes.

## Next Step
1. TODO: record the next smallest meaningful action.
"""


def ensure_process_file(root: pathlib.Path, branch: str, task_id: str | None = None) -> pathlib.Path:
    process_path = task_process_path(root, task_id)
    process_path.parent.mkdir(parents=True, exist_ok=True)
    if not process_path.exists():
        process_path.write_text(process_template(root, branch, task_id), encoding="utf-8")
    return process_path


def process_updated_line(text: str) -> str:
    match = re.search(r"^Updated:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else "(missing)"


def process_health(process_path: pathlib.Path) -> dict[str, Any]:
    if not process_path.exists():
        return {
            "status": "missing",
            "needs_attention": True,
            "updated": "(missing)",
            "mtime": "(missing)",
            "age_minutes": "(unknown)",
            "notes": ["process.md does not exist."],
        }

    text = process_path.read_text(encoding="utf-8", errors="replace")
    stat = process_path.stat()
    mtime_dt = _dt.datetime.fromtimestamp(stat.st_mtime).astimezone()
    age_minutes_value = max(0.0, (current_time() - mtime_dt).total_seconds() / 60)
    has_template_markers = any(marker in text for marker in PROCESS_TEMPLATE_MARKERS)
    is_stale = age_minutes_value > PROCESS_STALE_MINUTES

    notes: list[str] = []
    status = "ok"
    if has_template_markers:
        status = "template"
        notes.append("process.md still contains template TODO markers.")
    if is_stale:
        status = "template_stale" if status == "template" else "stale"
        notes.append(f"process.md file mtime is older than {PROCESS_STALE_MINUTES} minutes.")
    if not notes:
        notes.append("process.md is recent and no template markers were found.")

    return {
        "status": status,
        "needs_attention": status != "ok",
        "updated": process_updated_line(text),
        "mtime": mtime_dt.isoformat(timespec="seconds"),
        "age_minutes": f"{age_minutes_value:.1f}",
        "notes": notes,
    }


def truncate(text: Any, limit: int = 2000) -> str:
    if text is None:
        return ""
    value = str(text)
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def auto_compact_clear_threshold() -> int:
    raw = os.environ.get("CODEX_AUTO_CLEAR_AFTER_COMPACTS", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_AUTO_COMPACT_CLEAR_THRESHOLD
    return value if value > 0 else DEFAULT_AUTO_COMPACT_CLEAR_THRESHOLD


def guard_default() -> dict[str, Any]:
    return {
        "auto_compact_count": 0,
        "clear_required": False,
        "threshold": auto_compact_clear_threshold(),
        "last_event": "",
        "last_trigger": "",
        "last_source": "",
        "last_turn_id": "",
        "last_transcript": "",
        "last_updated": "",
        "last_reset_source": "",
        "last_reset_at": "",
    }


def read_guard(root: pathlib.Path, task_id: str | None = None) -> dict[str, Any]:
    path = task_guard_path(root, task_id)
    guard = guard_default()
    if not path.exists():
        return guard

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return guard
    if not isinstance(loaded, dict):
        return guard

    guard.update(loaded)
    try:
        guard["auto_compact_count"] = max(0, int(guard.get("auto_compact_count") or 0))
    except (TypeError, ValueError):
        guard["auto_compact_count"] = 0
    guard["threshold"] = auto_compact_clear_threshold()
    guard["clear_required"] = bool(guard.get("clear_required"))
    return guard


def write_guard(root: pathlib.Path, guard: dict[str, Any], task_id: str | None = None) -> pathlib.Path:
    path = task_guard_path(root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    guard["threshold"] = auto_compact_clear_threshold()
    guard["last_updated"] = now()
    path.write_text(json.dumps(guard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def update_context_guard(root: pathlib.Path, data: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
    event = str(data.get("hook_event_name") or data.get("hookEventName") or "")
    trigger = str(data.get("trigger") or "")
    source = str(data.get("source") or "")
    guard = read_guard(root, task_id)

    guard["last_event"] = event
    guard["last_trigger"] = trigger
    guard["last_source"] = source
    guard["last_turn_id"] = str(data.get("turn_id") or "")
    guard["last_transcript"] = str(data.get("transcript_path") or "")

    if event == "SessionStart" and source in {"clear", "startup"}:
        guard["auto_compact_count"] = 0
        guard["clear_required"] = False
        guard["last_reset_source"] = source
        guard["last_reset_at"] = now()
    elif event == "PostCompact" and trigger == "auto":
        guard["auto_compact_count"] = int(guard.get("auto_compact_count") or 0) + 1
        if guard["auto_compact_count"] >= int(guard.get("threshold") or auto_compact_clear_threshold()):
            guard["clear_required"] = True

    write_guard(root, guard, task_id)
    return guard


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            for key in ("text", "input_text", "output_text"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
                    break
        return "\n".join(parts)
    return ""


def transcript_event_text(record: dict[str, Any]) -> str:
    timestamp = str(record.get("timestamp") or "(no timestamp)")
    record_type = str(record.get("type") or "")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""

    if record_type == "event_msg":
        payload_type = str(payload.get("type") or "")
        if payload_type != "agent_message":
            return ""
        message = truncate(payload.get("message"), 700)
        phase = str(payload.get("phase") or "event")
        return f"- [{timestamp}] assistant/{phase}: {message}"

    if record_type != "response_item":
        return ""

    item_type = str(payload.get("type") or "")
    if item_type == "message":
        role = str(payload.get("role") or "message")
        text = truncate(extract_content_text(payload.get("content")), 700)
        return f"- [{timestamp}] {role}: {text}" if text else ""

    if item_type == "function_call":
        name = str(payload.get("name") or "tool")
        args = truncate(payload.get("arguments"), 500)
        return f"- [{timestamp}] tool call `{name}`: {args}"

    if item_type == "function_call_output":
        call_id = str(payload.get("call_id") or "tool")
        output = truncate(payload.get("output"), 700)
        return f"- [{timestamp}] tool output `{call_id}`: {output}" if output else ""

    if item_type == "web_search_call":
        action = payload.get("action")
        query = action.get("query") if isinstance(action, dict) else ""
        return f"- [{timestamp}] web search: {truncate(query, 300)}" if query else ""

    return ""


def recent_transcript_events(transcript_path: str, limit: int = RECENT_EVENT_LIMIT) -> list[str]:
    if not transcript_path:
        return []
    path = pathlib.Path(transcript_path).expanduser()
    if not path.exists() or not path.is_file():
        return []

    recent_lines: deque[str] = deque(maxlen=200)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    recent_lines.append(line)
    except OSError:
        return []

    events: deque[str] = deque(maxlen=limit)
    for line in recent_lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        text = transcript_event_text(record)
        if text:
            events.append(text)
    return list(events)


def should_write_recent(data: dict[str, Any], health: dict[str, Any]) -> bool:
    event = str(data.get("hook_event_name") or data.get("hookEventName") or "")
    trigger = str(data.get("trigger") or "")
    if event in {"PreCompact", "PostCompact"} and trigger == "auto":
        return True
    return bool(health.get("needs_attention"))


def write_recent_snapshot(
    root: pathlib.Path,
    data: dict[str, Any],
    health: dict[str, Any],
    task_id: str | None = None,
) -> pathlib.Path | None:
    if not should_write_recent(data, health):
        return None

    event = str(data.get("hook_event_name") or data.get("hookEventName") or "unknown")
    trigger = str(data.get("trigger") or "")
    transcript_path = str(data.get("transcript_path") or "")
    prompt = truncate(data.get("prompt"), 700)
    last_assistant_message = truncate(data.get("last_assistant_message"), 900)
    events = recent_transcript_events(transcript_path)

    process_rel = task_relative_path(PROCESS_REL, task_id)
    recent_path = task_recent_path(root, task_id)
    recent_path.parent.mkdir(parents=True, exist_ok=True)
    event_block = "\n".join(events) if events else "- No readable recent transcript events were available."
    recent_path.write_text(
        f"""# Recent Transcript Fallback

Updated: {now()}
Hook Event: {event}
Compaction Trigger: {trigger or "(none)"}
Transcript: {transcript_path or "(none)"}

This file is a lightweight fallback for the gap between the last semantic
`{process_rel}` update and an automatic compact. Prefer `{process_rel}` first;
use this file only when that state is stale, still a template, or missing key
recent steps.

## Process State Health At Write

Status: {health["status"]}
Needs Attention: {yes_no(bool(health["needs_attention"]))}
Process Updated: {health["updated"]}
Process MTime: {health["mtime"]}
Process Age Minutes: {health["age_minutes"]}

## Latest Hook Prompt

```text
{prompt or "(not provided)"}
```

## Latest Hook Assistant Message

```text
{last_assistant_message or "(not provided)"}
```

## Recent Events

{event_block}
""",
        encoding="utf-8",
    )
    return recent_path


def write_auto_snapshot(
    root: pathlib.Path,
    data: dict[str, Any],
    branch: str,
    health: dict[str, Any],
    recent_path: pathlib.Path | None,
    guard: dict[str, Any],
    goal_context: dict[str, Any],
    task_id: str | None = None,
) -> pathlib.Path:
    task_id = effective_task_id(task_id)
    event = str(data.get("hook_event_name") or data.get("hookEventName") or "unknown")
    trigger = str(data.get("trigger") or "")
    source = str(data.get("source") or "")
    prompt = truncate(data.get("prompt"), 1000)
    last_assistant_message = truncate(data.get("last_assistant_message"), 2000)
    transcript_path = str(data.get("transcript_path") or "")
    turn_id = str(data.get("turn_id") or "")

    status = run(["git", "status", "--short"], root)
    diff_stat = run(["git", "diff", "--stat"], root)
    head = run(["git", "rev-parse", "--short", "HEAD"], root)
    process_rel = task_relative_path(PROCESS_REL, task_id)
    recent_rel = task_relative_path(RECENT_REL, task_id)
    recent_display = relative(recent_path, root) if recent_path else f"{recent_rel} (not written)"
    notes = "\n".join(f"- {note}" for note in health["notes"])
    guard_path = task_guard_path(root, task_id)
    goal_active = bool(goal_context.get("active"))
    clear_required_effective = clear_required_is_effective(guard, goal_active)

    auto_path = task_auto_path(root, task_id)
    auto_path.parent.mkdir(parents=True, exist_ok=True)
    auto_path.write_text(
        f"""# Process Auto Snapshot

Updated: {now()}
Hook Event: {event}
Session Source: {source or "(none)"}
Compaction Trigger: {trigger or "(none)"}
Workspace: {root}
Task ID: {task_id}
Branch: {branch or "(none)"}
HEAD: {head or "(none)"}
Turn ID: {turn_id or "(none)"}
Transcript: {transcript_path or "(none)"}

## Process State Health

Status: {health["status"]}
Needs Attention: {yes_no(bool(health["needs_attention"]))}
Process Updated: {health["updated"]}
Process MTime: {health["mtime"]}
Process Age Minutes: {health["age_minutes"]}
Stale Threshold Minutes: {PROCESS_STALE_MINUTES}
Recent Fallback: {recent_display}

{notes}

## Context Guard

Guard File: {relative(guard_path, root)}
Auto Compact Count: {guard.get("auto_compact_count", 0)}
Auto Compact Clear Threshold: {guard.get("threshold", auto_compact_clear_threshold())}
Clear Required: {yes_no(bool(guard.get("clear_required")))}
Goal Active: {yes_no(goal_active)}
Goal Source: {goal_context.get("source") or "(none)"}
Goal Thread ID: {goal_context.get("thread_id") or "(none)"}
Last Guard Event: {guard.get("last_event") or "(none)"}
Last Guard Trigger: {guard.get("last_trigger") or "(none)"}
Last Guard Source: {guard.get("last_source") or "(none)"}
Last Guard Reset Source: {guard.get("last_reset_source") or "(none)"}

Next Action: {"perform controlled clear before substantial new work" if clear_required_effective else f"continue normally; keep {process_rel} current before context switches"}

## Git Status

```text
{status or "clean or unavailable"}
```

## Diff Stat

```text
{diff_stat or "none or unavailable"}
```

## Latest User Prompt

```text
{prompt or "(not provided)"}
```

## Latest Assistant Message

```text
{last_assistant_message or "(not provided)"}
```

## Resume Rule

Read `{process_rel}` first after `resume`, `compact`, or `clear`. Treat this
auto snapshot as supporting evidence only; semantic task state belongs in
`{process_rel}`.
""",
        encoding="utf-8",
    )
    return auto_path


def relative(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def context_message(
    root: pathlib.Path,
    process_path: pathlib.Path,
    auto_path: pathlib.Path,
    task_id: str | None = None,
) -> str:
    task_id = effective_task_id(task_id)
    process_rel = relative(process_path, root)
    auto_rel = relative(auto_path, root)
    recent_rel = task_relative_path(RECENT_REL, task_id)
    return (
        "Context handoff protocol is active for this workspace.\n"
        f"- Active task id: `{task_id}`.\n"
        f"- Read `{process_rel}` before continuing after resume, compact, or clear.\n"
        f"- Use `{auto_rel}` only as an objective snapshot of cwd/git/hook metadata.\n"
        f"- If `{process_rel}` is stale, missing, or still a TODO template, inspect `{recent_rel}` if present.\n"
        f"- Keep `{process_rel}` updated with Current Task, Done, Key Files, Verification, "
        "Current Constraints, and Next Step before long-running work, compact, clear, or handoff.\n"
        "- Do not rely on previous chat history after a context switch."
    )


def clear_required_message(
    root: pathlib.Path,
    process_path: pathlib.Path,
    auto_path: pathlib.Path,
    guard: dict[str, Any],
    goal_active: bool,
    task_id: str | None = None,
) -> str:
    task_id = effective_task_id(task_id)
    process_rel = relative(process_path, root)
    auto_rel = relative(auto_path, root)
    guard_rel = task_relative_path(GUARD_REL, task_id)
    count = guard.get("auto_compact_count", 0)
    threshold = guard.get("threshold", auto_compact_clear_threshold())
    target = f"task `{task_id}` in this workspace"
    if goal_active:
        target += " while an active goal is present"
    return (
        f"Controlled clear is required for {target}.\n"
        f"- It has reached {count} automatic compactions; threshold is {threshold}.\n"
        f"- Guard state: `{guard_rel}`.\n"
        f"- Before doing substantial new task work, update `{process_rel}` with Current Task, Done, "
        "Key Files, Verification, Current Constraints, and Next Step.\n"
        f"- Use `{auto_rel}` only as objective hook metadata; semantic state belongs in `{process_rel}`.\n"
        "- Then perform a controlled clear/new context step if the surface provides it, or ask the user to run `/clear`.\n"
        f"- After clear/resume, restore from `{process_rel}` first and verify `{guard_rel}` reset on `SessionStart(clear)`.\n"
        "- Do not continue substantial implementation work in the compressed context unless the user explicitly overrides."
    )


def json_out(value: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(value, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


def main() -> int:
    data = read_input()
    event = str(data.get("hook_event_name") or data.get("hookEventName") or "")
    cwd = session_cwd(data)
    root = workspace_root(cwd)
    branch = run(["git", "branch", "--show-current"], root)
    task_id = resolve_task_id(root, data)
    write_task_current(root, task_id)
    register_task_session(root, session_transcript_path(data), task_id)

    goal_context = resolve_goal_context(root, data, task_id)
    goal_active = bool(goal_context.get("active"))

    process_path = ensure_process_file(root, branch, task_id)
    guard = update_context_guard(root, data, task_id)
    health = process_health(process_path)
    recent_path = write_recent_snapshot(root, data, health, task_id)
    auto_path = write_auto_snapshot(root, data, branch, health, recent_path, guard, goal_context, task_id)

    if event == "SessionStart":
        source = str(data.get("source") or "")
        if source in {"startup", "resume", "clear", "compact"}:
            additional_context = context_message(root, process_path, auto_path, task_id)
            if clear_required_is_effective(guard, goal_active):
                additional_context = clear_required_message(root, process_path, auto_path, guard, goal_active, task_id)
            return json_out(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": additional_context,
                    }
                }
            )
        return json_out({})

    if event == "UserPromptSubmit":
        prompt = str(data.get("prompt") or "")
        if clear_required_is_effective(guard, goal_active) or CONTINUATION_RE.search(prompt):
            additional_context = context_message(root, process_path, auto_path, task_id)
            if clear_required_is_effective(guard, goal_active):
                additional_context = clear_required_message(root, process_path, auto_path, guard, goal_active, task_id)
            return json_out(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": additional_context,
                    }
                }
            )
        return json_out({})

    return json_out({})


if __name__ == "__main__":
    raise SystemExit(main())
