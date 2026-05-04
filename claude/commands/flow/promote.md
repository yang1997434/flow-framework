---
description: "Promote knowledge between memory tiers (Lv0 → Lv1 → Lv2 → Lv3)"
argument-hint: <source-file> <target-tier>
---

# /flow:promote

User wants to promote a piece of knowledge to a higher tier.

## Tiers

| Lv | Path | Scope |
|----|------|-------|
| Lv0 | `.flow/tasks/<slug>/` | Task scratch (auto-archives) |
| Lv1 | `.flow/{ADRs,patterns,pitfalls}/` | Project-level (committed) |
| Lv2 | `~/data/knowledge-base/{patterns,pitfalls}/` | Cross-project (vault) |
| Lv3 | `~/.claude/rules/<topic>.md` | Global hard rule |

## Promotion rules (data-driven)

**Lv0 → Lv1**: Phase 4 sediment decision — "worth keeping for future reference in this project"

**Lv1 → Lv2**: Same pattern referenced in ≥2 different tasks **across ≥2 projects** in last 90 days. Or char-cap pressure (Letta-style: project file growing past 500 lines on this topic).

**Lv2 → Lv3**: Used ≥3 times across projects with NO exceptions encountered. Plus user manual final approval (Lv3 is hard rule, almost-immutable).

## Step 1 — Validate source

```bash
[ -f "$SOURCE_FILE" ] || { echo "Source not found: $SOURCE_FILE"; exit 1; }
```

Read source file. Identify what kind (ADR / pattern / pitfall).

## Step 2 — Determine target path

| Source type → tier | Target path |
|------|------|
| ADR → vault | `~/data/knowledge-base/ADRs/<slug>.md` (create dir if needed) |
| pattern → vault | `~/data/knowledge-base/patterns/<slug>.md` |
| pitfall → vault | `~/data/knowledge-base/pitfalls/<slug>.md` |
| pattern → rules | `~/.claude/rules/<topic>.md` |
| pitfall → rules | `~/.claude/rules/pitfalls-<topic>.md` |

## Step 3 — Verify promotion criteria

Show user:
- Source content
- Target path
- Why this satisfies the promotion criteria for the chosen tier
- Cap check: target tier's char/line limit

If criteria don't match: warn user, ask if they want to override.

## Step 4 — Adapt content for target tier

Higher tiers should be **more general**:
- Strip project-specific paths
- Generalize the prevention / pattern
- Add a "First emerged in" reference back to source

For Lv3 rules: must be **declarative + concise**. Use frontmatter format like other `~/.claude/rules/*.md`.

## Step 5 — Write target + verify no credentials

```bash
# Credential grep on the new file
grep -E "(password|secret|api[_-]?key|token).*[:=]\\s*['\"][^'\"]+['\"]" "$TARGET_PATH" && {
    echo "ABORT: credential leak detected"
    rm "$TARGET_PATH"
    exit 1
}
```

## Step 6 — Update source (mark as promoted)

Don't delete source. Add note to source frontmatter / body:
```yaml
status: promoted
promoted_to: <target_path>
promoted_date: <date>
```

## Step 7 — Update MOC (if vault)

If target is vault, update `~/data/knowledge-base/_MOC/<area>.md` with the new entry pointer.

## Step 8 — Tell user

Summary of: what got promoted, where, why, what's next.

## Constraints

- **Never copy credentials** during promotion
- **Always verify** target tier criteria are met (or override explicitly)
- **Cap check**: warn if file would exceed tier cap
- **Lv3 needs explicit user "yes"** — hardest tier, hardest to revert
