#!/usr/bin/env bash
# test_ralph_loop.sh — smoke tests for scripts/flow_ralph.sh.
#
# Exercises:
#   - --help works
#   - --dry-run prints plan and does NOT call claude
#   - max-iterations bounds the loop (using FLOW_RALPH_FAKE)
#   - completion-promise short-circuits the loop early
#   - missing prd.md gives a friendly error
#
# Never invokes the real `claude --print` (would burn tokens). The script
# under test honours FLOW_RALPH_FAKE=1 to substitute an echo-based fake.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/flow_ralph.sh"

PASS=0
FAIL=0
FAILED_NAMES=()

log()  { printf '[test] %s\n' "$*"; }
ok()   { PASS=$((PASS + 1)); printf '  PASS — %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); FAILED_NAMES+=("$*"); printf '  FAIL — %s\n' "$*"; }

cleanup_dirs=()
cleanup() {
  for d in "${cleanup_dirs[@]}"; do
    [ -n "$d" ] && [ -d "$d" ] && rm -rf "$d"
  done
}
trap cleanup EXIT

# Build a minimal fake project tree:
#   <tmp>/.flow/tasks/05-04-<slug>/prd.md  (with Acceptance Criteria)
#   <tmp>/.flow/tasks/05-04-<slug>/progress.md
#   symlink scripts/ -> real repo scripts so flow_ralph.sh resolves
make_fake_project() {
  local slug="$1"
  local with_prd="${2:-yes}"
  local tmp
  tmp="$(mktemp -d)"
  cleanup_dirs+=("$tmp")
  mkdir -p "$tmp/.flow/tasks/05-04-${slug}"
  if [ "$with_prd" = "yes" ]; then
    cat >"$tmp/.flow/tasks/05-04-${slug}/prd.md" <<'EOF'
# Fake task

## Acceptance Criteria

- [ ] First thing
- [ ] Second thing
- [x] Already done

## Other

stuff
EOF
  fi
  cat >"$tmp/.flow/tasks/05-04-${slug}/progress.md" <<'EOF'
# progress.md — fake

## Plan

(none)

## Execute Log

EOF
  printf '%s' "$tmp"
}

# --- test 1: --help ----------------------------------------------------------
log "test: --help prints usage"
HELP_OUT="$("$SCRIPT" --help 2>&1 || true)"
HELP_RC=$?
# --help exits 2 by design; tolerate non-zero.
if echo "$HELP_OUT" | grep -q "USAGE" && echo "$HELP_OUT" | grep -q "completion-promise"; then
  ok "help text contains USAGE and --completion-promise"
else
  fail "help text missing expected sections (rc=$HELP_RC)"
  echo "$HELP_OUT" | sed 's/^/    /'
fi

# --- test 2: dry-run does NOT call claude ----------------------------------
log "test: --dry-run prints plan and skips claude"
DRY_PROJ="$(make_fake_project dry-slug)"
pushd "$DRY_PROJ" >/dev/null
DRY_OUT="$("$SCRIPT" dry-slug --dry-run --max-iterations 5 2>&1 || true)"
popd >/dev/null
if echo "$DRY_OUT" | grep -q "DRY-RUN: would invoke claude up to 5 times" \
    && echo "$DRY_OUT" | grep -q "dry-run prompt preview"; then
  ok "dry-run printed plan"
else
  fail "dry-run output missing expected lines"
  echo "$DRY_OUT" | sed 's/^/    /'
fi
if echo "$DRY_OUT" | grep -q "open_criteria_total=2"; then
  ok "dry-run counted 2 open criteria correctly"
else
  fail "dry-run did not parse Acceptance Criteria correctly"
fi

# --- test 3: max-iterations is a hard cap (with fake claude) -----------------
log "test: max-iterations bounds the loop"
CAP_PROJ="$(make_fake_project cap-slug)"
pushd "$CAP_PROJ" >/dev/null
# FLOW_RALPH_FAKE=1 makes flow_ralph.sh use the built-in fake responder.
# We do NOT set FLOW_RALPH_FAKE_FINISH_AT, so completion-promise is never
# emitted — we should hit max-iterations.
CAP_OUT="$(FLOW_RALPH_FAKE=1 "$SCRIPT" cap-slug --max-iterations 3 2>&1 || true)"
popd >/dev/null
if echo "$CAP_OUT" | grep -q "max-iterations (3) exhausted"; then
  ok "max-iterations reached as expected"
else
  fail "max-iterations cap did not fire"
  echo "$CAP_OUT" | tail -20 | sed 's/^/    /'
fi
if echo "$CAP_OUT" | grep -q "iteration 3 / 3"; then
  ok "ran exactly 3 iterations"
else
  fail "iteration counter did not reach 3"
fi
# Verify Execute Log row was appended.
if grep -q "ralph-loop" "$CAP_PROJ/.flow/tasks/05-04-cap-slug/progress.md"; then
  ok "Execute Log row appended"
else
  fail "Execute Log row missing from progress.md"
fi

# --- test 4: completion-promise short-circuits the loop ----------------------
log "test: completion-promise detection"
DONE_PROJ="$(make_fake_project done-slug)"
pushd "$DONE_PROJ" >/dev/null
# FLOW_RALPH_FAKE_FINISH_AT=2 → fake responder emits the promise on iter 2.
DONE_OUT="$(FLOW_RALPH_FAKE=1 FLOW_RALPH_FAKE_FINISH_AT=2 \
  "$SCRIPT" done-slug --max-iterations 10 --completion-promise RALPH_DONE 2>&1 || true)"
popd >/dev/null
if echo "$DONE_OUT" | grep -q "completion-promise 'RALPH_DONE' observed at iteration 2"; then
  ok "completion-promise detected at iter 2"
else
  fail "completion-promise NOT detected"
  echo "$DONE_OUT" | tail -20 | sed 's/^/    /'
fi
# Should NOT have run iteration 3.
if echo "$DONE_OUT" | grep -q "iteration 3 / 10"; then
  fail "loop did not stop at completion (ran iter 3)"
else
  ok "loop stopped before iteration 3"
fi

# --- test 5: custom completion-promise --------------------------------------
log "test: custom --completion-promise string"
CUSTOM_PROJ="$(make_fake_project custom-slug)"
pushd "$CUSTOM_PROJ" >/dev/null
CUSTOM_OUT="$(FLOW_RALPH_FAKE=1 FLOW_RALPH_FAKE_FINISH_AT=1 FLOW_RALPH_FAKE_PROMISE="ALL_GREEN" \
  "$SCRIPT" custom-slug --max-iterations 5 --completion-promise ALL_GREEN 2>&1 || true)"
popd >/dev/null
if echo "$CUSTOM_OUT" | grep -q "completion-promise 'ALL_GREEN' observed at iteration 1"; then
  ok "custom completion-promise honoured"
else
  fail "custom completion-promise not detected"
  echo "$CUSTOM_OUT" | tail -10 | sed 's/^/    /'
fi

# --- test 6: missing prd.md is a friendly error ------------------------------
log "test: missing prd.md → friendly error"
NOPRD_PROJ="$(make_fake_project noprd-slug no)"  # no prd.md created
pushd "$NOPRD_PROJ" >/dev/null
set +e
NOPRD_OUT="$("$SCRIPT" noprd-slug --dry-run 2>&1)"
NOPRD_RC=$?
set -e
popd >/dev/null
if [ "$NOPRD_RC" -ne 0 ] && echo "$NOPRD_OUT" | grep -q "prd.md not found"; then
  ok "missing prd.md exits non-zero with helpful message"
else
  fail "missing prd.md did not produce friendly error (rc=$NOPRD_RC)"
  echo "$NOPRD_OUT" | sed 's/^/    /'
fi

# --- test 7: bad slug -------------------------------------------------------
log "test: unknown slug → friendly error"
NOSLUG_PROJ="$(mktemp -d)"
cleanup_dirs+=("$NOSLUG_PROJ")
mkdir -p "$NOSLUG_PROJ/.flow/tasks"
pushd "$NOSLUG_PROJ" >/dev/null
set +e
NOSLUG_OUT="$("$SCRIPT" does-not-exist --dry-run 2>&1)"
NOSLUG_RC=$?
set -e
popd >/dev/null
if [ "$NOSLUG_RC" -ne 0 ] && echo "$NOSLUG_OUT" | grep -q "no task directory matching slug"; then
  ok "unknown slug exits non-zero with helpful message"
else
  fail "unknown slug did not produce friendly error (rc=$NOSLUG_RC)"
  echo "$NOSLUG_OUT" | sed 's/^/    /'
fi

# --- test 8: bad --max-iterations -------------------------------------------
log "test: non-numeric --max-iterations rejected"
BADN_PROJ="$(make_fake_project badn-slug)"
pushd "$BADN_PROJ" >/dev/null
set +e
BADN_OUT="$("$SCRIPT" badn-slug --max-iterations abc --dry-run 2>&1)"
BADN_RC=$?
set -e
popd >/dev/null
if [ "$BADN_RC" -ne 0 ] && echo "$BADN_OUT" | grep -q "must be a positive integer"; then
  ok "non-numeric --max-iterations rejected"
else
  fail "non-numeric --max-iterations not rejected (rc=$BADN_RC)"
fi

# --- summary ---------------------------------------------------------------
echo ""
echo "------------------------------------------------------------"
echo "ralph-loop smoke tests: ${PASS} passed, ${FAIL} failed"
if [ "${FAIL}" -gt 0 ]; then
  echo "Failures:"
  for n in "${FAILED_NAMES[@]}"; do
    echo "  - ${n}"
  done
  exit 1
fi
exit 0
