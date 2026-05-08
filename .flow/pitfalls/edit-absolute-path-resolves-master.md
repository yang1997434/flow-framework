---
name: edit-absolute-path-resolves-master
date: 2026-05-08
project: cross
severity: high
status: active
trigger_paths:
  - .claude/worktrees/**
  - scripts/flow_orchestrator.py
  - tests/**
last_verified: 2026-05-08
---

# edit-absolute-path-resolves-master

## Symptom（看到什么）

当 cwd 在 worktree 内（`.claude/worktrees/<branch>/`），但用 Edit / Write 工具时 file_path 给的是 master 路径前缀（`/data/Claude/flow-framework/scripts/...`），改动**全部落到 master**，而 worktree 的对应文件**完全未变**。

观测信号（v0.8.3 P0.1 实施时遇到）：

- `cd /data/.../worktrees/feat+x && grep "<edited symbol>" tests/smoke/foo.py` → 0 命中
- `git -C /data/Claude/flow-framework status` → master 上有 7 个 modified 文件（不该有）
- worktree branch 的 git status 是 clean
- Read 工具显示文件已是新内容（在缓存中），但 awk / cat / hexdump on disk 显示老内容

## Root cause（实际原因）

Edit 工具用绝对路径直接写盘 — Python `Path('/data/Claude/flow-framework/scripts/foo.py')` 解析到 master，与 cwd 无关。Worktree 是物理上分离的目录树，路径前缀不同：

- Master: `/data/Claude/flow-framework/scripts/foo.py`
- Worktree: `/data/Claude/flow-framework/.claude/worktrees/<branch>/scripts/foo.py`

bash hook 只看 cwd 不看绝对路径，所以即使 cwd 在 worktree，Edit `/data/master/path/...` 仍写 master。

工具间不一致放大问题：
- Read 有内容缓存，写后立刻 read 显示新内容（实际只是缓存）
- 真正的 disk 是 hexdump / awk / sed 看到的老内容
- 第二次 Edit 用相同 old_string 报"未匹配"（因为 disk 实际未变，但 Read 缓存仍认为已变）

## Fix（这次怎么解决的）

修复路径：
1. 在 master 上 `git diff > /tmp/patch.diff` 收集所有意外修改
2. `git checkout -- <files>` 回滚 master（保留 staging 上的 prefork commit `0bb233d`）
3. 切到 worktree dir，`git apply /tmp/patch.diff` 把改动迁移到 worktree
4. 后续所有 Edit 改用 worktree 绝对路径前缀：
   `/data/Claude/flow-framework/.claude/worktrees/feat+v0.8.3-p0.1-implementer-redispatch/scripts/...`

## Prevention（下次怎么避免）

**Hard rule**：进入 worktree 后，所有 Edit/Write 的 `file_path` 必须以 worktree 绝对路径前缀开始。具体：

1. **EnterWorktree 后立即记录前缀** — 把 `pwd` 输出抄一份在脑里，下次 Edit 前确认 file_path 以这个前缀开头。
2. **不要用 relative path / 短形** — `tests/smoke/foo.py` 在 cwd 是 worktree 时 awk/grep 看到的是 worktree 文件，但 Edit 工具行为不一致。一律用绝对路径。
3. **批量 refactor 前 sanity check** — 改第一个文件后，**用 awk/sed/cat 确认 disk 真有改动**（不要只信 Read 输出 — Read 有缓存）。如果 awk 看不到变更，立刻停下排查路径。
4. **每个 step 后 `git status` 在 worktree 检查** — uncommitted changes 应在 worktree 而非 master。

可执行 checklist（写代码前）：

```bash
# 1. 确认 cwd
pwd  # 应该是 .claude/worktrees/<branch>/...
# 2. 抄绝对路径前缀
WT="$(pwd)"
echo "Edit file_path 必须以 $WT/ 开头"
# 3. 改一个文件后立刻 disk 验证
awk 'NR==<line>' "<changed_file>"  # 应见新内容
# 4. master 应保持 clean
git -C /data/Claude/flow-framework status
```

## Why it matters

跨 worktree 的 file 编辑是 Flow v0.7+ 的核心隔离机制。如果 Edit 默默落到 master：
1. **Worktree branch 的 commit 不含这些改动** — 对 worktree 跑 test 用旧代码（不会发现 bug）
2. **Master 出现 uncommitted state** — 下次 master 有人 pull / 操作时会冲突
3. **codex review 看 worktree diff 报 "no changes"** — 给 GREEN 但实际没 review 任何东西
4. **整个 Flow 隔离假设失效** — pre-fork commit 红线变成形式

本次（v0.8.3 P0.1）发现得早（10 分钟内），但如果没 hexdump 验证就 commit，worktree commit 是空的、master 多了 1500 行 unstaged 状态、codex review 实际看的是 master 的 diff（机器学不到这是 worktree 错误）。

## Cross-project applicability

适用于任何 git worktree 工作流（Flow / Cognition Conductor / vanilla `git worktree add`）。**Promote to vault** if 同类问题在 catbus / FizzRead / 其他项目重现一次。
