"""flow_acceptance — per-criterion executors + idempotency + timeout (T7).

Owns 4 method executors (cmd / file_exists / json_query / http) wired to the
v0.8.1 contract's `acceptance_criteria` list. Each executor returns a
:class:`RunResult`; :meth:`AcceptanceRunner.run_one` emits ``started`` +
``completed``/``timeout`` events into ``acceptance-progress.jsonl`` via T4's
:func:`flow_state_writer.append_acceptance_progress`.

Phase 2 vs Phase 3 retry decisions live in flow_orchestrator.py via T8's
``evaluate_criterion()``. Tail-scan resume + override resolution live in T9.

================================================================
DESIGN REFS (v0.8.1 design §6)
================================================================
- **R5** type ⊥ method: 6 type values × 4 method values. T7 owns method dispatch;
  type only affects the orchestration shell (Y1 escalate routing) and the
  default-idempotency table (e2e always false).
- **R7** per-criterion `timeout_sec`. Defaults by method (R7 table):
  ``file_exists``/``json_query``=30s, ``cmd``=600s, ``http``=60s. ``type=e2e``
  overrides to 1800s. T1 already injects defaults at parse time; the runner's
  `_effective_timeout` is the safety net for tests that build criteria
  directly (skipping `parse_contract`).
- **R8** idempotency table (DEFAULT only — T9 owns override resolution):
  ``e2e`` always ``"false"`` (NO override accepted, design line 275); ``cmd``
  ``"false"`` (R8 hardened); ``file_exists``/``json_query``/``http`` (GET only
  in v0.8.1) ``"true"``. Returns the schema string ``{"true","false","unknown"}``
  for the ``acceptance-progress.jsonl`` ``idempotent`` field (Y7 forward-compat).
- **Y1** e2e timeout AND e2e fail force ``escalate=True`` so T8 routes to
  §1 row 6 ``blocked_escalate`` (NOT row 5 ``blocked``). Method-level executors
  stay type-blind for clean separation; Y1 routing happens in `run_one`.
- **Y7** ``criterion_hash`` from T4 — used as audit identity in
  ``acceptance-progress.jsonl`` (distinct from ``command_hash`` which is just
  sha256 of the resolved command line / URL).
- **§7 line 318** ``test_criterion_timeout_blocks.py`` is a ship-required
  smoke; T7 pins the runner-side half (timeout → status + escalate flag).

================================================================
SAFETY-BOUNDARY HARDENING (codex R1 fixes — T7 IS the boundary)
================================================================
- **Process-group kill on timeout** (cmd executor): we use
  ``subprocess.Popen(..., start_new_session=True)`` to put the child shell
  in its own process group, then on ``TimeoutExpired`` send ``SIGTERM``
  to the whole group via ``os.killpg`` (with a 2s drain wait), then
  ``SIGKILL`` if anything is still alive. With plain ``subprocess.run``
  + ``shell=True`` the timeout would only kill the shell, leaving any
  ``&``-backgrounded child / forked test runner alive. Fixed: a
  ``timed_out`` verdict now actually means "the criterion's process
  tree is dead", which is what the safety boundary promises.
- **Path containment** (file_exists + json_query): criterion ``path``
  is normalized via ``(worktree_root / path).resolve()`` and rejected
  with ``inconclusive`` if it lands outside the worktree. Blocks
  ``/etc/passwd`` (absolute) and ``../../etc/passwd`` (traversal).
  Reuse via ``_resolve_within_worktree`` helper.
- **JSON read size cap** (json_query): files larger than
  ``MAX_JSON_QUERY_FILE_BYTES`` (10 MB) are rejected with
  ``inconclusive`` before ``read_text()`` materializes them. Stops
  pathological-fixture memory exhaustion / timeout starvation.
- **HTTP redirect scheme re-validation** (http executor): a custom
  ``_SchemeValidatingRedirectHandler`` intercepts every redirect target
  and refuses anything outside http(s). Stdlib's default handler
  follows ``ftp://`` redirects, so the initial-URL scheme check was
  bypassable via a 301-to-file-URL.
- **HTTP wall-clock deadline** (http executor): redirects are capped
  at 1 (most legitimate APIs don't redirect-chain), AND each redirect
  callback checks ``time.monotonic()`` against a deadline computed at
  request start. ``urlopen(timeout=N)`` is per-socket-op only; without
  the wall-clock check a slow-redirect-chain attack stretches well
  past the criterion's ``timeout_sec``.
- **HTTP threaded wall-clock guard** (codex R3 [P1]): the post-call
  ``time.monotonic() > deadline`` check above is too late — by then
  the call already blocked (e.g., trickle attack: server sends 1 byte
  every 9s under a 10s per-socket timeout, total wall-clock unbounded).
  Fix: run the entire blocking ``opener.open(...)`` + body drain in a
  daemon worker thread, and ``Thread.join(timeout=timeout_sec)`` from
  the main thread. If the join times out we return ``timed_out``
  immediately; the worker becomes a brief orphan (eventually unblocked
  by socket timeouts / GC, dies as a daemon on interpreter exit). That
  trade-off is acceptable because the boundary's promise is "the
  CRITERION is bounded", not "no thread ever lingers". Body drain is
  done INSIDE the worker so it shares the same wall-clock guard.
- **HTTPException safety net** (codex R3 [P2]): ``urllib.urlopen`` can
  re-raise ``http.client.HTTPException`` (RemoteDisconnected,
  BadStatusLine, IncompleteRead, ...) without wrapping in URLError.
  The previous except-tuple ``(URLError, socket.timeout, TimeoutError)``
  let HTTPException escape, crashing ``run_one`` after it had already
  emitted ``started`` (orphan event, no completed). Fix: include
  ``http.client.HTTPException`` and broader ``OSError`` in the worker's
  except-tuple. Plus a final catch-all ``except Exception``
  (NOT ``BaseException`` — KeyboardInterrupt / SystemExit must
  propagate) that maps anything truly unexpected to ``inconclusive``.
  This means ``run_one`` always gets a RunResult and emits a paired
  ``completed`` event — defense-in-depth against future urllib changes.

================================================================
4-BLINDSPOT NOTES (high-risk module — every category triggers)
================================================================
- **A (Python falsy / .get bypass)**: criterion fields are dataclass attributes
  (typed, never dict-`.get()`). Pre-flight validation uses ``not field`` ONLY
  where the field's type is ``Optional[str]`` AND empty-string is semantically
  equivalent to absent (e.g. ``criterion.command``). Method dispatch never
  uses ``.get()``.
- **B (design cross-ref)**: each executor's contract is reproduced verbatim
  from §6 R7 + R8 above. e2e-only escalate routing matches Y1 verbatim.
- **C (architectural ordering)**: validation (presence + non-empty of
  required field for the method) runs BEFORE any side effect (subprocess
  spawn / file open / urlopen). Timeout enforcement is the outermost safety
  boundary inside each executor — `subprocess.run(timeout=...)` kills the
  child process; `urllib.request.urlopen(timeout=...)` aborts the socket.
- **D1 (post-fail gate)**: every code path through every executor returns
  a :class:`RunResult` with a status in
  ``{pass, fail, inconclusive, timed_out}``. No silent fall-through —
  `_dispatch_method` returns ``inconclusive`` on unknown method (defense-
  in-depth; T1's parser already rejects unknown methods).
- **D2 (try/except swallow)**:
  - subprocess: ``TimeoutExpired`` → ``timed_out`` (NOT fail — semantic
    distinct, drives Y1 routing). ``OSError`` (FileNotFoundError /
    PermissionError on the spawn) → ``inconclusive`` (tool didn't run).
    With ``shell=True`` the shell handles binary-not-found (rc=127) so we
    only see OSError on resource exhaustion or fs/permission errors at the
    libc layer.
  - urllib: ``HTTPError`` (server replied non-2xx) → ``fail`` with
    ``exit_code=e.code``. ``URLError`` whose ``.reason`` is
    ``socket.timeout``/``TimeoutError`` → ``timed_out`` (matches subprocess
    semantic). Other ``URLError`` (refused / DNS) → ``fail`` per design
    (server unreachable IS the verdict for an acceptance criterion).
    Bare ``TimeoutError`` (Python 3.10+ may raise this directly from
    ``urlopen``) → ``timed_out``.
  - JSON: ``JSONDecodeError`` → ``inconclusive`` (file structurally invalid
    is a data-quality issue, not a verdict).
- **D3 (subprocess rc lies)**: rc==0 is mapped to ``pass``, non-zero to
  ``fail``. With ``shell=True`` rc=127 means "command not found" — the
  shell's verdict, fundamentally a ``fail`` from the criterion's POV (the
  thing the criterion wanted to run isn't runnable). We do NOT special-case
  127 to ``inconclusive`` because the spec defines rc semantics at the
  criterion level: the contract author writes the command string; if their
  command is unfindable, that's a fail (per the same logic that says
  HTTP-refused is a fail). On TimeoutExpired we explicitly emit ``None`` for
  ``exit_code`` because the subprocess was killed by the timeout — there is
  no real exit code, distinct from rc==-9 / rc==-15 we would synthesize.
- **D4 (corrupt-after-match)**: the dotted-path JSON resolver scans EVERY
  segment. There is no first-match-skip-rest pattern; the `for part in
  query.split(".")` traverses every step before producing the verdict.

The HTTP executor does NOT follow redirects beyond what stdlib's default
``HTTPRedirectHandler`` does (urllib defaults: HTTP/1.1 301/302/303/307
followed; max 10 hops). v0.8.2 may add a ``follow_redirects`` knob; T7
relies on stdlib defaults to keep the surface minimal. SSRF is bounded by
the criterion author's URL choice — same trust boundary as ``cmd``.
"""
from __future__ import annotations

import datetime
import enum
import hashlib
import http.client
import json
import os
import queue as queue_mod
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

# T1 / T4 modules live alongside this one — flow scripts import each other
# via a sys.path mutation that resolves to <repo>/scripts. Mirrors the
# pattern in flow_state_writer.py / flow_orchestrator.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flow_contract import AcceptanceCriterion  # type: ignore  # noqa: E402
from flow_state_writer import (                  # type: ignore  # noqa: E402
    AcceptanceProgressEvent,
    append_acceptance_progress,
    compute_criterion_hash,
)


# R7 table — per-method default `timeout_sec` (when criterion omits + type !=
# e2e). T1's `parse_contract` already injects these at parse time; the
# runner's `_effective_timeout` is a safety net for direct-construction tests.
DEFAULT_TIMEOUT_BY_METHOD = {
    "file_exists": 30,
    "json_query": 30,
    "cmd": 600,
    "http": 60,
}
# Design line 277: type=e2e overrides method-based default to 30 min.
E2E_TYPE_TIMEOUT = 1800

# Hard upper bound on the JSON file we'll read for `json_query`. A larger
# file is rejected with `inconclusive` (criterion malformed / data too
# big to be reasonable). 10 MiB easily covers config / fixture sizes; the
# point is to cap before we materialize the whole file in memory.
MAX_JSON_QUERY_FILE_BYTES = 10 * 1024 * 1024

# Cap on HTTP redirects we'll follow. Most legitimate APIs don't
# redirect-chain; capping at 1 (initial → one redirect target) keeps the
# overall-deadline window tight without breaking common 301→302 cases.
MAX_HTTP_REDIRECTS = 1

# Time after SIGTERM we wait for a process group to drain before SIGKILL.
# Short by design — the criterion's `timeout_sec` already elapsed; we
# just want graceful termination if the tree responds promptly.
PROCESS_GROUP_KILL_GRACE_SEC = 2


class _HttpDeadlineExceeded(urllib.error.URLError):
    """Distinct subclass so the http executor can route deadline-induced
    aborts to ``timed_out`` instead of the generic ``fail`` bucket.

    SAFETY-BOUNDARY (codex R2 [P2]): a plain ``URLError("deadline...")``
    produces a string-typed ``.reason`` which the executor's URLError
    handler doesn't recognize as a timeout — the verdict was being routed
    to ``fail`` instead of ``timed_out``. Using a dedicated subclass means
    ``isinstance(e, _HttpDeadlineExceeded)`` works regardless of how the
    reason is represented, and is robust to future refactors of the
    string-matching logic.
    """


class _SchemeValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate scheme + wall-clock deadline on every redirect target.

    The default ``urllib.request.HTTPRedirectHandler`` happily follows
    ``ftp://`` (and other) schemes when a redirect target switches
    protocol. Our http executor's contract is "GET an http(s) URL"; a
    301 to ``file:///etc/passwd`` would otherwise turn into local file
    read with the success/failure flowing back as if it were the
    intended verdict. We refuse anything outside http(s) here — the
    URLError raised is caught by ``_run_http`` and produces ``fail``.

    Wall-clock: ``urlopen(timeout=N)`` is per-socket-op only. By
    checking ``time.monotonic() > deadline`` in this callback (called
    BEFORE issuing the next request), a slow-redirect-chain attack
    aborts at the criterion's ``timeout_sec`` rather than stretching
    to ``N * (hops + 1)``.
    """

    # The opener stashes deadline+max_redirects on the handler instance
    # via ``_set_request_state`` before each call to .open().
    _deadline_monotonic: Optional[float] = None
    _max_redirects: int = MAX_HTTP_REDIRECTS

    def _set_request_state(
        self, deadline: float, max_redirects: int,
    ) -> None:
        self._deadline_monotonic = deadline
        self._max_redirects = max_redirects
        # Stdlib counts redirects via the per-Request `redirect_dict`
        # attribute, but it caps at `max_repeats`/`max_redirections`
        # class attrs. Override the class attrs so our cap takes effect.
        self.max_repeats = max_redirects
        self.max_redirections = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Wall-clock check FIRST so a slow-redirect chain bails before
        # the next socket open. Use a distinct exception class so the
        # executor routes this to ``timed_out`` rather than the generic
        # URLError → ``fail`` bucket.
        if (
            self._deadline_monotonic is not None
            and time.monotonic() > self._deadline_monotonic
        ):
            raise _HttpDeadlineExceeded(
                "http executor wall-clock deadline exceeded "
                "during redirect chain"
            )
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme not in ("http", "https"):
            raise urllib.error.URLError(
                f"http executor refusing redirect to non-http(s) "
                f"scheme: {newurl!r}"
            )
        return super().redirect_request(
            req, fp, code, msg, headers, newurl,
        )


# ---------------------------------------------------------------------------
# T8 — evaluate_criterion routing constants + enum
# ---------------------------------------------------------------------------


class EvalDecision(str, enum.Enum):
    """Routing verdict for ``AcceptanceRunner.evaluate_criterion`` (T8).

    Consumed by orchestrator dispatch (T13 codex review whitelist + T15 gate
    harness loop): the runner produces the verdict, the orchestrator decides
    what to do with it (re-run, block, escalate to user).

    - ``PASS``: criterion satisfied, continue.
    - ``LOCAL_FIX_ALLOWED``: Phase 2 retry whitelist applies (R1).
    - ``BLOCK_ROW5``: §1 row 5 — regular block (no escalate-choice menu).
    - ``BLOCKED_ESCALATE_ROW6``: §1 row 6 — escalate menu
      ``{abort, interactive, split}``.
    - ``INCONCLUSIVE``: T9 resume / contract bug; deferred verdict.
    """

    PASS = "pass"
    LOCAL_FIX_ALLOWED = "local_fix_allowed"
    BLOCK_ROW5 = "block_row5"
    BLOCKED_ESCALATE_ROW6 = "blocked_escalate_row6"
    INCONCLUSIVE = "inconclusive"


# Per design §3 line 130–131 + plan §8.2: types that allow Phase 2 local fix.
# NOTE (B-blindspot — design vs plan): design §3 line 139–140 defines the
# whitelist as "gates 4 + 5-unit/integration/behavior" only. The plan
# (most recent codex-approved artifact) extends to ``smoke`` as well — this
# T8 implementation follows the plan; the discrepancy is flagged for review.
PHASE2_LOCAL_FIX_TYPES = frozenset({"unit", "integration", "behavior", "smoke"})
# Phase 3 types that NEVER local-fix (R2 — PRD §1.3): behavior + e2e + regression.
PHASE3_NEVER_LOCAL_TYPES = frozenset({"behavior", "e2e", "regression"})
# E2E always escalates regardless of phase (PRD §1.3 + Y1).
ALWAYS_ESCALATE_TYPES = frozenset({"e2e"})


@dataclass
class ResumePoint:
    """T9 resume state from tail-scanning ``acceptance-progress.jsonl``.

    Returned by :meth:`AcceptanceRunner.find_resume_point`. The
    orchestrator uses it to decide where to restart the criteria loop
    after an auto-mode crash (per design §6 Q6.1 + Y8).

    Attributes:
      ``next_idx``: criterion_idx to start fresh execution at —
        ``max(criterion_idx with completed/timeout) + 1`` within the
        attempt, or ``0`` if no criteria completed.
      ``in_flight_criterion_idx``: idx of a criterion whose ``started``
        event has no matching ``completed`` / ``timeout`` (the
        orchestrator crashed mid-run). ``None`` means every started
        was paired — no in-flight recovery needed.
      ``in_flight_event``: the raw started-event dict (so callers can
        inspect timing / hash / log paths for the blocked.md body).
        ``None`` iff ``in_flight_criterion_idx`` is ``None``.
    """
    next_idx: int
    in_flight_criterion_idx: Optional[int]
    in_flight_event: Optional[dict]


@dataclass
class IdempotencyVerdict:
    """T9 R8 hardened decision: auto-rerun vs block on an in-flight
    interrupted criterion.

    Returned by :meth:`AcceptanceRunner.resolve_in_flight_idempotency`.
    Orchestrator dispatch (T19 wires this into the resume path):

      - ``decision == "auto_rerun"``: the criterion is safe to re-run
        (read-only method / GET http / cmd in allowlist / cmd with a
        per-criterion ``idempotent.{value=True, rationale=...}``
        override). Orchestrator re-runs ``in_flight_criterion_idx``,
        then proceeds.
      - ``decision == "block_in_flight"``: re-running may double a
        side effect. Orchestrator writes ``blocked.md`` per §1 row 5
        with ``reason`` in the body and surfaces the in-flight
        criterion to the operator.

    ``reason`` is a single human-readable line — render it as-is into
    the blocked.md body. Format kept stable so downstream tooling
    (issue templates, dashboards) can pattern-match.
    """
    decision: str   # "auto_rerun" | "block_in_flight"
    reason: str


@dataclass
class RunResult:
    """Per-criterion executor return shape.

    ``status``:
      - ``pass``: criterion satisfied (rc==0 / 2xx HTTP / file present /
        json query produced truthy leaf).
      - ``fail``: criterion not satisfied (rc!=0 / non-2xx HTTP / file
        missing / json leaf falsy or path missing). For HTTP, also covers
        connection-refused / DNS-failure (server unreachable IS a verdict).
      - ``inconclusive``: tool/data couldn't produce a verdict (subprocess
        failed to spawn at OS layer; JSON file structurally invalid;
        required field missing on the criterion). T8 will route inconclusive
        differently from fail.
      - ``timed_out``: subprocess killed by timeout / urlopen socket timed
        out. Distinct from ``fail`` because Y1 escalation depends on the
        type+timeout combination.

    ``escalate``: Y1 — set True by `run_one` when ``criterion.type == "e2e"``
    AND status ∈ {timed_out, fail}. Method-level executors leave this False;
    only the orchestration shell knows the type.
    """
    status: str
    exit_code: Optional[int] = None
    duration_ms: int = 0
    stdout_log_path: Optional[str] = None
    stderr_log_path: Optional[str] = None
    command_hash: Optional[str] = None
    escalate: bool = False
    error_msg: Optional[str] = None


class AcceptanceRunner:
    """Per-criterion executor. T7 ships method dispatch + timeout + default
    idempotency. T8 wires Phase 2/3 retry routing. T9 wires resume +
    override classification.
    """

    def __init__(
        self,
        *,
        worktree_root: Path,
        log_dir: Path,
        slug: str,
        task_id: str,
        run_id: str,
        worktree_id: str,
    ):
        self.worktree_root = Path(worktree_root)
        self.log_dir = Path(log_dir)
        self.slug = slug
        self.task_id = task_id
        self.run_id = run_id
        self.worktree_id = worktree_id
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Cross-cutting helpers
    # ------------------------------------------------------------------

    def _effective_timeout(self, criterion: AcceptanceCriterion) -> int:
        """Resolve the timeout to apply.

        Precedence (R7):
          1. Explicit `criterion.timeout_sec` if it's a positive int.
          2. ``type=e2e`` override → 1800.
          3. Per-method default from ``DEFAULT_TIMEOUT_BY_METHOD``.

        T1's parser injects the per-method default at parse time, so a
        contract-loaded criterion always has a positive ``timeout_sec``.
        Tests that build criteria directly may pass ``None`` (or rely on
        the dataclass default ``0``); we treat both as "absent" here.

        The ``timeout_sec is None`` branch + ``<= 0`` guard is intentional
        and NOT a `.get()`-style A-blindspot bypass: we are NOT reading
        from a dict; we are normalizing a typed ``Optional[int]`` whose
        only valid runtime values are positive ints (T1 contract) or
        ``None``/``0`` (direct-construction sentinel). The dataclass field
        defaults to ``0`` for ordering reasons; reading 0 means "absent".
        """
        ts = getattr(criterion, "timeout_sec", None)
        if isinstance(ts, bool) or not isinstance(ts, int) or ts <= 0:
            ts = None
        if ts is not None:
            return int(ts)
        if criterion.type == "e2e":
            return E2E_TYPE_TIMEOUT
        # Method-default lookup — a method outside the R7 table is a contract
        # parser bug (T1 enforces VALID_CRITERION_METHODS); fall back to 60s
        # rather than raise so a test that forgets to set method doesn't
        # crash here. Production never hits this branch.
        return DEFAULT_TIMEOUT_BY_METHOD.get(criterion.method, 60)

    def default_idempotency(self, criterion: AcceptanceCriterion) -> str:
        """R8 default-idempotency table (NO override resolution — T9 owns).

        Returns one of ``{"true", "false", "unknown"}`` (the schema string
        for the ``acceptance-progress.jsonl`` ``idempotent`` field — see
        flow_state_writer.VALID_PROGRESS_IDEMPOTENT).

        Rules (per design line 270–275):
          - ``type == "e2e"`` → ``"false"`` (always; design line 275 says
            NO override accepted).
          - ``method`` ∈ {file_exists, json_query} → ``"true"`` (read-only).
          - ``method == "cmd"`` → ``"false"`` (R8 hardened; T9 layers
            ``idempotent_cmd_allowlist`` + per-criterion override).
          - ``method == "http"`` (GET only in v0.8.1) → ``"true"``.
            Forward-compat: when v0.8.2 adds ``http_method``, this returns
            ``"false"`` for POST/PUT/PATCH/DELETE (RFC 7231).

        Anything that falls outside the table returns ``"unknown"`` — defense-
        in-depth, since T1 rejects unknown methods at parse time.
        """
        # B-cross-ref: e2e check FIRST so an e2e+file_exists criterion still
        # comes out "false" (design line 275 — type wins over method).
        if criterion.type == "e2e":
            return "false"
        if criterion.method in ("file_exists", "json_query"):
            return "true"
        if criterion.method == "cmd":
            return "false"
        if criterion.method == "http":
            return "true"
        return "unknown"

    # ------------------------------------------------------------------
    # T8 — Phase 2 / Phase 3 retry-routing decision
    # ------------------------------------------------------------------

    def evaluate_criterion(
        self,
        criterion: AcceptanceCriterion,
        *,
        phase: int,
        runner_result: RunResult,
    ) -> EvalDecision:
        """Decide the next routing step for a finished criterion run.

        Inputs:
          - ``criterion``: the AcceptanceCriterion (gives us ``type``).
          - ``phase``: 2 = task execution in worktree (gates 5 + 6);
            3 = post-merge verification (gate 8).
          - ``runner_result``: the :class:`RunResult` produced by ``run_one``
            (or, in tests, hand-built). ``status`` ∈ ``{pass, fail,
            timed_out, inconclusive, interrupted}``; ``escalate`` is the Y1
            flag set by ``run_one`` for ``type=e2e`` failures.

        Output: :class:`EvalDecision` per the design §3 line 130–137 + Y1
        decision matrix:

        =============  ===========================  ===========================
        type           Phase 2                      Phase 3
        =============  ===========================  ===========================
        unit           pass→PASS / fail→LOCAL /     pass→PASS / fail→BLOCK_ROW5
                       timeout→BLOCK_ROW5           / timeout→BLOCK_ROW5
        integration    as unit                      as unit
        behavior       as unit (R1)                 fail/timeout→ESCALATE (R2)
        smoke          as unit (R1, plan extends)   as unit
        e2e            ESCALATE (Y1, always)        ESCALATE (always)
        regression     fail/timeout→BLOCK_ROW5      fail/timeout→ESCALATE (R2)
        =============  ===========================  ===========================

        Phase 2 regression deviates from design §3 line 134 ("always
        escalate") — the plan matrix pins it to BLOCK_ROW5 and we follow
        the plan; T13 owns the design alignment. Phase 3 regression
        matches the constant ``PHASE3_NEVER_LOCAL_TYPES`` and design §3
        line 134 + R2.

        Routing-order rationale (C-blindspot — order matters; codex
        round-1 caught the prior mis-ordering where Phase 3 regression
        was silently masked by the regression catch-all):
          1. ``status == "pass"`` short-circuits regardless of phase/type.
          2. ``inconclusive`` short-circuits to ``INCONCLUSIVE`` (T9 will
             distinguish "tool broke" from "criterion bug").
          3. ``type == "e2e"`` OR ``runner_result.escalate=True`` route to
             escalate BEFORE regression / phase-2-whitelist branches —
             otherwise an e2e criterion authored as ``type=regression``
             would route wrong, and a future executor that sets
             ``escalate=True`` for non-e2e types would be ignored.
          4. Phase 3 ∈ {behavior, regression} with status ∈ {fail,
             timed_out} → escalate (R2; e2e already handled above).
             MUST precede the regression catch-all so Phase 3 regression
             honors PHASE3_NEVER_LOCAL_TYPES. ``interrupted`` is excluded
             so it falls to the D5 catch-all (codex round-2).
          5. Phase 2 ``regression`` → BLOCK_ROW5 (plan matrix; design
             deviation tracked for T13).
          6. Phase 2 fail in whitelist → LOCAL_FIX_ALLOWED.
          7. Catch-all → BLOCK_ROW5 (D5 defense-in-depth: timeout for any
             non-e2e type / non-whitelisted Phase 2 fail / Phase 3 unit fail).

        D5 catch-all: the final ``return EvalDecision.BLOCK_ROW5`` is the
        safety net for any combination not explicitly handled above. It is
        load-bearing — do not promote to a raise (an unrouted verdict
        would crash the orchestrator gate-loop) and do not remove (silent
        fall-through would be a null verdict). All paths through the
        function return a member of :class:`EvalDecision`.
        """
        # D5: ``phase`` is the contract boundary — a misuse from the
        # orchestrator must surface immediately, not be silently routed.
        if phase not in (2, 3):
            raise ValueError(f"phase must be 2 or 3, got {phase!r}")

        status = runner_result.status

        # 1. Pass propagates regardless of type/phase.
        if status == "pass":
            return EvalDecision.PASS

        # 2. inconclusive: deferred to T9 (resume / contract bug discrimination).
        if status == "inconclusive":
            return EvalDecision.INCONCLUSIVE

        # 3. Y1 + always-escalate: e2e (or any executor-flagged escalate)
        # always routes to row 6 regardless of phase. MUST run before the
        # regression / whitelist branches — if the contract author put e2e
        # under type=regression by mistake we still honor the escalate flag.
        if (
            criterion.type in ALWAYS_ESCALATE_TYPES
            or runner_result.escalate
        ):
            return EvalDecision.BLOCKED_ESCALATE_ROW6

        # 4. Phase 3 R2: never-local types (behavior, regression — e2e
        # already handled above) escalate post-merge. MUST precede the
        # regression catch-all below; otherwise Phase 3 regression would
        # be intercepted before reaching this branch and PHASE3_NEVER_LOCAL_TYPES
        # would silently lie about regression coverage. (Codex round-1 found this
        # C-blindspot ordering issue; pitfall claude-review-blindspots.md C-class.)
        #
        # Status guard (codex round-2): R2 documents fail/timeout escalation
        # only. ``interrupted`` (signal / orchestrator crash mid-run) means we
        # don't know what happened — it falls through to the D5 catch-all so
        # it surfaces as a regular row-5 block, matching the catch-all
        # docstring contract. Without this guard, Phase 3 behavior+regression
        # interrupted would silently promote to the escalate menu.
        if (
            phase == 3
            and criterion.type in PHASE3_NEVER_LOCAL_TYPES
            and status in ("fail", "timed_out")
        ):
            return EvalDecision.BLOCKED_ESCALATE_ROW6

        # 5. Phase 2 regression: never local. Plan matrix pins this to
        # BLOCK_ROW5; design §3 line 134 reads "always escalate" without a
        # phase qualifier, suggesting row 6 — flagged as a T13 design
        # alignment follow-up, not corrected here. Phase 3 regression already
        # routes to row 6 via the branch above.
        if criterion.type == "regression":
            return EvalDecision.BLOCK_ROW5

        # 6. Phase 2 R1 retry whitelist: fail (NOT timeout) for whitelisted
        # types → orchestrator may attempt local fix + retry. Timeout falls
        # through to the catch-all (BLOCK_ROW5) per design line 279 (timeout
        # is treated as criterion fail; retry budget shouldn't burn on what
        # may be an environmental issue — extend_timeout_and_retry is the
        # operator-driven path).
        #
        # A-blindspot: ``criterion.type`` is a typed dataclass attribute, so
        # we use ``in PHASE2_LOCAL_FIX_TYPES`` (set membership) — NOT
        # ``criterion.type or "unit"``. Implicit defaults here would silently
        # accept malformed contracts; T1's parser already rejects empty types.
        if (
            phase == 2
            and status == "fail"
            and criterion.type in PHASE2_LOCAL_FIX_TYPES
        ):
            return EvalDecision.LOCAL_FIX_ALLOWED

        # 7. D5 catch-all: timeout (any type) / non-whitelisted Phase 2 fail /
        # Phase 3 unit-or-integration fail / interrupted. Block on §1 row 5
        # so the orchestrator surfaces a regular block (no escalate menu).
        return EvalDecision.BLOCK_ROW5

    def _resolve_within_worktree(
        self, rel_path: str, field_name: str,
    ) -> Tuple[Optional[Path], Optional[str]]:
        """Normalize a criterion-supplied path to a worktree-rooted absolute
        path, or refuse it as malformed.

        Returns ``(path, None)`` on success; ``(None, error_msg)`` if the
        path resolves outside the worktree (absolute path or ``..`` traversal).

        SAFETY-BOUNDARY: file_exists + json_query both resolve criterion
        paths against ``self.worktree_root``. Stdlib's ``Path /`` operator
        treats absolute right-hand operands as a REPLACEMENT (so
        ``worktree / "/etc/passwd"`` becomes ``/etc/passwd``); ``..``
        traversal segments aren't blocked by ``Path``. Both routes let a
        malformed contract poke at arbitrary FS paths. We resolve the
        candidate to an absolute path, then verify it's a descendant of
        the resolved worktree root via ``Path.relative_to``. Anything
        else is treated as ``inconclusive`` (the contract author needs to
        fix the path) — NOT ``fail`` (which would be a verdict on a
        legitimate criterion).
        """
        # SAFETY-BOUNDARY (codex R2 [P2]): ``Path.resolve()`` can raise
        # ``RuntimeError`` ("Symlink loop") on some platforms, or
        # ``OSError`` (ELOOP) on others, when the candidate path traverses
        # a symlink cycle. Don't crash — return inconclusive with an
        # operator-readable message. We also wrap the worktree-root
        # resolve since a poisoned worktree root (e.g. a symlink loop in
        # a parent dir) would otherwise crash the runner before we even
        # got to the relative_to check.
        try:
            candidate = (self.worktree_root / rel_path).resolve(strict=False)
            root = self.worktree_root.resolve(strict=False)
        except (OSError, RuntimeError) as e:
            return None, (
                f"{field_name}={rel_path!r} could not be resolved "
                f"(symlink loop or permission error): {e}"
            )
        try:
            candidate.relative_to(root)
        except ValueError:
            return None, (
                f"{field_name}={rel_path!r} resolves to {candidate} which "
                f"is outside worktree {root}; refuse as malformed contract."
            )
        return candidate, None

    # ------------------------------------------------------------------
    # Method executors
    # ------------------------------------------------------------------

    def _run_cmd(self, criterion: AcceptanceCriterion) -> RunResult:
        """``Popen`` with process-group kill on timeout, stdout/stderr to logs.

        Validation (C-ordering): missing/empty ``command`` → inconclusive
        BEFORE we open any file or spawn anything. Then capture command_hash
        BEFORE the subprocess runs (so even on TimeoutExpired we can still
        report the hash for audit).

        SAFETY-BOUNDARY (codex R1 [P1]): ``shell=True`` means the child IS
        the shell, not the user's command. With ``subprocess.run(timeout=N)``
        the timeout only kills that shell — descendants (``&``-backgrounded
        subshells, forked test runners, server processes) keep running. The
        criterion would report ``timed_out`` while real side effects
        continue, defeating the per-criterion timeout safety boundary.

        Fix: ``Popen(..., start_new_session=True)`` puts the child in its
        own process group (PGID == child PID). On ``TimeoutExpired`` we
        ``os.killpg(SIGTERM)`` the WHOLE group, drain stdout/stderr for
        ``PROCESS_GROUP_KILL_GRACE_SEC`` seconds, then escalate to
        ``SIGKILL`` if anything is still alive. This makes ``timed_out``
        actually mean "the criterion's process tree is dead" — what the
        boundary promises.

        Note: ``start_new_session=True`` (== ``setsid``) is POSIX. On
        Windows you'd want ``CREATE_NEW_PROCESS_GROUP`` + ``CTRL_BREAK``.
        Flow framework targets POSIX (Linux/macOS); Windows path can
        layer on later if needed.

        D2-distinctions:
          - ``TimeoutExpired`` (process group killed) → ``timed_out``,
            ``exit_code=None`` (the tree was killed; no clean rc).
          - ``OSError`` / ``FileNotFoundError`` (Popen itself failed:
            resource exhaustion, log file open failure) → ``inconclusive``.
        D3-distinctions:
          - rc==0 → ``pass``; rc!=0 → ``fail``. With ``shell=True``, rc=127
            means "command not found" — the shell's verdict; we treat it as
            a deterministic ``fail`` (the criterion's command is the thing
            being asserted; if it can't run, the assertion is unsatisfied).
        """
        if not criterion.command:
            # A-aware: ``not criterion.command`` covers None AND ""; both are
            # invalid for cmd. T1 rejects "" at parse time, but defensive
            # check here keeps direct-construction tests honest.
            return RunResult(
                status="inconclusive",
                error_msg="cmd method requires non-empty `command` field",
            )
        timeout = self._effective_timeout(criterion)
        command_hash = hashlib.sha256(
            criterion.command.encode("utf-8")
        ).hexdigest()
        # Per-run unique log paths so concurrent runs of the same criterion
        # don't clobber each other's stdout/stderr.
        suffix = uuid.uuid4().hex[:8]
        stdout_path = self.log_dir / f"{self.task_id}_{suffix}.stdout"
        stderr_path = self.log_dir / f"{self.task_id}_{suffix}.stderr"
        t0 = time.monotonic()
        try:
            out_fh = stdout_path.open("w")
            err_fh = stderr_path.open("w")
        except OSError as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            return RunResult(
                status="inconclusive",
                exit_code=None,
                duration_ms=duration_ms,
                stdout_log_path=str(stdout_path) if stdout_path.exists()
                else None,
                stderr_log_path=str(stderr_path) if stderr_path.exists()
                else None,
                command_hash=command_hash,
                error_msg=f"cmd OS error opening log files: {e}",
            )
        try:
            try:
                proc = subprocess.Popen(
                    criterion.command,
                    shell=True,
                    cwd=str(self.worktree_root),
                    stdout=out_fh,
                    stderr=err_fh,
                    # POSIX: new session => new process group. Lets us
                    # killpg the entire descendant tree on timeout.
                    start_new_session=True,
                )
            except OSError as e:
                # D2: spawn-time failure (resource exhaustion, fork failure).
                # Mark inconclusive — no verdict on the criterion.
                duration_ms = int((time.monotonic() - t0) * 1000)
                return RunResult(
                    status="inconclusive",
                    exit_code=None,
                    duration_ms=duration_ms,
                    stdout_log_path=str(stdout_path),
                    stderr_log_path=str(stderr_path),
                    command_hash=command_hash,
                    error_msg=f"cmd OS error before/during spawn: {e}",
                )
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # SAFETY-BOUNDARY: kill the WHOLE process group, not just
                # the shell. Two-stage kill: SIGTERM + drain + SIGKILL.
                self._kill_process_group(proc)
                # Reap the shell so we don't leave a zombie. The group
                # kill above guarantees the tree is dead; this just
                # collects the rc. Bounded wait — group is already dead
                # so this returns near-instantly, but we cap to avoid
                # hanging forever on a wedged kernel state.
                try:
                    proc.wait(timeout=PROCESS_GROUP_KILL_GRACE_SEC)
                except subprocess.TimeoutExpired:
                    pass
                duration_ms = int((time.monotonic() - t0) * 1000)
                return RunResult(
                    status="timed_out",
                    exit_code=None,
                    duration_ms=duration_ms,
                    stdout_log_path=str(stdout_path),
                    stderr_log_path=str(stderr_path),
                    command_hash=command_hash,
                    error_msg=f"cmd exceeded timeout_sec={timeout}",
                )
            duration_ms = int((time.monotonic() - t0) * 1000)
            return RunResult(
                status="pass" if returncode == 0 else "fail",
                exit_code=returncode,
                duration_ms=duration_ms,
                stdout_log_path=str(stdout_path),
                stderr_log_path=str(stderr_path),
                command_hash=command_hash,
            )
        finally:
            # Always close the log file handles. Popen kept its own fds via
            # dup2 on spawn, so closing here doesn't truncate the child's
            # output; it just releases the parent-side handle.
            for fh in (out_fh, err_fh):
                try:
                    fh.close()
                except OSError:
                    pass

    @staticmethod
    def _kill_process_group(proc: "subprocess.Popen[bytes]") -> None:
        """Kill every process in the child's group; defense-in-depth SIGKILL.

        ``proc.pid`` IS the process group leader (because we spawned with
        ``start_new_session=True``). ``os.killpg`` delivers the signal to
        every process in that group — the shell, every direct child, every
        ``&``-backgrounded grandchild that hasn't called setsid itself.

        SAFETY-BOUNDARY (codex R2 [P1]): we cannot rely on
        ``proc.wait(timeout=...)`` to confirm the group is dead — that only
        observes the shell. A child that ``trap`` 's SIGTERM and lets the
        shell exit cleanly will pass ``proc.wait()`` while the trapped child
        keeps running. Instead, probe the *group* via ``killpg(pgid, 0)``
        which raises ``ProcessLookupError`` only when ALL members are gone.
        After the grace window we ALWAYS send SIGKILL to the group as
        belt-and-suspenders — there is no reason to be gentle once
        ``timeout_sec`` has elapsed. The criterion already failed; we just
        need the tree dead.

        Sequence:
          1. SIGTERM the group (graceful request).
          2. Up to ``PROCESS_GROUP_KILL_GRACE_SEC`` poll: probe the group
             every 50ms via ``killpg(pgid, 0)``. Return as soon as the
             whole group is gone (ProcessLookupError on the probe).
          3. SIGKILL the group regardless. If everyone is already dead
             we'll get ProcessLookupError (benign); if anything is still
             alive (SIGTERM-trapping child, ignored signal, etc.) it dies
             here. Either way we exit knowing the tree is gone.

        ``ProcessLookupError`` is benign at every step — the process or
        group was already dead (race between TimeoutExpired and natural
        exit, or our SIGTERM landed and the OS reaped before we probed).
        """
        pgid = proc.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return  # whole group already gone before SIGTERM landed
        except OSError:
            # Permission / EINVAL — extremely unusual; fall through to
            # the SIGKILL stage anyway.
            pass
        deadline = time.monotonic() + PROCESS_GROUP_KILL_GRACE_SEC
        while time.monotonic() < deadline:
            try:
                # Probe the GROUP, not the shell. signal 0 doesn't deliver
                # anything; ProcessLookupError fires only when no process
                # in the group is still alive.
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return  # whole group drained gracefully
            except OSError:
                # Defensive: some platforms raise EPERM if any group member
                # exists we can't signal. Treat as "still alive" and keep
                # polling until the SIGKILL stage.
                pass
            time.sleep(0.05)
        # Defense-in-depth: SIGKILL the group regardless of what the
        # SIGTERM stage saw. Ignored-SIGTERM children die here.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # raced — group died between probe and SIGKILL; fine
        except OSError:
            pass

    def _run_file_exists(self, criterion: AcceptanceCriterion) -> RunResult:
        """File-presence check rooted at ``self.worktree_root``.

        ``Path.is_file()`` is True only for regular files; directories,
        broken symlinks, sockets, etc. all read as missing. This matches
        the spec semantic of "the artifact exists on disk".

        No timeout enforcement is needed — `is_file()` is a single stat()
        call. The R7 default of 30s is a safety upper bound; if a stat
        somehow blocks for 30s the FS is in trouble and an inconclusive
        verdict would be the honest read, but stdlib gives us no way to
        time-bound a stat call without a thread, which is overkill.
        """
        if not criterion.path:
            return RunResult(
                status="inconclusive",
                error_msg="file_exists method requires non-empty `path` field",
            )
        # SAFETY-BOUNDARY (codex R1 [P2]): block absolute paths and ``..``
        # traversal. Stdlib's ``Path /`` operator treats absolute RHS as a
        # replacement, so ``self.worktree_root / "/etc/passwd"`` => /etc/passwd.
        target, err = self._resolve_within_worktree(
            criterion.path, "file_exists path",
        )
        if target is None:
            return RunResult(status="inconclusive", error_msg=err)
        t0 = time.monotonic()
        try:
            exists = target.is_file()
        except OSError as e:
            # D2: stat() can fail with EACCES on a parent dir we can't
            # traverse, ELOOP on a symlink loop, etc. Mark inconclusive —
            # the tool (the FS) didn't give us a clean verdict.
            duration_ms = int((time.monotonic() - t0) * 1000)
            return RunResult(
                status="inconclusive",
                duration_ms=duration_ms,
                error_msg=f"file_exists OS error: {e}",
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
        return RunResult(
            status="pass" if exists else "fail",
            duration_ms=duration_ms,
        )

    def _run_json_query(self, criterion: AcceptanceCriterion) -> RunResult:
        """Load JSON from ``path`` (rooted at worktree), traverse the dotted
        ``json_query``, ``pass`` if the leaf is truthy, ``fail`` if falsy or
        the path is missing partway through, ``inconclusive`` if the file is
        missing or unparseable.

        Dotted-path resolver only — supports ``a.b.c`` (no ``[idx]``,
        no jsonpath). Forward-compat: if users ask for jsonpath in v0.8.2,
        we layer it on. Each segment is a dict-key lookup.

        D4-aware: the resolver iterates EVERY part of the dotted path before
        producing a verdict. There is no early "found" return that could
        skip later segments.
        """
        if not criterion.path or not criterion.json_query:
            return RunResult(
                status="inconclusive",
                error_msg=("json_query method requires non-empty `path` "
                           "+ `json_query` fields"),
            )
        # SAFETY-BOUNDARY (codex R1 [P2]): same path-containment fix as
        # file_exists — refuse anything that escapes the worktree.
        target, err = self._resolve_within_worktree(
            criterion.path, "json_query path",
        )
        if target is None:
            return RunResult(status="inconclusive", error_msg=err)
        # Existence check BEFORE read so a missing file is inconclusive,
        # not an OSError surprise. C-ordering: validate, then act.
        if not target.is_file():
            return RunResult(
                status="inconclusive",
                error_msg=f"json file not found: {target}",
            )
        # SAFETY-BOUNDARY (codex R2 [P2]): collapse stat() + read_text()
        # into a single open-and-read with a hard ceiling. The previous
        # stat-then-read pattern had a TOCTOU race — between
        # ``target.stat()`` and ``read_text()``, the file could grow or
        # be replaced, leaving the read unbounded. Read up to
        # MAX_JSON_QUERY_FILE_BYTES + 1 bytes; if the read returned more
        # than the cap, the file exceeds the limit regardless of what
        # stat would have said. This also avoids relying on stat() at
        # all, which on some filesystems (sparse files, network mounts)
        # can be misleading.
        t0 = time.monotonic()
        try:
            with target.open("rb") as fh:
                data = fh.read(MAX_JSON_QUERY_FILE_BYTES + 1)
        except OSError as e:
            return RunResult(
                status="inconclusive",
                duration_ms=int((time.monotonic() - t0) * 1000),
                error_msg=f"json read OS error: {e}",
            )
        if len(data) > MAX_JSON_QUERY_FILE_BYTES:
            return RunResult(
                status="inconclusive",
                duration_ms=int((time.monotonic() - t0) * 1000),
                error_msg=(
                    f"json_query file size exceeds cap "
                    f"{MAX_JSON_QUERY_FILE_BYTES} bytes (path={target}); "
                    f"refuse to materialize."
                ),
            )
        try:
            obj = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as e:
            return RunResult(
                status="inconclusive",
                duration_ms=int((time.monotonic() - t0) * 1000),
                error_msg=f"json parse error: {e}",
            )
        except UnicodeDecodeError as e:
            return RunResult(
                status="inconclusive",
                duration_ms=int((time.monotonic() - t0) * 1000),
                error_msg=f"json utf-8 decode error: {e}",
            )
        # Traverse the dotted path. A missing intermediate key is a fail
        # (the data shape doesn't match the contract author's expectation),
        # NOT inconclusive — the file parsed cleanly.
        val: object = obj
        for part in criterion.json_query.split("."):
            if isinstance(val, dict) and part in val:
                val = val[part]
            else:
                return RunResult(
                    status="fail",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    error_msg=f"json_query path missing at segment: {part!r}",
                )
        duration_ms = int((time.monotonic() - t0) * 1000)
        # Leaf truthiness IS the verdict — None / False / 0 / "" / [] / {} all
        # fail the criterion. This matches "the value at the path is set" as
        # the test author's likely intent. A-aware: this `if val:` is
        # intentional truthiness on a JSON value, NOT a `.get()`-style
        # absence-vs-falsy bypass — the absence case was already handled
        # above (the "path missing" branch returns fail).
        return RunResult(
            status="pass" if val else "fail",
            duration_ms=duration_ms,
        )

    def _run_http(self, criterion: AcceptanceCriterion) -> RunResult:
        """``urllib`` GET with timeout. 2xx → ``pass``; non-2xx → ``fail``;
        connection-refused / DNS-failure → ``fail`` (server unreachable IS
        the verdict per design); socket-timeout → ``timed_out``.

        D2/D3 distinctions:
          - ``HTTPError`` is a subclass of ``URLError``; catch it FIRST so
            we capture ``e.code`` for ``exit_code``.
          - Bare ``TimeoutError`` (Python 3.10+ may surface this directly
            from urlopen on a socket timeout — different from URLError
            wrapping a ``socket.timeout``). Both routed to ``timed_out``.
          - ``URLError`` whose ``.reason`` is ``socket.timeout`` (legacy
            wrapping) → ``timed_out``.
          - All other ``URLError`` (ConnectionRefusedError / gaierror /
            etc.) → ``fail`` per design (server unreachable verdict).
          - ``OSError`` is a parent of many of the above; we intentionally
            list ``URLError`` first and let plain OSError fall through to
            a final ``fail`` branch — but in practice every connection
            error already inherits via URLError in stdlib paths.
        """
        if not criterion.url:
            return RunResult(
                status="inconclusive",
                error_msg="http method requires non-empty `url` field",
            )
        # Reject non-http(s) schemes BEFORE handing the URL to urllib.
        # `urllib.request.Request` happily accepts ``file://``, ``ftp://``,
        # and any custom-handler scheme — a contract with
        # ``method: "http"`` + ``url: "file:///etc/passwd"`` would return
        # ``pass`` if the file exists, defeating the executor's stated
        # contract (network probe, not arbitrary I/O). Treat as
        # ``inconclusive`` (criterion malformed) rather than ``fail``
        # (verdict): the contract author needs to fix the URL.
        # Codex T7 R4 [P2]: urlsplit() raises ValueError on syntactically
        # malformed URLs (`http://[::1`, IPv6 with no closing bracket, etc.)
        # — that exception fires BEFORE the worker thread's safety net,
        # which would orphan the `started` event in run_one. Catch here
        # and route to inconclusive so run_one always paints a paired
        # completed event.
        try:
            parsed = urllib.parse.urlsplit(criterion.url)
        except ValueError as e:
            return RunResult(
                status="inconclusive",
                error_msg=(
                    f"http method received malformed URL "
                    f"(url={criterion.url!r}): {e}. Reject as malformed "
                    f"contract — fix the URL before re-running."
                ),
            )
        if parsed.scheme not in ("http", "https"):
            return RunResult(
                status="inconclusive",
                error_msg=(
                    f"http method requires http(s) URL scheme, got "
                    f"{parsed.scheme!r} (url={criterion.url!r}). Reject as "
                    f"malformed contract — refuse non-network access via "
                    f"the http executor."
                ),
            )
        timeout = self._effective_timeout(criterion)
        command_hash = hashlib.sha256(
            f"GET {criterion.url}".encode("utf-8")
        ).hexdigest()
        t0 = time.monotonic()
        # SAFETY-BOUNDARY (codex R1 [P2]):
        #   (a) The default ``HTTPRedirectHandler`` follows ALL schemes,
        #       including ``ftp://`` and (in some stdlib versions) ``file://``
        #       — bypasses the initial-URL scheme check.
        #   (b) ``urlopen(timeout=N)`` is per-socket-op, not total. A slow
        #       redirect chain stretches well past ``timeout_sec``.
        # Fix: custom redirect handler that re-validates scheme on every
        # target AND checks a wall-clock deadline computed at request start.
        # We also cap redirects at ``MAX_HTTP_REDIRECTS`` (=1) — most legit
        # APIs don't redirect-chain.
        deadline = time.monotonic() + timeout
        redirect_handler = _SchemeValidatingRedirectHandler()
        redirect_handler._set_request_state(deadline, MAX_HTTP_REDIRECTS)
        opener = urllib.request.build_opener(redirect_handler)
        req = urllib.request.Request(criterion.url, method="GET")

        # SAFETY-BOUNDARY (codex R3 [P1]): per-socket-op timeout is NOT a
        # wall-clock guarantee. A trickle attack (server sends 1 byte every
        # ``timeout - 1`` seconds) keeps each individual recv under the
        # socket timeout while total wall-clock blows up unboundedly. The
        # post-call ``time.monotonic() > deadline`` check is too late: the
        # call already blocked.
        #
        # Run the entire blocking pipeline (urlopen + body drain) in a
        # daemon worker thread. The main thread waits via
        # ``Thread.join(timeout=timeout_sec)``. If join times out we
        # return ``timed_out`` immediately — the worker becomes a brief
        # orphan (eventually unblocked by socket timeouts / GC, dies on
        # interpreter exit because daemon=True). That orphan-thread
        # trade-off is acceptable because the boundary's promise is
        # "the CRITERION is wall-clock bounded", not "no thread ever
        # lingers under server-side abuse". Body drain runs INSIDE the
        # worker so it shares the same wall-clock guard.
        result_q: "queue_mod.Queue[Tuple[object, ...]]" = queue_mod.Queue(
            maxsize=1,
        )

        def _http_worker() -> None:
            try:
                with opener.open(req, timeout=timeout) as resp:
                    status_code = resp.status
                    # Body drain inside the worker so its read is also
                    # under the same wall-clock guard. STATUS-ALREADY-
                    # DETERMINED: the verdict is captured above; an
                    # OSError mid-drain cannot change pass/fail. Swallow
                    # silently — every other OSError path flows through
                    # the except branches below into a real verdict.
                    try:
                        resp.read(64)
                    except OSError:
                        pass
                    result_q.put(("ok", status_code))
            except _HttpDeadlineExceeded as e:
                # Redirect-chain wall-clock exhaustion (raised by the
                # custom handler). Route to timed_out — matches criterion
                # intent rather than generic URLError → fail.
                result_q.put(("timed_out", str(e)))
            except urllib.error.HTTPError as e:
                # D3: server replied with non-2xx. ``e.code`` is the
                # real status. Route to fail with code.
                result_q.put(("http_error", e.code, str(e)))
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
            ) as e:
                # D2: distinguish socket timeout from connection error.
                reason = getattr(e, "reason", None)
                is_timeout = (
                    isinstance(e, (TimeoutError, socket.timeout))
                    or isinstance(reason, (TimeoutError, socket.timeout))
                )
                if is_timeout:
                    result_q.put(("timed_out", str(e)))
                else:
                    result_q.put(("url_error", str(e)))
            except http.client.HTTPException as e:
                # SAFETY-BOUNDARY (codex R3 [P2]): urllib re-raises
                # ``http.client.HTTPException`` (RemoteDisconnected,
                # BadStatusLine, IncompleteRead, ...) WITHOUT wrapping
                # in URLError. The previous except-tuple let these
                # escape and crashed run_one (orphan started event).
                # Map to fail — the server response was malformed,
                # which from the criterion's POV IS a verdict (the
                # endpoint isn't behaving like an http endpoint).
                result_q.put((
                    "url_error",
                    f"{type(e).__name__}: {e}",
                ))
            except OSError as e:
                # Defense in depth: a few stdlib versions surface plain
                # ``OSError`` (ECONNRESET mid-headers) without a URLError
                # wrapper. Treat as a connection-level fail, same bucket
                # as URLError-not-timeout.
                result_q.put(("url_error", f"OSError: {e}"))
            except Exception as e:  # noqa: BLE001 — see comment
                # FINAL CATCH-ALL (codex R3 [P2]): defense-in-depth. If
                # a future urllib version raises something not in the
                # tuple above, ``run_one`` MUST still get a RunResult
                # so it emits the paired ``completed`` event after its
                # ``started`` event. Mark inconclusive (not fail) —
                # this is an executor-bug bucket, not a verdict on the
                # criterion. NOTE: we catch ``Exception``, not
                # ``BaseException`` — KeyboardInterrupt and SystemExit
                # must propagate so an operator's Ctrl-C still works.
                result_q.put((
                    "inconclusive",
                    f"unexpected http executor error "
                    f"{type(e).__name__}: {e}",
                ))

        worker = threading.Thread(
            target=_http_worker, daemon=True, name="acceptance-http-worker",
        )
        worker.start()
        worker.join(timeout=timeout)
        if worker.is_alive():
            # Worker still blocked past wall-clock deadline. Mark
            # timed_out and return immediately. The thread will
            # eventually unblock on its own per-socket timeouts and
            # exit; as a daemon it cannot prevent interpreter exit.
            #
            # Trade-off: we don't try to forcibly close the underlying
            # socket from the main thread (Python provides no clean way
            # to reach into urllib's connection pool). Under extreme
            # abuse this leaves a brief orphan thread. The criterion
            # result is correctly bounded — that's the boundary's
            # contract. Worst case is many concurrent timed-out HTTP
            # criteria stacking up daemon threads, but the per-socket
            # ``timeout`` (= timeout_sec) ensures each one dies within
            # 2× timeout_sec at most.
            duration_ms = int((time.monotonic() - t0) * 1000)
            return RunResult(
                status="timed_out",
                duration_ms=duration_ms,
                command_hash=command_hash,
                error_msg=(
                    f"http exceeded timeout_sec={timeout} "
                    f"(wall-clock; worker thread orphaned)"
                ),
            )
        # Worker finished. Pull its result tuple. If the queue is empty
        # at this point (worker died without putting), that's an
        # executor invariant violation — synthesize an inconclusive so
        # run_one still emits a paired completed event.
        try:
            result = result_q.get_nowait()
        except queue_mod.Empty:
            duration_ms = int((time.monotonic() - t0) * 1000)
            return RunResult(
                status="inconclusive",
                duration_ms=duration_ms,
                command_hash=command_hash,
                error_msg=(
                    "http worker thread exited without producing a "
                    "result — executor invariant violation"
                ),
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
        kind = result[0]
        if kind == "ok":
            status_code = result[1]
            assert isinstance(status_code, int)
            return RunResult(
                status="pass" if 200 <= status_code < 300 else "fail",
                exit_code=status_code,
                duration_ms=duration_ms,
                command_hash=command_hash,
            )
        if kind == "timed_out":
            return RunResult(
                status="timed_out",
                duration_ms=duration_ms,
                command_hash=command_hash,
                error_msg=f"http exceeded timeout_sec={timeout}: {result[1]}",
            )
        if kind == "http_error":
            code = result[1]
            assert isinstance(code, int)
            return RunResult(
                status="fail",
                exit_code=code,
                duration_ms=duration_ms,
                command_hash=command_hash,
                error_msg=f"http {code}",
            )
        if kind == "url_error":
            return RunResult(
                status="fail",
                duration_ms=duration_ms,
                command_hash=command_hash,
                error_msg=f"http error: {result[1]}",
            )
        # kind == "inconclusive" (executor-bug bucket from final
        # catch-all) — unknown kind here would also be a bug; route
        # via the same bucket to keep run_one emitting its paired
        # completed event.
        return RunResult(
            status="inconclusive",
            duration_ms=duration_ms,
            command_hash=command_hash,
            error_msg=f"http executor: {result[1] if len(result) > 1 else kind}",
        )

    # ------------------------------------------------------------------
    # Orchestration shell — emits started + completed/timeout events
    # ------------------------------------------------------------------

    def _dispatch_method(
        self, criterion: AcceptanceCriterion,
    ) -> RunResult:
        """Method-name → executor. Unknown method → inconclusive (defense in
        depth; T1 already rejects unknown methods at parse time)."""
        if criterion.method == "cmd":
            return self._run_cmd(criterion)
        if criterion.method == "file_exists":
            return self._run_file_exists(criterion)
        if criterion.method == "json_query":
            return self._run_json_query(criterion)
        if criterion.method == "http":
            return self._run_http(criterion)
        return RunResult(
            status="inconclusive",
            error_msg=f"unknown method: {criterion.method!r}",
        )

    def run_one(
        self,
        criterion: AcceptanceCriterion,
        *,
        criterion_idx: int,
        attempt_id: str,
        retry_idx: int,
        task_dir: Path,
    ) -> RunResult:
        """Execute one criterion end-to-end:

          1. Emit ``started`` event into ``acceptance-progress.jsonl``.
          2. Dispatch to the per-method executor.
          3. Y1: if ``criterion.type == "e2e"`` AND ``status`` ∈
             ``{timed_out, fail}``, set ``result.escalate=True`` so T8
             routes to §1 row 6 ``blocked_escalate``.
          4. Emit ``completed`` (or ``timeout``) event with the RunResult
             fields populated.

        Method-level executors stay type-blind for clean separation; only
        the orchestration shell knows the criterion's ``type`` and applies
        the Y1 escalate flag.
        """
        criterion_id = self._criterion_id(criterion, criterion_idx)
        criterion_hash = self._criterion_hash(criterion)
        timeout_sec = self._effective_timeout(criterion)
        idempotent = self.default_idempotency(criterion)
        started_at = self._now_iso()

        # 1. Started event — outcome fields all None per Q6.1 invariant
        # (flow_state_writer rejects a started event that leaks any of the 7
        # outcome fields).
        append_acceptance_progress(task_dir, AcceptanceProgressEvent(
            event_id=uuid.uuid4().hex[:12],
            ts=started_at,
            slug=self.slug,
            task_id=self.task_id,
            run_id=self.run_id,
            worktree_id=self.worktree_id,
            attempt_id=attempt_id,
            retry_idx=retry_idx,
            criterion_id=criterion_id,
            criterion_idx=criterion_idx,
            criterion_hash=criterion_hash,
            type=criterion.type,
            method=criterion.method,
            idempotent=idempotent,
            event="started",
            started_at=started_at,
            completed_at=None,
            timeout_sec=timeout_sec,
            status=None,
            exit_code=None,
            duration_ms=None,
            stdout_log_path=None,
            stderr_log_path=None,
            command_hash=None,
        ))

        # 2. Dispatch to the per-method executor. Codex T7 R5 [P2]:
        # defense-in-depth catch-all. Embedded NUL bytes (and any other
        # unanticipated input that makes Path.resolve / subprocess.Popen /
        # urlsplit / etc. raise something OTHER than the executor's typed
        # except-tuple) used to escape past run_one's `started` event,
        # leaving an orphan progress entry. Any non-BaseException now maps
        # to inconclusive so the matching `completed` event always fires.
        # KeyboardInterrupt / SystemExit are NOT swallowed.
        try:
            result = self._dispatch_method(criterion)
        except Exception as e:  # noqa: BLE001 — intentional catch-all
            result = RunResult(
                status="inconclusive",
                error_msg=(
                    f"executor raised unexpected {type(e).__name__}: {e}. "
                    f"This is likely a malformed criterion (embedded NUL, "
                    f"control chars, etc.) — inconclusive routes the "
                    f"contract to operator review."
                ),
            )

        # 3. Y1: e2e timeout AND e2e fail force escalate=True. Method-level
        # executors leave escalate=False; only the orchestration shell knows
        # the type. Design line 528 / §1 row 6.
        if criterion.type == "e2e" and result.status in ("timed_out", "fail"):
            result.escalate = True

        # 4. Completed/timeout event. Q6.1 invariant: completed_at + status +
        # duration_ms are all required (validated by flow_state_writer).
        completed_at = self._now_iso()
        # duration_ms must be a non-None int per Q6.1; if an executor returned
        # the dataclass default 0 we pass it through (still a non-None int).
        append_acceptance_progress(task_dir, AcceptanceProgressEvent(
            event_id=uuid.uuid4().hex[:12],
            ts=completed_at,
            slug=self.slug,
            task_id=self.task_id,
            run_id=self.run_id,
            worktree_id=self.worktree_id,
            attempt_id=attempt_id,
            retry_idx=retry_idx,
            criterion_id=criterion_id,
            criterion_idx=criterion_idx,
            criterion_hash=criterion_hash,
            type=criterion.type,
            method=criterion.method,
            idempotent=idempotent,
            event="timeout" if result.status == "timed_out" else "completed",
            started_at=started_at,
            completed_at=completed_at,
            timeout_sec=timeout_sec,
            status=result.status,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            stdout_log_path=result.stdout_log_path,
            stderr_log_path=result.stderr_log_path,
            command_hash=result.command_hash,
        ))
        return result

    # ------------------------------------------------------------------
    # T9 — In-flight resume + R8 idempotency override resolution
    # ------------------------------------------------------------------

    def find_resume_point(
        self,
        task_dir: Path,
        *,
        attempt_id: str,
    ) -> "ResumePoint":
        """Tail-scan ``acceptance-progress.jsonl`` for the given attempt_id.

        Returns where to resume + whether a criterion was in-flight when
        the orchestrator crashed. Per design §6 Q6.1:

          - ``next_idx`` = ``max(criterion_idx with completed/timeout) + 1``
            within ``attempt_id``; ``0`` if no completed criteria exist.
          - ``in_flight_criterion_idx`` = the criterion_idx of a ``started``
            event with no matching ``completed`` / ``timeout`` (within
            ``attempt_id``). ``None`` if every started has been paired.
          - ``in_flight_event`` = the raw started event dict (so
            ``resolve_in_flight_idempotency`` can read fields off it for
            the blocked.md reason).

        Anti-patterns avoided:
          - **A (.get falsy)**: ``rec.get("attempt_id") != attempt_id``
            with ``attempt_id="0"`` would silently match every attempt.
            We filter via membership (``"attempt_id" in rec``) FIRST,
            then equality, so a missing field doesn't accidentally
            match the empty-string sentinel.
          - **D5 (catch-all)**: a malformed JSON line MUST NOT crash
            the runner — we ``json.JSONDecodeError`` → skip line, log
            via ``error_msg`` accumulator. The orchestrator's outer
            ``run_one`` catch-all in T7 already wraps any escaped
            exception, but we never want this method to be the source
            of one.
          - **Stale started**: if a started event sits at
            ``criterion_idx <= max(completed)``, it's a leftover from a
            prior retry within the same attempt; treat as no in-flight.
        """
        path = task_dir / "acceptance-progress.jsonl"
        if not path.is_file():
            return ResumePoint(
                next_idx=0,
                in_flight_criterion_idx=None,
                in_flight_event=None,
            )

        # Per-criterion last-seen event within this attempt. We keep the
        # latest event per criterion_idx so a (started → completed) pair
        # collapses to the completed entry; a dangling started (no
        # following completed) remains as the latest entry.
        last_event_per_idx: dict[int, dict] = {}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # If the progress file can't even be read, conservative
            # default: resume from the top with no in-flight. The
            # orchestrator's outer catch-all logs separately.
            return ResumePoint(
                next_idx=0,
                in_flight_criterion_idx=None,
                in_flight_event=None,
            )

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # D5 catch-all: malformed event. Skip and continue;
                # never crash the resume scan.
                continue
            if not isinstance(rec, dict):
                # Rare: top-level JSON is a number/list. Skip.
                continue
            # A-aware: explicit membership test BEFORE equality so a
            # missing attempt_id field doesn't sneak through with a
            # falsy match.
            if "attempt_id" not in rec or rec["attempt_id"] != attempt_id:
                continue
            idx = rec.get("criterion_idx")
            # bool is an int subclass — exclude explicitly so a stray
            # ``True`` doesn't masquerade as criterion_idx=1.
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            # Newer event for the same idx supersedes older.
            last_event_per_idx[idx] = rec

        in_flight_idx: Optional[int] = None
        in_flight_event: Optional[dict] = None
        completed_indices: set[int] = set()
        for idx, ev in last_event_per_idx.items():
            event_kind = ev.get("event")
            if event_kind == "started":
                # If multiple criteria are dangling-started (would only
                # happen with concurrent runs, which T9 doesn't claim
                # to handle), keep the highest idx — that's the most
                # recent-by-position in the criteria list.
                if in_flight_idx is None or idx > in_flight_idx:
                    in_flight_idx = idx
                    in_flight_event = ev
            elif event_kind in ("completed", "timeout"):
                completed_indices.add(idx)
            # Any other event_kind (or missing) → ignore. The schema
            # validator already rejects unknown event kinds at write
            # time, but we stay defensive for direct-fixture lines.

        next_idx = (max(completed_indices) + 1) if completed_indices else 0
        # Stale started: a started event at idx <= a completed idx
        # came from a prior retry within the same attempt. Discard.
        if in_flight_idx is not None and in_flight_idx < next_idx:
            in_flight_idx = None
            in_flight_event = None

        return ResumePoint(
            next_idx=next_idx,
            in_flight_criterion_idx=in_flight_idx,
            in_flight_event=in_flight_event,
        )

    def resolve_in_flight_idempotency(
        self,
        criterion: AcceptanceCriterion,
        *,
        contract,  # forward-ref (Contract); typed via duck-shape
        in_flight_event: dict,
    ) -> "IdempotencyVerdict":
        """R8 hardened in-flight idempotency decision (design line 268-275).

        Returns ``IdempotencyVerdict(decision, reason)`` where
        ``decision ∈ {"auto_rerun", "block_in_flight"}`` and ``reason``
        is human-readable for the §1 row 5 ``blocked.md`` body.

        Decision table (design line 268-275):
          - ``type == "e2e"`` → **always block**. PRD §1.3 + design
            line 275 explicit: NO override accepted. Type wins over
            method (an e2e+file_exists criterion still blocks).
          - ``method`` ∈ {file_exists, json_query} → auto_rerun
            (read-only — no side effects to undo on rerun).
          - ``method == "http"`` → auto_rerun (GET is RFC 7231
            idempotent; v0.8.1 is GET-only — non-GET awaits the
            v0.8.2 ``http_method`` field).
          - ``method == "cmd"`` → block UNLESS unblocked via either:
              (a) per-criterion ``idempotent`` override with
                  ``value=True`` AND non-empty ``rationale`` AND the
                  override is a dict (T1 schema enforces
                  ``timeout_sec`` + ``side_effect_class`` at parse
                  time; we still verify the decision-critical fields
                  here as defense-in-depth);
              (b) command starts with an entry in
                  ``contract.idempotent_cmd_allowlist`` (binary OR
                  multi-word prefix like ``flow doctor``).

        Anti-patterns avoided:
          - **B (cross-ref)**: e2e bypass FIRST so an e2e criterion
            authored as ``method=cmd`` with rationale=true still blocks
            (matches design line 275 verbatim — type wins).
          - **A (.get falsy)**: rationale check uses
            ``isinstance(rationale, str) and rationale.strip()`` rather
            than ``idem.get("rationale")`` truthiness — both ``None``
            and ``""`` are correctly treated as "missing".
          - **C2 (frozenset 撒谎)**: there is no `*_TYPES` set here;
            every method/type cell is exercised by a corresponding
            unit test in tests/unit/test_idempotent_in_flight_resume.py.
        """
        # 1. e2e bypass — design line 275: NO override accepted.
        if criterion.type == "e2e":
            return IdempotencyVerdict(
                decision="block_in_flight",
                reason=(
                    "type=e2e is always non-idempotent (PRD §1.3, "
                    "design line 275); no override accepted on "
                    "in-flight interrupt"
                ),
            )

        # 2. Read-only methods — always safe to rerun.
        if criterion.method in ("file_exists", "json_query"):
            return IdempotencyVerdict(
                decision="auto_rerun",
                reason=(
                    f"method={criterion.method} is read-only; "
                    f"safe to rerun"
                ),
            )

        # 3. http — GET-only in v0.8.1 (design line 274). Non-GET is a
        # v0.8.2 follow-up via the ``http_method`` field; all current
        # http criteria are RFC 7231 idempotent.
        if criterion.method == "http":
            return IdempotencyVerdict(
                decision="auto_rerun",
                reason=(
                    "http GET is idempotent per RFC 7231 (v0.8.1 "
                    "ships GET-only); non-GET awaits v0.8.2 "
                    "http_method field"
                ),
            )

        # 4. cmd — default block; unblock via override OR allowlist.
        if criterion.method == "cmd":
            # 4a. Per-criterion override (R8 path b). The dataclass
            # field is typed Optional[dict]; T1's parser validates
            # ``timeout_sec`` + ``side_effect_class`` at parse time.
            # We check the decision-critical pair (value + rationale)
            # here so direct-construction tests (no parser) can't
            # silently slip a half-formed override through.
            idem = criterion.idempotent
            if isinstance(idem, dict):
                value = idem.get("value")
                rationale = idem.get("rationale")
                # value MUST be exactly the bool ``True`` — not a
                # truthy non-bool like 1 / "true" / [1]. The schema
                # enforces this; the explicit ``is True`` check
                # blocks A-blindspot truthiness leaks for
                # direct-construction.
                value_ok = value is True
                rationale_ok = (
                    isinstance(rationale, str) and bool(rationale.strip())
                )
                if value_ok and rationale_ok:
                    return IdempotencyVerdict(
                        decision="auto_rerun",
                        reason=(
                            f"per-criterion override: rationale="
                            f"{rationale.strip()!r} "
                            f"(side_effect_class="
                            f"{idem.get('side_effect_class')!r})"
                        ),
                    )

            # 4b. Allowlist (R8 path a). Each entry can be a single
            # binary (``pytest``) OR a multi-word prefix (``flow
            # doctor``). We match by prefix-followed-by-space OR
            # exact-equality (the criterion is just the binary).
            command = (criterion.command or "").strip()
            allowlist = (
                getattr(contract, "idempotent_cmd_allowlist", None) or ()
            )
            for allowed in allowlist:
                if not isinstance(allowed, str) or not allowed.strip():
                    continue
                normalized = allowed.strip()
                if (
                    command == normalized
                    or command.startswith(normalized + " ")
                ):
                    return IdempotencyVerdict(
                        decision="auto_rerun",
                        reason=(
                            f"command matches allowlist entry "
                            f"{normalized!r}"
                        ),
                    )

            # No override, no allowlist match → R8 default block.
            binary = command.split(" ", 1)[0] if command else ""
            return IdempotencyVerdict(
                decision="block_in_flight",
                reason=(
                    f"cmd is non-idempotent by default (R8 hardened); "
                    f"no allowlist match for {binary!r} and no "
                    f"per-criterion idempotent override"
                ),
            )

        # 5. Unknown method (T1's parser already rejects these;
        # defense-in-depth so an unknown method block-fails-closed
        # rather than slipping into an auto-rerun).
        return IdempotencyVerdict(
            decision="block_in_flight",
            reason=(
                f"unknown method {criterion.method!r}; conservative "
                f"block per fail-closed policy"
            ),
        )

    def resume_attempt(
        self,
        task_dir: Path,
        *,
        attempt_id: str,
        criteria: list,
        contract,
    ) -> Tuple["IdempotencyVerdict", "ResumePoint"]:
        """Convenience wrapper combining ``find_resume_point`` +
        ``resolve_in_flight_idempotency``.

        Orchestrator call site (design §6 Y8): after T5's
        ``detect_auto_prepare_state`` reports a startable lock state
        AND the contract has ``acceptance_criteria``, the orchestrator
        calls this to learn:

          - The IdempotencyVerdict — auto_rerun (caller proceeds from
            ``ResumePoint.next_idx`` OR re-runs
            ``in_flight_criterion_idx``) OR block_in_flight (caller
            writes blocked.md per §6 R8 with ``verdict.reason`` in
            the body).
          - The ResumePoint — where to start in the criteria list.

        No in-flight criterion → ``IdempotencyVerdict("auto_rerun",
        "no in-flight criterion")``. The caller resumes at
        ``rp.next_idx`` without re-running anything.
        """
        rp = self.find_resume_point(task_dir, attempt_id=attempt_id)
        if rp.in_flight_criterion_idx is None:
            return (
                IdempotencyVerdict(
                    decision="auto_rerun",
                    reason="no in-flight criterion to classify",
                ),
                rp,
            )
        # Defensive index — if the criteria list is shorter than the
        # in-flight idx (truncated contract / replay across schema
        # changes), block rather than crash.
        if rp.in_flight_criterion_idx >= len(criteria):
            return (
                IdempotencyVerdict(
                    decision="block_in_flight",
                    reason=(
                        f"in-flight criterion_idx="
                        f"{rp.in_flight_criterion_idx} exceeds "
                        f"contract length {len(criteria)}; contract "
                        f"changed since attempt started"
                    ),
                ),
                rp,
            )
        criterion = criteria[rp.in_flight_criterion_idx]
        verdict = self.resolve_in_flight_idempotency(
            criterion,
            contract=contract,
            in_flight_event=rp.in_flight_event or {},
        )
        return (verdict, rp)

    # ------------------------------------------------------------------
    # Identity helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _criterion_dict(criterion: AcceptanceCriterion) -> dict:
        """Stable dict representation for hashing. ``__dict__`` works on
        dataclass instances; we copy to a fresh dict so caller mutations
        don't bleed back into the criterion."""
        if hasattr(criterion, "__dict__"):
            return dict(criterion.__dict__)
        # Fallback for callers passing a plain dict (shouldn't happen in
        # production; keeps tests resilient).
        return dict(criterion)  # type: ignore[arg-type]

    def _criterion_hash(self, criterion: AcceptanceCriterion) -> str:
        """Y7: full criterion hash via T4's helper. Stable across runs."""
        return compute_criterion_hash(self._criterion_dict(criterion))

    def _criterion_id(
        self, criterion: AcceptanceCriterion, idx: int,
    ) -> str:
        """Stable id from idx + 8-hex hash prefix. T8/T9 may override with a
        contract-supplied id later; T7 produces a synthesizing default."""
        return f"c{idx}_{self._criterion_hash(criterion)[:8]}"

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
