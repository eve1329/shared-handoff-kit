#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
from importlib.machinery import SourceFileLoader
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).with_name("codex-auto")
DEFAULT_TASK_ID = "main"


def load_auto_module():
    loader = SourceFileLoader("codex_auto_test_module", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load codex-auto")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_auto(
    cwd: pathlib.Path,
    args: list[str],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
    )


def write_guard(
    cwd: pathlib.Path,
    clear_required: bool = True,
    task_id: str | None = DEFAULT_TASK_ID,
) -> pathlib.Path:
    if task_id:
        state = cwd / ".agents" / "state" / "tasks" / task_id
    else:
        state = cwd / ".agents" / "state"
    state.mkdir(parents=True)
    guard = state / "context_guard.json"
    guard.write_text(
        json.dumps(
            {
                "auto_compact_count": 3 if clear_required else 0,
                "clear_required": clear_required,
                "threshold": 3,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return guard


class CodexAutoTests(unittest.TestCase):
    def test_passthrough_when_clear_not_required(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            result = run_auto(
                cwd,
                [
                    "--auto-dry-run",
                    "--auto-json",
                    "--auto-codex-bin",
                    "/tmp/fake-codex",
                    "继续",
                ],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["action"], "passthrough")
            self.assertEqual(plan["task_id"], DEFAULT_TASK_ID)
            self.assertFalse(plan["clear_required"])
            self.assertEqual(plan["command"], ["/tmp/fake-codex", "继续"])
            self.assertFalse((cwd / ".agents" / "state" / "current-task").exists())

    def test_default_codex_resolution_skips_wrapper_in_codex_bin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            real_bin_dir = cwd / "real-bin"
            real_bin_dir.mkdir()
            windows = os.name == "nt"
            executable_name = "codex.cmd" if windows else "codex"
            real_codex = real_bin_dir / executable_name
            real_codex.write_text("@echo off\r\nexit /b 0\r\n" if windows else "#!/bin/sh\nexit 0\n", encoding="utf-8")
            real_codex.chmod(0o755)

            wrapper = SCRIPT.parent / executable_name
            created_wrapper = False
            if not wrapper.exists():
                wrapper.write_text("@echo off\r\nexit /b 99\r\n" if windows else "#!/bin/sh\nexit 99\n", encoding="utf-8")
                wrapper.chmod(0o755)
                created_wrapper = True

            env = os.environ.copy()
            env.pop("CODEX_REAL_BIN", None)
            env["PATH"] = f"{SCRIPT.parent}{os.pathsep}{real_bin_dir}"
            try:
                result = run_auto(
                    cwd,
                    [
                        "--auto-dry-run",
                        "--auto-json",
                        "--",
                        "继续",
                    ],
                    env=env,
                )
            finally:
                if created_wrapper:
                    wrapper.unlink()

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["command"], [str(real_codex), "继续"])

    def test_windows_resolution_uses_cmd_and_skips_local_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            real_bin_dir = cwd / "real-bin"
            real_bin_dir.mkdir()
            real_codex = real_bin_dir / "codex.cmd"
            real_codex.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

            module = load_auto_module()
            with mock.patch.dict(os.environ, {"PATH": f"{SCRIPT.parent}{os.pathsep}{real_bin_dir}"}, clear=False):
                with mock.patch.object(module, "platform_is_windows", return_value=True):
                    self.assertEqual(module.resolve_codex_bin(), str(real_codex))

    def test_resume_is_converted_to_fresh_session_and_guard_is_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            guard = write_guard(cwd, clear_required=True)
            result = run_auto(
                cwd,
                [
                    "--auto-no-exec",
                    "--auto-json",
                    "--auto-codex-bin",
                    "/tmp/fake-codex",
                    "resume",
                    "--last",
                    "继续修前端",
                ],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["action"], "fresh-session")
            self.assertEqual(plan["task_id"], DEFAULT_TASK_ID)
            self.assertTrue(plan["clear_required"])
            self.assertEqual(plan["command"][0], "/tmp/fake-codex")
            self.assertIn("-C", plan["command"])
            self.assertIn("继续修前端", plan["command"][-1])
            self.assertIn("Task ID: main", plan["command"][-1])
            self.assertIn(".agents/state/tasks/main/process.md", plan["command"][-1])

            guard_data = json.loads(guard.read_text(encoding="utf-8"))
            self.assertEqual(guard_data["auto_compact_count"], 0)
            self.assertFalse(guard_data["clear_required"])
            self.assertEqual(guard_data["last_reset_source"], "codex-auto-launcher")
            self.assertTrue(pathlib.Path(plan["resume_file"]).exists())
            self.assertTrue(pathlib.Path(plan["guard_backup"]).exists())
            self.assertIn(
                ".agents/state/tasks/main/process.md",
                pathlib.Path(plan["resume_file"]).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (cwd / ".agents" / "state" / "current-task").read_text(encoding="utf-8").strip(),
                DEFAULT_TASK_ID,
            )

    def test_app_command_resets_guard_without_injecting_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            guard = write_guard(cwd, clear_required=True)
            result = run_auto(
                cwd,
                [
                    "--auto-no-exec",
                    "--auto-json",
                    "--auto-codex-bin",
                    "/tmp/fake-codex",
                    "app",
                    str(cwd),
                ],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["action"], "desktop-app-new-context")
            self.assertEqual(plan["task_id"], DEFAULT_TASK_ID)
            self.assertEqual(plan["command"], ["/tmp/fake-codex", "app", str(cwd)])

            guard_data = json.loads(guard.read_text(encoding="utf-8"))
            self.assertEqual(guard_data["auto_compact_count"], 0)
            self.assertFalse(guard_data["clear_required"])
            self.assertEqual(guard_data["last_reset_source"], "codex-auto-launcher")

    def test_task_option_uses_task_specific_guard_and_restore_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            root_guard = write_guard(cwd, clear_required=False, task_id=None)
            task_guard = write_guard(cwd, clear_required=True, task_id="image-async")

            result = run_auto(
                cwd,
                [
                    "--auto-no-exec",
                    "--auto-json",
                    "--auto-task",
                    "image-async",
                    "--auto-codex-bin",
                    "/tmp/fake-codex",
                    "resume",
                    "--last",
                    "继续修图片轮询",
                ],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["action"], "fresh-session")
            self.assertEqual(plan["task_id"], "image-async")
            self.assertTrue(plan["clear_required"])
            self.assertIn("-C", plan["command"])
            self.assertIn("Task ID: image-async", plan["command"][-1])
            self.assertIn(".agents/state/tasks/image-async/process.md", plan["command"][-1])

            task_guard_data = json.loads(task_guard.read_text(encoding="utf-8"))
            root_guard_data = json.loads(root_guard.read_text(encoding="utf-8"))
            self.assertEqual(task_guard_data["auto_compact_count"], 0)
            self.assertFalse(task_guard_data["clear_required"])
            self.assertFalse(root_guard_data["clear_required"])
            self.assertTrue(pathlib.Path(plan["resume_file"]).exists())
            self.assertIn(
                ".agents/state/tasks/image-async/process.md",
                pathlib.Path(plan["resume_file"]).read_text(encoding="utf-8"),
            )

    def test_legacy_root_guard_and_process_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            root_guard = write_guard(cwd, clear_required=True, task_id=None)
            root_process = cwd / ".agents" / "state" / "process.md"
            root_process.write_text("legacy root process\n", encoding="utf-8")
            original_guard = root_guard.read_text(encoding="utf-8")

            result = run_auto(
                cwd,
                [
                    "--auto-dry-run",
                    "--auto-json",
                    "--auto-codex-bin",
                    "/tmp/fake-codex",
                    "resume",
                    "--last",
                    "继续",
                ],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["task_id"], DEFAULT_TASK_ID)
            self.assertEqual(plan["action"], "passthrough")
            self.assertFalse(plan["clear_required"])
            self.assertEqual(root_process.read_text(encoding="utf-8"), "legacy root process\n")
            self.assertEqual(root_guard.read_text(encoding="utf-8"), original_guard)
            self.assertNotIn(".agents/state/process.md", json.dumps(plan))

    def test_valid_current_task_precedes_default_main(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            state_dir = cwd / ".agents" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "current-task").write_text("work\n", encoding="utf-8")
            write_guard(cwd, clear_required=True, task_id="work")

            result = run_auto(
                cwd,
                [
                    "--auto-dry-run",
                    "--auto-json",
                    "--auto-codex-bin",
                    "/tmp/fake-codex",
                    "resume",
                    "--last",
                    "继续",
                ],
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["task_id"], "work")
            self.assertEqual(plan["action"], "fresh-session")
            self.assertIn(".agents/state/tasks/work/process.md", plan["command"][-1])


if __name__ == "__main__":
    unittest.main()
