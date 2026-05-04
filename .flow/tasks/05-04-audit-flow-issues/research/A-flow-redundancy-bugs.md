# Research A — flow 代码冗余 / bug / 死代码扫描

> Scope: scripts/*.py, claude/hooks/*.py, claude/skills/flow/*/SKILL.md, claude/commands/flow/*.md, templates/, install.sh, uninstall.sh
> Method: read-only static review，未运行代码

## Summary

5 条关键发现：

- **P0** `pre-tool-task.py` 内 `if impl_keywords.search(prompt) or True:` —— `or True` 让 if 永远为真，整个 elif 设计失效（实际副作用小，但是明显代码错误）
- **P0** `flow_task.py` `cmd_archive` 在归档后**总是**会清空 `.current-task` pointer（cur is None 永远成立，因 task_dir 已被 move），即"归档非当前 task 也会清空当前指针"
- **P1** `install.sh` / `settings.json.snippet` / 多处 SKILL.md 把仓库路径硬编码成 `~/projects/flow-framework`，但实际仓库在 `/data/Claude/flow-framework`；安装到非默认路径会出现 stale 引用
- **P1** `common/config.py::_parse_simple_yaml` 自写 YAML parser 完全不处理 list-of-strings（line 79-81 显式 pass），`flow.config.yaml` 内 `forbidden_in:` `pre_commit_grep_patterns:` 等 list 字段全读不出来
- **P2** 大量重复实现：`find_project_flow` / `find_project_root` 在 4 个 hook 文件 + `paths.py` 中各写一份；credential pattern 在 `post-tool-bash.py`、`flow_promote.py`、`config.yaml.template`、`finish.md`、`promote.md` 各写一份

## 详细发现

### P0 - 必修 bug 或破坏性问题

- **`claude/hooks/pre-tool-task.py:62`** — `if impl_keywords.search(prompt) or True:` 永真表达式。意图是"默认走 implement"，但 `or True` 把 elif 短路，check.jsonl 路径分支虽在 line 57 上面提前 return，所以未造成功能 bug；但代码本身是错误习语 + 注释 `# default` 与实际逻辑分离。**建议**：改成两段独立 if-return，或用 `target = task_dir / "implement.jsonl"` 兜底无条件检查。

- **`scripts/flow_task.py:132-137`** — `cmd_archive` 的 pointer 清理逻辑有 bug：
  ```
  shutil.move(str(task_dir), str(target))      # task_dir 已不存在
  cur = get_current_task_path()                 # 此时 get_current_task_path 必返回 None（dir 已 move）
  if cur is None or not cur.is_dir():           # 总是 True
      pointer.unlink()                          # 任何 archive 操作都清空 pointer
  ```
  归档"非当前 task"也会把当前 task 指针清掉。**建议**：归档前先 capture pointer 字符串，比对是否真等于被归档 task 再决定清。

- **`scripts/flow_promote.py:283-289`** — 写 frontmatter 的逻辑用 `content[3:split_pos]` + `content[split_pos + 5:]` 切片，但 `re.search(r"\n---\n", content[3:])` 返回的 match.start() 是相对 content[3:] 的偏移；实际需要在 split 后切片为 `f"---{new_frontmatter}---\n{rest}"`。当前写法在 frontmatter 末尾会重复 `---` 或丢字符。文件确实已 promote 但 frontmatter 落地后格式可能错位（未运行验证，仅静态分析）。**建议**：用 `re.match(r"^---\n(.*?)\n---\n(.*)", content, re.DOTALL)` 重写。

### P1 - 显著坏味 / 边界处理缺失

- **路径硬编码 `~/projects/flow-framework`** — 出现位置：
  - `claude/hooks/settings.json.snippet`（4 处 command 路径）
  - `claude/hooks/README.md`（多处）
  - `claude/skills/flow/flow-orchestrator/SKILL.md:57,81,88`
  - `claude/skills/flow/flow-phase4-sediment/SKILL.md:92`
  - `claude/commands/flow/start.md:44,47,53`、`finish.md:57`、`pitfall.md:31`
  
  实际仓库在 `/data/Claude/flow-framework`。`install.sh` 不会改这些字符串。**影响**：用户 clone 到非 `~/projects/flow-framework` 时所有 SKILL.md 内的文档示例 + hook command 都需手动改。**建议**：install.sh 渲染时用 `sed -i "s|~/projects/flow-framework|${REPO_ROOT}|g"` 写入用户副本，或 SKILL.md 改成相对引用 + flow CLI lookup。

- **`scripts/common/config.py:75-82`** — list-of-strings YAML 完全不支持，注释明确写 "This minimal parser doesn't fully support — caller should YAML-parse for arrays" 然后 `pass`。但 `flow.config.yaml.template` 的 `credentials.forbidden_in` / `pre_commit_grep_patterns` / `phases.codex_review.triggers` 全是 list-of-strings。当前调用方（其实只有 `load_config`，**实际无 caller**——见下条）都拿不到这些字段。**建议**：直接依赖 PyYAML（已是事实标准）；或承认 config.py 现在是死代码删除。

- **`scripts/common/config.py` 整个模块是死代码** — `grep -rn "load_config\|from common.config\|from .config" scripts claude` 没有任何 import 或使用。`get_machine_id_from_local` 也未被调用（`paths.py::get_machine_id` 重写了一份）。**建议**：删除 config.py 或在 capability registry 重构（v0.4 #2）时合并到 paths.py。

- **`scripts/flow_save.py:75`** — `if not args.no_commit and not dirty:` —— 含义反了。注释说"如果用户工作树脏不要 auto-commit 因为会混合"，但实际是"工作树干净才 commit journal"。问题：journal 写入后 `is_dirty()` 调用是在写入**前**捕获的状态（line 50），写入 journal 本身让工作树变脏，所以 commit 时会成功（add+commit 仅 stage journal）。逻辑能跑但变量命名误导。更重要：当 journal 不在 git index 内（例如 `.flow/workspace/<user>/` 被 gitignore），`git add` silently 忽略，commit 报 "nothing to commit" → except 静默吞错 → 用户以为提交了。**建议**：检查 add 后 `git diff --cached --quiet` 再决定 commit；或直接放弃 auto-commit 这条路（journal 本就在 gitignore 的 workspace 下）。

- **`scripts/flow_task.py:81`** — `glob(f"*-{args.slug}")` 在 slug 末尾有歧义（如 slug=`fix` 会 match `0504-some-task-fix`）；多 archived 月份目录下 archive task 不会被检索（archive 在子目录里）。**建议**：精确匹配 `<MM-DD>-<slug>` 或 `<slug>` 全等。

- **`claude/hooks/session-start.py:63`** — pitfalls 列表 cap at 10 但是按字典序排序而不是按相关性（trigger_paths 匹配），完全失去 "auto-load by trigger_paths" 设计意图（流程文档里反复强调）。**当前实现 ≈ 把前 10 个文件名贴出来给 model 自决**。**建议**：要么真做 trigger_paths 匹配（读 frontmatter + glob 当前目录），要么干脆只贴文件清单不暗示"relevant"。

- **`claude/hooks/stop.py` 与 `flow_save.py` 双层 best-effort 静默** — stop.py 在 line 51 起套 try/except 吞所有异常；flow_save.py 在 line 89 又对 git 静默。**任何 save 失败都不会让用户看到**。配合"事件驱动 Lv3 LLM 蒸馏"（v0.4 #7）的 fail-soft error.log 思路，这层应该有一个 hook-level error log 落盘。

- **`claude/hooks/post-tool-bash.py:43`** — `"git commit" not in command` 字面匹配，被 `git commit-tree`、`echo "git commit"` 等误触发或漏触发；也匹配 `git commit --no-edit`（amend）但跳不出 `git commit -F file`（注释或 squash）。低概率但运行成本是 grep 整个 .flow/。**建议**：用 shlex 拆分 + 检查首两个 token。

- **`scripts/flow_staleness.py:104`** — `Path(cited).expanduser() if Path(cited).is_absolute() else None` 逻辑误：is_absolute=True 不需要 expanduser；要 expanduser 的恰好是 `~/...`。同一行还把 None 放进 candidates 在 line 105 用 truthy 过滤，但 `len(candidates) - candidates.count(None)` 计错（line 112）：candidates 总数固定 3，count(None) ≥1（绝对路径分支永远 None），detail 里报错的 "checked N candidates" 数总比实际少 1。**建议**：拆分清晰，去掉 None 占位。

### P2 - 风格 / 可读性 / minor cleanup

- **`find_project_flow` / `find_project_root` 重复实现 4 处**：`session-start.py`、`stop.py`、`user-prompt-submit.py`、`pre-tool-task.py`、`post-tool-bash.py` 各写一份；`paths.py::get_project_root` 已有官方实现但 hook 不 import（hook 不能依赖 scripts/common）。**建议**：把这个函数复制到 `claude/hooks/_common.py`，或所有 hook 改 `sys.path.insert(scripts/)`。

- **Credential regex 散落 5 处**：`post-tool-bash.py:19`、`flow_promote.py:83`、`flow.config.yaml.template:63`、SKILL.md / commands/*.md 多处文档。**建议**：统一到 `scripts/common/credential.py` 或 config 字段 + 文档 link。

- **`flow.py:62-63` stub 错误信息无意义** — `routing` 列表写死，缺 stub 检测早就失效（v0.3.1 全部 stub 已实现）。**建议**：删 `if not target.is_file(): print "stub"`，直接 subprocess.call。

- **`flow_init.py:21` `SUBDIRS` 列表与 `templates/gitignore.snippet` 与 `flow_init.py:32` `GITIGNORE_BLOCK` 三处 .flow/ 子目录定义重复**。subdirs 写 `archive` 但 `flow_init.py:21` 用 `tasks/archive`；`gitignore.snippet` 还少了 `.flow/.runtime/` 注释。**建议**：单一 source of truth。

- **`flow_init.py:60` 循环里每个目录都 print** — 安静选项缺失；同样 `cmd_create` 全程 print 无 --quiet。

- **`flow_promote.py:90-122` `count_mentions_in_archived_tasks`** —— 同时维护 `task_dirs` 和 `task_dirs2`，前者从未使用（line 105）。dead code。

- **`flow_conflict.py::extract_directives`** — 用单 regex 跨整个文件 finditer，长 markdown 表格 / code block 内的 "must / should" 也会被抓（误报来源）。**建议**：先剥 fenced code block 再扫。

- **`flow_staleness.py::scan_memory_file`** — `set(PATH_PATTERN.findall(text))` 用 set 去重但不保留顺序；如果同一 path 在文件多处被引用（不同上下文），findings 只输出 1 次但用户失去定位上下文。**建议**：保留 line number。

- **`templates/progress.md.template:11-14`** — 表格示例放注释里，`user-prompt-submit.py::is_section_filled` 已 strip HTML comments，所以这部分不会被当成"填充"，OK。但表格示例的 `首行为表头，自动生效` 仅用户能看见，Claude 看不见 → Phase 2 sub-agent 不知道格式约定。**建议**：把表头放在非注释里作为骨架。

- **`uninstall.sh` 不删 `~/.local/bin/flow` 命令的 wrapper**（line 24 删了，没问题），但**不删 `~/.flow/credentials.local`** 是设计意图（line 28 注释明确）。OK。

- **整个 `tests/smoke/` 是空目录** — 没有任何测试文件。详见下文测试覆盖空缺。

## 重构后会被删除/简化的代码

### (a) 引入 context-mode 后，stop.py / session-start.py 让出哪些段

按 PRD #5 决策："flow 的 SessionStart hook 只剩：读 .flow/.current-task + progress.md → 输出 brief 给 Claude"

**stop.py**:
- 整个文件几乎可以删除。当前唯一职责是 fire-and-forget 调用 flow_save.py 写 journal entry。如果 context-mode 接管原始捕获（PreCompact + Stop hook → SQLite），flow 自己的 stop.py 应该只剩"事件驱动 Lv3 触发"逻辑（PRD #7）。当前 line 51-58 的 subprocess.run 调用整个让出。
- 保留：可能保留一个 "Lv3 蒸馏" 触发分支（30 分钟冷却 + 50 工具调用阈值）。

**session-start.py**:
- `find_relevant_pitfalls` (line 52-63) — 现在仅按字典序前 10 个，重构为 trigger_paths 匹配后会重写，不"让出"但需重写
- `quick_read_guide()` (line 66-79) — 与 context-mode 输出重复时去重；context-mode 的 active task brief 包括路径 + 标题，flow 这部分简化
- `load_active_task` (line 31-49) — 让出给 context-mode 的 SQLite 记录（context-mode 更全面）

**user-prompt-submit.py**:
- `determine_phase` (line 69-93) + `extract_section` (line 47-51) + `is_section_filled` (line 54-66) —— 这部分是 v0.3.1 dogfood 修复的痛点，CHANGELOG 里专门标注。重构后 phase 状态由 progress.md frontmatter 记录（建议加 `current_phase:` 字段）替代 section 内容启发，整段 60+ 行都简化。

### (b) Capability registry 后哪些 hard-coded skill 引用被替换

完整清单（出现位置 + 当前硬编码 skill → 抽象 capability 名）：

| 文件:行 | 当前引用 | 抽象 capability |
|---|---|---|
| `flow-phase1-plan/SKILL.md:17` | `superpowers:brainstorming` | `brainstorm` |
| `flow-phase1-plan/SKILL.md:23` | `impeccable:shape` | `ui_shape` |
| `flow-phase1-plan/SKILL.md:86` | `gstack:codex` (consult) | `cross_model_consult` |
| `flow-phase2-execute/SKILL.md:72` | `impeccable:frontend-design` | `ui_design` |
| `flow-phase2-execute/SKILL.md:76` | `superpowers:test-driven-development` | `tdd` |
| `flow-phase2-execute/SKILL.md:111` | `gstack:codex` (challenge) | `cross_model_challenge` |
| `flow-phase3-finish/SKILL.md:53` | `gstack:codex` (review) | `cross_model_review` |
| `flow-phase3-finish/SKILL.md:55` | `impeccable:audit` + `polish` + `gstack:design-review` | `ui_audit` + `ui_polish` + `ui_visual_review` |
| `flow-phase4-sediment/SKILL.md:88` | `yangpeng-claude-skills:save` | `session_save` |
| `flow-orchestrator/SKILL.md:40` | `gstack:ship + canary` | `deploy_ship` + `deploy_canary` |
| `commands/flow/start.md:62-66` | superpowers:brainstorming + impeccable:shape + gstack:codex | （同上）|
| `commands/flow/continue.md:36,49-52,57,77` | 6 处 superpowers / impeccable / gstack / yangpeng | （同上）|
| `commands/flow/finish.md:21,22,46` | gstack:codex / impeccable:audit+polish / gstack:design-review / yangpeng-claude-skills:save | （同上）|
| `commands/flow/pause.md:37` | `yangpeng-claude-skills:save` | `session_save` |
| `commands/flow/codex-review.md:35` | `gstack:codex` | `cross_model_review` |

去重后约 **10 个 capability 名**，对应 PRD #2 估的 "10-15 个" 是合理的下限。注意 `gstack:codex` 三种模式（consult/challenge/review）应区分为不同 capability 还是单 capability + 参数 —— 倾向后者，看实施时调用 site 是否一致。

### (c) Worktree-per-task 后 flow_task.py 哪些逻辑要扩展

按 PRD #4：默认每 task 独立 worktree。

**`cmd_create`** (line 36-77):
- line 50 `task_dir.mkdir` 之外，新增 `git worktree add ../<repo>-flow-<slug> -b flow/<slug>` 调用
- task_isolation 配置读 → 三种模式 worktree / branch / shared
- 当前 line 73 `.current-task` 写的是 .flow 相对路径；worktree 模式下 .flow 在主仓库，但代码 worktree 在外面。需要保留 path-resolution 让 task_dir 仍指向 .flow/tasks/.../，但工作目录在 worktree。

**`cmd_start`** (line 79-91):
- 切换 task = 切换 worktree。需要新增 cd hint 输出，或自动 print path 让 shell wrapper eval cd

**`cmd_finish`** (line 103-110) + **`cmd_archive`** (line 113-139):
- worktree 模式下 finish/archive 应 `git worktree remove`，但只有合并完才能 remove。
- archive 之前需检查 worktree 是否 clean + branch 是否 merged。

**新增 `cmd_status`**（PRD #4 提到的"树形列出 task 状态"）和 **`cmd_switch`** —— flow_task.py 文件预计从 194 行涨到 350+ 行，可能需要拆 `flow_task_lifecycle.py` 和 `flow_task_worktree.py`。

**`progress.md.template`** —— 需加 frontmatter `blocked_by:` 和 `phase:` 字段（PRD #4 + #7 都需要）。

## 测试覆盖空缺

`tests/smoke/` 是空目录，全仓库**零测试文件**。

下面是真正应该被测的 6 条关键路径：

| 优先级 | 路径 | 风险 |
|---|---|---|
| **P0** | `flow_task.py::cmd_archive` 的 pointer 清理 | 已识别 bug（见 P0 第 2 条），无回归测试就会再犯 |
| **P0** | `flow_init.py` 在已有 .gitignore 上 idempotent append | 多次运行会重复或破坏 gitignore |
| **P0** | `flow_promote.py::frontmatter` 重写 | 已识别可疑切片（P0 第 3 条） |
| **P1** | `flow_staleness.py::scan_memory_file` 的 path 候选解析 | candidate truthy + None 放法，跨 repo / vault / 绝对路径 |
| **P1** | `flow_conflict.py::find_conflicts` regex + overlap 阈值 | v0.3.1 刚 fix 过 regex，没回归测试就是炸弹 |
| **P1** | `user-prompt-submit.py::determine_phase` 在 5 个 progressive states 的判定 | v0.3.1 dogfood 抓到的最痛点（CHANGELOG 第一条），保护起来 |
| **P2** | `pre-tool-task.py` 的 jsonl 加载 + truncation | MAX_INJECTED_BYTES 与 MAX_PER_FILE_BYTES 边界 |
| **P2** | `flow_triage.py::classify` 的中英文模式 + 默认回退 | 简单纯函数，最容易 1 文件 covers all |

测试形式建议：
- Python 文件 → pytest，固定 fixture 用 tmp_path 模拟 .flow 结构
- Hook → 给 hook 喂 stdin JSON + 断言 stdout 行为；可以纯 bash 不引 pytest
- Smoke 测试链：`flow init → task create → task list → task archive → task list --archive` 端到端跑一次

`v0.4` 重构动作大，**没有这些测试就是撞机**。建议把 `cmd_archive` + `determine_phase` + `scan_memory_file` 三个最优先 fixture 化。
