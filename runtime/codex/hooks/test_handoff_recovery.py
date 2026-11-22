"""Behavioral regressions for stale state and modern Codex rollout recovery."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from test_context_handoff import SCRIPT, run_hook, task_state_dir


def load_hook():
    spec = importlib.util.spec_from_file_location("handoff_recovery_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_state(root, text="Verified local task state\n", stale=False):
    path = task_state_dir(root) / "process.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if stale:
        os.utime(path, (time.time() - 3600, time.time() - 3600))
    return path


class RecoveryTests(unittest.TestCase):
    def submit(self, root, event="UserPromptSubmit", **extra):
        result = run_hook(root, {"cwd": str(root), "hook_event_name": event,
            "transcript_path": str(root / "session.jsonl"), **extra})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_ordinary_prompt_repairs_stale_state_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            state = seed_state(root, stale=True)
            before = state.read_bytes()
            output = self.submit(root, prompt="检查这个错误")
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Handoff state needs repair: stale", context)
            self.assertIn(str(state), context)
            self.assertIn("process.recent.md", context)
            self.assertEqual(state.read_bytes(), before)

    def test_startup_template_requires_semantic_update(self):
        with tempfile.TemporaryDirectory() as td:
            output = self.submit(pathlib.Path(td), "SessionStart", source="startup")
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Handoff state needs repair: template", context)
            self.assertIn("Do not merely touch", context)

    def test_stop_requests_update_then_accepts_repaired_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            state = seed_state(root, stale=True)
            output = self.submit(root, "Stop", stop_hook_active=False)
            self.assertEqual(output["decision"], "block")
            self.assertIn(str(state), output["reason"])
            state.write_text("Verified completed work and next step\n")
            self.assertEqual(self.submit(root, "Stop", stop_hook_active=True), {})
            self.assertEqual(self.submit(root, "Stop", stop_hook_active=False), {})

    def test_stop_cannot_loop_if_agent_does_not_repair(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            seed_state(root, stale=True)
            self.assertEqual(self.submit(root, "Stop")["decision"], "block")
            self.assertEqual(self.submit(root, "Stop", stop_hook_active=True), {})
            # The following user turn still sees the unresolved condition.
            output = self.submit(root, prompt="下一步")
            self.assertIn("needs repair: stale", output["hookSpecificOutput"]["additionalContext"])

    def test_stop_rejects_new_template(self):
        with tempfile.TemporaryDirectory() as td:
            output = self.submit(pathlib.Path(td), "Stop")
            self.assertEqual(output["decision"], "block")
            self.assertIn("template", output["reason"])

    def test_worktree_notice_uses_metadata_without_importing_peer_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td).resolve()
            primary, linked = base / "primary", base / "linked"
            subprocess.run(["git", "init", "-q", str(primary)], check=True)
            subprocess.run(["git", "-C", str(primary), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                "commit", "--allow-empty", "-qm", "fixture"], check=True)
            subprocess.run(["git", "-C", str(primary), "worktree", "add", "--detach", "-q", str(linked)], check=True)
            local = seed_state(primary, "LOCAL_SENTINEL\n", stale=True)
            peer = seed_state(linked, "PEER_PRIVATE_SENTINEL\n")
            before = {p: p.read_bytes() for p in [local, peer]}
            output = self.submit(primary, prompt="继续")
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn(str(local), context)
            self.assertIn(str(peer), context)
            self.assertIn("newer than active state", context)
            self.assertNotIn("PEER_PRIVATE_SENTINEL", context)
            self.assertEqual(before, {p: p.read_bytes() for p in [local, peer]})
            mapping = json.loads((primary / ".agents/state/session-tasks.json").read_text())
            self.assertEqual(mapping["sessions"][str(primary / "session.jsonl")]["task_id"], "main")
            module = load_hook()
            read_text = pathlib.Path.read_text
            def guarded_read(path, *args, **kwargs):
                if path == peer:
                    raise AssertionError("peer semantic contents must not be read")
                return read_text(path, *args, **kwargs)
            with mock.patch.object(pathlib.Path, "read_text", guarded_read):
                self.assertIn(str(peer), module.state_location_message(primary, "main"))


class TranscriptRecoveryTests(unittest.TestCase):
    def event(self, item):
        return {"timestamp": "2026-11-23T00:00:00Z", "type": "event_msg",
            "payload": {"type": "item_completed", "item": item}}

    def write_records(self, root, records):
        path = root / "rollout.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return path

    def test_modern_completed_items_keep_messages_commands_changes_and_mcp_status(self):
        module = load_hook()
        items = [
            {"type": "UserMessage", "content": [{"type": "text", "text": "恢复工作"}]},
            {"type": "AgentMessage", "content": [{"type": "Text", "text": "验证通过"}]},
            {"type": "CommandExecution", "command": ["python3", "-m", "unittest"],
                "cwd": "/tmp/project", "exit_code": 0, "status": "completed"},
            {"type": "FileChange", "changes": {"/tmp/project/a.py": {"type": "update", "unified_diff": "PRIVATE_DIFF"}}},
            {"type": "McpToolCall", "server": "canvas", "tool": "get_status", "status": "completed", "arguments": {"secret": "PRIVATE_ARG"}},
            {"type": "Reasoning", "raw_content": ["PRIVATE_REASONING"]},
        ]
        with tempfile.TemporaryDirectory() as td:
            path = self.write_records(pathlib.Path(td), [self.event(i) for i in items])
            events = module.recent_transcript_events(str(path))
        text = "\n".join(events)
        self.assertEqual(len(events), 5)
        for expected in ["恢复工作", "验证通过", "python3 -m unittest", "exit=0", "/tmp/project/a.py", "canvas.get_status"]:
            self.assertIn(expected, text)
        for excluded in ["PRIVATE_DIFF", "PRIVATE_ARG", "PRIVATE_REASONING"]:
            self.assertNotIn(excluded, text)

    def test_freeform_tools_and_structured_outputs_are_recovered(self):
        module = load_hook()
        records = [
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch",
                "input": "*** Begin Patch\n*** Update File: /tmp/a.py\n+PRIVATE_DIFF\n*** End Patch"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call1",
                "output": [{"type": "text", "text": "Patch applied"}]}},
            self.event({"type": "file_change", "changes": [{"path": "/tmp/b.py"}]}),
        ]
        with tempfile.TemporaryDirectory() as td:
            path = self.write_records(pathlib.Path(td), records)
            events = module.recent_transcript_events(str(path))
        text = "\n".join(events)
        for expected in ["apply_patch", "/tmp/a.py", "Patch applied", "/tmp/b.py"]:
            self.assertIn(expected, text)
        self.assertNotIn("PRIVATE_DIFF", text)

    def test_legacy_compatibility_deduplication_and_limit(self):
        module = load_hook()
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            records = [
                {"type": "response_item", "payload": {"type": "message", "id": "same-message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "one answer"}]}},
                self.event({"type": "AgentMessage", "id": "same-message", "content": [{"type": "Text", "text": "one answer"}]}),
                {"type": "response_item", "payload": {"type": "function_call", "name": "test", "arguments": "{}"}},
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "test", "output": "PASS"}},
                {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": "INTERNAL_INSTRUCTIONS"}},
            ]
            path = self.write_records(root, records)
            with path.open("a") as f:
                f.write("{partial-json\n")
            events = module.recent_transcript_events(str(path))
            self.assertEqual(len(events), 3)
            self.assertEqual("\n".join(events).count("one answer"), 1)
            self.assertNotIn("INTERNAL_INSTRUCTIONS", "\n".join(events))
            self.assertEqual(len(module.recent_transcript_events(str(path), limit=2)), 2)
            self.assertIn("PASS", events[-1])

    def test_precompact_persists_modern_evidence_to_task_local_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            transcript = self.write_records(root, [self.event({"type": "FileChange",
                "changes": {"/tmp/fixed.py": {"type": "update"}}})])
            result = run_hook(root, {"cwd": str(root), "hook_event_name": "PreCompact", "trigger": "auto",
                "transcript_path": str(transcript)})
            self.assertEqual(result.returncode, 0, result.stderr)
            recent = task_state_dir(root) / "process.recent.md"
            self.assertIn("/tmp/fixed.py", recent.read_text())
            self.assertFalse((root / ".agents/state/process.recent.md").exists())


if __name__ == "__main__":
    unittest.main()
