#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).with_name("bootstrap_task_id.py")


def run_bootstrap(
    repo: pathlib.Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    effective_env = os.environ.copy()
    effective_env.pop("CODEX_HOME", None)
    effective_env.pop("CODEX_THREAD_ID", None)
    if env:
        effective_env.update(env)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            *arguments,
        ],
        env=effective_env,
        text=True,
        capture_output=True,
    )


def read_session_tasks(repo: pathlib.Path) -> dict:
    return json.loads(
        (repo / ".agents" / "state" / "session-tasks.json").read_text(encoding="utf-8")
    )


class BootstrapTaskIdTests(unittest.TestCase):
    def test_existing_root_process_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            transcript = repo / "rollout-current.jsonl"
            transcript.write_text("", encoding="utf-8")
            root_process = repo / ".agents" / "state" / "process.md"
            root_process.parent.mkdir(parents=True)
            original = "# Root Process\n\nThis belongs to another task.\n"
            root_process.write_text(original, encoding="utf-8")

            result = run_bootstrap(
                repo,
                "--task-id",
                "parallel-task",
                "--transcript-path",
                str(transcript),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(root_process.read_text(encoding="utf-8"), original)
            self.assertTrue(
                (repo / ".agents" / "state" / "tasks" / "parallel-task" / "process.md").exists()
            )

    def test_explicit_transcript_activates_task_and_replaces_stale_binding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            transcript = repo / "rollout-current.jsonl"
            transcript.write_text("", encoding="utf-8")
            state_dir = repo / ".agents" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "current-task").write_text("sol\n", encoding="utf-8")
            (state_dir / "session-tasks.json").write_text(
                json.dumps(
                    {
                        "sessions": {
                            str(transcript): {
                                "task_id": "sol",
                                "updated": "2026-07-10T00:00:00+00:00",
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_bootstrap(
                repo,
                "--task-id",
                "map-sub5.6",
                "--transcript-path",
                str(transcript),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                (repo / ".agents" / "state" / "tasks" / "map-sub5.6" / "process.md").exists()
            )
            self.assertEqual(
                (state_dir / "current-task").read_text(encoding="utf-8").strip(),
                "map-sub5.6",
            )
            self.assertEqual(
                read_session_tasks(repo)["sessions"][str(transcript)]["task_id"],
                "map-sub5.6",
            )
            self.assertIn(f"Session binding: {transcript}", result.stdout)
            self.assertIn("Effective task: map-sub5.6", result.stdout)

    def test_thread_id_resolves_unique_rollout_under_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            repo = root / "repo"
            repo.mkdir()
            codex_home = root / "codex-home"
            thread_id = "019f1234-abcd-7000-8000-123456789abc"
            transcript_dir = codex_home / "sessions" / "2026" / "07" / "10"
            transcript_dir.mkdir(parents=True)
            transcript = transcript_dir / f"rollout-2026-07-10T12-00-00-{thread_id}.jsonl"
            transcript.write_text("", encoding="utf-8")

            result = run_bootstrap(
                repo,
                "--task-id",
                "map-sub5.6",
                env={"CODEX_HOME": str(codex_home), "CODEX_THREAD_ID": thread_id},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                read_session_tasks(repo)["sessions"][str(transcript)]["task_id"],
                "map-sub5.6",
            )
            self.assertIn(f"Session binding: {transcript}", result.stdout)

    def test_missing_session_identity_reports_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)

            result = run_bootstrap(repo, "--task-id", "map-sub5.6")

            self.assertEqual(result.returncode, 2)
            self.assertTrue(
                (repo / ".agents" / "state" / "tasks" / "map-sub5.6" / "process.md").exists()
            )
            self.assertEqual(
                (repo / ".agents" / "state" / "current-task").read_text(encoding="utf-8").strip(),
                "map-sub5.6",
            )
            self.assertFalse((repo / ".agents" / "state" / "session-tasks.json").exists())
            self.assertIn("Session binding: NOT BOUND", result.stderr)
            self.assertIn("CODEX_THREAD_ID", result.stderr)

    def test_ambiguous_thread_rollouts_are_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            repo = root / "repo"
            repo.mkdir()
            codex_home = root / "codex-home"
            thread_id = "019f1234-abcd-7000-8000-123456789abc"
            for day in ("09", "10"):
                transcript_dir = codex_home / "sessions" / "2026" / "07" / day
                transcript_dir.mkdir(parents=True)
                (transcript_dir / f"rollout-2026-07-{day}T12-00-00-{thread_id}.jsonl").write_text(
                    "", encoding="utf-8"
                )

            result = run_bootstrap(
                repo,
                "--task-id",
                "map-sub5.6",
                env={"CODEX_HOME": str(codex_home), "CODEX_THREAD_ID": thread_id},
            )

            self.assertEqual(result.returncode, 2)
            self.assertFalse((repo / ".agents" / "state" / "session-tasks.json").exists())
            self.assertIn("multiple rollout transcripts", result.stderr)

    def test_invalid_task_id_cannot_escape_tasks_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            transcript = repo / "rollout-current.jsonl"
            transcript.write_text("", encoding="utf-8")

            result = run_bootstrap(
                repo,
                "--task-id",
                "../escape",
                "--transcript-path",
                str(transcript),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid task-id", result.stderr)
            self.assertFalse((repo / ".agents" / "state" / "escape").exists())


if __name__ == "__main__":
    unittest.main()
