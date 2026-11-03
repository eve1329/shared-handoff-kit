"""Regressions for real continuation prompts and durable rollout bindings."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from test_context_handoff import SCRIPT, run_hook, seed_task_binding, task_state_dir


class TaskBindingTests(unittest.TestCase):
    def submit(self, root, transcript, prompt=None, event="UserPromptSubmit", **extra):
        payload = {"cwd": str(root), "transcript_path": str(transcript), "hook_event_name": event, **extra}
        if prompt is not None:
            payload["prompt"] = prompt
        result = run_hook(root, payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def mapping(self, root, transcript):
        sessions = json.loads((root / ".agents/state/session-tasks.json").read_text())["sessions"]
        for path, entry in sessions.items():
            if pathlib.Path(path).resolve() == transcript.resolve():
                return entry
        raise KeyError(str(transcript))

    def pair(self, base):
        primary, linked = base / "primary", base / "linked"
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        subprocess.run(["git", "-C", str(primary), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                        "commit", "--allow-empty", "-qm", "fixture"], check=True)
        subprocess.run(["git", "-C", str(primary), "worktree", "add", "--detach", "-q", str(linked)], check=True)
        return primary, linked

    def test_actual_recent_continuation_prompts_replace_wrong_bindings(self):
        cases = [("继续main", "main", "upstream-pr-review-20261102"),
                 ("main继续", "main", "review"),
                 ("继续 epay，至尊7.8折用于【分类名称】", "epay", "handoff")]
        for prompt, wanted, stale in cases:
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                transcript = root / "current.jsonl"
                task_state_dir(root, wanted).mkdir(parents=True)
                seed_task_binding(root, str(transcript), stale)
                output = self.submit(root, transcript, prompt)
                self.assertEqual(self.mapping(root, transcript)["task_id"], wanted)
                context = output["hookSpecificOutput"]["additionalContext"]
                self.assertIn(f"active task id: `{wanted}`", context)
                self.assertIn("Task binding: VERIFIED", context)

    def test_explicit_assignment_injects_binding_without_continuation_keyword(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            transcript = root / "current.jsonl"
            seed_task_binding(root, str(transcript), "review")
            output = self.submit(root, transcript, "task-id=image-async")
            self.assertIn("Task binding: VERIFIED", output["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(self.mapping(root, transcript)["binding_strength"], "explicit")

    def test_regular_followup_reasserts_binding_and_keeps_selection_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            a, b = root / "a.jsonl", root / "b.jsonl"
            self.submit(root, a, "task-id=epay")
            before = self.mapping(root, a)["bound_at"]
            self.submit(root, b, "task-id=handoff")
            output = self.submit(root, a, "以后我如何操作")
            self.assertEqual(self.mapping(root, a)["task_id"], "epay")
            self.assertEqual(self.mapping(root, a)["bound_at"], before)
            self.assertIn("active task id: `epay`", output["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(self.mapping(root, b)["task_id"], "handoff")

    def test_branch_commands_and_unknown_short_words_do_not_switch_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            transcript = root / "current.jsonl"
            self.submit(root, transcript, "task-id=review")
            task_state_dir(root, "epay").mkdir(parents=True)
            for prompt in ("切到main分支", "继续 main 分支", "继续main分支", "开新分支epay", "继续debug"):
                with self.subTest(prompt=prompt):
                    self.submit(root, transcript, prompt)
                    self.assertEqual(self.mapping(root, transcript)["task_id"], "review")

    def test_worktree_binding_beats_newer_fallback_and_does_not_copy_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            primary, linked = self.pair(base)
            transcript = base / "current.jsonl"
            self.submit(primary, transcript, "task-id=epay")
            semantic = task_state_dir(primary, "epay") / "process.md"
            semantic.write_text("Private authoritative task state\n")
            seed_task_binding(linked, str(transcript), "wrong")
            sessions = linked / ".agents/state/session-tasks.json"
            sessions.write_text(json.dumps({"sessions": {str(transcript): {
                "task_id": "wrong", "binding_strength": "provisional", "updated": "2099-01-01T00:00:00+00:00"}}}))
            self.submit(linked, transcript, "继续")
            self.assertEqual(self.mapping(linked, transcript)["task_id"], "epay")
            self.assertEqual(semantic.read_text(), "Private authoritative task state\n")
            self.assertNotEqual((task_state_dir(linked, "epay") / "process.md").read_text(), semantic.read_text())

    def test_explicit_rebind_in_linked_worktree_survives_return_to_primary(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            primary, linked = self.pair(base)
            transcript = base / "current.jsonl"
            self.submit(primary, transcript, "task-id=old")
            self.submit(linked, transcript, "task-id=new")
            selected_at = self.mapping(linked, transcript)["bound_at"]
            self.submit(primary, transcript, event="SessionStart", source="compact")
            self.assertEqual(self.mapping(primary, transcript)["task_id"], "new")
            self.assertEqual(self.mapping(primary, transcript)["bound_at"], selected_at)

    def test_stop_does_not_reselect_task_from_old_prompt_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            transcript = root / "current.jsonl"
            self.submit(root, transcript, "task-id=new")
            self.submit(root, transcript, "task-id=old", event="Stop")
            self.assertEqual(self.mapping(root, transcript)["task_id"], "new")

    def test_missing_transcript_resolves_exact_thread_and_ambiguity_is_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            home = root / "codex"
            sessions = home / "sessions"
            sessions.mkdir(parents=True)
            thread = "019eb4bd-dbf7-7432-843e-0317ed0639ea"
            transcript = sessions / f"rollout-test-{thread}.jsonl"
            transcript.write_text("")
            payload = {"cwd": str(root), "hook_event_name": "UserPromptSubmit", "prompt": "task-id=epay"}
            env = {"CODEX_HOME": str(home), "CODEX_THREAD_ID": thread}
            result = run_hook(root, payload, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.mapping(root, transcript)["task_id"], "epay")
            (sessions / f"rollout-other-{thread}.jsonl").write_text("")
            result = run_hook(root, payload, env)
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Task binding: NOT VERIFIED", context)

    def test_unrelated_repo_cannot_supply_session_binding(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            primary, linked = self.pair(base)
            unrelated = base / "unrelated"
            unrelated.mkdir()
            transcript = base / "current.jsonl"
            self.submit(primary, transcript, "task-id=private")
            self.submit(unrelated, transcript, "继续")
            self.assertEqual(self.mapping(unrelated, transcript)["task_id"], "main")

    def test_binding_status_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            transcript = root / "current.jsonl"
            self.submit(root, transcript, "task-id=epay")
            path = root / ".agents/state/session-tasks.json"
            before = path.read_bytes()
            result = subprocess.run([sys.executable, str(SCRIPT), "--binding-status"], text=True, capture_output=True,
                input=json.dumps({"cwd": str(root), "transcript_path": str(transcript)}))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["task_id"], "epay")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
