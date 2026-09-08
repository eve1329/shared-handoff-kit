---
name: shared-handoff-kit
description: Autonomously install or verify a shareable Codex and Claude handoff bundle in the current workspace. Use when an AI receives this package and the user wants the same hook-driven task-local process.md workflow, repeated compaction support, /handoff continuity, or task-id isolation on another machine. Run the bundled activation rather than giving the user shell instructions.
---

# Shared Handoff Kit

这是一套可复制的运行时组合，不只是一个提示词技能。它把 Codex 和 Claude
共同使用的 task-local 状态、hook、handoff、自动 compact 计数，以及达到阈值后的
controlled clear 规则放在一个可分享目录里。

## AI 自动启用

把此目录交给 Codex 或 Claude 后，用户只需要表达“安装/启用这个包”或“让我也拥有
这套 handoff”。AI 必须自己运行 `scripts/activate.py`：它在一次流程中安装运行时、
校验已安装目标，并在当前工作仓库保留既有 task-id 或初始化 `main`。不要把安装命令
交给用户执行。

包根的 `AGENTS.md` 和 `CLAUDE.md` 也写入了同一条规则，因此 AI 即使尚未把这个技能
复制到全局 skills 目录，也能在打开该目录后执行自动启用。若用户只打开了压缩包目录而
没有选择项目，AI 仍安装并校验全局运行时，但不在该压缩包目录创建 task 状态。

仅在用户明确要求检查、dry-run 或不修改配置时走非写入路径。自动启用会备份已有文件、
合并 hook JSON、保留其它设置与密钥；JSON 不能解析时必须中止，不能覆盖。

自动启用会：

- 安装 Codex `SessionStart`、`UserPromptSubmit`、`PreCompact`、`PostCompact`、`Stop` hook、`codex-auto` wrapper 和全局 handoff 约定。
- 安装 Claude `SessionStart`、`UserPromptSubmit`、`Stop` hook，并复用同一个 `.agents/state/` 布局。
- 安装 `task-id-bootstrap`、`handoff`、`claude-handoff` 三个 companion skill。
- 将已有目标文件按时间戳备份，合并 hook 配置，保留其它设置和密钥；不会写入任何代理、模型或 token 配置。
- 在 Windows 安装 `codex-auto.py`、`codex.cmd`、`codex-auto.cmd`；hook 使用 `py -3 "<path>"`，锁实现使用 `msvcrt`。macOS/Linux 保留 shell wrapper、`python3` 和 `flock` 路径。

## 行为契约

- 状态只写入 `.agents/state/tasks/<task-id>/`。`process.md` 是语义真相，`process.auto.md` 是 hook 快照，`process.recent.md` 是短期 fallback，`context_guard.json` 记录自动 compact 次数。
- task-id 解析顺序是显式 payload、prompt 中的 `task=<id>`/`task-id=<id>`、当前 session 映射、有效的 `current-task`，最后才是 `main`。
- 每次有意义的工作后更新当前 task 的 `process.md`；在 `compact`、`clear`、`resume` 或 `/handoff` 前必须包含 Current Task、Done、Key Files、Verification、Current Constraints、Next Step。
- 自动 compact 达到 guard 的阈值（默认 3 次）后，下一次实质性工作前要求 controlled clear；恢复时先读同一个 task-local `process.md`。
- 并行 Claude session 使用 `claude:<session-id>` 映射，不能只依赖共享的 `current-task`。

详细路径和事件映射见 [references/contract.md](references/contract.md)。不要直接编辑
`runtime/` 中的副本；要更新版本时，从已经验证的本地运行时重新复制后再运行校验。

## task-id 与 handoff

自动启用使用当前工作仓库已有的有效 task-id；没有时初始化 `main`。它会尝试绑定当前
rollout；运行环境未暴露 transcript 时，只报告 task 文件已初始化，不能虚报为已绑定。

用户说“继续”“handoff”“新上下文”“compact”时，先解析同一个 task，再读取
`.agents/state/tasks/<task-id>/process.md`。不要把旧的 root-level
`.agents/state/process*.md` 当成语义状态。

## 运行边界

Windows 与 macOS/Linux 由同一入口自动识别。Windows 使用 `msvcrt` 文件锁、`.cmd`
启动包装器和 Python Launcher；macOS/Linux 使用 `flock` 路径。AI 执行后只应汇报通过或
失败、备份位置、目标工作仓库与 task 状态；不要把平台差异变成需要用户手动输入的步骤。
