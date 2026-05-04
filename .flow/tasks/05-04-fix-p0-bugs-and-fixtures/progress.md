# progress.md — fix-p0-bugs-and-fixtures

## Plan

(single, main session implements) — 3 个 P0 bug 修复 + 最小 unittest fixture 建立。来源：
audit task `05-04-audit-flow-issues` 的 research/A 报告。

子项目 #0 是 v0.4 重构前置硬要求（tests/smoke/ 此前为空）。

## Execute Log

| 时间 (YYYY-MM-DD HH:MM) | Agent | Scope | Outcome |
|------|-------|-------|---------|
| 2026-05-04 | main | P0-1 fix `claude/hooks/pre-tool-task.py:62` `or True` | 替换为显式默认到 implement 分支 |
| 2026-05-04 | main | P0-2 fix `scripts/flow_task.py:cmd_archive` | move 之前先 capture was_current；只在归档当前 task 时清 pointer |
| 2026-05-04 | main | P0-3 fix `scripts/flow_promote.py` frontmatter | 抽出纯函数 `rewrite_frontmatter_for_promotion`；用 strip() 而非 rstrip() 消除前后空行；改成 P1 后仍处理（cosmetic） |
| 2026-05-04 | main | 建 `tests/smoke/test_p0_fixes.py` + `run.sh` | 10 个 unittest 全部通过 |
| 2026-05-04 | main | CLI 健全性 (flow_task --help / list, flow_promote --help, pre-tool-task import) | 全部 OK，无 regression |

## Verify Report

- ✅ P0-1 fix 验证：`pick_jsonl` 在无关键词 prompt 下仍返回 implement.jsonl（4 cases pass）
- ✅ P0-2 fix 验证：archive task-b 后 pointer 保留指向 task-a；archive task-a 后 pointer 清空（2 cases pass）
- ✅ P0-3 fix 验证：单次重写无空行；连续 promote 两次空行不累积；状态 = 2（4 cases pass）
- ✅ CLI 健全性：3 个 entry 全部正常导入和响应 --help
- ✅ Credential grep self-check: 无凭据泄露（修改的 4 个文件均 source code，无 inline secrets）
- ⚠️ A 报告原 P0-3 实为 cosmetic（offset 计算无 off-by-N），prd.md 已就地降为 P1 但仍修
- ⚠️ A 报告其余 8 P1 / 10 P2 留给后续子项目（#1-#7 实施过程中顺带或单独 task）

## Sediment Notes

**Pattern**: 测试 jsonl-style 模板加载逻辑时，hyphen-named hook 文件需要用 `importlib.util.spec_from_file_location` 而非常规 import — 这个模式可能复用到其它 hook 测试上，归到 v0.4 wrap-up 时再考虑入 vault patterns/。

**Pitfall**: `shutil.move` + 后续状态检查的顺序陷阱（P0-2）—— 任何"移动后还要查询移动前状态"的代码都要先 capture 状态再移动。这个值得 promote 到 vault `pitfalls/` 一条。文件名建议：`shutil-move-then-query-stale.md`。

**ADR**: 无新 ADR（这次都是 bug 修复）。

## Retro

- ✅ Worked: 先验证 sub-agent A 的判断再修，发现 P0-3 其实是 P1，避免了过度紧张
- ✅ Worked: 把 `rewrite_frontmatter_for_promotion` 抽成纯函数，单测立刻可写
- ❌ Didn't: A 报告对 P0-3 的描述（"split_pos+5 与 match.start() 偏移混算"）有误导性，没仔细手算就会跟着写错位的 fixture
- 💡 框架反馈：tests/smoke/ 应该在 install 时自动建一个最小 placeholder（如本次的 test_p0_fixes.py 占位），让"零测试"不再是默认状态
