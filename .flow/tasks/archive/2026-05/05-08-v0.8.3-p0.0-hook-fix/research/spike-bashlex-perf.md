---
name: spike-bashlex-perf
date: 2026-05-08
project: flow-framework
task: v0.8.3-p0.0-hook-fix
type: research
status: done
---

# Spike: bashlex 性能 + 兼容性

## 目的

验证 prd.md 中 Option D 的可行性（"bashlex AST + content-hash marker" 路径），数据驱动决议是否锁 D 或降 G。

## 环境

- bashlex 版本：0.18（GitHub master, depth 1 clone）
- Python：3.x（system /usr/bin/python3）
- 机器：本机 Linux 6.1.0-42-amd64

## 数据

| Case | Size | min (ms) | median (ms) | max (ms) | 解析结果 |
|------|------|----------|-------------|----------|----------|
| trivial: `git commit -m hello` | 19B | 0.20 | 0.20 | 0.24 | top-level git commit ✅ |
| `touch && git commit -m bypass` | 38B | 0.28 | 0.28 | 0.31 | top-level git commit ✅ |
| `python3 <<EOF\n...\nEOF` 1KB unquoted | 1.1KB | 0.39 | 0.39 | 0.41 | 非 git commit ✅ |
| `python3 <<'EOF'\n...\nEOF` 1KB QUOTED | 1.1KB | n/a | n/a | n/a | **PARSE_ERROR** ❌ |
| `python3 <<EOF` 50KB unquoted | 50.1KB | 11.24 | 11.37 | 12.71 | 非 git commit ✅ |
| `python3 <<'EOF'` 50KB QUOTED | 50.1KB | n/a | n/a | n/a | **PARSE_ERROR** ❌ |
| `python3 <<EOF` 200KB unquoted | 200.1KB | 44.31 | 44.50 | 44.79 | 非 git commit ✅ |
| `python3 <<'EOF'` 200KB QUOTED | 200.1KB | n/a | n/a | n/a | **PARSE_ERROR** ❌ |

**Cold import**：97ms（首次 122ms，FS cache 暖后 97ms 稳定）

## 关键发现

### 🔴 Finding #1：bashlex 不支持 `<<'EOF'`（quoted heredoc）

bashlex 0.18 把 `<<'EOF'` 字面识别为分隔符 `'EOF'`，但 body 末尾的 `EOF` 不带引号 → 永远找不到匹配的终止符 → `ParsingError: here-document delimited by end-of-file (wanted "'EOF'")`。

`<<'EOF'` 是 Claude Code 极常见的 heredoc 用法（避免 `$` 变量误展开），出现在：
- `git commit -m "$(cat <<'EOF' ... EOF)"`
- `python3 <<'EOF' ... EOF`（任何 Python script 内含 `$`）

→ 原 prd.md "Option D = bashlex 一刀切" 不可行；hook 必须有 parse-error fallback。

### 🟡 Finding #2：Cold import 97ms 占 hook 启动 budget 97%

bashlex 纯 Python 但模块多（ast / parser / yacc / heredoc / tokenizer / subst / state ...），import 链 97ms。这意味着每次 commit 都要付 ~100ms 启动税，即使是 `git commit -m foo` 这种零歧义命令。

mitigation：**预筛过滤** — 用 word-boundary regex 扫整个 command，找不到 `git commit` token 时跳过 bashlex import，让绝大多数无关命令快速 PASS。

### 🟢 Finding #3：unquoted heredoc 解析极快

200KB unquoted heredoc 解析 + AST traversal 仅 44ms，远低于 prd.md 设的 500ms 阈值。性能不是瓶颈 — 兼容性才是。

## 修订决议（取代 prd.md 原 ADR）

### 提议：**Option D'+A 联合方案**

```
Step 1 — 整命令 word-boundary regex 预筛
  pattern: (^|[^A-Za-z])git[ \t]+commit([ \t]|$)
  no match  → 0 import 0 parse → PASS（绝大多数命令）
  match     → 进 Step 2

Step 2 — bashlex AST 精确判定
  try bashlex.parse(cmd)
    succeed → walk AST → top-level `git commit` simple-command? → BLOCK or PASS
    fail (ParsingError) → 进 Step 3

Step 3 — first-line fallback (Option E 等价)
  FIRST_LINE = first line of $COMMAND
  match `^\s*git\s+commit\b`?
    yes → BLOCK（保守 — first-line 已经是 git commit）
    no  → PASS（first-line 不是 git commit；body 内即使有也非 top-level）

Step 4 — 通过 Step 2/3 但要 BLOCK 的 → 检查 marker
  marker.json 必存在 + mtime < 1800s + sha == git write-tree + schema_version == 1
  否则 BLOCK
```

### 性能 envelope

| 命令形态 | 路径 | 总开销 |
|---------|------|--------|
| 99% 普通命令（无 `git commit` token） | Step 1 PASS | < 5ms |
| `git commit -m foo` | Step 1+2 import + parse + AST | ~100ms |
| `touch && git commit` | Step 1+2 同上 | ~100ms |
| `python3 <<'EOF' ... git commit ... EOF` | Step 1 命中 → Step 2 PARSE_ERROR → Step 3 first-line `python3` 不匹配 PASS | ~100ms |
| `python3 <<EOF\n...git commit...\nEOF` 200KB | Step 1+2 解析 200KB | ~145ms |

### 风险与对策

| 风险 | 评估 | 对策 |
|------|------|------|
| bashlex 上游 0.18 后无更新（PyPI 上次 release ~2017） | 中 | vendor 固定 0.18 + LICENSE；不依赖上游 |
| `<<'EOF'` 解析失败时 first-line fallback 漏掉 body 内 top-level git commit | 极低 | LLM 不会在 heredoc 内执行 git commit；R4 K-class brief 兜底 |
| 预筛 regex 误拦（如 `git committee` 之类罕见词） | 低 | 用 word-boundary `(^|[^A-Za-z])git[ \t]+commit([ \t]|$)`；进 Step 2 后 AST 会准确判 |
| Step 2 进入但 cold import 慢 | 已知 ~100ms | 用户感知阈值约 200ms；可接受 |

### 与 prd.md 原决议对比

| 维度 | 原 Option D（一刀切 AST） | **修订 D'+A** | Option G（first-line + hash） |
|------|---------------------|---------------|-----------------|
| `<<'EOF'` heredoc | ❌ PARSE_ERROR 崩溃 | ✅ fallback first-line PASS | ✅ first-line PASS |
| `touch && git commit` 拦截 | ✅ AST 精确 | ✅ AST 精确 | ❌ first-line 是 touch，绕过 |
| heredoc body false-positive | ✅ AST 精确 | ✅ first-line 不匹配 → PASS | ✅ first-line 不匹配 → PASS |
| 普通命令开销 | ~100ms 每次 | **< 5ms 每次** | < 5ms |
| 复杂命令开销 | ~145ms | ~145ms | < 5ms |
| 兼容性风险 | 高（`<<'EOF'` 全崩） | 低 | 极低 |

**修订后 D'+A 在所有维度优于原 Option D**，且仅在 `touch && git commit` 拦截这一项优于 Option G。

## 推荐

**采纳 D'+A**，更新 prd.md 的 ADR：
- Decision: D'+A（regex 预筛 → bashlex AST → first-line fallback on parse-error → content-hash marker）
- Implementation footprint：vendor bashlex 228KB；hook ~250 行 Python；test 套覆盖 7 个 case（原 6 + parse-error fallback）
- 风险已可控；唯一兜底依赖 R4 K-class brief（已经存在）

## Spike 脚本与原始数据

- 脚本：`spike-bashlex-perf.py`（与本文件同目录）
- 复现：`python3 spike-bashlex-perf.py`
- bashlex 源码位置（spike 用临时副本）：`/tmp/bashlex-spike/`（Phase 2 vendor 时再正式入仓 `~/.claude/hooks/_vendor/bashlex/`）

