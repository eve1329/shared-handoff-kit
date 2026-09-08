#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIVATE = ROOT / "scripts" / "activate.py"
INSTALL = ROOT / "scripts" / "install.py"
VERIFY = ROOT / "scripts" / "verify.py"


def run_script(script: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )


class SharedHandoffInstallTests(unittest.TestCase):
    def test_dry_run_does_not_create_target_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            codex = root / "codex"
            claude = root / "claude"
            result = run_script(
                INSTALL,
                "--codex-home",
                str(codex),
                "--claude-home",
                str(claude),
                "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(codex.exists())
            self.assertFalse(claude.exists())
            self.assertIn("DRY-RUN", result.stdout)

    def test_install_merges_settings_and_installs_runtime_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            codex = root / "codex"
            claude = root / "claude"
            (codex / "hooks").mkdir(parents=True)
            (claude).mkdir(parents=True)
            (codex / "hooks.json").write_text(
                json.dumps({"custom": {"keep": True}, "hooks": {"UserPromptSubmit": []}}) + "\n",
                encoding="utf-8",
            )
            (claude / "settings.json").write_text(
                json.dumps({"permissions": {"defaultMode": "auto"}}) + "\n",
                encoding="utf-8",
            )

            result = run_script(INSTALL, "--codex-home", str(codex), "--claude-home", str(claude))
            self.assertEqual(result.returncode, 0, result.stderr)

            self.assertTrue((codex / "hooks/context_handoff.py").is_file())
            self.assertTrue((codex / "bin/codex-auto").is_file())
            self.assertTrue((claude / "hooks/session_start.py").is_file())
            self.assertTrue((codex / "skills/task-id-bootstrap/SKILL.md").is_file())
            self.assertTrue((codex / "skills/handoff/SKILL.md").is_file())
            self.assertTrue((codex / "skills/claude-handoff/SKILL.md").is_file())
            self.assertTrue((codex / "skills/shared-handoff-kit/SKILL.md").is_file())
            self.assertTrue(os.access(codex / "bin/codex-auto", os.X_OK))

            codex_settings = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
            self.assertTrue(codex_settings["custom"]["keep"])
            self.assertEqual(set(codex_settings["hooks"]), {"SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop"})
            claude_settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(claude_settings["permissions"]["defaultMode"], "auto")
            self.assertIn("Stop", claude_settings["hooks"])
            self.assertIn("Global Context Handoff", (codex / "AGENTS.md").read_text(encoding="utf-8"))

    def test_changed_runtime_file_is_backed_up_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            codex = root / "codex"
            claude = root / "claude"
            first = run_script(INSTALL, "--codex-home", str(codex), "--claude-home", str(claude))
            self.assertEqual(first.returncode, 0, first.stderr)
            target = codex / "hooks/context_handoff.py"
            target.write_text("locally modified\n", encoding="utf-8")

            second = run_script(INSTALL, "--codex-home", str(codex), "--claude-home", str(claude))
            self.assertEqual(second.returncode, 0, second.stderr)
            backups = list((codex / "hooks").glob("context_handoff.py.bak.shared-handoff-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "locally modified\n")
            self.assertNotEqual(target.read_text(encoding="utf-8"), "locally modified\n")

    def test_malformed_settings_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            codex = root / "codex"
            claude = root / "claude"
            codex.mkdir()
            claude.mkdir()
            codex_settings = codex / "hooks.json"
            codex_settings.write_text("not json\n", encoding="utf-8")
            result = run_script(INSTALL, "--codex-home", str(codex), "--claude-home", str(claude))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(codex_settings.read_text(encoding="utf-8"), "not json\n")

    def test_verify_accepts_installed_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            codex = root / "codex"
            claude = root / "claude"
            install = run_script(INSTALL, "--codex-home", str(codex), "--claude-home", str(claude))
            self.assertEqual(install.returncode, 0, install.stderr)
            result = run_script(VERIFY, "--codex-home", str(codex), "--claude-home", str(claude))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("verification complete", result.stdout)

    def test_windows_target_installs_cmd_launchers_and_windows_hook_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            codex = root / "codex"
            claude = root / "claude"
            result = run_script(
                INSTALL,
                "--codex-home",
                str(codex),
                "--claude-home",
                str(claude),
                "--platform",
                "windows",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((codex / "bin/codex-auto.py").is_file())
            self.assertTrue((codex / "bin/codex.cmd").is_file())
            self.assertTrue((codex / "bin/codex-auto.cmd").is_file())
            self.assertTrue((codex / "hooks/shared_handoff_lock.py").is_file())
            self.assertTrue((claude / "hooks/shared_handoff_lock.py").is_file())

            codex_settings = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
            claude_settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
            codex_commands = [
                hook["command"]
                for groups in codex_settings["hooks"].values()
                for group in groups
                for hook in group["hooks"]
            ]
            claude_commands = [
                hook["command"]
                for groups in claude_settings["hooks"].values()
                for group in groups
                for hook in group["hooks"]
            ]
            self.assertTrue(all(command.startswith('py -3 "') for command in codex_commands))
            self.assertTrue(all(command.startswith('py -3 "') for command in claude_commands))
            self.assertIn("Target platform: windows", result.stdout)

            verify = run_script(
                VERIFY,
                "--codex-home",
                str(codex),
                "--claude-home",
                str(claude),
                "--platform",
                "windows",
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_ai_activation_installs_verifies_and_initializes_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            codex = root / "codex"
            claude = root / "claude"
            project = root / "project"
            project.mkdir()

            result = run_script(
                ACTIVATE,
                "--repo",
                str(project),
                "--codex-home",
                str(codex),
                "--claude-home",
                str(claude),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((codex / "hooks/context_handoff.py").is_file())
            self.assertTrue((claude / "hooks/session_start.py").is_file())
            self.assertTrue((project / ".agents/state/tasks/main/process.md").is_file())
            self.assertEqual((project / ".agents/state/current-task").read_text(encoding="utf-8"), "main\n")
            self.assertIn("AI activation complete.", result.stdout)

    def test_ai_activation_keeps_existing_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            codex = root / "codex"
            claude = root / "claude"
            project = root / "project"
            (project / ".agents/state").mkdir(parents=True)
            (project / ".agents/state/current-task").write_text("existing-task\n", encoding="utf-8")

            result = run_script(
                ACTIVATE,
                "--repo",
                str(project),
                "--codex-home",
                str(codex),
                "--claude-home",
                str(claude),
                "--platform",
                "windows",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((codex / "bin/codex.cmd").is_file())
            self.assertTrue((codex / "bin/codex-auto.cmd").is_file())
            self.assertTrue((project / ".agents/state/tasks/existing-task/process.md").is_file())
            hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for groups in hooks["hooks"].values()
                for group in groups
                for hook in group["hooks"]
            ]
            self.assertTrue(all(command.startswith('py -3 "') for command in commands))
            self.assertIn("task 'existing-task'", result.stdout)

    def test_ai_activation_dry_run_writes_neither_targets_nor_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            codex = root / "codex"
            claude = root / "claude"
            project = root / "project"
            project.mkdir()

            result = run_script(
                ACTIVATE,
                "--repo",
                str(project),
                "--codex-home",
                str(codex),
                "--claude-home",
                str(claude),
                "--dry-run",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(codex.exists())
            self.assertFalse(claude.exists())
            self.assertFalse((project / ".agents").exists())
            self.assertIn("AI activation dry run complete.", result.stdout)


if __name__ == "__main__":
    unittest.main()
