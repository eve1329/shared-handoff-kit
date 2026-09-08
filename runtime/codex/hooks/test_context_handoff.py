#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import sqlite3


SCRIPT = pathlib.Path(__file__).with_name("context_handoff.py")
DEFAULT_TASK_ID = "main"


def task_state_dir(cwd: pathlib.Path, task_id: str = DEFAULT_TASK_ID) -> pathlib.Path:
    return cwd / ".agents" / "state" / "tasks" / task_id


def root_payload_paths(cwd: pathlib.Path) -> tuple[pathlib.Path, ...]:
    state_dir = cwd / ".agents" / "state"
    return tuple(
        state_dir / name
        for name in (
            "process.md",
            "process.auto.md",
            "process.recent.md",
            "context_guard.json",
            "codex-auto-resume.md",
        )
    )


def run_hook(
    cwd: pathlib.Path,
    payload: dict,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    effective_env = os.environ.copy()
    if env:
        effective_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(cwd),
        env=effective_env,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )


def write_goal_db(codex_home: pathlib.Path, thread_id: str, status: str = "active") -> None:
    db_dir = codex_home / "sqlite"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "goals_1.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            create table if not exists thread_goals (
                thread_id text primary key not null,
                goal_id text not null,
                objective text not null,
                status text not null,
                token_budget integer,
                tokens_used integer not null default 0,
                time_used_seconds integer not null default 0,
                created_at_ms integer not null,
                updated_at_ms integer not null
            )
            """
        )
        connection.execute(
            "insert or replace into thread_goals (thread_id, goal_id, objective, status, created_at_ms, updated_at_ms) values (?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                f"goal-{thread_id}",
                f"goal objective for {thread_id}",
                status,
                1,
                1,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def seed_task_binding(cwd: pathlib.Path, transcript: str, task_id: str) -> None:
    state_dir = cwd / ".agents" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "current-task").write_text(f"{task_id}\n", encoding="utf-8")
    (state_dir / "session-tasks.json").write_text(
        json.dumps(
            {
                "sessions": {
                    transcript: {
                        "task_id": task_id,
                        "updated": "2026-07-10T00:00:00+00:00",
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def register_task_session_after_synchronized_read(
    root: str,
    transcript: str,
    task_id: str,
    barrier: object,
) -> None:
    spec = importlib.util.spec_from_file_location("context_handoff_concurrency", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load context_handoff.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    original_read = module.read_task_sessions

    def synchronized_read(root_path: pathlib.Path) -> dict:
        value = original_read(root_path)
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return value

    module.read_task_sessions = synchronized_read
    module.register_task_session(pathlib.Path(root), transcript, task_id)


class ContextHandoffHookTests(unittest.TestCase):
    def test_session_start_clear_creates_process_and_injects_resume_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            result = run_hook(cwd, {"hook_event_name": "SessionStart", "source": "clear"})

            self.assertEqual(result.returncode, 0, result.stderr)
            process = task_state_dir(cwd) / "process.md"
            self.assertTrue(process.exists())
            self.assertIn("Process State", process.read_text(encoding="utf-8"))

            stdout = json.loads(result.stdout)
            hook_output = stdout["hookSpecificOutput"]
            self.assertEqual(hook_output["hookEventName"], "SessionStart")
            self.assertIn(".agents/state/tasks/main/process.md", hook_output["additionalContext"])
            self.assertIn("Active task id: `main`", hook_output["additionalContext"])
            self.assertIn("Do not rely on previous chat history", hook_output["additionalContext"])
            self.assertEqual(
                (cwd / ".agents" / "state" / "current-task").read_text(encoding="utf-8").strip(),
                DEFAULT_TASK_ID,
            )
            self.assertTrue(all(not path.exists() for path in root_payload_paths(cwd)))

    def test_precompact_writes_snapshot_and_returns_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            result = run_hook(
                cwd,
                {
                    "hook_event_name": "PreCompact",
                    "trigger": "auto",
                    "turn_id": "turn-test",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            stdout = json.loads(result.stdout)
            self.assertEqual(stdout, {})

            auto_state = task_state_dir(cwd) / "process.auto.md"
            self.assertTrue(auto_state.exists())
            text = auto_state.read_text(encoding="utf-8")
            self.assertIn("Hook Event: PreCompact", text)
            self.assertIn("Compaction Trigger: auto", text)

    def test_precompact_flags_template_process_and_writes_recent_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            transcript = cwd / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-06-12T08:00:00.000Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "请实现部署脚本"}],
                                },
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-06-12T08:01:00.000Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [{"type": "output_text", "text": "我会先检查 repo。"}],
                                },
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_hook(
                cwd,
                {
                    "hook_event_name": "PreCompact",
                    "trigger": "auto",
                    "transcript_path": str(transcript),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})

            auto_state = task_state_dir(cwd) / "process.auto.md"
            auto_text = auto_state.read_text(encoding="utf-8")
            self.assertIn("## Process State Health", auto_text)
            self.assertIn("Status: template", auto_text)
            self.assertIn("Needs Attention: yes", auto_text)
            self.assertIn(".agents/state/tasks/main/process.recent.md", auto_text)

            recent = task_state_dir(cwd) / "process.recent.md"
            self.assertTrue(recent.exists())
            recent_text = recent.read_text(encoding="utf-8")
            self.assertIn("Recent Transcript Fallback", recent_text)
            self.assertIn("请实现部署脚本", recent_text)
            self.assertIn("我会先检查 repo", recent_text)

    def test_precompact_accepts_recent_non_template_process(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            state_dir = task_state_dir(cwd)
            state_dir.mkdir(parents=True)
            (state_dir / "process.md").write_text(
                """# Process State

Updated: 2026-06-12T16:00:00+08:00
Workspace: /tmp/example
Branch: main

## Current Task
- Ship the context handoff hook hardening.

## Done
- Added tests for process health.

## Key Files
- /tmp/example/hook.py - hook implementation.

## Verification
- Pending.

## Current Constraints
- Keep handoff summaries short.

## Next Step
1. Implement health checks.
""",
                encoding="utf-8",
            )

            result = run_hook(cwd, {"hook_event_name": "PreCompact", "trigger": "auto"})

            self.assertEqual(result.returncode, 0, result.stderr)
            auto_text = (state_dir / "process.auto.md").read_text(encoding="utf-8")
            self.assertIn("## Process State Health", auto_text)
            self.assertIn("Status: ok", auto_text)
            self.assertIn("Needs Attention: no", auto_text)

    def test_user_prompt_submit_continuation_injects_process_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            result = run_hook(cwd, {"hook_event_name": "UserPromptSubmit", "prompt": "继续"})

            self.assertEqual(result.returncode, 0, result.stderr)
            stdout = json.loads(result.stdout)
            hook_output = stdout["hookSpecificOutput"]
            self.assertEqual(hook_output["hookEventName"], "UserPromptSubmit")
            self.assertIn(".agents/state/tasks/main/process.md", hook_output["additionalContext"])
            self.assertIn(".agents/state/tasks/main/process.recent.md", hook_output["additionalContext"])

    def test_default_task_binds_transcript_and_ignores_legacy_root_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            state_dir = cwd / ".agents" / "state"
            state_dir.mkdir(parents=True)
            sentinels: dict[pathlib.Path, str] = {}
            for path in root_payload_paths(cwd):
                value = f"legacy sentinel: {path.name}\n"
                path.write_text(value, encoding="utf-8")
                sentinels[path] = value
            transcript = str(cwd / "session-default.jsonl")

            result = run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "继续",
                    "transcript_path": transcript,
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            default_state = task_state_dir(cwd)
            self.assertTrue((default_state / "process.md").exists())
            self.assertTrue((default_state / "process.auto.md").exists())
            self.assertTrue((default_state / "context_guard.json").exists())
            self.assertEqual(
                (state_dir / "current-task").read_text(encoding="utf-8").strip(),
                DEFAULT_TASK_ID,
            )
            session_tasks = json.loads((state_dir / "session-tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(session_tasks["sessions"][transcript]["task_id"], DEFAULT_TASK_ID)
            self.assertIn(
                ".agents/state/tasks/main/process.md",
                json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"],
            )
            for path, value in sentinels.items():
                self.assertEqual(path.read_text(encoding="utf-8"), value)

    def test_path_helpers_map_none_to_default_task(self) -> None:
        spec = importlib.util.spec_from_file_location("context_handoff_paths", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        root = pathlib.Path("/tmp/default-task-path-test")
        self.assertEqual(module.state_root(root, None), root / ".agents/state/tasks/main")
        self.assertEqual(
            module.task_relative_path(module.PROCESS_REL, None),
            ".agents/state/tasks/main/process.md",
        )

    def test_valid_current_task_precedes_default_main(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            state_dir = cwd / ".agents" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "current-task").write_text("work\n", encoding="utf-8")

            result = run_hook(cwd, {"hook_event_name": "UserPromptSubmit", "prompt": "继续"})

            self.assertEqual(result.returncode, 0, result.stderr)
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn(".agents/state/tasks/work/process.md", context)
            self.assertFalse(task_state_dir(cwd).exists())

    def test_task_prompt_uses_task_specific_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            transcript = str(cwd / "session-image.jsonl")

            result = run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "task=image-async 继续修图片轮询",
                    "transcript_path": transcript,
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            stdout = json.loads(result.stdout)
            hook_output = stdout["hookSpecificOutput"]
            self.assertIn(".agents/state/tasks/image-async/process.md", hook_output["additionalContext"])
            self.assertIn(".agents/state/tasks/image-async/process.recent.md", hook_output["additionalContext"])

            task_state = cwd / ".agents" / "state" / "tasks" / "image-async"
            self.assertTrue((task_state / "process.md").exists())
            self.assertTrue((task_state / "process.auto.md").exists())
            self.assertTrue((task_state / "context_guard.json").exists())
            self.assertFalse((cwd / ".agents" / "state" / "process.md").exists())
            self.assertEqual((cwd / ".agents" / "state" / "current-task").read_text(encoding="utf-8").strip(), "image-async")

            session_tasks = json.loads((cwd / ".agents" / "state" / "session-tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(session_tasks["sessions"][transcript]["task_id"], "image-async")

    def test_task_prompt_with_trailing_punctuation_still_binds_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            transcript = str(cwd / "session-issue.jsonl")

            result = run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "新开 task-id=issue-kmp，这样子也不会",
                    "transcript_path": transcript,
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})

            task_state = cwd / ".agents" / "state" / "tasks" / "issue-kmp"
            self.assertTrue((task_state / "process.md").exists())
            self.assertTrue((task_state / "process.auto.md").exists())
            self.assertTrue((task_state / "context_guard.json").exists())
            self.assertFalse((cwd / ".agents" / "state" / "process.md").exists())
            self.assertEqual((cwd / ".agents" / "state" / "current-task").read_text(encoding="utf-8").strip(), "issue-kmp")

            session_tasks = json.loads((cwd / ".agents" / "state" / "session-tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(session_tasks["sessions"][transcript]["task_id"], "issue-kmp")

    def test_unspaced_chinese_task_prompt_replaces_stale_session_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            transcript = str(cwd / "session-map.jsonl")
            seed_task_binding(cwd, transcript, "sol")

            result = run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "新开task-id=map-sub5.6",
                    "transcript_path": transcript,
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            task_state = cwd / ".agents" / "state" / "tasks" / "map-sub5.6"
            self.assertTrue((task_state / "process.md").exists())
            self.assertEqual(
                (cwd / ".agents" / "state" / "current-task").read_text(encoding="utf-8").strip(),
                "map-sub5.6",
            )
            session_tasks = json.loads(
                (cwd / ".agents" / "state" / "session-tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session_tasks["sessions"][transcript]["task_id"], "map-sub5.6")

    def test_chinese_punctuation_before_task_prompt_replaces_stale_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            transcript = str(cwd / "session-map.jsonl")
            seed_task_binding(cwd, transcript, "sol")

            result = run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "继续，task-id=map-sub5.6",
                    "transcript_path": transcript,
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            stdout = json.loads(result.stdout)
            self.assertIn(
                ".agents/state/tasks/map-sub5.6/process.md",
                stdout["hookSpecificOutput"]["additionalContext"],
            )
            session_tasks = json.loads(
                (cwd / ".agents" / "state" / "session-tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session_tasks["sessions"][transcript]["task_id"], "map-sub5.6")

    def test_ascii_word_prefix_does_not_create_a_task_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            transcript = str(cwd / "session-map.jsonl")
            seed_task_binding(cwd, transcript, "sol")

            result = run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "notask-id=map-sub5.6 继续",
                    "transcript_path": transcript,
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            session_tasks = json.loads(
                (cwd / ".agents" / "state" / "session-tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session_tasks["sessions"][transcript]["task_id"], "sol")
            self.assertFalse((cwd / ".agents" / "state" / "tasks" / "map-sub5.6").exists())

    def test_chinese_word_prefix_does_not_turn_task_alias_into_a_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            transcript = str(cwd / "session-map.jsonl")
            seed_task_binding(cwd, transcript, "sol")

            result = run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "子任务=map-sub5.6 继续",
                    "transcript_path": transcript,
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            session_tasks = json.loads(
                (cwd / ".agents" / "state" / "session-tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session_tasks["sessions"][transcript]["task_id"], "sol")
            self.assertFalse((cwd / ".agents" / "state" / "tasks" / "map-sub5.6").exists())

    def test_parallel_session_registration_preserves_both_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            process_context = multiprocessing.get_context("spawn")
            barrier = process_context.Barrier(2)
            registrations = (
                (str(cwd / "session-map.jsonl"), "map-sub5.6"),
                (str(cwd / "session-sol.jsonl"), "sol"),
            )
            processes = [
                process_context.Process(
                    target=register_task_session_after_synchronized_read,
                    args=(str(cwd), transcript, task_id, barrier),
                )
                for transcript, task_id in registrations
            ]

            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=5)
                self.assertFalse(process.is_alive(), "session registration process hung")
                self.assertEqual(process.exitcode, 0)

            session_tasks = json.loads(
                (cwd / ".agents" / "state" / "session-tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {
                    transcript: entry["task_id"]
                    for transcript, entry in session_tasks["sessions"].items()
                },
                dict(registrations),
            )

    def test_task_goal_state_is_reused_across_new_threads_for_same_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            codex_home = cwd / ".codex"
            transcript_a = str(cwd / "session-a.jsonl")
            transcript_b = str(cwd / "session-b.jsonl")
            active_thread = "019eb4bd-dbf7-7432-843e-0317ed0639ea"
            new_thread = "019eb4bd-dbf7-7432-843e-0317ed0639eb"

            write_goal_db(codex_home, active_thread, "active")
            run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "task=image-async 继续",
                    "transcript_path": transcript_a,
                    "thread_id": active_thread,
                },
                env={"CODEX_HOME": str(codex_home)},
            )
            run_hook(
                cwd,
                {
                    "hook_event_name": "PostCompact",
                    "trigger": "auto",
                    "transcript_path": transcript_a,
                    "thread_id": active_thread,
                },
                env={"CODEX_HOME": str(codex_home)},
            )
            run_hook(
                cwd,
                {
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                    "transcript_path": transcript_b,
                    "thread_id": new_thread,
                },
                env={"CODEX_HOME": str(codex_home)},
            )

            result = run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "继续",
                    "transcript_path": transcript_b,
                    "thread_id": new_thread,
                },
                env={"CODEX_HOME": str(codex_home)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            auto_state = cwd / ".agents" / "state" / "tasks" / "image-async" / "process.auto.md"
            auto_text = auto_state.read_text(encoding="utf-8")
            self.assertIn("Goal Source: task", auto_text)
            self.assertIn("Goal Active: yes", auto_text)
            self.assertIn(active_thread, auto_text)

    def test_transcript_mapping_keeps_parallel_task_guards_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            image_transcript = str(cwd / "session-image.jsonl")
            login_transcript = str(cwd / "session-login.jsonl")

            run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "task=image-async 继续",
                    "transcript_path": image_transcript,
                },
            )
            run_hook(
                cwd,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "task=login-fix 继续",
                    "transcript_path": login_transcript,
                },
            )

            for _ in range(3):
                run_hook(
                    cwd,
                    {
                        "hook_event_name": "PostCompact",
                        "trigger": "auto",
                        "transcript_path": image_transcript,
                    },
                )

            image_guard = cwd / ".agents" / "state" / "tasks" / "image-async" / "context_guard.json"
            login_guard = cwd / ".agents" / "state" / "tasks" / "login-fix" / "context_guard.json"
            root_guard = cwd / ".agents" / "state" / "context_guard.json"

            image_guard_data = json.loads(image_guard.read_text(encoding="utf-8"))
            login_guard_data = json.loads(login_guard.read_text(encoding="utf-8"))
            self.assertEqual(image_guard_data["auto_compact_count"], 3)
            self.assertTrue(image_guard_data["clear_required"])
            self.assertEqual(login_guard_data["auto_compact_count"], 0)
            self.assertFalse(login_guard_data["clear_required"])
            self.assertFalse(root_guard.exists())

    def test_stop_records_last_assistant_message_without_continuing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            result = run_hook(
                cwd,
                {
                    "hook_event_name": "Stop",
                    "last_assistant_message": "已完成第一步，下一步跑测试。",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})

            auto_state = task_state_dir(cwd) / "process.auto.md"
            text = auto_state.read_text(encoding="utf-8")
            self.assertIn("Hook Event: Stop", text)
            self.assertIn("已完成第一步", text)

    def test_postcompact_auto_sets_clear_required_after_third_compact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)

            first = run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto", "turn_id": "turn-1"})
            second = run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto", "turn_id": "turn-2"})
            third = run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto", "turn_id": "turn-3"})

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(third.returncode, 0, third.stderr)

            guard = task_state_dir(cwd) / "context_guard.json"
            self.assertTrue(guard.exists())
            guard_data = json.loads(guard.read_text(encoding="utf-8"))
            self.assertEqual(guard_data["auto_compact_count"], 3)
            self.assertTrue(guard_data["clear_required"])
            self.assertEqual(guard_data["threshold"], 3)

            auto_text = (task_state_dir(cwd) / "process.auto.md").read_text(encoding="utf-8")
            self.assertIn("## Context Guard", auto_text)
            self.assertIn("Auto Compact Count: 3", auto_text)
            self.assertIn("Clear Required: yes", auto_text)

    def test_postcompact_auto_does_not_require_clear_before_third_compact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)

            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            result = run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})

            self.assertEqual(result.returncode, 0, result.stderr)

            guard = task_state_dir(cwd) / "context_guard.json"
            guard_data = json.loads(guard.read_text(encoding="utf-8"))
            self.assertEqual(guard_data["auto_compact_count"], 2)
            self.assertFalse(guard_data["clear_required"])
            self.assertEqual(guard_data["threshold"], 3)

    def test_session_start_clear_resets_context_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)

            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            result = run_hook(cwd, {"hook_event_name": "SessionStart", "source": "clear"})

            self.assertEqual(result.returncode, 0, result.stderr)

            guard = task_state_dir(cwd) / "context_guard.json"
            guard_data = json.loads(guard.read_text(encoding="utf-8"))
            self.assertEqual(guard_data["auto_compact_count"], 0)
            self.assertFalse(guard_data["clear_required"])
            self.assertEqual(guard_data["last_reset_source"], "clear")

    def test_session_start_startup_resets_context_guard_as_new_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)

            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            result = run_hook(cwd, {"hook_event_name": "SessionStart", "source": "startup"})

            self.assertEqual(result.returncode, 0, result.stderr)

            guard = task_state_dir(cwd) / "context_guard.json"
            guard_data = json.loads(guard.read_text(encoding="utf-8"))
            self.assertEqual(guard_data["auto_compact_count"], 0)
            self.assertFalse(guard_data["clear_required"])
            self.assertEqual(guard_data["last_reset_source"], "startup")

            stdout = json.loads(result.stdout)
            hook_output = stdout["hookSpecificOutput"]
            self.assertEqual(hook_output["hookEventName"], "SessionStart")
            self.assertIn(".agents/state/tasks/main/process.md", hook_output["additionalContext"])

    def test_session_start_compact_does_not_reset_context_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)

            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            result = run_hook(cwd, {"hook_event_name": "SessionStart", "source": "compact"})

            self.assertEqual(result.returncode, 0, result.stderr)

            guard = task_state_dir(cwd) / "context_guard.json"
            guard_data = json.loads(guard.read_text(encoding="utf-8"))
            self.assertEqual(guard_data["auto_compact_count"], 3)
            self.assertTrue(guard_data["clear_required"])
            self.assertNotEqual(guard_data.get("last_reset_source"), "compact")

    def test_user_prompt_submit_injects_clear_required_context_without_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)

            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            run_hook(cwd, {"hook_event_name": "PostCompact", "trigger": "auto"})
            result = run_hook(cwd, {"hook_event_name": "UserPromptSubmit", "prompt": "看一下这个文件"})

            self.assertEqual(result.returncode, 0, result.stderr)
            stdout = json.loads(result.stdout)
            hook_output = stdout["hookSpecificOutput"]
            self.assertEqual(hook_output["hookEventName"], "UserPromptSubmit")
            self.assertIn("Controlled clear is required", hook_output["additionalContext"])
            self.assertIn(".agents/state/tasks/main/context_guard.json", hook_output["additionalContext"])


if __name__ == "__main__":
    unittest.main()
