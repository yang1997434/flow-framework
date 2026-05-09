---
title: Frozen schema must normalize at write boundary, not at call sites
class: D-class (data shape / schema integrity)
tags: [telemetry, schema, frozen, normalization, fail-closed]
trigger_paths:
  - scripts/common/telemetry.py
  - templates/telemetry-schema.md
  - scripts/flow_orchestrator.py
related_pitfalls:
  - dispatch-shim-silent-kw-drop.md
discovered: 2026-05-09 (v0.8.5 codex R1 I2)
---

# Frozen schema must normalize at write boundary, not at call sites

## 症状

PRD 锁定 frozen schema（如 v0.8.5 telemetry `outcome ∈ {pass, fail, skip, null}`），
但实施处直接把上游对象的状态码透传写入 telemetry：

- GateRunner verdict.status = `"blocked"` → 直接写入 telemetry
- dispatch_with_retry outcome = `"rejected_with_rationale"` → 直接写入 telemetry
- schema doc（`templates/telemetry-schema.md`）跟着错误"扩枚举"以匹配实际写入

→ frozen schema 不变量破坏；后续 ad-hoc 聚合 / aggregator 按 frozen 集合解析失真。

## 根因

Frozen schema 是契约，不是实现细节。任何调用方往写入流里送的字段值，都必须**先映射**
到允许的取值集合。

错的做法：
- 期待每个调用方都"自觉"传 frozen 值（不可能，因为调用方知道自己的语义而不是 schema）
- 把 `blocked`、`rejected_with_rationale` 等具体语义写进 schema doc（schema 失去 frozen 意义）

对的做法：
- normalize 必须发生在**写入边界**（`emit_event` 内）
- 对调用方透明：调用方传什么字符串都行
- 原始字符串放副字段（如 `fail_reason_raw`），无信息丢失
- 副字段不在 frozen 集合内，可任意取值

## v0.8.5 实例

```python
# scripts/common/telemetry.py
VALID_OUTCOMES = ("pass", "fail", "skip", None)

_OUTCOME_NORMALISATION_TABLE = {
    "pass": "pass",
    "fail": "fail",
    "blocked": "fail",
    "rejected_with_rationale": "fail",
    "skip": "skip",
    None: None,
}

def normalize_outcome(raw):
    return _OUTCOME_NORMALISATION_TABLE.get(raw, "fail")  # 未知 → fail

def emit_event(..., outcome, fail_reason_raw=None, ...):
    normalized = normalize_outcome(outcome)
    # 即使调用方不小心也传 normalized；副字段保留 raw 用于诊断
    write_jsonl({"outcome": normalized, "fail_reason_raw": fail_reason_raw or str(outcome)})
```

调用方（GateRunner / dispatch_with_retry）只管传 verdict.status 原文，不需知道 frozen 集合：

```python
emit_fn(phase="codex_review", outcome=verdict.status,
        fail_reason_raw=verdict.feedback)
```

## 测试（fail-closed assertion）

```python
class FrozenSchemaInvariant(unittest.TestCase):
    def test_emit_event_normalises_blocked_to_fail(self):
        ...
        self.assertIn(written["outcome"], VALID_OUTCOMES)
        self.assertEqual(written["fail_reason_raw"], "blocked")  # 原文保留

    def test_unknown_outcome_falls_to_fail(self):
        ...
        self.assertEqual(written["outcome"], "fail")  # 未知 outcome 不漏过
```

## 预防

- 任何 frozen schema 字段，**不允许**在调用点（caller）做"我已经传 frozen 值了"的假定
- emit / write 函数内部加 `assert outcome in VALID_OUTCOMES` 或自动 normalize
- schema doc **永远**只列 frozen 集合；不"扩枚举"以匹配 bug
- 任何加扩 schema 的 PR 都要回看：是否真的 schema 演进，还是没做 normalization 的偷懒

## 参考

- v0.8.5 codex R1 I2 原始报告: `.flow/tasks/archive/2026-05/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/research/codex-review-r3-output.md`
- v0.8.5 fix commit: `8c4139c v0.8.5 codex-review I2: frozen schema outcome enumeration`
- 实现：`scripts/common/telemetry.py:88-180`
- 测试：`tests/unit/test_telemetry_outcome_normalization_v085.py`（12 tests）
