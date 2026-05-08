---
id: schema-parsing-get-vs-in
title: dict.get() 在 schema 校验里把 absent 和 falsy/null 混淆
discovered_at: 2026-05-06
discovered_in: feat/v0.8.1-safety-stack T1 (commits 18bc32f..27d5ca0)
trigger_paths:
  - "scripts/flow_contract.py"
  - "scripts/flow_*.py"
  - "**/contract*.py"
  - "**/schema*.py"
severity: high
recurrence_risk: high
---

# Schema parsing：`.get()` 把 absent 和 falsy/null 混淆

## 症状

跨 `/codex review` 6 轮在 T1 反复发现同一个 bug 类。每轮 codex 找出 1-3 条都是同一根因：

```python
# 看起来对，实际是个陷阱：
method = c.get("method") or _infer_method(c, idx)   # falsy "" 被当缺失
idem = c.get("idempotent")                          # null 被当缺失
budget = dict(raw.get("budget") or {})              # null 被当缺失
if c.get(field) is None:                            # 只拒 None，不拒 ""/0/false
    raise ContractError(...)
```

每个都让"显式给了无效/空/null"和"完全没给"走同一条路径，silently 应用 default。

## 根本原因

Python `dict.get(key)`：
- key 不存在 → 返回 None（或自定义 default）
- key 存在但值是 None → 也返回 None
- 配合 `or` 操作符 → 任何 falsy（"", 0, False, [], {}）都触发 fallback

**跟 fail-closed 设计冲突**：schema 设计说"显式 null = 格式错误，应该拒绝"，但 `.get()` 读出来跟"absent"长得一模一样，于是 default 静默兜底。

## 修复方案

引入两个 helper + 一组 validators（stdlib only，参考 codex consult session `019dfd48-634c-7720-922d-15313dcc96c7`）：

```python
def require_field(obj: dict, key: str, validate):
    if key not in obj:
        raise ContractError(f"{key} missing")
    return validate(key, obj[key])

def optional_field(obj: dict, key: str, validate, default):
    if key not in obj:
        return default
    return validate(key, obj[key])

def non_empty_str(key, value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{key} must be a non-empty string, got {value!r}")
    return value

# 同款：bool_value, positive_int, non_negative_int, non_empty_str_list,
#       dict_value, nullable_str（注释说明 null 是合法的）
```

**核心判定**：用 `key in obj` 做存在性检查，绝不要 `.get()` + 值检查。`.get(key, default)` 只在 default 必然合法且 null 也合法时才能用（罕见，比如 `notification.command`）。

## 触发条件 / Recurrence

任何地方加新 schema 字段都会重蹈，除非：
1. 用上述 helper 而不是 `.get()`
2. 新加字段必须为每个 validator + parser 路径补齐 5 case：absent / null / "" / wrong-type / valid-falsy（如果 0 合法）

T2-T22 实现者 / 将来类似 release：

- 写 schema parser 之前先看 `flow_contract.py` 顶部 contributor 注释
- code review 前自查：grep `\.get(` 的每个调用点，确认不是 v0.8.1+ 字段
- 跑 `/codex review` 前可以先 grep 一遍降低 review 反复

## 防御措施

1. **Lint**：可考虑写一个 ruff rule / 自定义 ast walker，在 `flow_contract.py` 等敏感文件里禁止 `dict.get()` 的 schema 字段访问（白名单兜底）。优先级低，先靠人工 + helper 文档。
2. **CI**：把 5-case 回归（absent/null/""/wrong-type/valid-falsy）写成 `tests/smoke/test_contract.py` 模板，新增字段必须复制粘贴。
3. **Helper 复用**：T2-T22 任何加新字段的 task **必须**走 `flow_contract.py` 已有的 helper，不能复制 `.get()` 写法。

## 历史代价

- 6 轮 codex review × 5 分钟 + ~20k token 每轮 = 显著 review 成本
- T1 7 commits（应 1-2）
- 学到的：**当某个 review 维度反复抓同一类 bug，停下来抽抽象，不要 whack-a-mole**

## 相关

- 路径 B 决策：codex consult mode，session `019dfd48-634c-7720-922d-15313dcc96c7`
- bug 历史 commit 链：`be52061..51dc4d3`
- helper 提取 commit：`27d5ca0`
- 模块文档化在 `scripts/flow_contract.py` 顶部 docstring
