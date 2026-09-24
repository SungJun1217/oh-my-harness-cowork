from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest

from omhc import index, watch

from . import _repo


class Base(unittest.TestCase):
    def setUp(self):
        self.t = _repo.TempRepo()
        self.addCleanup(self.t.close)
        self.home = self.t.home
        self.root = self.t.root
        self.state = self.t.state
        # 스킵 캐시는 모듈 전역이다. 테스트 간 오염을 막는다.
        watch.forget()

    def plant_codex(self, session_id="cx1", extra_turns=0):
        return self.t.plant_codex(session_id=session_id, human="첫 말",
                                  shell_turns=extra_turns)

    def append_turn(self, path, i):
        _repo.append_codex_turn(path, ordinal=90 + i)


class TestSweepCursor(Base):
    def test_session_indexed_by_an_older_parser_still_gets_new_rows(self):
        """업그레이드 전 파서가 더 많은 이벤트를 세어 색인의 seq 가 지금 파서보다
        크다(#23). seq 커서였다면 이어진 턴이 `seq > last_seq` 에 걸려 영영
        색인되지 않았다."""
        path = self.plant_codex()
        watch.sweep(self.root, self.state, home=self.home)
        idx = os.path.join(self.state, "index", "cx1.idx")
        old = index.rows(idx)
        self.assertTrue(old)
        with open(idx, "w", encoding="utf-8") as fh:
            for row in old:
                fh.write("\t".join((str(row.seq + 10), "{:.0f}".format(row.epoch),
                                    row.author, row.verb, "1" if row.ok else "0",
                                    str(row.offset), str(row.length),
                                    ",".join(row.paths), row.arg)) + "\n")

        self.append_turn(path, 1)
        watch.forget()
        self.assertGreater(watch.sweep(self.root, self.state, home=self.home), 0)
        rows = index.rows(idx)
        self.assertGreater(len(rows), len(old))
        self.assertGreater(rows[-1].offset, old[-1].offset)
        seqs = [r.seq for r in rows]
        self.assertEqual(seqs, list(range(11, 11 + len(rows))))


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

    def test_unchanged_files_are_not_reread(self):
        """5초 폴링 데몬이 정상 상태에서 3.2MB 를 매번 재파싱하면 시간당 CPU 4분,
        재독 28GB 인데 새 이벤트는 0건이다."""
        path = self.plant_codex(extra_turns=2)
        watch.sweep(self.root, self.state, home=self.home)
        reads = {"n": 0}
        from omhc.adapters import codex_cli

        original = codex_cli.CodexCliAdapter.read_session

        def counting(self_, ref):
            reads["n"] += 1
            return original(self_, ref)

        codex_cli.CodexCliAdapter.read_session = counting
        try:
            watch.sweep(self.root, self.state, home=self.home)
            self.assertEqual(reads["n"], 0, "변하지 않은 파일을 다시 읽었다")
            self.append_turn(path, 1)
            watch.sweep(self.root, self.state, home=self.home)
            self.assertEqual(reads["n"], 1, "자란 파일은 다시 읽어야 한다")
        finally:
            codex_cli.CodexCliAdapter.read_session = original

    def test_forget_makes_the_next_sweep_re_read(self):
        self.plant_codex(extra_turns=1)
        watch.sweep(self.root, self.state, home=self.home)
        self.assertEqual(watch.sweep(self.root, self.state, home=self.home), 0)
        watch.forget()
        # 다시 읽어도 색인은 증분이므로 새 행은 0이다 — 읽기만 다시 일어난다.
        self.assertEqual(watch.sweep(self.root, self.state, home=self.home), 0)

    def test_sweep_pins_the_source(self):
        path = self.plant_codex()
        watch.sweep(self.root, self.state, home=self.home)
        pinned = os.path.join(self.state, "pinned", "cx1", "source.jsonl")
        self.assertTrue(os.path.exists(pinned))
        self.assertEqual(os.stat(path).st_ino, os.stat(pinned).st_ino)

    def test_lag_reports_pinned_false_when_indexed_but_never_pinned(self):
        """omhc status 의 archive 행이 이 필드로 "고정 안 됨"과 "고정됐지만
        꼬리가 0바이트"를 구분한다(리뷰 결함) — 색인만 있고 핀이 없으면 size 도
        lag_bytes 도 0 이라 `pinned` 없이는 구분할 방법이 없었다."""
        idx_dir = os.path.join(self.state, "index")
        os.makedirs(idx_dir, exist_ok=True)
        from omhc.event import Event

        ev = Event(seq=1, epoch=1700000000.0, author="human", verb="said",
                  ok=True, text="hi", arg="hi", paths=(), offset=0, length=10)
        index.append_rows(os.path.join(idx_dir, "s1.idx"), [ev])

        rows = watch.lag(self.state)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["pinned"])
        self.assertEqual(rows[0]["size"], 0)

    def test_lag_reports_pinned_true_once_sweep_has_pinned_it(self):
        self.plant_codex()
        watch.sweep(self.root, self.state, home=self.home)
        rows = watch.lag(self.state)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["pinned"])

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
    def test_brief_does_not_import_watch(self):
        """데몬이 정확성을 담당하지 않는다는 것을 import 그래프로 확인한다.

        문자열 검사로는 안 된다 — 주석에 watch 를 언급하는 것은 정당하다.
        """
        import ast
        import inspect

        from omhc import brief

        tree = ast.parse(inspect.getsource(brief))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(a.name for a in node.names)
        self.assertNotIn("watch", imported)
        self.assertNotIn("omhc.watch", imported)


if __name__ == "__main__":
    unittest.main()
