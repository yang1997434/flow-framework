---
description: "Capture a pitfall (踩坑) — symptom / root cause / fix / prevention"
argument-hint: <symptom or trigger description>
---

# /flow:pitfall

User just hit a pitfall worth recording. Capture it before forgetting.

## Step 1 — Determine tier

| Where to write | Trigger |
|----------------|---------|
| `.flow/pitfalls/<slug>.md` | Project-specific (only this repo) |
| `~/data/knowledge-base/pitfalls/<slug>.md` | Cross-project (this library/tool/pattern) |
| `~/.claude/rules/pitfalls-<topic>.md` | Hard rule (always, no exceptions) |

Default to `.flow/pitfalls/` unless user specifies tier.

## Step 2 — Quick check: not duplicate

```bash
# grep existing pitfalls for similar symptom/root cause
grep -l -i "<symptom keyword>" .flow/pitfalls/ ~/data/knowledge-base/pitfalls/ 2>/dev/null
```

If duplicate found: ask user — update existing or create new?

## Step 3 — Fill template

Use `~/projects/flow-framework/templates/pitfall.md.template`. Substitute:
- `{{SLUG}}` = user-provided or auto-derived
- `{{DATE}}` = today
- `{{PROJECT_OR_CROSS}}` = current project name or "cross"

Required sections (don't leave blank):
1. **Symptom** — concrete reproduction
2. **Root cause** — actual underlying reason
3. **Fix** — what worked this time
4. **Prevention** — actionable checklist for next time
5. **Why it matters** — cost of repeat

If `$ARGUMENTS` is provided, use it as initial Symptom. Then ask user for the other sections one at a time (don't ask all at once).

## Step 4 — `trigger_paths` field

Critical: ask user "什么文件路径 / library / 命令 出现时应该自动加载这个 pitfall？"

Examples:
- `trigger_paths: ["package.json", "*.tsx", "yarn.lock"]` for npm peer-dep issue
- `trigger_paths: ["docker-compose.yml", "Dockerfile.*"]` for Docker port issue
- `trigger_paths: ["server.py", "argparse"]` for argparse default issue

## Step 5 — Write file + confirm

Show user the rendered file content for one-shot review.

On approval: write to chosen tier path.

If task is currently active, also append a line to `${CURRENT}/progress.md` `## Sediment Notes`:
```markdown
- Pitfall captured: `.flow/pitfalls/<slug>.md` ([[${slug}]])
```

## Constraints

- **Never write credentials** — Symptom / Root cause should describe behavior, not contain passwords
- **Body should be <800 chars** (Letta-anchored cap)
- **trigger_paths must be specific** — overly broad globs (`**/*`) defeat the auto-load purpose
