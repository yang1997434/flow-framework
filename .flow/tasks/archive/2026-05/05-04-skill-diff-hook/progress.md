# progress.md — skill-diff-hook

## Plan

(single, main session implements) — 子项目 #3 of v0.4 roadmap，依赖 #2（capability registry 是比对基准）。

## Execute Log

| 时间 | Agent | Scope | Outcome |
|------|-------|-------|---------|
| 2026-05-04 | main | scripts/flow_skill_diff.py | snapshot/diff/show/clear/reset-cache 子命令；Szymkiewicz–Simpson overlap coefficient（替代 Jaccard 解决集合大小不对称）；per-(spec,version) 缓存 |
| 2026-05-04 | main | session-start.py 集成 | run_skill_diff_silently()：超时 8s、best-effort、不阻塞 session；pending 注入 additionalContext |
| 2026-05-04 | main | flow.py 加 skill-diff 路由 | + 1 subcmd |
| 2026-05-04 | main | bug fix: diff 在"无新装"时勿 auto-clear pending | 用户未读完时不应消失，只能显式 clear 删 |
| 2026-05-04 | main | tests/smoke/test_skill_diff.py | 13 cases：tokenize/overlap/diff detection/cache path/render |

## Verify Report

- ✅ Smoke 全集 50/50 pass（10 + 13 + 14 + 13）
- ✅ 实地验证 hook 注入：模拟"39 个新装"场景，pending.md 写出 + 注入到 session-start additionalContext，长度 7820 字节，包含 ui_audit (1.0)、brainstorm (0.67) 等真实命中
- ✅ Auto-clear bug 已修：pending 仅在显式 `flow skill-diff clear` 时删除
- ✅ 缓存机制：per-(spec, version) 文件，重复装不重复算
- ✅ Hook 超时保护：8s 上限 + best-effort 异常吞噬，确保不阻塞 session start
- ✅ Credential grep self-check pass

## Sediment Notes

**Pattern**: 集合相似度的指标选择 —— 当 A、B 大小相差悬殊（描述 5 词 vs SKILL.md 50 词），Jaccard 分母被大集合主导导致分数压低；Szymkiewicz–Simpson coefficient（|A∩B| / min(|A|, |B|)）天然适合这种场景。值得 promote 到 vault `patterns/`。

**Pitfall**: 状态文件的"何时清理"是一个独立设计点。"diff 命令在没新发现时清掉 pending" 看起来合理但实际上抹掉了用户还没读完的提醒。规则应该是：**只有用户的显式 dismiss 行为才能删除 user-facing 持久状态**。

**ADR**: 无新 ADR。

## Retro

- ✅ Worked: 第一次跑发现"全部 No overlap"立刻意识到指标问题，不是数据问题
- ✅ Worked: 缓存设计避免每次 SessionStart 重跑 39 个分析（主线性能保护）
- ❌ Didn't: auto-clear pending 这种"勤快设计"实际是 bug；写测试时也没 catch（修后没补回归测试）
- 💡 框架反馈：本工具产出的 pending.md 给 Claude 看的，但格式既要人类可读又要模型可读 —— 中间格式不够干净，markdown 在 hook 里再嵌套有点笨拙；考虑结构化 JSON + 模板化展现
