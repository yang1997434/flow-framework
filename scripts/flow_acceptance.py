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
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
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
        # the next socket open.
        if (
            self._deadline_monotonic is not None
            and time.monotonic() > self._deadline_monotonic
        ):
            raise urllib.error.URLError(
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
        candidate = (self.worktree_root / rel_path).resolve()
        try:
            root = self.worktree_root.resolve()
        except OSError as e:
            return None, (
                f"{field_name}={rel_path!r} could not resolve worktree "
                f"root: {e}"
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
        """Send SIGTERM to the child's process group; SIGKILL if it lingers.

        ``proc.pid`` IS the process group leader (because we spawned with
        ``start_new_session=True``). ``os.killpg`` delivers the signal to
        every process in that group — the shell, every direct child, every
        ``&``-backgrounded grandchild that hasn't called setsid itself.

        ``ProcessLookupError`` is benign — the process was already dead
        (race between TimeoutExpired and natural exit). After SIGTERM we
        wait briefly for graceful shutdown; processes that ignore SIGTERM
        get SIGKILL. ``proc.wait()`` at the end reaps the zombie.
        """
        pgid = proc.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return  # already dead — wait below is a no-op
        except OSError:
            # Permission / EINVAL — extremely unusual; fall through to wait.
            pass
        try:
            proc.wait(timeout=PROCESS_GROUP_KILL_GRACE_SEC)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait()
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
        # SAFETY-BOUNDARY (codex R1 [P2]): cap file size BEFORE materializing
        # via read_text(). A pathological multi-GB JSON would exhaust memory
        # / blow the per-criterion timeout. 10 MiB easily covers config /
        # fixture cases; anything larger is malformed contract.
        try:
            stat_size = target.stat().st_size
        except OSError as e:
            return RunResult(
                status="inconclusive",
                error_msg=f"json stat OS error: {e}",
            )
        if stat_size > MAX_JSON_QUERY_FILE_BYTES:
            return RunResult(
                status="inconclusive",
                error_msg=(
                    f"json_query file size {stat_size} bytes exceeds cap "
                    f"{MAX_JSON_QUERY_FILE_BYTES} bytes (path={target}); "
                    f"refuse to materialize."
                ),
            )
        t0 = time.monotonic()
        try:
            obj = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return RunResult(
                status="inconclusive",
                duration_ms=int((time.monotonic() - t0) * 1000),
                error_msg=f"json parse error: {e}",
            )
        except OSError as e:
            # D2: file disappeared between is_file() and read_text(),
            # or permissions changed. Don't silently treat as missing —
            # inconclusive.
            return RunResult(
                status="inconclusive",
                duration_ms=int((time.monotonic() - t0) * 1000),
                error_msg=f"json read OS error: {e}",
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
        parsed = urllib.parse.urlsplit(criterion.url)
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
        try:
            req = urllib.request.Request(criterion.url, method="GET")
            with opener.open(req, timeout=timeout) as resp:
                status_code = resp.status
                duration_ms = int((time.monotonic() - t0) * 1000)
                # Drain a small amount of body so the connection closes
                # cleanly; we don't store body, just ensure the server
                # finished sending. Bounded read so a giant body doesn't
                # blow memory.
                #
                # STATUS-ALREADY-DETERMINED — body drain only.
                # The HTTP status is already captured above; this drain
                # is best-effort cleanup, NOT a verdict input. An OSError
                # here (mid-stream disconnect, connection reset post-
                # headers) cannot change pass/fail. This is the ONE
                # justified silent OSError swallow in this module —
                # every other OSError flows through URLError / HTTPError
                # branches into a real verdict. Future readers: do not
                # remove this try/except without considering that some
                # servers close the socket aggressively after sending
                # the status line on small responses.
                try:
                    resp.read(64)
                except OSError:
                    pass
                return RunResult(
                    status="pass" if 200 <= status_code < 300 else "fail",
                    exit_code=status_code,
                    duration_ms=duration_ms,
                    command_hash=command_hash,
                )
        except urllib.error.HTTPError as e:
            # D3: server replied with non-2xx. ``e.code`` is the real status.
            duration_ms = int((time.monotonic() - t0) * 1000)
            return RunResult(
                status="fail",
                exit_code=e.code,
                duration_ms=duration_ms,
                command_hash=command_hash,
                error_msg=f"http {e.code}",
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            # D2: distinguish socket timeout from connection error.
            reason = getattr(e, "reason", None)
            is_timeout = (
                isinstance(e, (TimeoutError, socket.timeout))
                or isinstance(reason, (TimeoutError, socket.timeout))
            )
            if is_timeout:
                return RunResult(
                    status="timed_out",
                    duration_ms=duration_ms,
                    command_hash=command_hash,
                    error_msg=f"http exceeded timeout_sec={timeout}",
                )
            return RunResult(
                status="fail",
                duration_ms=duration_ms,
                command_hash=command_hash,
                error_msg=f"http error: {e}",
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

        # 2. Dispatch to the per-method executor.
        result = self._dispatch_method(criterion)

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
