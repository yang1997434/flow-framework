# v0.8.3.1 hotfix: AFK park rc=5 regression (test time-bomb)

> Created: 2026-05-08
> Slug: v0.8.3.1-hotfix-afk-park-rc5
> Type: backend (test fix)
> Complexity: simple (post-recon — was triaged moderate before root cause)

## Goal

修 `tests/smoke/test_phase2_retry_loop.py:849`
`test_phase2_dispatch_park_returns_rc5_no_merge` 的 time-bomb：v0.8.3
ship 后用户 P0.6 中跑全套时发现该 test FAIL（reproducible at master
`958b611` + `4c17c81` (P0.1) + `ae340dc` (v0.8.2.1)）。

## Recon 结论：mock-only test bug，NOT prod regression

直接 repro 后 trace 出 `_phase2_dispatch` 返回 rc=3 + notifier 触发
`block_type=phase2_afk_hard_cap`（不是 idle_park）。Root cause:

- 测试用 hardcoded `start = datetime(2026, 5, 8, 0, 0, 0, UTC)` +
  `hard_cap_seconds=99_999.0` (~27.7h)
- 测试通过 `_resolve_afk_monitor` patch 注入 wait_afk
- 但 `_phase2_dispatch` 的 `now_iso_fn=now_iso_utc` 使用 **real time**
- 当 real now 离 hardcoded start > 27.7h → AfkMonitor.evaluate 看到
  `active_seconds >= hard_cap_seconds` → 返回 `afk_hard_cap`（terminal
  rc=3），先于 wait-mode idle 检查
- 写 test 时（2026-05-08 当天前 27.7h 内）PASS；之后永远 FAIL

**Production wait-mode AFK 行为正确，无需改产**。其他 4 处 `99_999.0`
hard_cap site（lines 545, 585）配 `now_iso_fn=mock` 的 `_make_now_fn`，
不走 real time，安全。

## Acceptance Criteria

- [x] 修 test line 805-818：`start = datetime.now(timezone.utc)` 替代
      hardcode；`hard_cap_seconds = 99_999_999.0` (~3 years time-bomb-proof)
- [x] 加 inline comment 解释 root cause + 历史教训（防其他人再写出
      hardcoded date + 27.7h cap 的组合）
- [x] 全套 980+105=985 PASS（test count 不变，单纯把红改绿）
- [x] 0 production code 改动

## Definition of Done

- 全套 985 PASS at master ← achieved
- v0.8.3.1 tag at hotfix commit
- CHANGELOG `v0.8.3.1` entry
- pitfall sediment：「test 用 hardcoded date + real now() 比对是 time-bomb」
- 不 codex review（trivial test-only 改动，不进 mandatory opus gate 范围）

## Out of Scope

- 其他 hardcoded date pattern 的 audit（grep 已确认 `99_999.0` 5-digit
  仅此一处暴露 real time；其他 hardcoded date 在 mock-clock 范围内，
  安全；deferred to v0.8.4+ 健康清扫）
- _phase2_dispatch / AfkMonitor 任何 prod-side 改动

## Decision (ADR-lite)

**Context**: time-bomb test discovered post-ship。原 author 写测试当天
PASS，几小时后失效。v0.8.3 带这个 red ship 出去 — earlier "985 PASS"
report 不准（real time 那天还在 27.7h 窗口内 OR 报告 grep filter 漏了
FAIL row）。

**Decision**: 修 test 而不修产 — root cause 是测试自己的 hardcoded date
+ insufficient hard_cap，不是 production state machine bug。Fix 模式：
`datetime.now()` 派生 + `99_999_999.0` (~3y) ceiling。

**Consequences**:
- Short: single-line code change + inline comment
- Long: time-bomb pattern 显式 documented；将来 review/写新 AFK test 时
  可直接引用此 comment 避免重犯
- Reversibility: 高（pure test change，rollback 删 commit 即可）

**Revisit triggers**: 出现 99_999_999.0 也撑不住的长期运行场景（不会，3 年）
