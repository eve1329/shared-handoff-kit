# Shared Handoff Kit

一套可复制的 **Codex + Claude 交接（handoff）运行时**，通过 hook 自动维护
task-local 的任务状态，让 AI 在「compact / clear / resume / 新线程 / 换机器」之后
依然能恢复同一个任务上下文，无需依赖聊天历史。

> 这不是一个普通提示词技能，而是一个可分享的运行时组合：它包含
> hook 脚本、启动包装器、companion skill 和一份跨平台的行为契约。

---

## 它解决什么问题

| 场景 | 没有本插件 | 有本插件 |
| --- | --- | --- |
| 上下文太长需要 compact | 上下文丢失，任务要重讲 | 自动在 compact 前后写快照，次数记入 guard |
| 新开线程继续上次 | 靠聊天记录口述 | 新线程读取同一份 `process.md` 恢复 |
| 同时开多个 Claude 会话 | `current-task` 互相覆盖 | 每个 rollout 用 `session-tasks.json` 独立绑定 |
| 换一台电脑 | 一切重来 | 复制此包 + 运行激活脚本即恢复同一套工作流 |

状态只写入目标工作仓库的 `.agents/state/`，与项目代码隔离。

---

## 安装要求

- Python 3（`python3`；Windows 上可用 `py -3` 或 `python`）
- 已安装 Codex CLI 和/或 Claude Code CLI
- 目标工作仓库（要被这套工作流管理的项目目录）

本包不包含任何凭据、模型 provider、代理或 token 配置，也不要求你提供它们。

---

## 安装 / 启用（二选一）

### 方式 A：交给 AI 自动启用（推荐）

把本目录（或本仓库克隆）交给 Codex 或 Claude，然后说：

> 安装/启用这个包 / 让我也拥有这套 handoff 工作流

AI 会自己执行 `scripts/activate.py`，一次完成：**安装运行时 → 校验已安装目标 →
在工作仓库初始化 task 状态**（保留已有 task-id，没有则初始化 `main`）。如果只打开了
本包目录而没有目标项目，AI 会以 `--skip-task` 执行，只安装全局运行时、不在包内创建
任务状态。

### 方式 B：手动执行

```bash
# 安装到默认位置（~/.codex 和 ~/.claude）并在当前目录初始化 task 状态
python3 scripts/activate.py --repo /absolute/path/to/your/project

# 只安装运行时，不在仓库创建任务状态
python3 scripts/activate.py --skip-task

# 只看会做什么，不修改任何配置
python3 scripts/activate.py --dry-run
```

`activate.py` 内部调用 `scripts/install.py`（安装）+ `scripts/verify.py`（校验），
等价于：

```bash
python3 scripts/install.py \
  --codex-home "$HOME/.codex" \
  --claude-home "$HOME/.claude"

python3 scripts/verify.py \
  --codex-home "$HOME/.codex" \
  --claude-home "$HOME/.claude"
```

---

## 安装了什么

激活脚本只写两个地方：`CODEX_HOME`（默认 `~/.codex`）和
`CLAUDE_HOME`（默认 `~/.claude`），写入前都会做带时间戳的备份
（`.bak.shared-handoff-<时间戳>`）。

**Codex**（`~/.codex/`）
- `hooks/context_handoff.py`、`hooks/shared_handoff_lock.py`
- `hooks.json` 合并注册 5 个事件：
  `SessionStart` / `UserPromptSubmit` / `PreCompact` / `PostCompact` / `Stop`
- `bin/codex-auto` 包装器：把受 guard 保护的 `resume` 转成带恢复提示词的全新会话，
  支持 `--auto-task <id>` 和 `CODEX_TASK_ID=<id>`

**Claude**（`~/.claude/`）
- `hooks/task_routing.py`、`session_start.py`、`task_context.py`、
  `context_handoff.py`、`shared_handoff_lock.py`
- `settings.json` 合并注册 3 个事件：`SessionStart` / `UserPromptSubmit` / `Stop`

**Companion skills**（安装到 `~/.codex/skills/`）
- `task-id-bootstrap`：`新开task-id=...`、`继续，task-id=...` 等请求
- `handoff` / `claude-handoff`：交接与恢复的标准流程

**全局约定**：`~/.codex/AGENTS.md` 会追加一份全局 contract（带
`<!-- shared-handoff-kit:global-contract -->` 标记，重复安装不会叠加）。

> 安装器只做**合并**：已有 JSON 设置会被解析后合并，保留其它设置与密钥；
> JSON 无法解析时报错中止，绝不覆盖。不会写入任何代理、模型或 token 配置。

---

## 日常使用

### 1. 开一个 task

```text
新开task-id=init-kmp，然后开个 init-kmp 分支
```

AI 会调用 `bootstrap_task_id.py` 创建
`.agents/state/tasks/init-kmp/`，写 `current-task` 指针，并把当前
rollout 绑定到 `session-tasks.json`。只有文件创建 + 指针写入 + 会话绑定三者都成功，
才算真正切换了任务。

### 2. 状态文件布局

| 文件 | 作用 |
| --- | --- |
| `.agents/state/tasks/<task-id>/process.md` | 语义真相，唯一可信的任务状态 |
| `.agents/state/tasks/<task-id>/process.auto.md` | hook 自动快照 |
| `.agents/state/tasks/<task-id>/process.recent.md` | 短期 fallback |
| `.agents/state/tasks/<task-id>/context_guard.json` | 自动 compact 计数 |
| `.agents/state/current-task` | 最后活跃任务指针 |
| `.agents/state/session-tasks.json` | 每个 rollout 的会话绑定 |

`process.md` 固定包含六段：**Current Task / Done / Key Files / Verification /
Current Constraints / Next Step**。每次有意义的工作后都会更新当前任务的
`process.md`；compact、clear、resume 或 `/handoff` 前必须写全这六段。

### 3. 继续 / 交接 / 新线程

```text
继续 / 继续上次 / 继续，task-id=xxx
handoff / 交接 / 新开线程继续 / resume
```

AI 先解析出同一个 task-id，再读取该任务的 `process.md` 恢复上下文，然后直接
做下一步，而不是重新复述历史。交接输出是「状态 + 证据 + 下一步决策」，
不是故事。

### 4. compact 与 controlled clear

- 每次自动 compact 都会写快照并把计数记入 `context_guard.json`。
- 自动 compact 达到阈值（默认 **3 次**）后，下一次实质性工作前会要求一次
  **controlled clear**。
- 恢复时使用 `codex-auto` 把受保护的 resume 转成带恢复提示词的全新会话，
  并重置 guard。

### 5. 并行会话

多个 Claude 会话同时工作时，每个 rollout 用 `claude:<session-id>` 独立绑定到
`session-tasks.json`，不会互相覆盖 `current-task`。

---

## 平台差异（自动处理，无需手动配置）

- **macOS / Linux**：hook 用 `python3 <绝对路径>`，文件锁用 `flock`，包装器是
  shell 脚本。
- **Windows**：hook 用 `py -3 "<绝对路径>"`（无 Python Launcher 时回退
  `python`），文件锁用 `msvcrt`，安装 `codex.cmd` / `codex-auto.cmd`。
- 任务状态布局与解析顺序在三个平台上完全一致。

---

## 校验

```bash
# 只校验包本身（必需文件、Python 语法、hook 注册、无 token/机器路径泄露）
python3 scripts/verify.py

# 同时校验已安装的 Codex / Claude 运行时（逐字节比对 + hook 注册检查）
python3 scripts/verify.py --codex-home "$HOME/.codex" --claude-home "$HOME/.claude"
```

---

## 回滚

所有被替换的文件都会先备份为 `原文件名.bak.shared-handoff-<时间戳>`。
想回滚就删除新文件并把备份改回原名；companion skill 目录的旧版本会整体复制为
带时间戳的备份目录。

---

## 设计边界与安全

- 只写 `CODEX_HOME` / `CLAUDE_HOME` 和仓库的 `.agents/state/`，不碰项目代码。
- 不包含、不写入任何凭据、API key、模型 provider 或代理配置。
- 包内文档与脚本会经 `verify.py` 检查：不得出现 API token 格式的字符串、
  机器专属路径，运行时锁实现必须是平台中立的。
- task-id 必须匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$`。
- 根目录的旧版 `process*.md`、`context_guard.json` 是遗留数据，插件不读不写。

---

## 相关文件

- `references/contract.md` — 完整行为契约（文件、事件、安全边界）
- `SKILL.md` — 给 AI 的启用说明
- `runtime/codex/`、`runtime/claude/` — 各运行时 hook 与包装器
- `runtime/skills/` — 三个 companion skill
- `scripts/` — `activate.py`（一键）、`install.py`（安装）、`verify.py`（校验）
