"""flow_notification — Notifier owning v0.8.1 3-tier emission stack.

Tiers (per design §5 + §7):
  Tier 1 — `blocked.md` atomic write (T5/T6 v0.8.0 helper). ALWAYS fires;
           `throttle_min` does NOT affect Tier 1 (per Q5.1 + R9 clarified).
  Tier 2 — OSC 9 desktop ping + BEL safety floor on stderr. Throttled per
           `(task_id, issue_id)` key with `throttle_min` minute window;
           `tier2_enabled=false` silences both. Terminal capability auto-
           detected via `$TERM_PROGRAM` allowlist (kitty / ghostty / iTerm.app);
           anything else falls back to BEL-only (still respects
           `tier2_enabled`).
  Tier 3 — schema-only in v0.8.1: `notification.command` field is parsed by
           T1 but NOT executed. PRD §3 defers Tier 3 hook to v0.8.2. We log
           a NOTE to stderr when the field is non-empty so operators see
           the field was honored as schema-accept.

Throttle persistence (R9 + M-class scope discipline):
  state @ `<task_dir>/.notification_throttle.json` (in-place write under
  fcntl.flock):
    {"<task_id>::<issue_id>": "YYYY-MM-DDTHH:MM:SSZ"}
  Read-modify-write protected by `_locked_throttle_rmw` helper (modelled
  after safe_io.locked_text_rmw — same inode under lock so racing PIDs
  serialize correctly; we deliberately avoid atomic_write_json's
  mkstemp+rename pattern here because it would change the inode and
  break the flock invariant). On JSON corruption → fail-open (re-fire).
  On individual timestamp parse failure → fail-open (re-fire). Both
  choices: throttle is ergonomic, NOT a safety boundary. Tier 1
  (blocked.md) is the safety boundary; throttle errors degrade to
  "user gets one extra ping", not "user misses an emission".

Pitfall defenses (per .flow/pitfalls/claude-review-blindspots.md):
  - K-class (plausible-justification deviation): no try/except wrapping
    fire_block dispatch. Errors propagate naturally.
  - L-class (key-in-dict bypass with non-string field): isinstance(str)
    enforced on task_id and issue_id BEFORE composing throttle key.
  - F-class (identity check fail-open): we don't perform identity checks;
    notification side effects do not depend on hash equivalence.
  - D5 (typed except gaps): timestamp parse catches BOTH ValueError
    (malformed ISO string per `datetime.fromisoformat` contract) AND
    TypeError (naive datetime arithmetic when state file holds a
    timestamp without tzinfo — naive datetime → coerced to UTC then
    rewritten in canonical aware form). Outer RMW wrapper catches
    OSError + json.JSONDecodeError per fail-open contract. Other
    exceptions propagate (no catch-all `except Exception`).
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Allow `from flow_contract import Contract` and `from flow_state_writer
# import write_blocked` without callers needing to fix sys.path themselves.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from flow_contract import Contract  # noqa: E402
from flow_state_writer import write_blocked  # noqa: E402  (T5/T6 helper)


# ----------------------------------------------------------------------
# Constants — terminal allowlist + state filename. Frozen tuples so the
# allowlist cannot be mutated at runtime (defense-in-depth for security
# decisions that hinge on the membership check).
# ----------------------------------------------------------------------
OSC9_TERM_ALLOWLIST: tuple[str, ...] = ("kitty", "ghostty", "iTerm.app")
THROTTLE_FILENAME = ".notification_throttle.json"

# OSC 9 sanitize budget. Long OSC strings can be silently dropped by
# terminal parsers (xterm caps internal OSC buffer at ~1k; conservative
# cap at 200 keeps us well under any plausible limit and bounds blast
# radius if a caller passes a megabyte of text).
OSC9_BODY_MAXLEN = 200


def _sanitize_osc_text(body: str, max_len: int = OSC9_BODY_MAXLEN) -> str:
    """Strip control chars + truncate. Prevents OSC sequence injection.

    Threat model: ``body`` reaches us as ``why_blocked`` from the
    orchestrator. v0.8.1's source is contract-derived violations
    (file paths / manifest keys — internal), but v0.8.x evolves toward
    operator-supplied issue strings, and OSC injection is on the
    pitfall list (`.flow/pitfalls/claude-review-blindspots.md`). A
    crafted body containing ``\\x07\\x1b]0;HACKED\\x07`` would close
    our OSC 9, open a fresh OSC 0 (window-title set), and hijack the
    operator's terminal.

    Allowlist: printable ASCII (0x20–0x7E) plus ``\\t``. Explicitly
    excludes ``\\x07`` (BEL — closes the OSC), ``\\x1b`` (ESC — opens
    a new sequence), and all other C0/C1 control chars. Non-ASCII is
    dropped too — terminal handling of non-ASCII inside OSC is
    implementation-defined; safer to coerce to ASCII for this surface.

    Truncation runs AFTER stripping so the budget reflects characters
    actually emitted, not raw input length.
    """
    safe = "".join(ch for ch in body if 0x20 <= ord(ch) < 0x7F or ch == "\t")
    return safe[:max_len]


def _utcnow() -> datetime.datetime:
    """Single source of truth for "now" in this module — easier to mock."""
    return datetime.datetime.now(datetime.timezone.utc)


# ----------------------------------------------------------------------
# Locked read-modify-write for JSON throttle state. Modelled after
# safe_io.locked_text_rmw: keep the SAME inode under flock so two PIDs
# racing on the same path serialize correctly. We deliberately do NOT
# use atomic_write_json here — its mkstemp+rename pattern would change
# the file's inode and break the flock invariant (the second PID would
# block on the now-unlinked ghost inode while the first wrote to the
# new one). For a small JSON state file (one short string per
# (task, issue) key, well under one filesystem block) the in-place
# write under lock is single-syscall and crash-equivalent to atomic-
# rename in the common case.
# ----------------------------------------------------------------------
def _locked_throttle_rmw(
    path: Path,
    transform,
    *,
    timeout_s: float = 2.0,
) -> bool:
    """Acquire LOCK_EX on path (creating empty if missing) → read JSON →
    transform(state_dict) returns (new_state, write?) → in-place write
    if write?=True. Returns True on write, False on no-op or lock timeout.

    The lock is held across read + transform + write (and the inode is
    stable: no rename), so two PIDs racing on the same throttle file
    serialize correctly: the second observes the first's write.

    On JSON corruption the in-memory state passed to transform is `{}`
    (fail-open — operator sees an extra ping, throttle resets after this
    write).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # `open(..., "a+")` creates the file if missing without truncating
    # an existing file; we re-`seek(0)` for read below.
    deadline = time.monotonic() + timeout_s
    with open(path, "a+", encoding="utf-8") as f:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
        try:
            f.seek(0)
            raw = f.read()
            if not raw.strip():
                state: dict = {}
            else:
                try:
                    state = json.loads(raw)
                    if not isinstance(state, dict):
                        # Schema invariant violation — treat as corrupt.
                        state = {}
                except json.JSONDecodeError:
                    state = {}
            new_state, do_write = transform(state)
            if not do_write:
                return False
            # In-place write: truncate + seek + write under the same fd
            # holding the lock. Same inode preserved → flock invariant
            # holds for racing PIDs.
            text = json.dumps(new_state, ensure_ascii=False, indent=2) + "\n"
            f.seek(0)
            f.truncate()
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
            return True
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ----------------------------------------------------------------------
# Notifier
# ----------------------------------------------------------------------
class Notifier:
    """Owns Tier 1+2 emission. Tier 3 is schema-accept (deferred).

    Constructor inputs are all required-keyword to prevent positional
    argument confusion at call sites; all 3-tier dispatch flows through
    `fire_block` (steady-state) or `fire_terminal` (aborted_* states).
    """

    def __init__(
        self,
        *,
        contract: Contract,
        slug: str,
        task_dir: Path,
        term_program: Optional[str] = None,
    ):
        self.contract = contract
        self.slug = slug
        self.task_dir = task_dir
        # Caller-provided term_program wins over $TERM_PROGRAM. Falls back
        # to env when omitted; empty string is legitimate ("unknown
        # terminal" → BEL-only fallback).
        if term_program is None:
            term_program = os.environ.get("TERM_PROGRAM", "")
        self.term_program = term_program
        # `_stderr` is overridable for tests. Defaults to sys.stderr at
        # invocation time (fetched lazily in case tests re-bind sys.stderr
        # after Notifier construction).
        self._stderr = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def fire_block(
        self,
        *,
        block_type: str,
        phase: int,
        task_id: str,
        issue_id: str,
        why_blocked: str,
        required_choice: list[str],
        safe_resume_command: str,
        frontmatter_extra: Optional[dict] = None,
    ) -> Path:
        """Tier 1 (always) + Tier 2 (throttled) + Tier 3 (schema-only).

        Returns the path to the freshly-written blocked.md. Raises
        TypeError if task_id / issue_id are non-string (L-class defense).
        Errors from write_blocked (e.g., I/O failures) propagate — Tier 1
        is a safety surface, fail-closed.

        ``frontmatter_extra`` is passed through to ``write_blocked`` so
        operator-critical metadata (e.g. ``{"block_row": 4}``) lands in
        blocked.md frontmatter. Codex round-2 [P2] regression fix: the
        previous round's ``NotImplementedError`` guard broke the live
        T15/T16 orchestrator path — ``flow_orchestrator.auto_dispatch_task``
        passes a truthy ``frontmatter_extra`` for every manifest violation,
        so the loud-fail aborted Tier 1 (``write_blocked``) before the
        safety surface landed. Pass-through restores the safety boundary;
        ``write_blocked`` validates the extras dict (key shape, reserved-
        key collision, scalar-only values, no newlines) and raises
        ``ValueError`` BEFORE any disk write on bad input — that's the
        proper layer for input shape validation, not Notifier.
        """
        # L-class: enforce string identity BEFORE composing throttle key.
        # `isinstance` is the gate; non-string raises rather than silently
        # stringifying via `f"{x}::{y}"` and producing inconsistent keys.
        if not isinstance(task_id, str):
            raise TypeError(
                f"task_id must be str, got {type(task_id).__name__}"
            )
        if not isinstance(issue_id, str):
            raise TypeError(
                f"issue_id must be str, got {type(issue_id).__name__}"
            )

        # Tier 1: ALWAYS write blocked.md. Errors propagate.
        # Codex round-2 [P2] fix: pass `frontmatter_extra` through —
        # production caller flow_orchestrator.py:824 already supplies
        # `{"block_row": verdict.block_row}`. write_blocked owns the
        # validation contract for the extras shape.
        path = write_blocked(
            self.task_dir,
            phase=phase,
            task=task_id,
            why_blocked=why_blocked,
            required_choice=required_choice,
            safe_resume_command=safe_resume_command,
            block_type=block_type,
            frontmatter_extra=frontmatter_extra,
        )

        # Tier 2: throttled.
        self._maybe_fire_tier2(task_id=task_id, issue_id=issue_id,
                               body=why_blocked)

        # Tier 3: schema-only (deferred to v0.8.2 per PRD §3).
        cmd = self.contract.notification.get("command")
        if cmd:
            self._write_stderr(
                "NOTE: notification.command is set but Tier 3 hooks are "
                "deferred to v0.8.2 — field accepted, not executed.\n"
            )

        return path

    def fire_terminal(
        self,
        *,
        block_type: str,  # noqa: ARG002 (kept for caller documentation)
        task_id: str,
        issue_id: str,
        body: str,
    ) -> None:
        """Terminal-state ping (aborted_* per §1 rows 12+16, §5 line 211–212).

        Bypasses throttle; emits Tier 2 unconditionally subject to
        `tier2_enabled`. Does NOT write blocked.md (aborted_* tasks have
        their own `aborted/` directory written elsewhere — Notifier does
        not own that path).
        """
        if not isinstance(task_id, str):
            raise TypeError(
                f"task_id must be str, got {type(task_id).__name__}"
            )
        if not isinstance(issue_id, str):
            raise TypeError(
                f"issue_id must be str, got {type(issue_id).__name__}"
            )
        if not self.contract.notification.get("tier2_enabled", True):
            return
        self._emit_tier2(body=body, with_osc9=self._osc9_supported())

    def archive_on_resume(self, *, ts: str) -> Path:
        """Q5.3: atomic-move blocked.md → archive/blocked/<ts>.md.

        POSIX `rename` (via os.replace) is atomic within a filesystem,
        so the archived file appears at its final location in one step;
        the live file disappears in the same step. Caller is responsible
        for ts uniqueness (collision of <ts>.md raises FileExistsError on
        rename target — actually os.replace overwrites, so we explicitly
        guard against silent collisions).
        """
        live = self.task_dir / "blocked.md"
        if not live.is_file():
            raise FileNotFoundError(f"no blocked.md to archive at {live}")
        archive_dir = self.task_dir / "archive" / "blocked"
        archive_dir.mkdir(parents=True, exist_ok=True)
        # Reuse write_blocked's safe-ts pattern: replace ":" with "-" so
        # the path is filesystem-portable (Windows / case-insensitive
        # mounts) without losing reversibility.
        safe_ts = ts.replace(":", "-")
        target = archive_dir / f"{safe_ts}.md"
        if target.exists():
            # O-class echo: same-second collisions can happen on fast
            # pipelines. Don't silently overwrite — caller decides.
            raise FileExistsError(
                f"archive target already exists: {target}"
            )
        os.replace(live, target)
        return target

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _osc9_supported(self) -> bool:
        return self.term_program in OSC9_TERM_ALLOWLIST

    def _maybe_fire_tier2(
        self, *, task_id: str, issue_id: str, body: str,
    ) -> None:
        """Tier 2 dispatch with disable-switch + throttle in that order.

        Order matters: tier2_enabled=False is full disable and must NOT
        update throttle state (otherwise toggling it on later would have
        a stale "last fired" entry that suppresses the first real
        emission).
        """
        if not self.contract.notification.get("tier2_enabled", True):
            return  # full disable: skip BOTH OSC 9 and BEL
        if not self._allowed_by_throttle(task_id=task_id, issue_id=issue_id):
            return  # throttled — no emission
        self._emit_tier2(body=body, with_osc9=self._osc9_supported())

    def _emit_tier2(self, *, body: str, with_osc9: bool) -> None:
        """Write OSC 9 + BEL (or BEL-only) to stderr.

        OSC 9 format: ESC `]` `9` `;` <text> BEL — Apple's iTerm format
        adopted by kitty + Ghostty as the de-facto desktop notification
        sequence. We use BEL as the closing delimiter (not ST/`\\x1b\\\\`)
        because BEL is universally treated as terminator and also serves
        as our "safety floor" audible ping in non-OSC9 terminals.
        """
        if with_osc9:
            # OSC 9 carries the message text; trailing BEL closes the
            # sequence AND serves as audible floor. Sanitize body to
            # prevent control-char injection (e.g. embedded BEL closing
            # our OSC 9 + nested OSC 0 setting the window title).
            safe_body = _sanitize_osc_text(body)
            text = f"\x1b]9;flow blocked: {safe_body}\x07"
        else:
            # BEL-only floor — operator hears a ping even if no desktop
            # notification API is available.
            text = "\x07"
        self._write_stderr(text)

    def _write_stderr(self, text: str) -> None:
        """Send `text` to the captured stderr handle (test-friendly).

        Uses a property-style late-binding: tests assign `n._stderr =
        io.StringIO()` AFTER construction and we honor that. Default is
        `sys.stderr` resolved at write-time so tests that patch
        `sys.stderr` see the redirect.
        """
        target = self._stderr if self._stderr is not None else sys.stderr
        target.write(text)
        flush = getattr(target, "flush", None)
        if callable(flush):
            flush()

    def _allowed_by_throttle(
        self, *, task_id: str, issue_id: str,
    ) -> bool:
        """Returns True iff (task, issue) is OUTSIDE the throttle window
        (i.e., emission allowed). On allow, records this firing's ts.

        throttle_min=0 → no throttle, always True (R9 clarified).
        throttle_min<0 → defensively treated as no throttle (parser already
                          rejected negatives at T1, but be robust).
        Persists state at task_dir/.notification_throttle.json.
        """
        # Coerce defensively. Parser at T1 already validates as
        # non_negative_int; this isinstance check is L-class belt-and-
        # suspenders for callers that bypass parse_contract.
        raw = self.contract.notification.get("throttle_min", 5)
        try:
            throttle_min = int(raw)
        except (TypeError, ValueError):
            throttle_min = 5
        if throttle_min <= 0:
            return True

        # P3 (codex round-1): reject '::' inside task_id / issue_id BEFORE
        # composing the throttle key. Otherwise ``"a" + "b::c"`` collides
        # with ``"a::b" + "c"`` → cross-issue/task throttle suppression.
        # Up-front reject is cheap and forces callers to use unambiguous
        # identifiers (task_id / issue_id should not contain reserved
        # delimiters anyway).
        if "::" in task_id or "::" in issue_id:
            raise ValueError(
                "task_id / issue_id must not contain '::' "
                "(reserved as throttle-state key delimiter)"
            )

        key = f"{task_id}::{issue_id}"
        now = _utcnow()
        path = self.task_dir / THROTTLE_FILENAME

        # Build closure for the locked RMW. The closure decides:
        #   - allowed = True / False (returned to caller via outer var)
        #   - new_state for write (only when allowed)
        # Initialize to True (fail-open): per module docstring, throttle is
        # ergonomic, NOT a safety boundary. Lock timeout / corruption /
        # missing-prior all degrade to "operator gets one extra ping",
        # never to "operator misses an emission". The transform only flips
        # to False when it has explicitly observed an in-window prior entry.
        decision = {"allowed": True}

        def _transform(state: dict) -> tuple[dict, bool]:
            last_iso = state.get(key)
            if isinstance(last_iso, str):
                # Tolerate trailing 'Z' (parser writes that form below).
                # P2.1 (codex round-1): catch BOTH ValueError (malformed
                # ISO string) AND TypeError. TypeError arises when:
                #   - state file holds a naive ISO timestamp (no tzinfo)
                #     and `now - last` raises "can't subtract offset-naive
                #     and offset-aware datetimes"
                # Mitigation: coerce naive → UTC (assume legacy state
                # written without tzinfo represents UTC) so that we still
                # honor the throttle window when state is recoverable;
                # only fall through to fail-open + rewrite when truly
                # unparseable.
                try:
                    last = datetime.datetime.fromisoformat(
                        last_iso.replace("Z", "+00:00")
                    )
                    if last.tzinfo is None:
                        # Naive datetime — coerce to UTC. The next write
                        # below rewrites the state in canonical aware
                        # format ("...Z"), so this is one-shot recovery.
                        last = last.replace(tzinfo=datetime.timezone.utc)
                    age_sec = (now - last).total_seconds()
                    if age_sec < throttle_min * 60:
                        decision["allowed"] = False
                        return state, False  # no write — within window
                except (ValueError, TypeError):
                    # Corrupt timestamp — fail-open (re-fire). Documented
                    # in module docstring: throttle is ergonomic, not a
                    # safety boundary.
                    pass
            # Either no prior entry, prior entry expired, or prior entry
            # corrupt → emit + record this firing. (decision["allowed"]
            # already True from the fail-open default.)
            new_state = dict(state)
            new_state[key] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            return new_state, True

        # Two outcomes return False from _locked_throttle_rmw:
        #   1. Lock timeout (≥2s contended) — transform never ran, so
        #      decision["allowed"] stays at its fail-open default (True).
        #      Operator gets the emission but throttle state isn't updated;
        #      next call may re-fire too. Acceptable: throttle is
        #      ergonomic, not a safety boundary.
        #   2. transform decided "no write" (within window) — decision was
        #      explicitly flipped to False inside the closure.
        # Either way the answer is decision["allowed"].
        #
        # P2.1 outer fail-open (codex round-1): even with the typed except
        # inside _transform, the RMW helper itself can raise OSError (FS
        # gone away mid-call) or json.JSONDecodeError (corrupt state that
        # somehow slipped past the inner sanitizer — defense in depth).
        # Per module docstring, ALL throttle-state IO failures degrade to
        # "operator gets the emission". We deliberately do NOT use
        # `except Exception` — that's the D5 anti-pattern (catch-all
        # swallows real bugs); we enumerate the documented IO failure
        # modes.
        try:
            _locked_throttle_rmw(path, _transform)
        except (OSError, json.JSONDecodeError):
            # IO / corruption mid-RMW → fail-open per docstring. Caller
            # gets the emission; next call re-attempts the lock.
            return True
        return decision["allowed"]
