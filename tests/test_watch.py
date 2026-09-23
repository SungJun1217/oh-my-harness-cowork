from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest

from omhc import index, locate, watch


def git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        self.root = os.path.realpath(self.repo)
        self.state = locate.state_dir(locate.repo_key(self.root), home=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def plant_codex(self, session_id="cx1", extra_turns=0):
        stamp = time.gmtime()
        directory = os.path.join(self.home, ".codex", "sessions",
                                 time.strftime("%Y/%m/%d", stamp))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "rollout-{}.jsonl".format(session_id))
        rows = [
            {"timestamp": "2026-09-22T16:30:00.000Z", "ordinal": 0,
             "type": "session_meta",
             "payload": {"session_id": session_id, "cwd": self.root}},
            {"timestamp": "2026-09-22T16:30:01.000Z", "ordinal": 1,
             "type": "response_item",
             "payload": {"type": "message", "role": "user", "id": "u0",
                         "content": [{"type": "input_text", "text": "첫 말"}]}},
        ]
        for i in range(extra_turns):
            rows.append({
                "timestamp": "2026-09-22T16:30:0{}.000Z".format(2 + i % 8),
                "ordinal": 2 + i, "type": "response_item",
                "payload": {"type": "function_call", "name": "shell",
                            "call_id": "c{}".format(i),
                            "arguments": json.dumps({"command": ["ls", str(i)]})}})
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def append_turn(self, path, i):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "timestamp": "2026-09-22T16:31:00.000Z", "ordinal": 90 + i,
                "type": "response_item",
                "payload": {"type": "function_call", "name": "shell",
                            "call_id": "late{}".format(i),
                            "arguments": json.dumps({"command": ["echo", str(i)]})},
            }, ensure_ascii=False) + "\n")


class TestLock(Base):
    def test_acquire_then_release(self):
        watch.acquire(self.state)
        self.assertEqual(watch.read_lock(self.state), os.getpid())
        watch.release(self.state)
        self.assertIsNone(watch.read_lock(self.state))

    def test_second_acquire_is_refused(self):
        watch.acquire(self.state)
        try:
            with self.assertRaises(watch.LockBusy):
                watch.acquire(self.state)
        finally:
            watch.release(self.state)

    def test_stale_lock_from_a_dead_pid_is_cleaned(self):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, watch.LOCK_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("999999999")
        self.assertIsNone(watch.read_lock(self.state))
        watch.acquire(self.state)
        watch.release(self.state)

    def test_garbage_lock_file_does_not_raise(self):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, watch.LOCK_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("not a pid")
        self.assertIsNone(watch.read_lock(self.state))


class TestSweep(Base):
    def test_sweep_indexes_a_new_session(self):
        self.plant_codex(extra_turns=3)
        written = watch.sweep(self.root, self.state, home=self.home)
        self.assertGreater(written, 0)
        idx = os.path.join(self.state, "index", "cx1.idx")
        self.assertTrue(os.path.exists(idx))

    def test_sweep_is_incremental_not_duplicating(self):
        path = self.plant_codex(extra_turns=2)
        first = watch.sweep(self.root, self.state, home=self.home)
        second = watch.sweep(self.root, self.state, home=self.home)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0, "같은 내용을 두 번 색인하면 안 된다")
        idx = os.path.join(self.state, "index", "cx1.idx")
        seqs = [r.seq for r in index.rows(idx)]
        self.assertEqual(len(seqs), len(set(seqs)), "중복 seq 가 생겼다")
        self.append_turn(path, 1)
        third = watch.sweep(self.root, self.state, home=self.home)
        self.assertEqual(third, 1, "새로 자란 부분만 색인해야 한다")

    def test_sweep_pins_the_source(self):
        path = self.plant_codex()
        watch.sweep(self.root, self.state, home=self.home)
        pinned = os.path.join(self.state, "pinned", "cx1", "source.jsonl")
        self.assertTrue(os.path.exists(pinned))
        self.assertEqual(os.stat(path).st_ino, os.stat(pinned).st_ino)

    def test_sweep_with_nothing_to_do_returns_zero(self):
        self.assertEqual(watch.sweep(self.root, self.state, home=self.home), 0)

    def test_sweep_never_raises_on_a_broken_session_file(self):
        path = self.plant_codex()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{broken\n")
        watch.sweep(self.root, self.state, home=self.home)

    def test_lag_reports_the_gap(self):
        path = self.plant_codex(extra_turns=2)
        watch.sweep(self.root, self.state, home=self.home)
        self.append_turn(path, 1)
        rows = watch.lag(self.state)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["lag_bytes"], 0)
        watch.sweep(self.root, self.state, home=self.home)
        self.assertLess(watch.lag(self.state)[0]["lag_bytes"],
                        rows[0]["lag_bytes"])


class TestRunLoop(Base):
    def test_run_exits_after_max_sweeps_and_releases_the_lock(self):
        self.plant_codex(extra_turns=1)
        code = watch.run(self.root, home=self.home, poll=0.0, max_sweeps=1)
        self.assertEqual(code, 0)
        self.assertIsNone(watch.read_lock(self.state))

    def test_run_exits_when_idle_past_the_timeout(self):
        clock = {"t": 1000.0}

        def fake_now():
            clock["t"] += 10_000.0
            return clock["t"]

        code = watch.run(self.root, home=self.home, poll=0.0, idle_exit=60.0,
                         now=fake_now)
        self.assertEqual(code, 0)
        self.assertIsNone(watch.read_lock(self.state))

    def test_run_refuses_a_second_instance(self):
        watch.acquire(self.state)
        try:
            with self.assertRaises(watch.LockBusy):
                watch.run(self.root, home=self.home, poll=0.0, max_sweeps=1)
        finally:
            watch.release(self.state)


class TestCorrectnessIndependence(unittest.TestCase):
    def test_brief_does_not_import_or_require_watch(self):
        """데몬이 정확성을 담당하지 않는다는 것을 구조로 확인한다."""
        import inspect

        from omhc import brief

        source = inspect.getsource(brief)
        self.assertNotIn("watch", source)


if __name__ == "__main__":
    unittest.main()
