---
title: Fake-deps test can fake production-path coverage
class: T-class (test infrastructure)
tags: [smoke, integration, mock-boundary, codex-blindspot]
trigger_paths:
  - tests/smoke/**
  - tests/unit/**
  - scripts/flow_orchestrator.py
related_pitfalls:
  - claude-review-blindspots.md
  - dispatch-shim-silent-kw-drop.md
discovered: 2026-05-09 (v0.8.5 codex R1 + R2)
---

# Fake-deps test can fake production-path coverage

## 症状

实施者写新 smoke / integration test 时，倾向用 fake deps（fake `RetryDeps`、
fake `RetrySessionState`、fake `_build_*` lambda）直调内部函数，自报"覆盖了生产
路径"。实际上**绕过**了生产 dispatch 的真实调用链：

- `auto_dispatch_task` / `_phase2_dispatch` 入口不走
- `dispatch_with_retry` / `_prod_impl` / `_prod_review` 不进
- `_dispatch_implementer_fresh_worktree`（真 git fork）不调
- `GateRunner.run_phase2`（真 5 gate 序列）不跑
- subagent dispatch shim（捕获 prompt 的真路径）不上

→ 真生产 bug 在测试中**全部假阳性通过**。

## v0.8.5 实例（4 个 bug 因此漏抓）

R1 codex review 在已知 1025 PASS 的 v0.8.5 diff 中抓到 6 issues，4 个 P1：

1. `codex_review` event 在生产路径几乎不写（`_prod_review` collapse 所有非 pass 为 `"fail"`，
   `if review_outcome == "rejected_with_rationale"` 在生产从不命中）
2. `outcome` 枚举漂移（写了 `blocked` / `rejected_with_rationale`，违 frozen schema）
3. diff map 只覆盖 committed diff（漏 staged/unstaged/untracked）
4. Round 1 `worktree_create` 没埋点

**这 4 个全部** 因为 smoke test 用 fake `RetrySessionState` + fake `dispatch_with_retry`
入口直调而被掩盖。R2 中又因 sub-agent 第一次 fix 仍部分用 fake-coverage 而 reopen I6。

## 根因

Fake deps 是 **unit test 工具**——测某个具体函数的输入/输出契约。
被误用到 **integration scope**——声称"覆盖了生产 dispatch"。

实施者倾向选 fake deps 因为：
- 便宜（不需要真 git fork、不需要真 subagent）
- 可控（不会因外部 IO flake）
- 容易写成 GREEN

但便宜 = 弱信号。

## 修复 / 正确做法

Integration test 必须以 **真生产入口** 驱动：

- 入口：`auto_dispatch_task` / `_phase2_dispatch` / `_cmd_auto_execute`
- 真路径不 mock：`dispatch_with_retry`, `_prod_impl`, `_prod_review`,
  `_dispatch_implementer_fresh_worktree`, `GateRunner.run_phase2`,
  prompt builder, `_build_prev_round_diff_summary`
- 仅 mock 最外层不可避免的 IO：
  - subagent dispatch shim（`flow_subagent_dispatch.invoke` /
    `_invoke_subagent_dispatch`）—— 捕获 prompt
  - 外部 CLI（`_run_shell_with_pgkill` 用于 codex CLI）
- worktree 真 fork（`TemporaryDirectory` 隔离 host）

## 自检（写测试时强制问）

> "如果生产代码 X 出 bug，我这个测试会 RED 吗？"

如果答案需要解释超过 1 句话，或者你需要去看 mock 边界确认——这个测试**很可能**
是假覆盖。

## 预防 / 兜底

- codex review 的 mock-boundary 检查（"mock 了 X / 没 mock Z"必须明示）
- TDD 顺序：RED 测试先确证你能让生产路径 fail；如果 mock 太重导致 test 不可能 RED，
  就是假覆盖
- v0.8.6 backlog: 加 lint rule 检测 smoke 目录里直调 internal helper 而非生产 entry

## 参考

- v0.8.5 codex R1 抓 6 issue: `.flow/tasks/archive/2026-05/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/research/codex-review-r3-output.md`
- v0.8.5 codex R2 重抓 I6 fake-coverage: `research/codex-review-r4-output.md`
- v0.8.5 codex R3 GREEN（I6 真 production-path）: `research/codex-review-r5-output.md`
- 测试范例：`tests/smoke/test_v085_production_path.py::TestRound2EnrichmentViaFullRetryLoop`
