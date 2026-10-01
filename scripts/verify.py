#!/usr/bin/env python3
"""Validate the bundle and, optionally, an installed target."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "runtime"
REQUIRED_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "SKILL.md",
    "agents/openai.yaml",
    "scripts/activate.py",
    "scripts/install.py",
    "scripts/verify.py",
    "runtime/codex/hooks/context_handoff.py",
    "runtime/codex/hooks/shared_handoff_lock.py",
    "runtime/codex/hooks.json",
    "runtime/codex/bin/codex-auto",
    "runtime/codex/bin/codex",
    "runtime/codex/bin/codex.cmd",
    "runtime/codex/bin/codex-auto.cmd",
    "runtime/claude/hooks/task_routing.py",
    "runtime/claude/hooks/session_start.py",
    "runtime/claude/hooks/task_context.py",
    "runtime/claude/hooks/context_handoff.py",
    "runtime/claude/hooks/shared_handoff_lock.py",
    "runtime/skills/task-id-bootstrap/scripts/bootstrap_task_id.py",
    "runtime/skills/handoff/SKILL.md",
    "runtime/skills/claude-handoff/SKILL.md",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the shared handoff bundle.")
    parser.add_argument("--codex-home", type=Path, help="Also verify installed Codex runtime files.")
    parser.add_argument("--claude-home", type=Path, help="Also verify installed Claude runtime files.")
    parser.add_argument("--platform", choices=("auto", "posix", "windows"), default="auto")
    return parser.parse_args()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)
    print(f"PASS: {message}")


def run_runtime_hook(
    script: Path,
    payload: dict[str, Any],
    root: Path,
    environment: dict[str, str],
) -> None:
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=str(root),
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    check(
        result.returncode == 0,
        f"runtime hook executes: {script.relative_to(PACKAGE_ROOT)}"
        + (f" ({result.stderr.strip()})" if result.returncode else ""),
    )
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"runtime hook returned invalid JSON: {script.relative_to(PACKAGE_ROOT)}: {error}"
        ) from error
    check(isinstance(output, dict), f"runtime hook returns JSON object: {script.relative_to(PACKAGE_ROOT)}")


def verify_runtime_entrypoints() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(root / "codex-home")

        scenarios = (
            (
                RUNTIME_ROOT / "codex" / "hooks" / "context_handoff.py",
                {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(root)},
            ),
            (
                RUNTIME_ROOT / "codex" / "hooks" / "context_handoff.py",
                {"hook_event_name": "UserPromptSubmit", "prompt": "task=verify-smoke", "cwd": str(root)},
            ),
            (
                RUNTIME_ROOT / "codex" / "hooks" / "context_handoff.py",
                {"hook_event_name": "PreCompact", "trigger": "auto", "cwd": str(root)},
            ),
            (
                RUNTIME_ROOT / "codex" / "hooks" / "context_handoff.py",
                {"hook_event_name": "PostCompact", "trigger": "auto", "cwd": str(root)},
            ),
            (
                RUNTIME_ROOT / "codex" / "hooks" / "context_handoff.py",
                {"hook_event_name": "Stop", "cwd": str(root)},
            ),
            (
                RUNTIME_ROOT / "claude" / "hooks" / "session_start.py",
                {"cwd": str(root), "session_id": "verify-claude"},
            ),
            (
                RUNTIME_ROOT / "claude" / "hooks" / "task_context.py",
                {"cwd": str(root), "prompt": "task=verify-smoke", "session_id": "verify-claude"},
            ),
            (
                RUNTIME_ROOT / "claude" / "hooks" / "context_handoff.py",
                {"cwd": str(root), "session_id": "verify-claude"},
            ),
        )
        for script, payload in scenarios:
            run_runtime_hook(script, payload, root, environment)


def target_platform(value: str) -> str:
    if value != "auto":
        return value
    return "windows" if os.name == "nt" else "posix"


def verify_package() -> None:
    for relative in REQUIRED_FILES:
        check((PACKAGE_ROOT / relative).is_file(), f"package contains {relative}")

    for source in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        with tempfile.NamedTemporaryFile(suffix=".pyc") as compiled:
            py_compile.compile(str(source), cfile=compiled.name, doraise=True)
        print(f"PASS: Python syntax {source.relative_to(PACKAGE_ROOT)}")

    hooks = json.loads((RUNTIME_ROOT / "codex" / "hooks.json").read_text(encoding="utf-8"))
    events = set(hooks.get("hooks", {}))
    check({"SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop"} <= events, "Codex hook events are registered")
    verify_runtime_entrypoints()
    documentation = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in PACKAGE_ROOT.rglob("*.md"))
    check(not re.search(r"sk-[A-Za-z0-9]{16,}", documentation), "bundle documentation contains no API token")
    all_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in PACKAGE_ROOT.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    machine_path = "/Users/" + "ming"
    check(machine_path not in all_text, "bundle contains no machine-specific user path")
    for runtime_script in (
        RUNTIME_ROOT / "codex" / "hooks" / "context_handoff.py",
        RUNTIME_ROOT / "claude" / "hooks" / "task_routing.py",
        RUNTIME_ROOT / "skills" / "task-id-bootstrap" / "scripts" / "bootstrap_task_id.py",
    ):
        check("import fcntl" not in runtime_script.read_text(encoding="utf-8"), f"runtime lock import is platform-neutral: {runtime_script.name}")


def verify_installed(codex_home: Path | None, claude_home: Path | None, platform: str) -> None:
    if codex_home:
        codex_home = codex_home.expanduser().resolve()
        pairs = {
            "runtime/codex/hooks/context_handoff.py": codex_home / "hooks/context_handoff.py",
            "runtime/codex/hooks/shared_handoff_lock.py": codex_home / "hooks/shared_handoff_lock.py",
        }
        if platform == "windows":
            pairs.update(
                {
                    "runtime/codex/bin/codex-auto": codex_home / "bin/codex-auto.py",
                    "runtime/codex/bin/codex.cmd": codex_home / "bin/codex.cmd",
                    "runtime/codex/bin/codex-auto.cmd": codex_home / "bin/codex-auto.cmd",
                }
            )
        else:
            pairs.update(
                {
                    "runtime/codex/bin/codex-auto": codex_home / "bin/codex-auto",
                    "runtime/codex/bin/codex": codex_home / "bin/codex",
                }
            )
        for source, target in pairs.items():
            check(target.is_file(), f"installed Codex file exists: {target}")
            check(digest(PACKAGE_ROOT / source) == digest(target), f"installed Codex file matches: {target}")
        hooks_path = codex_home / "hooks.json"
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        commands = json.dumps(hooks, ensure_ascii=False)
        check("context_handoff.py" in commands, "installed Codex hooks reference context_handoff.py")
        if platform == "windows":
            check("py -3" in commands, "installed Codex hooks use the Windows Python launcher")

    if claude_home:
        claude_home = claude_home.expanduser().resolve()
        pairs = {
            "runtime/claude/hooks/task_routing.py": claude_home / "hooks/task_routing.py",
            "runtime/claude/hooks/session_start.py": claude_home / "hooks/session_start.py",
            "runtime/claude/hooks/task_context.py": claude_home / "hooks/task_context.py",
            "runtime/claude/hooks/context_handoff.py": claude_home / "hooks/context_handoff.py",
            "runtime/claude/hooks/shared_handoff_lock.py": claude_home / "hooks/shared_handoff_lock.py",
        }
        for source, target in pairs.items():
            check(target.is_file(), f"installed Claude file exists: {target}")
            check(digest(PACKAGE_ROOT / source) == digest(target), f"installed Claude file matches: {target}")
        settings = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
        commands = json.dumps(settings, ensure_ascii=False)
        check("session_start.py" in commands and "task_context.py" in commands and "context_handoff.py" in commands, "installed Claude hooks are registered")
        if platform == "windows":
            check("py -3" in commands, "installed Claude hooks use the Windows Python launcher")


def main() -> int:
    try:
        args = parse_args()
        verify_package()
        platform = target_platform(args.platform)
        verify_installed(args.codex_home, args.claude_home, platform)
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError, py_compile.PyCompileError) as error:
        print(f"FAIL: {error}")
        return 1
    print(f"Shared Handoff Kit verification complete (platform: {target_platform(args.platform)}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
