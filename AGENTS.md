# Shared Handoff Kit Bootstrap

This directory is an AI-operated installation artifact, not a manual procedure.

When the user asks to install, enable, use, or apply this package in their current worktree:

1. Execute `scripts/activate.py` yourself through the available terminal tool. Do not reply with PowerShell, shell, or Python commands for the user to run.
2. Pass the actual worktree with `--repo <workspace-root>` and let the script retain an existing valid task id or initialize `main`.
3. If this extracted package is the only open directory and no target project is available, execute it with `--skip-task`; do not create `.agents/state/` inside the package merely to demonstrate installation.
4. Respect an explicit `inspect`, `verify`, or `dry-run` request by using the corresponding non-mutating path instead of installing.
5. Report only the completed outcome, backups if any, verification result, and whether the current rollout was bound. Do not describe a task as bound unless the bootstrap output proves it.

The installer is explicitly authorized by an installation/enabling request. It merges hook settings, makes timestamped backups before replacement, and refuses malformed JSON rather than overwriting it.
