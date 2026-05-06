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
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_acceptance import (   # type: ignore  # noqa: E402
    AcceptanceRunner,
    RunResult,
    DEFAULT_TIMEOUT_BY_METHOD,
    E2E_TYPE_TIMEOUT,
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


if __name__ == "__main__":
    unittest.main()
