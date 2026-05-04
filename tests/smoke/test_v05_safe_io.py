#!/usr/bin/env python3
"""Smoke tests for v0.5 safe_io — atomic writes + fcntl.flock."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class AtomicWriteText(unittest.TestCase):
    def test_writes_file_with_content(self):
        from common.safe_io import atomic_write_text
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.txt"
            atomic_write_text(p, "hello\n")
            self.assertEqual(p.read_text(), "hello\n")

    def test_overwrite_is_atomic_no_temp_left_behind(self):
        from common.safe_io import atomic_write_text
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.txt"
            atomic_write_text(p, "v1\n")
            atomic_write_text(p, "v2\n")
            self.assertEqual(p.read_text(), "v2\n")
            tmp_files = [f for f in Path(tmp).iterdir() if f.name != "out.txt"]
            self.assertEqual(tmp_files, [], f"stray temp files: {tmp_files}")


class AtomicWriteJson(unittest.TestCase):
    def test_writes_valid_json(self):
        from common.safe_io import atomic_write_json
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.json"
            atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
            data = json.loads(p.read_text())
            self.assertEqual(data, {"a": 1, "b": [1, 2, 3]})

    def test_trailing_newline(self):
        from common.safe_io import atomic_write_json
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.json"
            atomic_write_json(p, {"x": 1})
            self.assertTrue(p.read_text().endswith("\n"))


class AppendJsonlLocked(unittest.TestCase):
    def test_simple_append(self):
        from common.safe_io import append_jsonl_locked
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "log.jsonl"
            self.assertTrue(append_jsonl_locked(p, {"a": 1}))
            self.assertTrue(append_jsonl_locked(p, {"a": 2}))
            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), {"a": 1})
            self.assertEqual(json.loads(lines[1]), {"a": 2})

    # NOTE: this validates concurrent THREAD appends only (GIL-serialized);
    # cross-process safety from fcntl.flock is not exercised here. v0.6 may
    # add a multiprocessing test if cross-process audit becomes load-bearing.
    def test_concurrent_thread_appends_complete(self):
        from common.safe_io import append_jsonl_locked
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "log.jsonl"
            N_THREADS = 8
            N_PER_THREAD = 25

            def worker(tid):
                for i in range(N_PER_THREAD):
                    append_jsonl_locked(p, {"tid": tid, "i": i})

            threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), N_THREADS * N_PER_THREAD)
            for ln in lines:
                obj = json.loads(ln)
                self.assertIn("tid", obj)
                self.assertIn("i", obj)


if __name__ == "__main__":
    unittest.main(verbosity=2)
