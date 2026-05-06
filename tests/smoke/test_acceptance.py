"""T7 — AcceptanceRunner method executors + timeout + idempotency defaults.

Coverage matrix (per-method × per-status) per the plan T7 Step 7.16 contract:
  - cmd          : pass / fail / timed_out / inconclusive (missing field)
  - file_exists  : pass / fail / inconclusive
  - json_query   : pass / fail (path missing) / fail (falsy leaf) /
                   inconclusive (missing file) / inconclusive (parse error)
  - http         : pass (2xx) / fail (5xx via HTTPError) / fail (refused) /
                   timed_out / inconclusive (missing url)

Plus:
  - _effective_timeout precedence (R7 table)
  - default_idempotency (R8 table — e2e always false; cmd false; file/json/
    http true)
  - run_one Y1 escalate flag (e2e timeout + e2e fail)
  - run_one started + completed/timeout events emitted into
    acceptance-progress.jsonl (T4 schema).
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_acceptance import (   # type: ignore  # noqa: E402
    AcceptanceRunner,
    RunResult,
    DEFAULT_TIMEOUT_BY_METHOD,
    E2E_TYPE_TIMEOUT,
    MAX_JSON_QUERY_FILE_BYTES,
    MAX_HTTP_REDIRECTS,
)
from flow_contract import AcceptanceCriterion  # type: ignore  # noqa: E402


def _make_runner(td: str) -> AcceptanceRunner:
    return AcceptanceRunner(
        worktree_root=Path(td),
        log_dir=Path(td) / "logs",
        slug="demo",
        task_id="T1",
        run_id="r1",
        worktree_id="demo+t1+abc1234",
    )


# ---------------------------------------------------------------------------
# cmd executor
# ---------------------------------------------------------------------------


class TestCmdMethod(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_cmd_zero_exit_pass(self):
        crit = AcceptanceCriterion(
            description="trivial", type="unit", method="cmd",
            command="true", timeout_sec=30,
        )
        r = self.runner._run_cmd(crit)
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.exit_code, 0)
        self.assertIsNotNone(r.command_hash)
        self.assertGreaterEqual(r.duration_ms, 0)
        self.assertIsNotNone(r.stdout_log_path)
        self.assertTrue(Path(r.stdout_log_path).exists())
        self.assertFalse(r.escalate)  # method-level executor stays type-blind

    def test_cmd_nonzero_exit_fail(self):
        crit = AcceptanceCriterion(
            description="trivial", type="unit", method="cmd",
            command="false", timeout_sec=30,
        )
        r = self.runner._run_cmd(crit)
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.exit_code, 1)

    def test_cmd_timeout_returns_timed_out_status(self):
        crit = AcceptanceCriterion(
            description="long", type="unit", method="cmd",
            command="sleep 5", timeout_sec=1,
        )
        r = self.runner._run_cmd(crit)
        # D2: TimeoutExpired → timed_out, NOT fail. exit_code is None
        # (the process was killed; no clean rc).
        self.assertEqual(r.status, "timed_out")
        self.assertIsNone(r.exit_code)
        self.assertGreaterEqual(r.duration_ms, 0)
        self.assertIn("timeout_sec=1", r.error_msg or "")

    def test_cmd_missing_command_inconclusive(self):
        # Direct construction; T1's parser would have rejected this.
        crit = AcceptanceCriterion(
            description="empty", type="unit", method="cmd",
            command=None, timeout_sec=30,
        )
        r = self.runner._run_cmd(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIn("command", r.error_msg or "")

    def test_cmd_runs_inside_worktree_root(self):
        # cwd is set to worktree_root, so a touch should land there.
        crit = AcceptanceCriterion(
            description="touch", type="unit", method="cmd",
            command="touch sentinel.txt", timeout_sec=30,
        )
        r = self.runner._run_cmd(crit)
        self.assertEqual(r.status, "pass")
        self.assertTrue((Path(self.tmp) / "sentinel.txt").exists())

    def test_cmd_stdout_captured_to_log(self):
        crit = AcceptanceCriterion(
            description="echo", type="unit", method="cmd",
            command="echo hello-world", timeout_sec=30,
        )
        r = self.runner._run_cmd(crit)
        self.assertEqual(r.status, "pass")
        body = Path(r.stdout_log_path).read_text(encoding="utf-8")
        self.assertIn("hello-world", body)


# ---------------------------------------------------------------------------
# file_exists executor
# ---------------------------------------------------------------------------


class TestFileExistsMethod(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        (Path(self.tmp) / "VERSION").write_text("0.8.1\n")
        self.runner = _make_runner(self.tmp)

    def test_file_exists_pass(self):
        crit = AcceptanceCriterion(
            description="version pinned", type="smoke",
            method="file_exists", path="VERSION", timeout_sec=30,
        )
        r = self.runner._run_file_exists(crit)
        self.assertEqual(r.status, "pass")

    def test_file_exists_fail_when_missing(self):
        crit = AcceptanceCriterion(
            description="missing", type="smoke",
            method="file_exists", path="DOES_NOT_EXIST", timeout_sec=30,
        )
        r = self.runner._run_file_exists(crit)
        self.assertEqual(r.status, "fail")

    def test_file_exists_inconclusive_when_path_field_missing(self):
        crit = AcceptanceCriterion(
            description="bad config", type="smoke",
            method="file_exists", path=None, timeout_sec=30,
        )
        r = self.runner._run_file_exists(crit)
        self.assertEqual(r.status, "inconclusive")

    def test_file_exists_directory_is_fail(self):
        # A directory should NOT pass file_exists — we use is_file().
        (Path(self.tmp) / "subdir").mkdir()
        crit = AcceptanceCriterion(
            description="dir", type="smoke",
            method="file_exists", path="subdir", timeout_sec=30,
        )
        r = self.runner._run_file_exists(crit)
        self.assertEqual(r.status, "fail")


# ---------------------------------------------------------------------------
# json_query executor
# ---------------------------------------------------------------------------


class TestJsonQueryMethod(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        (Path(self.tmp) / "config.json").write_text(json.dumps({
            "version": "0.8.1",
            "features": {"autonomy": True, "off": False},
            "list": [10, 20, 30],
            "nested": {"a": {"b": {"c": "deep"}}},
        }))
        self.runner = _make_runner(self.tmp)

    def test_json_query_dotted_path_pass(self):
        crit = AcceptanceCriterion(
            description="autonomy on", type="smoke",
            method="json_query", path="config.json",
            json_query="features.autonomy", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "pass")

    def test_json_query_value_falsy_fails(self):
        crit = AcceptanceCriterion(
            description="autonomy off", type="smoke",
            method="json_query", path="config.json",
            json_query="features.off", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "fail")

    def test_json_query_full_dotted_path_traversal(self):
        # D4-aware: every segment must be visited; this test has 3 segments.
        crit = AcceptanceCriterion(
            description="deep", type="smoke",
            method="json_query", path="config.json",
            json_query="nested.a.b.c", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "pass")

    def test_json_query_intermediate_segment_missing_fails(self):
        crit = AcceptanceCriterion(
            description="missing-mid", type="smoke",
            method="json_query", path="config.json",
            json_query="features.nonexistent.further", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "fail")
        self.assertIn("nonexistent", r.error_msg or "")

    def test_json_query_missing_file_inconclusive(self):
        crit = AcceptanceCriterion(
            description="missing", type="smoke",
            method="json_query", path="nope.json",
            json_query="x", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "inconclusive")

    def test_json_query_parse_error_inconclusive(self):
        (Path(self.tmp) / "bad.json").write_text("{not valid json")
        crit = AcceptanceCriterion(
            description="bad", type="smoke",
            method="json_query", path="bad.json",
            json_query="x", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIn("parse", r.error_msg or "")

    def test_json_query_missing_field_inconclusive(self):
        crit = AcceptanceCriterion(
            description="no query", type="smoke",
            method="json_query", path="config.json",
            json_query=None, timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "inconclusive")


# ---------------------------------------------------------------------------
# http executor
# ---------------------------------------------------------------------------


class _StubHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Minimal localhost HTTP fixture for T7 acceptance tests."""

    # Class attribute toggled by test setup to simulate slow servers.
    slow_response_sec: float = 0.0

    def do_GET(self):
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/slow":
            # Blocking sleep on the handler thread → urlopen socket timeout.
            # The client (urlopen) will have closed the socket by the time we
            # come back; suppress the resulting BrokenPipeError so the test
            # stderr stays clean.
            import time as _t
            _t.sleep(self.slow_response_sec)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"slow")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *_a, **_kw):  # silence the default access log
        pass


class _QuietHTTPServer(http.server.HTTPServer):
    """Suppress the default `handle_error` traceback for the broken-pipe race
    that's expected when the test deliberately times the client out.
    """

    def handle_error(self, request, client_address):
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class TestHttpMethod(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _QuietHTTPServer(
            ("127.0.0.1", 0), _StubHTTPHandler,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_http_2xx_pass(self):
        crit = AcceptanceCriterion(
            description="ok", type="integration", method="http",
            url=f"http://127.0.0.1:{self.port}/ok", timeout_sec=10,
        )
        r = self.runner._run_http(crit)
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.exit_code, 200)
        self.assertIsNotNone(r.command_hash)

    def test_http_5xx_fail(self):
        crit = AcceptanceCriterion(
            description="bad", type="integration", method="http",
            url=f"http://127.0.0.1:{self.port}/missing", timeout_sec=10,
        )
        r = self.runner._run_http(crit)
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.exit_code, 503)

    def test_http_refused_fail(self):
        # Bind an ephemeral port and immediately release it — the kernel
        # won't reuse it instantly for a fresh listener, so connecting
        # there reliably yields ECONNREFUSED. Avoids the "port 1 might
        # be bound on rare dev machines" hazard while still proving the
        # "server unreachable IS a verdict" path.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        crit = AcceptanceCriterion(
            description="dead", type="integration", method="http",
            url=f"http://127.0.0.1:{port}/", timeout_sec=2,
        )
        r = self.runner._run_http(crit)
        self.assertEqual(r.status, "fail")
        self.assertIsNone(r.exit_code)

    def test_http_socket_timeout_returns_timed_out(self):
        # Make the stub block longer than the timeout_sec.
        _StubHTTPHandler.slow_response_sec = 2.0
        try:
            crit = AcceptanceCriterion(
                description="slow", type="integration", method="http",
                url=f"http://127.0.0.1:{self.port}/slow", timeout_sec=1,
            )
            r = self.runner._run_http(crit)
            # Some Python builds raise URLError(reason=socket.timeout);
            # others raise TimeoutError directly. Both → timed_out.
            self.assertEqual(r.status, "timed_out", msg=r.error_msg)
            self.assertIsNone(r.exit_code)
        finally:
            _StubHTTPHandler.slow_response_sec = 0.0

    def test_http_missing_url_inconclusive(self):
        crit = AcceptanceCriterion(
            description="no url", type="integration", method="http",
            url=None, timeout_sec=10,
        )
        r = self.runner._run_http(crit)
        self.assertEqual(r.status, "inconclusive")

    def test_http_file_scheme_rejected_inconclusive(self):
        # Defense in depth: a contract with method=http but a file://
        # URL must NOT silently turn into local file access. The
        # executor's contract is "network probe"; reject the URL as
        # malformed (inconclusive), don't return a verdict.
        crit = AcceptanceCriterion(
            description="file scheme", type="integration", method="http",
            url="file:///etc/passwd", timeout_sec=10,
        )
        r = self.runner._run_http(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIsNotNone(r.error_msg)
        self.assertIn("file", r.error_msg)
        self.assertIn("scheme", r.error_msg)
        self.assertIsNone(r.exit_code)

    def test_http_ftp_scheme_rejected_inconclusive(self):
        # Same guard for ftp:// — anything outside http(s) is rejected
        # as a malformed http-method criterion.
        crit = AcceptanceCriterion(
            description="ftp scheme", type="integration", method="http",
            url="ftp://example.com/", timeout_sec=10,
        )
        r = self.runner._run_http(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIsNotNone(r.error_msg)
        self.assertIn("ftp", r.error_msg)
        self.assertIn("scheme", r.error_msg)

    def test_http_https_scheme_passes_validation(self):
        # Control: https URLs must NOT be rejected by the scheme guard.
        # Point at the in-test ephemeral port — connection will refuse
        # since the stub is plain http, but the verdict path is
        # ``fail`` (URLError), not ``inconclusive`` (scheme reject).
        # That asserts the guard didn't trip on https.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        crit = AcceptanceCriterion(
            description="https control", type="integration", method="http",
            url=f"https://127.0.0.1:{port}/", timeout_sec=2,
        )
        r = self.runner._run_http(crit)
        # Either fail (connection refused) or timed_out — both prove
        # the guard accepted https and dispatched to urlopen.
        self.assertIn(r.status, ("fail", "timed_out"))
        self.assertNotEqual(r.status, "inconclusive")


# ---------------------------------------------------------------------------
# _effective_timeout (R7 table)
# ---------------------------------------------------------------------------


class TestEffectiveTimeout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.r = _make_runner(self.tmp)

    def _crit(self, **kw):
        # Build a criterion bypassing T1's parser so we can probe the
        # safety-net path. timeout_sec defaults to 0 (the dataclass
        # ordering placeholder); _effective_timeout treats 0 as absent.
        return AcceptanceCriterion(
            description="x",
            type=kw.get("type", "unit"),
            method=kw.get("method", "cmd"),
            timeout_sec=kw.get("timeout_sec", 0),
        )

    def test_explicit_timeout_wins(self):
        self.assertEqual(
            self.r._effective_timeout(self._crit(timeout_sec=42)), 42,
        )

    def test_default_per_method(self):
        for method, expected in DEFAULT_TIMEOUT_BY_METHOD.items():
            with self.subTest(method=method):
                self.assertEqual(
                    self.r._effective_timeout(self._crit(method=method)),
                    expected,
                )

    def test_e2e_type_overrides_to_1800(self):
        self.assertEqual(
            self.r._effective_timeout(
                self._crit(method="cmd", type="e2e")),
            E2E_TYPE_TIMEOUT,
        )
        # Even when method default is short (file_exists=30), e2e wins.
        self.assertEqual(
            self.r._effective_timeout(
                self._crit(method="file_exists", type="e2e")),
            E2E_TYPE_TIMEOUT,
        )

    def test_explicit_timeout_beats_e2e_default(self):
        # If contract author explicitly sets timeout_sec, it overrides
        # even the e2e default — they own the override.
        self.assertEqual(
            self.r._effective_timeout(
                self._crit(method="cmd", type="e2e", timeout_sec=120)),
            120,
        )


# ---------------------------------------------------------------------------
# default_idempotency (R8 table)
# ---------------------------------------------------------------------------


class TestDefaultIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.r = _make_runner(self.tmp)

    def _crit(self, type_="unit", method="cmd"):
        return AcceptanceCriterion(
            description="x", type=type_, method=method, timeout_sec=30,
        )

    def test_file_exists_default_true(self):
        self.assertEqual(
            self.r.default_idempotency(self._crit(method="file_exists")),
            "true",
        )

    def test_json_query_default_true(self):
        self.assertEqual(
            self.r.default_idempotency(self._crit(method="json_query")),
            "true",
        )

    def test_cmd_default_false(self):
        # R8 hardened: cmd default non-idempotent. T9 layers allowlist +
        # per-criterion override.
        self.assertEqual(
            self.r.default_idempotency(self._crit(method="cmd")),
            "false",
        )

    def test_http_get_default_true(self):
        self.assertEqual(
            self.r.default_idempotency(self._crit(method="http")),
            "true",
        )

    def test_e2e_type_always_false_no_override(self):
        # Design line 275: e2e ALWAYS non-idempotent regardless of method.
        for method in ("cmd", "file_exists", "json_query", "http"):
            with self.subTest(method=method):
                self.assertEqual(
                    self.r.default_idempotency(
                        self._crit(type_="e2e", method=method)),
                    "false",
                )


# ---------------------------------------------------------------------------
# run_one — orchestration shell + Y1 escalate + acceptance-progress events
# ---------------------------------------------------------------------------


def _read_progress_jsonl(task_dir: Path) -> list[dict]:
    path = task_dir / "acceptance-progress.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line
    ]


class TestRunOneOrchestration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.task_dir = Path(self.tmp) / "task"
        self.task_dir.mkdir()
        self.runner = _make_runner(self.tmp)

    def test_run_one_emits_started_and_completed(self):
        crit = AcceptanceCriterion(
            description="trivial", type="unit", method="cmd",
            command="true", timeout_sec=30,
        )
        r = self.runner.run_one(
            crit, criterion_idx=0, attempt_id="a1", retry_idx=0,
            task_dir=self.task_dir,
        )
        self.assertEqual(r.status, "pass")
        events = _read_progress_jsonl(self.task_dir)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "started")
        self.assertEqual(events[1]["event"], "completed")
        self.assertEqual(events[1]["status"], "pass")
        # T4 schema: started has all outcome fields = None.
        self.assertIsNone(events[0]["status"])
        self.assertIsNone(events[0]["completed_at"])
        # Both share criterion_hash.
        self.assertEqual(
            events[0]["criterion_hash"], events[1]["criterion_hash"],
        )
        self.assertEqual(events[1]["idempotent"], "false")  # cmd default

    def test_run_one_emits_timeout_event_on_timeout(self):
        crit = AcceptanceCriterion(
            description="long", type="unit", method="cmd",
            command="sleep 5", timeout_sec=1,
        )
        r = self.runner.run_one(
            crit, criterion_idx=0, attempt_id="a1", retry_idx=0,
            task_dir=self.task_dir,
        )
        self.assertEqual(r.status, "timed_out")
        events = _read_progress_jsonl(self.task_dir)
        # The 2nd event must be `timeout` (not `completed`).
        self.assertEqual(events[1]["event"], "timeout")
        self.assertEqual(events[1]["status"], "timed_out")

    def test_e2e_cmd_timeout_sets_escalate_flag(self):
        crit = AcceptanceCriterion(
            description="long e2e", type="e2e", method="cmd",
            command="sleep 5", timeout_sec=1,
        )
        r = self.runner.run_one(
            crit, criterion_idx=0, attempt_id="a", retry_idx=0,
            task_dir=self.task_dir,
        )
        self.assertEqual(r.status, "timed_out")
        # Y1: e2e timeout MUST escalate for §1 row 6 routing.
        self.assertTrue(r.escalate)

    def test_e2e_cmd_fail_sets_escalate_flag(self):
        crit = AcceptanceCriterion(
            description="bad e2e", type="e2e", method="cmd",
            command="false", timeout_sec=30,
        )
        r = self.runner.run_one(
            crit, criterion_idx=0, attempt_id="a", retry_idx=0,
            task_dir=self.task_dir,
        )
        self.assertEqual(r.status, "fail")
        # Y1: e2e fail MUST escalate too (design line 528).
        self.assertTrue(r.escalate)

    def test_non_e2e_timeout_does_not_escalate(self):
        crit = AcceptanceCriterion(
            description="long unit", type="unit", method="cmd",
            command="sleep 5", timeout_sec=1,
        )
        r = self.runner.run_one(
            crit, criterion_idx=0, attempt_id="a", retry_idx=0,
            task_dir=self.task_dir,
        )
        self.assertEqual(r.status, "timed_out")
        # §1 row 5 (block) — escalate stays False.
        self.assertFalse(r.escalate)

    def test_non_e2e_pass_does_not_escalate(self):
        crit = AcceptanceCriterion(
            description="ok", type="unit", method="cmd",
            command="true", timeout_sec=30,
        )
        r = self.runner.run_one(
            crit, criterion_idx=0, attempt_id="a", retry_idx=0,
            task_dir=self.task_dir,
        )
        self.assertEqual(r.status, "pass")
        self.assertFalse(r.escalate)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


class TestDispatchUnknownMethod(unittest.TestCase):
    """Defense-in-depth: T1 rejects unknown methods, but the dispatcher must
    not silently fall through (D1 — no silent path). We force an unknown
    method via direct construction (bypassing the parser)."""

    def test_unknown_method_inconclusive(self):
        with tempfile.TemporaryDirectory() as td:
            runner = _make_runner(td)
            crit = AcceptanceCriterion(
                description="bogus", type="unit", method="cmd",
                command="true", timeout_sec=30,
            )
            # Mutate post-construction to evade dataclass validation.
            crit.method = "fictional_method"  # type: ignore[assignment]
            r = runner._dispatch_method(crit)
            self.assertEqual(r.status, "inconclusive")
            self.assertIn("unknown method", r.error_msg or "")


# ---------------------------------------------------------------------------
# SAFETY-BOUNDARY hardening (codex R1 — T7 IS the safety boundary)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Return True if PID is still a live process. POSIX-only.

    ``os.kill(pid, 0)`` is the standard idiom: signal 0 doesn't deliver
    anything, but ``ProcessLookupError`` is raised iff PID is unknown to
    the kernel. ``PermissionError`` means the PID exists but we don't
    own it — for our test, the child is ours, so this won't trip.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestCmdProcessGroupKill(unittest.TestCase):
    """[P1] BLOCKER fix: ``cmd`` timeout must kill the WHOLE process group,
    not just the shell. With ``shell=True`` + ``subprocess.run``, the
    timeout would only kill the shell, leaving ``&``-backgrounded children
    alive. We now spawn with ``start_new_session=True`` and SIGTERM/SIGKILL
    the process group on TimeoutExpired."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_cmd_timeout_kills_backgrounded_descendant(self):
        # Spawn a shell that backgrounds a long sleep, writes the sleep's
        # PID to a sentinel file, then waits forever. When the timeout
        # fires we expect the whole process group dead — including the
        # backgrounded sleep. Without the process-group-kill fix the
        # sleep would survive the shell's death.
        pidfile = Path(self.tmp) / "child.pid"
        # 60s sleep — far longer than the 2s timeout; if it survives we'll
        # see it via ps/kill -0 and fail the test loud.
        cmd = (
            f"sleep 60 & echo $! > {pidfile}; "
            f"# wait blocks the shell so the timeout has a clean target\n"
            f"wait"
        )
        crit = AcceptanceCriterion(
            description="bg-sleep", type="unit", method="cmd",
            command=cmd, timeout_sec=2,
        )
        r = self.runner._run_cmd(crit)
        self.assertEqual(r.status, "timed_out", msg=r.error_msg)
        # Allow up to 2s for our SIGTERM/SIGKILL drain to land before we
        # check liveness — the runner already waited up to
        # PROCESS_GROUP_KILL_GRACE_SEC=2s, so this is belt-and-suspenders.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if pidfile.exists():
                break
            time.sleep(0.05)
        self.assertTrue(
            pidfile.exists(),
            msg="shell didn't write the sentinel pidfile before timeout",
        )
        child_pid = int(pidfile.read_text().strip())
        # Critical assertion: the backgrounded sleep must NOT be alive.
        # Without the fix, the shell dies but the sleep keeps going for
        # 60s. With the fix, killpg(SIGTERM/SIGKILL) takes out the group.
        # Wait briefly for kill to propagate.
        for _ in range(20):
            if not _pid_alive(child_pid):
                break
            time.sleep(0.1)
        self.assertFalse(
            _pid_alive(child_pid),
            msg=(
                f"backgrounded sleep PID {child_pid} survived per-criterion "
                f"timeout — process group kill is NOT working"
            ),
        )

    def test_cmd_timeout_signal_handler_ignored_falls_back_to_sigkill(self):
        # Trap SIGTERM so the shell ignores graceful shutdown. Without
        # the SIGKILL fallback the runner would block forever waiting
        # for an unkillable shell. With the fallback the criterion
        # times out cleanly + the shell dies.
        cmd = (
            "trap '' TERM; "
            "echo trapped; "
            "sleep 30"
        )
        crit = AcceptanceCriterion(
            description="trap-term", type="unit", method="cmd",
            command=cmd, timeout_sec=2,
        )
        t0 = time.monotonic()
        r = self.runner._run_cmd(crit)
        elapsed = time.monotonic() - t0
        self.assertEqual(r.status, "timed_out")
        # Should complete within timeout + 2s SIGTERM drain + slack.
        # If SIGKILL fallback didn't fire we'd see ~30s.
        self.assertLess(
            elapsed, 10,
            msg=f"timeout took {elapsed:.1f}s — SIGKILL fallback may not fire",
        )


class TestPathContainment(unittest.TestCase):
    """[P2] codex fix: file_exists and json_query reject paths that
    escape the worktree root (absolute paths or ``..`` traversal)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_file_exists_absolute_path_rejected(self):
        # ``/etc/passwd`` exists on every linux box; if path containment
        # were broken this would return ``pass``. With the fix it must
        # return ``inconclusive`` (malformed contract).
        crit = AcceptanceCriterion(
            description="abs", type="smoke", method="file_exists",
            path="/etc/passwd", timeout_sec=30,
        )
        r = self.runner._run_file_exists(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIn("outside worktree", r.error_msg or "")

    def test_file_exists_dotdot_traversal_rejected(self):
        # ``../../../etc/passwd`` would resolve outside the temp
        # worktree. Containment guard must catch that.
        crit = AcceptanceCriterion(
            description="traversal", type="smoke", method="file_exists",
            path="../../../etc/passwd", timeout_sec=30,
        )
        r = self.runner._run_file_exists(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIn("outside worktree", r.error_msg or "")

    def test_file_exists_relative_path_inside_worktree_unchanged(self):
        # Control: legitimate relative path inside the worktree still
        # works post-fix. Asserts the guard didn't over-fire.
        (Path(self.tmp) / "VERSION").write_text("0.8.1\n")
        crit = AcceptanceCriterion(
            description="ok", type="smoke", method="file_exists",
            path="VERSION", timeout_sec=30,
        )
        r = self.runner._run_file_exists(crit)
        self.assertEqual(r.status, "pass")

    def test_json_query_absolute_path_rejected(self):
        crit = AcceptanceCriterion(
            description="abs json", type="smoke", method="json_query",
            path="/etc/hostname",  # exists but not JSON; containment trips first
            json_query="x", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIn("outside worktree", r.error_msg or "")

    def test_json_query_dotdot_traversal_rejected(self):
        crit = AcceptanceCriterion(
            description="trav json", type="smoke", method="json_query",
            path="../../../etc/hostname", json_query="x", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIn("outside worktree", r.error_msg or "")


class TestJsonQuerySizeCap(unittest.TestCase):
    """[P2] codex fix: json_query refuses files > MAX_JSON_QUERY_FILE_BYTES
    BEFORE materializing them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_json_query_oversized_file_inconclusive(self):
        # Write a file just over the cap. We use a sparse file via
        # truncate() so we don't actually allocate 11 MB on disk —
        # but stat().st_size still reports the full size, which is
        # what the cap guard checks. (Sparse vs dense doesn't matter
        # here; we never get to read_text.)
        big = Path(self.tmp) / "huge.json"
        with open(big, "wb") as f:
            f.truncate(MAX_JSON_QUERY_FILE_BYTES + 1)
        crit = AcceptanceCriterion(
            description="huge", type="smoke", method="json_query",
            path="huge.json", json_query="x", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "inconclusive")
        # error_msg must mention the size (operator visibility) AND
        # the cap so an operator knows to either shrink the file or
        # raise the cap.
        self.assertIn("size", (r.error_msg or "").lower())
        self.assertIn(str(MAX_JSON_QUERY_FILE_BYTES), r.error_msg or "")

    def test_json_query_under_cap_unchanged(self):
        # Control: files within the cap still parse normally.
        small = Path(self.tmp) / "small.json"
        small.write_text(json.dumps({"ok": True}))
        crit = AcceptanceCriterion(
            description="small", type="smoke", method="json_query",
            path="small.json", json_query="ok", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "pass")


class _RedirectHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Configurable redirect server for HTTP boundary tests."""

    # Class attributes set per-test before each server start.
    redirect_target: str = "/ok"
    redirect_chain_remaining: int = 0

    def do_GET(self):
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/to-file":
            # 301 to a file:// URL — the bypass codex flagged.
            self.send_response(301)
            self.send_header("Location", "file:///etc/passwd")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/to-ftp":
            self.send_response(301)
            self.send_header("Location", "ftp://example.com/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/chain"):
            # /chain/N → redirects to /chain/(N-1) → ... → /ok.
            try:
                n = int(self.path.rsplit("/", 1)[1])
            except ValueError:
                n = 0
            if n <= 0:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            self.send_response(301)
            self.send_header("Location", f"/chain/{n - 1}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_a, **_kw):
        pass


class TestHttpRedirectBoundary(unittest.TestCase):
    """[P2] codex fix: redirect targets must re-validate scheme; redirect
    count + wall-clock deadline must bound total time."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(
            ("127.0.0.1", 0), _RedirectHTTPHandler,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_redirect_to_file_scheme_rejected(self):
        # A 301 → file:///etc/passwd would, without the fix, become
        # a successful local file read returning ``pass``. With the
        # custom redirect handler, the cross-scheme redirect is refused
        # and the verdict becomes ``fail`` (either via URLError on the
        # stdlib re-raise, or via HTTPError(301) when the chain dies).
        # The critical assertion is "NOT pass" — we never let the
        # file:// hop succeed.
        crit = AcceptanceCriterion(
            description="redirect-to-file", type="integration",
            method="http",
            url=f"http://127.0.0.1:{self.port}/to-file",
            timeout_sec=5,
        )
        r = self.runner._run_http(crit)
        self.assertNotEqual(
            r.status, "pass",
            msg="cross-scheme redirect to file:// MUST NOT succeed",
        )
        self.assertEqual(r.status, "fail")
        # If exit_code is set, it must be the 301 from the original
        # redirect response, NOT 200 (which would mean the file:// hop
        # succeeded). 2xx here is the bypass we're blocking.
        if r.exit_code is not None:
            self.assertNotIn(r.exit_code, range(200, 300))

    def test_redirect_to_ftp_scheme_rejected(self):
        crit = AcceptanceCriterion(
            description="redirect-to-ftp", type="integration",
            method="http",
            url=f"http://127.0.0.1:{self.port}/to-ftp",
            timeout_sec=5,
        )
        r = self.runner._run_http(crit)
        self.assertNotEqual(r.status, "pass")
        self.assertEqual(r.status, "fail")
        if r.exit_code is not None:
            self.assertNotIn(r.exit_code, range(200, 300))

    def test_redirect_chain_capped(self):
        # MAX_HTTP_REDIRECTS=1; a chain of 5 must NOT pass even though
        # the final hop is /ok. Stdlib raises HTTPError on too-many-
        # redirects, which our executor maps to ``fail``. Either way
        # the verdict must NOT be ``pass``.
        crit = AcceptanceCriterion(
            description="long-chain", type="integration", method="http",
            url=f"http://127.0.0.1:{self.port}/chain/5",
            timeout_sec=5,
        )
        r = self.runner._run_http(crit)
        self.assertNotEqual(
            r.status, "pass",
            msg=(
                "5-hop chain should exceed MAX_HTTP_REDIRECTS="
                f"{MAX_HTTP_REDIRECTS}; got pass which means cap is broken"
            ),
        )

    def test_single_redirect_under_cap_succeeds(self):
        # MAX_HTTP_REDIRECTS=1: a single hop /chain/1 → /ok must pass.
        # Verifies the cap doesn't over-fire on legitimate 1-redirect
        # APIs.
        crit = AcceptanceCriterion(
            description="single-hop", type="integration", method="http",
            url=f"http://127.0.0.1:{self.port}/chain/1",
            timeout_sec=5,
        )
        r = self.runner._run_http(crit)
        self.assertEqual(r.status, "pass", msg=r.error_msg)
        self.assertEqual(r.exit_code, 200)


# ---------------------------------------------------------------------------
# SAFETY-BOUNDARY hardening — codex R2 follow-on fixes
# ---------------------------------------------------------------------------


class TestCmdProcessGroupKillR2(unittest.TestCase):
    """[P1 codex R2] Process-group kill must defeat SIGTERM-trapping
    grandchildren even when the shell exits cleanly on SIGTERM.

    The previous fix relied on ``proc.wait(timeout=...)`` to confirm the
    group was dead, but that only observes the SHELL — a child that
    ``trap`` 's SIGTERM and lets the shell exit cleanly will pass that
    wait while keeping running. The fix probes the GROUP via
    ``killpg(pgid, 0)`` and ALWAYS sends SIGKILL after the grace window
    as defense-in-depth.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_grandchild_traps_sigterm_dies_via_sigkill(self):
        # The shell spawns a child Python that traps SIGTERM (ignores it),
        # writes its PID to a sentinel file, and sleeps. The shell then
        # exits via the same SIGTERM (we don't trap in the shell). Without
        # the SIGKILL-the-group fallback, the grandchild Python keeps
        # running because:
        #   1. SIGTERM goes to the group → shell + child both receive it
        #   2. shell exits → proc.wait() returns
        #   3. old code returns "drain succeeded" → no SIGKILL fires
        #   4. child Python (SIGTERM-ignored) keeps running for 60s
        # With the fix we ALWAYS SIGKILL the group, so the child dies.
        pidfile = Path(self.tmp) / "grandchild.pid"
        # Use Python directly so we get a real signal handler. ``signal``
        # at the shell level can be unreliable across shells.
        py = sys.executable
        # The grandchild is a Python process that ignores SIGTERM and
        # sleeps. We run it in the SAME process group as the shell (no
        # extra setsid) so it shares the group PID.
        grandchild_script = (
            f"import signal, time, os; "
            f"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
            f"time.sleep(60)"
        )
        # Background the grandchild, then ``wait`` so the shell has
        # something to time out on.
        cmd = (
            f"{py} -c {json.dumps(grandchild_script)} & "
            f"echo $! >&2; "
            f"wait"
        )
        crit = AcceptanceCriterion(
            description="trap-grandchild", type="unit", method="cmd",
            command=cmd, timeout_sec=2,
        )
        r = self.runner._run_cmd(crit)
        self.assertEqual(r.status, "timed_out", msg=r.error_msg)
        # The grandchild should have written its PID before the timeout
        # fires (it does so before sleep). Wait briefly for it.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if pidfile.exists():
                break
            time.sleep(0.05)
        self.assertTrue(
            pidfile.exists(),
            msg="grandchild didn't write the sentinel pidfile in time",
        )
        gc_pid = int(pidfile.read_text().strip())
        # Critical: the SIGTERM-ignored grandchild must NOT be alive.
        # Wait briefly for the SIGKILL to propagate post-runner-return.
        for _ in range(30):
            if not _pid_alive(gc_pid):
                break
            time.sleep(0.1)
        self.assertFalse(
            _pid_alive(gc_pid),
            msg=(
                f"SIGTERM-ignored grandchild PID {gc_pid} survived per-"
                f"criterion timeout — SIGKILL fallback NOT firing"
            ),
        )


class TestHttpInitialDeadline(unittest.TestCase):
    """[P2 codex R2] The HTTP initial request must respect a wall-clock
    deadline, not just per-socket-op timeouts.

    A server that delays its first byte for >timeout_sec wall-clock can
    pass through ``opener.open(timeout=N)`` if no individual socket op
    takes >N seconds. We add a wall-clock deadline check after the
    open returns.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = _QuietHTTPServer(("127.0.0.1", 0), _StubHTTPHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_slow_server_exceeds_deadline_returns_timed_out(self):
        # Server delays response by 3s; criterion timeout is 1s. With
        # the deadline check, the verdict must be ``timed_out`` rather
        # than ``pass`` (which would happen if we waited 3s for the
        # response and then returned 200).
        # Note: depending on socket timing the per-socket-op timeout
        # may fire first → ``timed_out`` directly; OR the read may
        # complete past deadline → wall-clock check fires. Either path
        # MUST result in ``timed_out``, never ``pass``.
        _StubHTTPHandler.slow_response_sec = 3.0
        try:
            crit = AcceptanceCriterion(
                description="slow", type="integration", method="http",
                url=f"http://127.0.0.1:{self.port}/slow", timeout_sec=1,
            )
            r = self.runner._run_http(crit)
            self.assertEqual(
                r.status, "timed_out",
                msg=f"slow server should hit deadline, got {r.status}: "
                    f"{r.error_msg}",
            )
            self.assertNotEqual(
                r.status, "pass",
                msg="MUST NOT silently pass a >timeout_sec wall-clock request",
            )
        finally:
            _StubHTTPHandler.slow_response_sec = 0.0


class _SlowChainHandler(http.server.BaseHTTPRequestHandler):
    """Redirect chain handler that sleeps between hops to exhaust
    wall-clock deadline mid-chain."""

    delay_per_hop: float = 0.0

    def do_GET(self):
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path.startswith("/slow-chain"):
            try:
                n = int(self.path.rsplit("/", 1)[1])
            except ValueError:
                n = 0
            if self.delay_per_hop > 0:
                import time as _t
                _t.sleep(self.delay_per_hop)
            if n <= 0:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            self.send_response(301)
            self.send_header("Location", f"/slow-chain/{n - 1}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_a, **_kw):
        pass


class TestHttpRedirectDeadlineRouting(unittest.TestCase):
    """[P2 codex R2] When a redirect-chain wall-clock deadline fires,
    the verdict must be ``timed_out`` (matching criterion intent), not
    ``fail`` (the generic URLError bucket).

    The fix introduces ``_HttpDeadlineExceeded`` as a dedicated URLError
    subclass that the executor catches FIRST for clean routing.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(
            ("127.0.0.1", 0), _SlowChainHandler,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_redirect_chain_deadline_routes_to_timed_out(self):
        # Each hop sleeps 0.6s. With MAX_HTTP_REDIRECTS=1 we only get
        # one redirect, so we can't easily blow the deadline via
        # redirect-chain alone here — but the code path we care about
        # (deadline-during-redirect → _HttpDeadlineExceeded → timed_out)
        # is exercised when the chain hop sleep itself pushes us past
        # deadline before the redirect callback. Set timeout_sec to a
        # value just above the FIRST hop but below TWO hops; the
        # redirect callback runs AFTER the first hop so by then deadline
        # has elapsed → _HttpDeadlineExceeded → timed_out.
        _SlowChainHandler.delay_per_hop = 0.6
        try:
            crit = AcceptanceCriterion(
                description="slow-chain", type="integration",
                method="http",
                # /slow-chain/2 → /slow-chain/1 → /slow-chain/0 (200).
                # MAX_HTTP_REDIRECTS=1 also kills this, but that path
                # routes via stdlib HTTPError → fail. The deadline path
                # we're testing fires when the redirect callback runs
                # AFTER the wall-clock has already elapsed.
                url=f"http://127.0.0.1:{self.port}/slow-chain/2",
                timeout_sec=1,
            )
            r = self.runner._run_http(crit)
            # The redirect callback's deadline check is the canonical
            # path here. timeout_sec=1 + 0.6s first-hop delay means
            # by the time the redirect callback fires, monotonic() >
            # deadline → _HttpDeadlineExceeded → timed_out.
            #
            # Edge case: the per-socket-op timeout may also fire if
            # the socket layer detects the slow read first → also
            # timed_out via the URLError reason=timeout branch. Both
            # paths produce timed_out; the assertion is on the
            # verdict not the path.
            self.assertEqual(
                r.status, "timed_out",
                msg=f"redirect-chain deadline should route to timed_out, "
                    f"got {r.status}: {r.error_msg}",
            )
        finally:
            _SlowChainHandler.delay_per_hop = 0.0


class TestJsonQueryBoundedRead(unittest.TestCase):
    """[P2 codex R2] json_query must enforce its size cap via a bounded
    read, not via stat() + read(). The previous stat-then-read pattern
    had a TOCTOU race where the file could grow between the stat and
    the read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_oversize_dense_file_rejected_via_bounded_read(self):
        # Write a real (non-sparse) file just over the cap. The cap
        # check must trip from the BOUNDED READ (len(data) > cap+1
        # bytes worth), not from stat().st_size. This proves we don't
        # depend on stat — covers the TOCTOU race even though we don't
        # exercise the race directly.
        big = Path(self.tmp) / "huge.json"
        # 1 MiB chunks of '{' to keep memory low while writing.
        chunk = b"{" * (1024 * 1024)
        with open(big, "wb") as f:
            for _ in range(11):  # 11 MiB > 10 MiB cap
                f.write(chunk)
        crit = AcceptanceCriterion(
            description="huge dense", type="smoke", method="json_query",
            path="huge.json", json_query="x", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIn("size", (r.error_msg or "").lower())
        self.assertIn(str(MAX_JSON_QUERY_FILE_BYTES), r.error_msg or "")

    def test_under_cap_file_still_parses(self):
        # Control: post-fix, a normal-sized JSON file still parses
        # correctly via the new bounded-read path.
        small = Path(self.tmp) / "small.json"
        small.write_text(json.dumps({"ok": True, "list": [1, 2, 3]}))
        crit = AcceptanceCriterion(
            description="small", type="smoke", method="json_query",
            path="small.json", json_query="ok", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "pass")


class TestPathResolveSymlinkLoop(unittest.TestCase):
    """[P2 codex R2] ``Path.resolve()`` can raise OSError(ELOOP) or
    RuntimeError on symlink loops. Containment must catch these and
    return inconclusive instead of crashing the runner."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = _make_runner(self.tmp)

    def test_symlink_loop_path_returns_inconclusive(self):
        # Create a symlink loop: a -> b, b -> a. resolve() will raise
        # on this. The containment helper must catch and return
        # inconclusive — NOT propagate the exception to the runner.
        a = Path(self.tmp) / "a"
        b = Path(self.tmp) / "b"
        a.symlink_to(b)
        b.symlink_to(a)
        crit = AcceptanceCriterion(
            description="loop", type="smoke", method="file_exists",
            path="a", timeout_sec=30,
        )
        # Must not raise.
        r = self.runner._run_file_exists(crit)
        self.assertEqual(r.status, "inconclusive")
        self.assertIsNotNone(r.error_msg)
        # Either ELOOP-flavored OS error or symlink-loop RuntimeError —
        # both routed via the same helper. Just check the helper
        # produced an operator-readable signal.
        msg = (r.error_msg or "").lower()
        self.assertTrue(
            "symlink" in msg or "could not be resolved" in msg
            or "loop" in msg,
            msg=f"expected symlink-loop diagnostic, got: {r.error_msg!r}",
        )

    def test_symlink_loop_json_query_returns_inconclusive(self):
        # Same race for json_query — both methods route through the
        # same _resolve_within_worktree helper.
        a = Path(self.tmp) / "loop_a.json"
        b = Path(self.tmp) / "loop_b.json"
        a.symlink_to(b)
        b.symlink_to(a)
        crit = AcceptanceCriterion(
            description="loop json", type="smoke", method="json_query",
            path="loop_a.json", json_query="x", timeout_sec=30,
        )
        r = self.runner._run_json_query(crit)
        self.assertEqual(r.status, "inconclusive")


if __name__ == "__main__":
    unittest.main()
