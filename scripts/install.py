#!/usr/bin/env python3
"""Install the shared Codex/Claude handoff runtime without leaking local config."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "runtime"
SKILL_NAME = PACKAGE_ROOT.name
SKIP_NAMES = {".DS_Store", "__pycache__"}


def timestamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser().resolve()


def default_claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude")).expanduser().resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install shared task-local handoff hooks.")
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--claude-home", type=Path, default=default_claude_home())
    parser.add_argument("--dry-run", action="store_true", help="Show writes without changing files.")
    parser.add_argument(
        "--platform",
        choices=("auto", "posix", "windows"),
        default="auto",
        help="Target runtime platform. Defaults to the current system; use only for isolated validation.",
    )
    parser.add_argument("--skip-self", action="store_true", help="Do not install this skill into Codex skills.")
    parser.add_argument("--skip-companion-skills", action="store_true", help="Do not install task/handoff companion skills.")
    return parser.parse_args()


def normalized(path: Path) -> Path:
    return path.expanduser().resolve()


def target_platform(args: argparse.Namespace) -> str:
    if args.platform != "auto":
        return str(args.platform)
    return "windows" if os.name == "nt" else "posix"


def hook_command(script: Path, platform: str) -> str:
    if platform == "windows":
        return f'py -3 "{script}"'
    return f"python3 {shlex.quote(str(script))}"


def same_bytes(left: Path, right: Path) -> bool:
    try:
        return left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak.shared-handoff-{timestamp()}")


def backup_existing(path: Path, dry_run: bool) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    backup = backup_path(path)
    if dry_run:
        print(f"DRY-RUN backup {path} -> {backup}")
        return backup
    if path.is_dir() and not path.is_symlink():
        shutil.copytree(path, backup)
    else:
        shutil.copy2(path, backup)
    print(f"BACKUP {path} -> {backup}")
    return backup


def atomic_write(path: Path, content: bytes, mode: int, dry_run: bool) -> bool:
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_file() and path.read_bytes() == content:
        if stat.S_IMODE(path.stat().st_mode) != mode:
            if dry_run:
                print(f"DRY-RUN chmod {path} {oct(mode)}")
            else:
                path.chmod(mode)
        return False

    backup_existing(path, dry_run)
    if dry_run:
        print(f"DRY-RUN write {path}")
        return True

    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"WRITE {path}")
    return True


def install_file(source: Path, target: Path, dry_run: bool) -> bool:
    mode = stat.S_IMODE(source.stat().st_mode)
    return atomic_write(target, source.read_bytes(), mode, dry_run)


def install_text(target: Path, content: str, dry_run: bool, mode: int = 0o644) -> bool:
    return atomic_write(target, content.encode("utf-8"), mode, dry_run)


def install_tree(source: Path, target: Path, dry_run: bool) -> None:
    for path in sorted(source.rglob("*")):
        if any(part in SKIP_NAMES for part in path.parts):
            continue
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            if not dry_run:
                destination.mkdir(parents=True, exist_ok=True)
            continue
        install_file(path, destination, dry_run)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse JSON settings {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"settings root must be an object: {path}")
    return value


def has_command(groups: list[Any], command: str) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if isinstance(hook, dict) and hook.get("command") == command:
                return True
    return False


def add_hook(
    settings: dict[str, Any],
    event: str,
    command: str,
    *,
    matcher: str | None = None,
    timeout: int = 10,
    status_message: str | None = None,
) -> None:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.hooks must be an object")
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise ValueError(f"settings.hooks.{event} must be an array")
    if has_command(groups, command):
        return
    hook: dict[str, Any] = {"type": "command", "command": command, "timeout": timeout}
    if status_message:
        hook["statusMessage"] = status_message
    group: dict[str, Any] = {"hooks": [hook]}
    if matcher:
        group["matcher"] = matcher
    groups.append(group)


def merge_codex_hooks(path: Path, codex_home: Path, platform: str) -> str:
    settings = read_json(path)
    command = hook_command(codex_home / "hooks" / "context_handoff.py", platform)
    add_hook(settings, "SessionStart", command, matcher="startup|resume|clear|compact", status_message="Checking context handoff state")
    add_hook(settings, "UserPromptSubmit", command, status_message="Checking continuation context")
    add_hook(settings, "PreCompact", command, matcher="manual|auto", timeout=20, status_message="Saving context snapshot before compact")
    add_hook(settings, "PostCompact", command, matcher="manual|auto", timeout=20, status_message="Recording compact completion")
    add_hook(settings, "Stop", command, status_message="Updating context handoff snapshot")
    return json.dumps(settings, ensure_ascii=False, indent=2) + "\n"


def merge_claude_settings(path: Path, claude_home: Path, platform: str) -> str:
    settings = read_json(path)
    hooks_dir = claude_home / "hooks"
    add_hook(settings, "SessionStart", hook_command(hooks_dir / "session_start.py", platform), matcher="startup|resume|clear|compact", status_message="Restoring task handoff")
    add_hook(settings, "UserPromptSubmit", hook_command(hooks_dir / "task_context.py", platform), status_message="Checking task handoff")
    add_hook(settings, "Stop", hook_command(hooks_dir / "context_handoff.py", platform), status_message="Updating task handoff snapshot")
    return json.dumps(settings, ensure_ascii=False, indent=2) + "\n"


def install_global_agents(path: Path, source: Path, dry_run: bool) -> None:
    source_text = source.read_text(encoding="utf-8").strip()
    marker = "<!-- shared-handoff-kit:global-contract -->"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if source_text in existing or marker in existing:
            return
        content = f"{existing.rstrip()}\n\n{marker}\n{source_text}\n{marker}\n"
    else:
        content = source_text + "\n"
    install_text(path, content, dry_run)


def install_runtime(args: argparse.Namespace) -> None:
    codex_home = normalized(args.codex_home)
    claude_home = normalized(args.claude_home)
    dry_run = bool(args.dry_run)
    platform = target_platform(args)

    codex_files = {
        RUNTIME_ROOT / "codex" / "hooks" / "context_handoff.py": codex_home / "hooks" / "context_handoff.py",
        RUNTIME_ROOT / "codex" / "hooks" / "shared_handoff_lock.py": codex_home / "hooks" / "shared_handoff_lock.py",
    }
    if platform == "windows":
        codex_files.update(
            {
                RUNTIME_ROOT / "codex" / "bin" / "codex-auto": codex_home / "bin" / "codex-auto.py",
                RUNTIME_ROOT / "codex" / "bin" / "codex.cmd": codex_home / "bin" / "codex.cmd",
                RUNTIME_ROOT / "codex" / "bin" / "codex-auto.cmd": codex_home / "bin" / "codex-auto.cmd",
            }
        )
    else:
        codex_files.update(
            {
                RUNTIME_ROOT / "codex" / "bin" / "codex-auto": codex_home / "bin" / "codex-auto",
                RUNTIME_ROOT / "codex" / "bin" / "codex": codex_home / "bin" / "codex",
            }
        )
    for source, target in codex_files.items():
        install_file(source, target, dry_run)

    claude_files = {
        RUNTIME_ROOT / "claude" / "hooks" / "task_routing.py": claude_home / "hooks" / "task_routing.py",
        RUNTIME_ROOT / "claude" / "hooks" / "session_start.py": claude_home / "hooks" / "session_start.py",
        RUNTIME_ROOT / "claude" / "hooks" / "task_context.py": claude_home / "hooks" / "task_context.py",
        RUNTIME_ROOT / "claude" / "hooks" / "context_handoff.py": claude_home / "hooks" / "context_handoff.py",
        RUNTIME_ROOT / "claude" / "hooks" / "shared_handoff_lock.py": claude_home / "hooks" / "shared_handoff_lock.py",
    }
    for source, target in claude_files.items():
        install_file(source, target, dry_run)

    install_global_agents(codex_home / "AGENTS.md", RUNTIME_ROOT / "codex" / "AGENTS.md", dry_run)
    install_text(codex_home / "hooks.json", merge_codex_hooks(codex_home / "hooks.json", codex_home, platform), dry_run)
    install_text(claude_home / "settings.json", merge_claude_settings(claude_home / "settings.json", claude_home, platform), dry_run)

    if not args.skip_companion_skills:
        companion_root = RUNTIME_ROOT / "skills"
        for skill_dir in sorted(path for path in companion_root.iterdir() if path.is_dir()):
            install_tree(skill_dir, codex_home / "skills" / skill_dir.name, dry_run)

    if not args.skip_self:
        target = codex_home / "skills" / SKILL_NAME
        if normalized(PACKAGE_ROOT) == normalized(target):
            print(f"SKIP {target} (package is already installed there)")
        else:
            install_tree(PACKAGE_ROOT, target, dry_run)


def main() -> int:
    args = parse_args()
    try:
        install_runtime(args)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Shared Handoff Kit installation complete.")
    print(f"Target platform: {target_platform(args)}")
    print(f"Codex home: {normalized(args.codex_home)}")
    print(f"Claude home: {normalized(args.claude_home)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
