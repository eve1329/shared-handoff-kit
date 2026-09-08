#!/usr/bin/env python3
"""Activate the shared handoff runtime on behalf of an AI agent."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
INSTALL = PACKAGE_ROOT / "scripts" / "install.py"
VERIFY = PACKAGE_ROOT / "scripts" / "verify.py"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser().resolve()


def default_claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude")).expanduser().resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install, verify, and initialize the shared handoff runtime for an AI session."
    )
    parser.add_argument("--repo", type=Path, help="Target worktree for task-local state. Defaults to the caller's cwd.")
    parser.add_argument("--task-id", help="Task to initialize. Defaults to the active task or main.")
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--claude-home", type=Path, default=default_claude_home())
    parser.add_argument("--platform", choices=("auto", "posix", "windows"), default="auto")
    parser.add_argument("--dry-run", action="store_true", help="Plan installation only; do not change settings or task state.")
    parser.add_argument("--skip-task", action="store_true", help="Install the runtime without creating repository task state.")
    return parser.parse_args()


def run_stage(label: str, command: list[str], environment: dict[str, str]) -> int:
    print(f"== {label} ==", flush=True)
    return subprocess.run(command, cwd=str(PACKAGE_ROOT), env=environment, check=False).returncode


def resolve_repo(args: argparse.Namespace) -> Path | None:
    if args.skip_task:
        return None
    if args.repo:
        return args.repo.expanduser().resolve()

    caller = Path.cwd().resolve()
    # Opening the archive itself is not a request to create state in the archive.
    return None if caller == PACKAGE_ROOT else caller


def resolve_task_id(repo: Path, explicit_task_id: str | None) -> str:
    if explicit_task_id:
        return explicit_task_id.strip()

    pointer = repo / ".agents" / "state" / "current-task"
    try:
        candidate = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        candidate = ""
    return candidate if TASK_ID_RE.fullmatch(candidate) else "main"


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    claude_home = args.claude_home.expanduser().resolve()
    repo = resolve_repo(args)
    if repo and not repo.is_dir():
        print(f"ERROR: repository path is not a directory: {repo}", file=sys.stderr)
        return 1

    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    environment["CLAUDE_HOME"] = str(claude_home)

    install_command = [
        sys.executable,
        str(INSTALL),
        "--codex-home",
        str(codex_home),
        "--claude-home",
        str(claude_home),
        "--platform",
        args.platform,
    ]
    if args.dry_run:
        install_command.append("--dry-run")
    if run_stage("Install shared handoff runtime", install_command, environment) != 0:
        return 1

    verify_command = [sys.executable, str(VERIFY), "--platform", args.platform]
    if not args.dry_run:
        verify_command.extend(("--codex-home", str(codex_home), "--claude-home", str(claude_home)))
    if run_stage("Verify shared handoff runtime", verify_command, environment) != 0:
        return 1

    if args.dry_run:
        print("AI activation dry run complete. No configuration or task state was changed.")
        return 0
    if repo is None:
        print("AI activation complete. Runtime is installed and verified; no target worktree was selected for task state.")
        return 0

    task_id = resolve_task_id(repo, args.task_id)
    bootstrap = codex_home / "skills" / "task-id-bootstrap" / "scripts" / "bootstrap_task_id.py"
    task_command = [
        sys.executable,
        str(bootstrap),
        "--repo",
        str(repo),
        "--task-id",
        task_id,
        "--current-task",
        "Shared handoff runtime is active for this workspace.",
        "--next-step",
        "Begin or resume work using this task-local process state.",
        "--allow-unbound",
    ]
    if run_stage("Initialize task-local handoff state", task_command, environment) != 0:
        return 1

    print(f"AI activation complete. Runtime is installed and verified; task '{task_id}' is initialized at {repo}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
