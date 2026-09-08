#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HOOKS_DIR = pathlib.Path(__file__).resolve().parent
CLAUDE_DIR = HOOKS_DIR.parent
DEFAULT_TASK_ID = "main"


def initialize_repo(root: pathlib.Path) -> pathlib.Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    child = root / "nested" / "module"
    child.mkdir(parents=True)
    return child


def run_hook(script_name: str, cwd: pathlib.Path, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / script_name)],
        cwd=str(cwd),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )


def task_state(root: pathlib.Path, task_id: str = DEFAULT_TASK_ID) -> pathlib.Path:
    return root / ".agents" / "state" / "tasks" / task_id


class ClaudeDefaultTaskRoutingTests(unittest.TestCase):
    def test_session_start_uses_main_from_repo_subdirectory_and_ignores_root_process(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            child = initialize_repo(root)
            state = root / ".agents" / "state"
            state.mkdir(parents=True)
            (state / "process.md").write_text("legacy root process\n", encoding="utf-8")
            main_state = task_state(root)
            main_state.mkdir(parents=True)
            (main_state / "process.md").write_text("main task context\n", encoding="utf-8")

            result = run_hook("session_start.py", child, {"cwd": str(child)})

            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Task ID: main", context)
            self.assertIn("main task context", context)
            self.assertNotIn("legacy root process", context)
            self.assertEqual((state / "current-task").read_text(encoding="utf-8").strip(), "main")

    def test_session_start_falls_back_to_task_local_auto_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            child = initialize_repo(root)
            main_state = task_state(root)
            main_state.mkdir(parents=True)
            (main_state / "process.auto.md").write_text("main auto snapshot\n", encoding="utf-8")

            result = run_hook("session_start.py", child, {"cwd": str(child)})

            self.assertEqual(result.returncode, 0, result.stderr)
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("main auto snapshot", context)

    def test_user_prompt_explicit_task_writes_repo_pointer_from_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            child = initialize_repo(root)
            work_state = task_state(root, "work")
            work_state.mkdir(parents=True)
            (work_state / "process.md").write_text("work task context\n", encoding="utf-8")

            result = run_hook(
                "task_context.py",
                child,
                {"cwd": str(child), "prompt": "task=work 继续"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Task ID: work", context)
            self.assertIn("work task context", context)
            self.assertEqual(
                (root / ".agents" / "state" / "current-task").read_text(encoding="utf-8").strip(),
                "work",
            )
            self.assertFalse((child / ".agents").exists())

    def test_user_prompt_without_task_uses_main_hint_not_root_process(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            child = initialize_repo(root)
            state = root / ".agents" / "state"
            state.mkdir(parents=True)
            (state / "process.md").write_text("legacy root process\n", encoding="utf-8")

            result = run_hook(
                "task_context.py",
                child,
                {"cwd": str(child), "prompt": "继续"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Task ID: main", context)
            self.assertIn(".agents/state/tasks/main/process.md", context)
            self.assertNotIn("legacy root process", context)

    def test_stop_writes_only_main_task_payload_and_returns_valid_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            child = initialize_repo(root)

            result = run_hook(
                "context_handoff.py",
                child,
                {"cwd": str(child), "session_id": "session-main"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertIn("systemMessage", output)
            self.assertNotIn("hookSpecificOutput", output)
            self.assertIn("Task ID: main", output["systemMessage"])
            self.assertTrue((task_state(root) / "process.auto.md").exists())
            self.assertTrue((task_state(root) / "context_guard.json").exists())
            root_state = root / ".agents" / "state"
            self.assertFalse((root_state / "process.auto.md").exists())
            self.assertFalse((root_state / "context_guard.json").exists())
            self.assertEqual((root_state / "current-task").read_text(encoding="utf-8").strip(), "main")

    def test_invalid_current_task_falls_back_to_main_on_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            child = initialize_repo(root)
            state = root / ".agents" / "state"
            state.mkdir(parents=True)
            (state / "current-task").write_text("../invalid\n", encoding="utf-8")

            result = run_hook(
                "context_handoff.py",
                child,
                {"cwd": str(child), "session_id": "session-invalid"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Task ID: main", json.loads(result.stdout)["systemMessage"])
            self.assertTrue((task_state(root) / "process.auto.md").exists())
            self.assertEqual((state / "current-task").read_text(encoding="utf-8").strip(), "main")

    def test_parallel_sessions_keep_their_own_task_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            child = initialize_repo(root)
            for task_id in ("task-a", "task-b"):
                state = task_state(root, task_id)
                state.mkdir(parents=True)
                (state / "process.md").write_text(f"context for {task_id}\n", encoding="utf-8")

            first = run_hook(
                "task_context.py",
                child,
                {"cwd": str(child), "session_id": "session-a", "prompt": "task=task-a 继续"},
            )
            second = run_hook(
                "task_context.py",
                child,
                {"cwd": str(child), "session_id": "session-b", "prompt": "task=task-b 继续"},
            )
            first_again = run_hook(
                "task_context.py",
                child,
                {"cwd": str(child), "session_id": "session-a", "prompt": "继续"},
            )
            first_stop = run_hook(
                "context_handoff.py",
                child,
                {"cwd": str(child), "session_id": "session-a"},
            )
            first_start = run_hook(
                "session_start.py",
                child,
                {"cwd": str(child), "session_id": "session-a"},
            )

            for result in (first, second, first_again, first_stop, first_start):
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "context for task-a",
                json.loads(first_again.stdout)["hookSpecificOutput"]["additionalContext"],
            )
            self.assertIn("Task ID: task-a", json.loads(first_stop.stdout)["systemMessage"])
            self.assertIn(
                "context for task-a",
                json.loads(first_start.stdout)["hookSpecificOutput"]["additionalContext"],
            )
            self.assertTrue((task_state(root, "task-a") / "process.auto.md").exists())
            self.assertFalse((task_state(root, "task-b") / "process.auto.md").exists())
            mappings = json.loads(
                (root / ".agents" / "state" / "session-tasks.json").read_text(encoding="utf-8")
            )["sessions"]
            self.assertEqual(mappings["claude:session-a"]["task_id"], "task-a")
            self.assertEqual(mappings["claude:session-b"]["task_id"], "task-b")

    def test_settings_register_all_three_handoff_hooks(self) -> None:
        settings = json.loads((CLAUDE_DIR / "settings.json").read_text(encoding="utf-8"))
        hooks = settings.get("hooks", {})
        self.assertTrue({"SessionStart", "UserPromptSubmit", "Stop"} <= set(hooks))
        commands = json.dumps(hooks)
        self.assertIn("session_start.py", commands)
        self.assertIn("task_context.py", commands)
        self.assertIn("context_handoff.py", commands)


if __name__ == "__main__":
    unittest.main()
