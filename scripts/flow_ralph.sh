#!/usr/bin/env bash
# flow_ralph.sh — Phase 2 ralph bash-loop executor.
#
# Repeatedly invokes `claude --print` (headless, fresh context per iteration)
# until either the PRD's Acceptance Criteria checklist is fully ticked
# (Claude emits the completion-promise string) or --max-iterations is hit.
#
# We deliberately do NOT use the official anthropics ralph-wiggum plugin:
#   it loops via an in-session Stop hook which collides with flow's stop.py
#   and cannot be nested inside a sub-agent.
# See: .flow/tasks/05-04-audit-flow-issues/research/B-context-mode-ralph-loop.md
#
# Usage:
#   scripts/flow_ralph.sh <task-slug> [--max-iterations N] [--completion-promise STR] [--dry-run]
#
# Exit codes:
#   0  success (completion-promise observed OR max iterations exhausted gracefully)
#   1  bad usage / missing prd.md / unrecoverable setup error
#   2  --help requested (informational)

set -euo pipefail

# ---------- defaults ----------
MAX_ITERATIONS=20
COMPLETION_PROMISE="RALPH_DONE"
DRY_RUN=0
TASK_SLUG=""
BUDGET_USD=5

# ---------- helpers ----------
print_help() {
  cat <<'EOF'
flow_ralph.sh — Phase 2 ralph bash-loop executor

USAGE
  scripts/flow_ralph.sh <task-slug> [options]

OPTIONS
  --max-iterations N        Hard cap on iterations (default: 20)
  --completion-promise STR  Exit when this exact string appears in claude output
                            (default: RALPH_DONE)
  --dry-run                 Print planned invocations; do NOT call claude
  --budget-usd N            Per-iteration budget passed to --max-budget-usd
                            (default: 5)
  -h, --help                Show this help and exit 2

DESCRIPTION
  Reads .flow/tasks/<date>-<task-slug>/prd.md (Acceptance Criteria checklist)
  and progress.md (already-completed items). Each iteration runs
  `claude --print --max-budget-usd <N>` with a synthesised prompt asking it
  to pick the next unchecked item, implement it, tick the box in progress.md,
  and emit the completion-promise once everything is done.

  Logs go to ~/.flow/.runtime/ralph-<slug>.log .

  Failures within an iteration are logged and the loop continues.

NOT IMPLEMENTED
  - Real `claude --print` invocation is gated behind --dry-run for tests.
    When NOT in dry-run, the loop calls the real CLI; ensure FLOW_RALPH_FAKE
    is unset in production.

ENVIRONMENT
  FLOW_RALPH_FAKE=1   Substitute a fake echo-based responder for `claude`
                      (used by tests).
EOF
}

log_runtime_dir() {
  # Use HOME so this is writable in CI / sandboxes.
  printf '%s/.flow/.runtime' "${HOME:-/tmp}"
}

logf() {
  local msg="$*"
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  printf '[%s] %s\n' "$ts" "$msg"
}

# Find the project root (looks for .flow/ upward from cwd).
find_project_root() {
  local d
  d="$(pwd)"
  while [ "$d" != "/" ]; do
    if [ -d "$d/.flow" ]; then
      printf '%s' "$d"
      return 0
    fi
    d="$(dirname "$d")"
  done
  return 1
}

# Resolve task dir from slug. Returns absolute path on stdout, exits 1 on error.
resolve_task_dir() {
  local slug="$1"
  local root
  if ! root="$(find_project_root)"; then
    echo "ERROR: no .flow/ directory found above cwd ($(pwd))" >&2
    return 1
  fi
  local matches=()
  # shellcheck disable=SC2207
  matches=($(find "$root/.flow/tasks" -maxdepth 1 -type d -name "*-${slug}" 2>/dev/null))
  if [ "${#matches[@]}" -eq 0 ]; then
    echo "ERROR: no task directory matching slug '${slug}' under $root/.flow/tasks/" >&2
    return 1
  fi
  if [ "${#matches[@]}" -gt 1 ]; then
    echo "ERROR: multiple task dirs match slug '${slug}': ${matches[*]}" >&2
    return 1
  fi
  printf '%s' "${matches[0]}"
}

# Extract Acceptance Criteria unchecked items from prd.md.
# Output: one item per line, leading "- [ ] " stripped.
extract_open_criteria() {
  local prd="$1"
  awk '
    /^## Acceptance Criteria[[:space:]]*$/ { in_section=1; next }
    /^## / && in_section { in_section=0 }
    in_section && /^- \[ \] / {
      sub(/^- \[ \] /, "")
      print
    }
  ' "$prd"
}

# Build a single iteration prompt. Stdout = full prompt text.
build_prompt() {
  local prd="$1"
  local progress="$2"
  local completion="$3"
  cat <<EOF
You are working in a flow-framework task. Do exactly ONE iteration of work,
then stop. Do NOT invoke /flow:start, /flow:continue, or any flow command —
you are already inside a ralph loop.

PRD path:      ${prd}
PROGRESS path: ${progress}

Task per iteration:
1. Read the PRD's "## Acceptance Criteria" section.
2. Read PROGRESS to see which criteria already have outcomes.
3. Pick the SINGLE next "- [ ]" item that is unblocked and not yet done.
4. Implement it (write code, edit files).
5. In PROGRESS, append a row to "## Execute Log" describing what was done.
6. In PRD, change that one criterion's "- [ ] " to "- [x] " if (and only if)
   it is now testable as passing.
7. If — and only if — every "- [ ] " in the PRD is now "- [x] ",
   end your reply with this exact line on its own:

   ${completion}

   Otherwise, end with a brief 1-line summary so the next iteration sees fresh
   state. Do NOT emit "${completion}" speculatively; only when truly done.

Constraints:
- Do not commit; flow Phase 3 handles commits.
- Do not start sub-agents or other ralph loops (no nesting).
- Keep diffs small and focused on the single chosen criterion.
EOF
}

# Run one iteration. Stdout = claude's stdout. Returns exit code from claude.
run_one_iteration() {
  local prompt="$1"
  if [ "${FLOW_RALPH_FAKE:-0}" = "1" ]; then
    # Test-only fake responder: echoes a short marker so detection logic
    # can be exercised without the real CLI.
    printf 'FAKE_RALPH_RESPONSE iter=%s\n' "${FLOW_RALPH_FAKE_ITER:-?}"
    if [ "${FLOW_RALPH_FAKE_FINISH_AT:-}" = "${FLOW_RALPH_FAKE_ITER:-?}" ]; then
      printf '%s\n' "${FLOW_RALPH_FAKE_PROMISE:-RALPH_DONE}"
    fi
    return 0
  fi
  # Real invocation. We deliberately keep this simple — no system prompt
  # that could re-enter flow.
  claude --print --max-budget-usd "${BUDGET_USD}" -- "${prompt}"
}

# ---------- arg parsing ----------
if [ "$#" -eq 0 ]; then
  print_help
  exit 2
fi

POSITIONAL=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      print_help
      exit 2
      ;;
    --max-iterations)
      [ "$#" -ge 2 ] || { echo "ERROR: --max-iterations needs a value" >&2; exit 1; }
      MAX_ITERATIONS="$2"
      shift 2
      ;;
    --max-iterations=*)
      MAX_ITERATIONS="${1#--max-iterations=}"
      shift
      ;;
    --completion-promise)
      [ "$#" -ge 2 ] || { echo "ERROR: --completion-promise needs a value" >&2; exit 1; }
      COMPLETION_PROMISE="$2"
      shift 2
      ;;
    --completion-promise=*)
      COMPLETION_PROMISE="${1#--completion-promise=}"
      shift
      ;;
    --budget-usd)
      [ "$#" -ge 2 ] || { echo "ERROR: --budget-usd needs a value" >&2; exit 1; }
      BUDGET_USD="$2"
      shift 2
      ;;
    --budget-usd=*)
      BUDGET_USD="${1#--budget-usd=}"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --)
      shift
      while [ "$#" -gt 0 ]; do POSITIONAL+=("$1"); shift; done
      ;;
    -*)
      echo "ERROR: unknown flag '$1' (try --help)" >&2
      exit 1
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [ "${#POSITIONAL[@]}" -lt 1 ]; then
  echo "ERROR: missing <task-slug> (try --help)" >&2
  exit 1
fi
TASK_SLUG="${POSITIONAL[0]}"

# Validate numeric --max-iterations.
case "${MAX_ITERATIONS}" in
  ''|*[!0-9]*)
    echo "ERROR: --max-iterations must be a positive integer (got '${MAX_ITERATIONS}')" >&2
    exit 1
    ;;
esac
if [ "${MAX_ITERATIONS}" -lt 1 ]; then
  echo "ERROR: --max-iterations must be >= 1" >&2
  exit 1
fi

# ---------- resolve paths ----------
TASK_DIR="$(resolve_task_dir "${TASK_SLUG}")" || exit 1
PRD_FILE="${TASK_DIR}/prd.md"
PROGRESS_FILE="${TASK_DIR}/progress.md"

if [ ! -f "${PRD_FILE}" ]; then
  echo "ERROR: prd.md not found at ${PRD_FILE}" >&2
  echo "Hint: ralph mode requires an existing PRD with '## Acceptance Criteria' checklist." >&2
  exit 1
fi
if [ ! -f "${PROGRESS_FILE}" ]; then
  echo "ERROR: progress.md not found at ${PROGRESS_FILE}" >&2
  exit 1
fi

RUNTIME_DIR="$(log_runtime_dir)"
mkdir -p "${RUNTIME_DIR}"
LOG_FILE="${RUNTIME_DIR}/ralph-${TASK_SLUG}.log"

# ---------- header ----------
{
  logf "=== ralph loop start ==="
  logf "task_slug=${TASK_SLUG}"
  logf "task_dir=${TASK_DIR}"
  logf "max_iterations=${MAX_ITERATIONS}"
  logf "completion_promise=${COMPLETION_PROMISE}"
  logf "dry_run=${DRY_RUN}"
  logf "budget_usd=${BUDGET_USD}"
  logf "log_file=${LOG_FILE}"
} | tee -a "${LOG_FILE}"

# Show open criteria (informational).
OPEN_COUNT=0
while IFS= read -r line; do
  OPEN_COUNT=$((OPEN_COUNT + 1))
  logf "open_criterion[${OPEN_COUNT}]: ${line}" | tee -a "${LOG_FILE}" >/dev/null
done < <(extract_open_criteria "${PRD_FILE}")
logf "open_criteria_total=${OPEN_COUNT}" | tee -a "${LOG_FILE}"

if [ "${OPEN_COUNT}" -eq 0 ]; then
  logf "no open '- [ ]' criteria found; nothing to do." | tee -a "${LOG_FILE}"
  exit 0
fi

# ---------- dry-run path ----------
if [ "${DRY_RUN}" -eq 1 ]; then
  logf "DRY-RUN: would invoke claude up to ${MAX_ITERATIONS} times." | tee -a "${LOG_FILE}"
  PROMPT_PREVIEW="$(build_prompt "${PRD_FILE}" "${PROGRESS_FILE}" "${COMPLETION_PROMISE}")"
  echo "----- dry-run prompt preview -----"
  printf '%s\n' "${PROMPT_PREVIEW}"
  echo "----- end preview -----"
  logf "DRY-RUN complete." | tee -a "${LOG_FILE}"
  exit 0
fi

# ---------- main loop ----------
DONE_REASON=""
ITER=0
while [ "${ITER}" -lt "${MAX_ITERATIONS}" ]; do
  ITER=$((ITER + 1))
  logf "--- iteration ${ITER} / ${MAX_ITERATIONS} ---" | tee -a "${LOG_FILE}"

  PROMPT="$(build_prompt "${PRD_FILE}" "${PROGRESS_FILE}" "${COMPLETION_PROMISE}")"
  ITER_OUT_FILE="${RUNTIME_DIR}/ralph-${TASK_SLUG}.iter-${ITER}.out"

  # Run claude; capture output. set -e is on, but we want to swallow per-iter
  # failures so the loop can continue.
  set +e
  FLOW_RALPH_FAKE_ITER="${ITER}" run_one_iteration "${PROMPT}" >"${ITER_OUT_FILE}" 2>&1
  RC=$?
  set -e

  if [ "${RC}" -ne 0 ]; then
    logf "iteration ${ITER} FAILED (rc=${RC}); continuing." | tee -a "${LOG_FILE}"
    {
      echo "----- iter ${ITER} stderr/stdout -----"
      cat "${ITER_OUT_FILE}"
      echo "----- end -----"
    } >>"${LOG_FILE}"
    continue
  fi

  # Append iteration output to log.
  {
    echo "----- iter ${ITER} output -----"
    cat "${ITER_OUT_FILE}"
    echo "----- end -----"
  } >>"${LOG_FILE}"

  # Check completion-promise.
  if grep -Fxq "${COMPLETION_PROMISE}" "${ITER_OUT_FILE}"; then
    DONE_REASON="completion-promise '${COMPLETION_PROMISE}' observed at iteration ${ITER}"
    logf "${DONE_REASON}" | tee -a "${LOG_FILE}"
    break
  fi
done

if [ -z "${DONE_REASON}" ]; then
  DONE_REASON="max-iterations (${MAX_ITERATIONS}) exhausted"
  logf "${DONE_REASON}" | tee -a "${LOG_FILE}"
fi

# ---------- write Execute Log summary ----------
SUMMARY_LINE=$(printf '| %s | ralph-loop | task=%s | %s (iters=%d) |' \
  "$(date '+%Y-%m-%d %H:%M')" "${TASK_SLUG}" "${DONE_REASON}" "${ITER}")
# Append after "## Execute Log" header. If the section already has table rows,
# we just append; the table head is parsed positionally so a trailing row is fine.
{
  echo ""
  echo "${SUMMARY_LINE}"
} >>"${PROGRESS_FILE}"
logf "appended Execute Log row to ${PROGRESS_FILE}" | tee -a "${LOG_FILE}"
logf "=== ralph loop end ===" | tee -a "${LOG_FILE}"

exit 0
