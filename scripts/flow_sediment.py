#!/usr/bin/env python3
"""flow sediment — render pitfall / pattern / ADR templates and link from progress.md.

Usage:
  flow_sediment.py pitfall <slug> [--severity LEVEL] [--trigger-paths GLOBS] [--cross] [--edit]
  flow_sediment.py pattern <slug> [--tier TIER] [--cross] [--edit]
  flow_sediment.py adr <slug> [--cross] [--edit]

ADRs auto-number with `0001-` prefix unless slug already starts with `\\d{4}-`.
Renders to .flow/{pitfalls,patterns,ADRs}/<slug>.md and (if active task exists)
appends a link under `## Sediment Notes` in the active task's progress.md. When
the active task has a `.checkpoint/`, a `sediment` event is also appended to
history.jsonl.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.paths import REPO_ROOT, get_flow_dir, get_current_task_path
from common import safe_io
from common.checkpoint_paths import history_path


VALID_TYPES = ("pitfall", "pattern", "adr")
VALID_SEVERITY = ("low", "medium", "high")
VALID_TIER = ("project", "cross-project", "rules")

TYPE_TO_DIR = {
    "pitfall": "pitfalls",
    "pattern": "patterns",
    "adr": "ADRs",
}
TYPE_TO_TEMPLATE = {
    "pitfall": "pitfall.md.template",
    "pattern": "pattern.md.template",
    "adr": "ADR-lite.md.template",
}

# Allow optional 4-digit prefix for ADRs (e.g. `0042-foo`).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ADR_PREFIX_RE = re.compile(r"^(\d{4})-")


def _validate_slug(slug: str, *, type_: str) -> None:
    """Slug must be lowercase kebab. ADR may carry a leading 4-digit prefix."""
    if type_ == "adr":
        # Allow `0042-explicit` form. Strip prefix for validation only.
        bare = _ADR_PREFIX_RE.sub("", slug)
        if not _SLUG_RE.match(bare):
            print(
                f"ERROR: invalid slug '{slug}'. Use lowercase kebab "
                f"(letters/digits/hyphens), optional 4-digit prefix.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        if not _SLUG_RE.match(slug):
            print(
                f"ERROR: invalid slug '{slug}'. Use lowercase kebab "
                f"(letters/digits/hyphens, must start with letter or digit).",
                file=sys.stderr,
            )
            sys.exit(1)


def _next_adr_number(adr_dir: Path) -> int:
    """Scan existing ADR files; return next 4-digit number (1-based)."""
    if not adr_dir.is_dir():
        return 1
    nums: list[int] = []
    for entry in adr_dir.iterdir():
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        m = re.match(r"^(\d{4})-", entry.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def _resolve_adr_filename(slug: str, adr_dir: Path) -> str:
    """Return final filename for ADR. Respects explicit 4-digit prefix."""
    if _ADR_PREFIX_RE.match(slug):
        return f"{slug}.md"
    n = _next_adr_number(adr_dir)
    return f"{n:04d}-{slug}.md"


def _render_template(template_path: Path, substitutions: dict[str, str]) -> str:
    """Read template and apply substitutions. Falls back to raw file if missing."""
    text = template_path.read_text(encoding="utf-8")
    for k, v in substitutions.items():
        text = text.replace(k, v)
    return text


def _append_sediment_notes_link(progress: Path, line: str) -> bool:
    """Append `line` under `## Sediment Notes`. Creates section if missing.

    Strips an existing template stub like `<!-- TEMPLATE: ... -->` directly
    under the heading on first append, so notes don't end up below the
    placeholder. Idempotent w.r.t. repeated identical lines (re-running
    would still append, but locked_text_rmw guarantees serialization).
    """
    line = line.rstrip("\n")
    marker = "## Sediment Notes"

    def _transform(text: str) -> str:
        idx = text.find(marker)
        if idx < 0:
            sep = "" if text.endswith("\n") else "\n"
            return text + f"{sep}\n{marker}\n\n{line}\n"
        # Find next `\n## ` or EOF.
        next_idx = text.find("\n## ", idx + len(marker))
        section_end = next_idx if next_idx >= 0 else len(text)
        section = text[idx:section_end]

        # If the section body (after the heading) is just the template stub,
        # replace the stub with our line. Otherwise append before the next
        # section.
        body = section[len(marker):]
        # Drop leading blank lines for inspection.
        body_stripped = body.lstrip("\n")
        if body_stripped.startswith("<!-- TEMPLATE:") and "-->" in body_stripped:
            # Replace stub line(s) with our entry. Keep one blank between
            # heading and the new line.
            after_stub = body_stripped.split("-->", 1)[1]
            # Drop leading newlines on remainder; we'll prepend the line.
            after_stub = after_stub.lstrip("\n")
            new_section_body = "\n\n" + line + "\n"
            if after_stub.strip():
                new_section_body += "\n" + after_stub
            new_section = marker + new_section_body
        else:
            # Append our line at the end of the section body.
            trimmed = section.rstrip()
            new_section = trimmed + "\n" + line + "\n"
        if next_idx >= 0:
            new_section = new_section.rstrip() + "\n\n"
        return text[:idx] + new_section + text[section_end:]

    return safe_io.locked_text_rmw(progress, _transform)


def _render_to_disk(
    type_: str,
    slug: str,
    *,
    cross: bool,
    severity: str | None = None,
    trigger_paths: str | None = None,
    tier: str | None = None,
) -> Path:
    """Render the appropriate template to .flow/<dir>/<slug>.md and return path."""
    flow = get_flow_dir()
    if not flow.is_dir():
        print(f"ERROR: {flow} not found. Run flow_init.py first.", file=sys.stderr)
        sys.exit(1)
    out_dir = flow / TYPE_TO_DIR[type_]
    out_dir.mkdir(parents=True, exist_ok=True)

    if type_ == "adr":
        filename = _resolve_adr_filename(slug, out_dir)
    else:
        filename = f"{slug}.md"

    out_path = out_dir / filename
    if out_path.exists():
        print(f"ERROR: {out_path} already exists", file=sys.stderr)
        sys.exit(1)

    template = REPO_ROOT / "templates" / TYPE_TO_TEMPLATE[type_]
    if not template.is_file():
        print(f"ERROR: template missing: {template}", file=sys.stderr)
        sys.exit(1)

    today = date.today().isoformat()
    project_or_cross = "cross-project" if cross else "project"
    # The pattern template uses `tier:` in frontmatter — our `--tier` flag
    # also affects `{{PROJECT_OR_CROSS}}` substitution because tier=cross-project
    # implies cross.
    if type_ == "pattern" and tier == "cross-project":
        project_or_cross = "cross-project"
    if type_ == "pattern" and tier == "rules":
        project_or_cross = "cross-project"

    substitutions = {
        "{{SLUG}}": slug if not (type_ == "adr" and not _ADR_PREFIX_RE.match(slug))
                          else filename.removesuffix(".md"),
        "{{DATE}}": today,
        "{{PROJECT_OR_CROSS}}": project_or_cross,
        # ADR-lite template uses `{{PROJECT}}` (not _OR_CROSS); cover both.
        "{{PROJECT}}": project_or_cross,
        # `{{TITLE}}` is left alone if the template uses it; substitute slug
        # as a sane default.
        "{{TITLE}}": slug.replace("-", " "),
    }

    text = _render_template(template, substitutions)

    # Type-specific frontmatter overrides (best-effort; only mutates if the
    # template has the matching field on a single line).
    if type_ == "pitfall":
        sev = severity or "medium"
        text = re.sub(
            r"(?m)^severity:\s+\S+(\s*#.*)?$",
            lambda m: f"severity: {sev}{m.group(1) or ''}",
            text,
            count=1,
        )
        if trigger_paths:
            # Replace the placeholder list under `trigger_paths:` with comma-
            # separated entries. Conservative: only touches the first
            # `  - <...>` line directly under `trigger_paths:`.
            paths_yaml = "\n".join(
                f"  - {p.strip()}"
                for p in trigger_paths.split(",")
                if p.strip()
            )
            text = re.sub(
                r"(?ms)^trigger_paths:\n(?:  - .*\n)+",
                f"trigger_paths:\n{paths_yaml}\n",
                text,
                count=1,
            )
    if type_ == "pattern":
        chosen_tier = tier or "project"
        text = re.sub(
            r"(?m)^tier:\s+\S+(\s*#.*)?$",
            lambda m: f"tier: {chosen_tier}{m.group(1) or ''}",
            text,
            count=1,
        )

    out_path.write_text(text, encoding="utf-8")
    return out_path


def _maybe_link_progress_md(out_path: Path, type_: str, slug: str) -> bool:
    """Append a link to active task's progress.md `## Sediment Notes`.

    Returns True if linked, False if no active task / progress.md missing.
    """
    cur = get_current_task_path()
    if cur is None:
        return False
    progress = cur / "progress.md"
    if not progress.is_file():
        return False
    rel = os.path.relpath(out_path, progress.parent)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"- [{ts}] {type_}: [{slug}]({rel})"
    _append_sediment_notes_link(progress, line)

    # Also append a sediment event to history.jsonl when .checkpoint/ exists.
    cp = cur / ".checkpoint"
    if cp.is_dir():
        safe_io.append_jsonl_locked(
            history_path(cur),
            {
                "event": "sediment",
                "type": type_,
                "slug": slug,
                "path": str(out_path),
                "ts": datetime.now().isoformat(timespec="seconds"),
            },
        )
    return True


def _maybe_open_editor(path: Path, edit: bool) -> None:
    """Open $EDITOR on path only when --edit AND $EDITOR is set."""
    if not edit:
        return
    editor = os.environ.get("EDITOR")
    if not editor:
        return
    try:
        import subprocess
        subprocess.call([editor, str(path)])
    except (FileNotFoundError, OSError) as e:
        print(f"WARN: failed to open $EDITOR ({editor}): {e}", file=sys.stderr)


def _run(args: argparse.Namespace, type_: str) -> None:
    slug = args.slug
    _validate_slug(slug, type_=type_)

    severity = getattr(args, "severity", None)
    trigger_paths = getattr(args, "trigger_paths", None)
    tier = getattr(args, "tier", None)

    out_path = _render_to_disk(
        type_, slug,
        cross=getattr(args, "cross", False),
        severity=severity,
        trigger_paths=trigger_paths,
        tier=tier,
    )

    linked = _maybe_link_progress_md(out_path, type_, slug)
    _maybe_open_editor(out_path, getattr(args, "edit", False))

    print(f"Created {out_path}.")
    if linked:
        print("Linked to active task progress.md.")
    else:
        print("(no active task; skipped progress.md link).")


def cmd_pitfall(args):
    _run(args, "pitfall")


def cmd_pattern(args):
    _run(args, "pattern")


def cmd_adr(args):
    _run(args, "adr")


def main():
    parser = argparse.ArgumentParser(
        description="Sediment a pitfall / pattern / ADR from template."
    )
    sub = parser.add_subparsers(dest="type", required=True)

    common_kwargs = {}

    p_pitfall = sub.add_parser("pitfall", help="render pitfall template")
    p_pitfall.add_argument("slug")
    p_pitfall.add_argument(
        "--severity", choices=VALID_SEVERITY, default="medium",
        help="重犯代价 (default: medium)",
    )
    p_pitfall.add_argument(
        "--trigger-paths", dest="trigger_paths", default=None,
        help="comma-separated path globs / library names",
    )
    p_pitfall.add_argument("--cross", action="store_true", help="cross-project sediment")
    p_pitfall.add_argument("--edit", action="store_true", help="open $EDITOR after creation")
    p_pitfall.set_defaults(func=cmd_pitfall)

    p_pattern = sub.add_parser("pattern", help="render pattern template")
    p_pattern.add_argument("slug")
    p_pattern.add_argument(
        "--tier", choices=VALID_TIER, default="project",
        help="tier (default: project)",
    )
    p_pattern.add_argument("--cross", action="store_true", help="cross-project sediment")
    p_pattern.add_argument("--edit", action="store_true", help="open $EDITOR after creation")
    p_pattern.set_defaults(func=cmd_pattern)

    p_adr = sub.add_parser("adr", help="render ADR-lite template (auto-numbered)")
    p_adr.add_argument("slug")
    p_adr.add_argument("--cross", action="store_true", help="cross-project sediment")
    p_adr.add_argument("--edit", action="store_true", help="open $EDITOR after creation")
    p_adr.set_defaults(func=cmd_adr)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
