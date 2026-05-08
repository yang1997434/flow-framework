"""
v0.8.3 P0.0 spike — bashlex perf bench for hook decision (Option D vs G).

Run:
    python3 spike-bashlex-perf.py

Decision rule (from prd.md):
    - Option D path viable if 200KB heredoc parse + traverse < 500ms
      AND total hook latency (import + parse + traverse) < ~600ms
    - Otherwise downgrade to Option G (first-line check + content-hash)
"""

import os
import sys
import time

BASHLEX_PATH = "/tmp/bashlex-spike"


def measure(label, fn, iters=5):
    """Run fn iters times, return min/median/max ms."""
    times = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    times.sort()
    return {
        "label": label,
        "min": times[0],
        "median": times[len(times) // 2],
        "max": times[-1],
    }


def make_heredoc(body_kb, quote_delim=False):
    """Construct a python3 heredoc command with body_kb KB body containing git commit text."""
    lines = []
    target_bytes = body_kb * 1024
    counter = 0
    while sum(len(l) + 1 for l in lines) < target_bytes:
        counter += 1
        if counter % 50 == 0:
            lines.append(
                f"# line {counter}: discussion of git commit -m 'old fix' (this should NOT block)"
            )
        else:
            lines.append(
                f"print('line {counter}: lorem ipsum dolor sit amet consectetur adipiscing elit')"
            )
    body = "\n".join(lines)
    delim = "'EOF'" if quote_delim else "EOF"
    return f"python3 <<{delim}\n{body}\nEOF\n"


def is_top_level_git_commit(node):
    """Walk bashlex AST, return True iff any top-level command is `git commit ...`."""
    import bashlex.ast as ast_mod

    # node may be a list or single ListNode/CommandNode
    if isinstance(node, list):
        return any(is_top_level_git_commit(n) for n in node)

    kind = getattr(node, "kind", None)
    if kind == "command":
        parts = getattr(node, "parts", [])
        if len(parts) >= 2:
            p0, p1 = parts[0], parts[1]
            w0 = getattr(p0, "word", None)
            w1 = getattr(p1, "word", None)
            if w0 == "git" and w1 == "commit":
                return True
        return False
    if kind in ("list", "compound", "pipeline", "if", "for", "while", "function"):
        for child in getattr(node, "parts", []) or []:
            if is_top_level_git_commit(child):
                return True
    return False


def main():
    print(f"# Spike: bashlex perf bench for v0.8.3 P0.0")
    print(f"# bashlex path: {BASHLEX_PATH}")

    # --- 1. Measure import (cold) — already measured in shell, but redo here for completeness
    t0 = time.perf_counter()
    sys.path.insert(0, BASHLEX_PATH)
    import bashlex

    t1 = time.perf_counter()
    import_ms = (t1 - t0) * 1000
    print(f"\n## Import")
    print(f"  cold import: {import_ms:.2f}ms")

    # --- 2. Small command (baseline)
    print(f"\n## Parse + traverse")
    cases = [
        ("trivial: `git commit -m hello`", "git commit -m hello"),
        ("touch && git commit", "touch /tmp/foo && git commit -m bypass"),
        ("python3 + heredoc 1KB unquoted", make_heredoc(1, quote_delim=False)),
        ("python3 + heredoc 1KB QUOTED", make_heredoc(1, quote_delim=True)),
        ("python3 + heredoc 50KB unquoted", make_heredoc(50, quote_delim=False)),
        ("python3 + heredoc 50KB QUOTED", make_heredoc(50, quote_delim=True)),
        ("python3 + heredoc 200KB unquoted", make_heredoc(200, quote_delim=False)),
        ("python3 + heredoc 200KB QUOTED", make_heredoc(200, quote_delim=True)),
    ]

    rows = []
    for label, cmd in cases:
        size_b = len(cmd.encode())

        def run():
            try:
                tree = bashlex.parse(cmd)
                return is_top_level_git_commit(tree)
            except Exception as e:
                return f"PARSE_ERROR: {type(e).__name__}: {str(e)[:60]}"

        # warm once
        result = run()
        if isinstance(result, str) and result.startswith("PARSE_ERROR"):
            stats = {"label": label, "min": float("nan"), "median": float("nan"), "max": float("nan")}
        else:
            stats = measure(label, run, iters=5)
        rows.append((label, size_b, stats, result))

    # --- 3. Print table
    print(f"\n  {'Case':<42} {'Size':>8} {'min(ms)':>10} {'med(ms)':>10} {'max(ms)':>10}  result")
    print(f"  {'-'*42} {'-'*8} {'-'*10} {'-'*10} {'-'*10}  {'-'*40}")
    for label, size_b, stats, result in rows:
        size_str = f"{size_b/1024:.1f}KB" if size_b > 1024 else f"{size_b}B"
        med = "n/a" if stats["median"] != stats["median"] else f"{stats['median']:.2f}"
        mn = "n/a" if stats["min"] != stats["min"] else f"{stats['min']:.2f}"
        mx = "n/a" if stats["max"] != stats["max"] else f"{stats['max']:.2f}"
        print(
            f"  {label:<42} {size_str:>8} {mn:>10} {med:>10} {mx:>10}  {result}"
        )

    # --- 4. Decision summary
    print(f"\n## Decision summary")

    def safe_median(predicate):
        for r in rows:
            if predicate(r[0]):
                v = r[2]["median"]
                if v == v:  # not nan
                    return v
        return None

    h_200_unq = safe_median(lambda lbl: "200KB unquoted" in lbl)
    h_200_q = safe_median(lambda lbl: "200KB QUOTED" in lbl)
    h_50_unq = safe_median(lambda lbl: "50KB unquoted" in lbl)
    h_50_q = safe_median(lambda lbl: "50KB QUOTED" in lbl)

    print(f"  heredoc 50KB  unquoted: {h_50_unq}")
    print(f"  heredoc 50KB  QUOTED  : {h_50_q}")
    print(f"  heredoc 200KB unquoted: {h_200_unq}")
    print(f"  heredoc 200KB QUOTED  : {h_200_q}")
    print(f"  cold import: {import_ms:.0f}ms (DOMINATES hook latency)")

    quoted_failures = sum(
        1 for r in rows if "QUOTED" in r[0] and isinstance(r[3], str) and "ERROR" in r[3]
    )
    print(f"\n  QUOTED heredoc parse failures: {quoted_failures}/3")
    if quoted_failures > 0:
        print(
            "  ⚠️  CRITICAL: bashlex cannot parse quoted heredocs (`<<'EOF'`)."
        )
        print(
            "      This is COMMON in Claude Code commands (heredoc with no var expansion)."
        )
        print(
            "      Option D viability is broken — fallback path needed for parse errors."
        )


if __name__ == "__main__":
    main()
