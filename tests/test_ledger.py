from __future__ import annotations

import json
import os
import tempfile
import unittest

from omhc import ledger


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self):
        return os.path.join(self.home, ".omhc", ledger.LEDGER_NAME)

    def test_append_then_read_roundtrip(self):
        ledger.append({"repo": "r", "harness": "claude", "event": "start"}, home=self.home)
        rows = ledger.read(home=self.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["harness"], "claude")

    def test_lines_stay_under_400_bytes(self):
        """장식용 필드(cwd)를 줄여 상한에 맞춘다."""
        ok = ledger.append(
            {"repo": "k", "harness": "claude", "session": "s", "event": "start",
             "path": "/p" * 60, "cwd": "/very/long/cwd" * 20},
            home=self.home,
        )
        self.assertTrue(ok)
        with open(self._path(), "rb") as fh:
            self.assertLessEqual(len(fh.readline()), 400)

    def test_identity_fields_are_never_truncated(self):
        """path 와 session 을 자르면 문법상 유효하지만 아무것도 가리키지 않는
        줄이 되어 핸드오프가 조용히 실패하고 guard.log 에도 남지 않는다."""
        path = "/home/user/work/clients/acme/monorepo/" + "x" * 80
        session = "s" * 36
        ledger.append(
            {"repo": "k", "harness": "codex-cli", "session": session,
             "event": "start", "path": path, "cwd": "/c" * 80},
            home=self.home,
        )
        row = ledger.read(home=self.home)[0]
        self.assertEqual(row["path"], path)
        self.assertEqual(row["session"], session)

    def test_a_row_that_cannot_fit_is_refused_not_mangled(self):
        ok = ledger.append(
            {"repo": "k", "harness": "codex-cli", "session": "s" * 36,
             "event": "start", "path": "/p" * 300},
            home=self.home,
        )
        self.assertFalse(ok, "맞출 수 없으면 쓰지 않고 False 를 돌려줘야 한다")
        self.assertEqual(ledger.read(home=self.home), [])

    def test_repo_filter_is_applied_before_the_line_limit(self):
        """원장은 머신 전체가 공유하는 한 파일이다. 먼저 자르면 이 레포의 줄이
        창 밖으로 밀려나 핸드오프가 조용히 멈춘다."""
        for i in range(50):
            ledger.append({"repo": "other-repo", "harness": "claude",
                           "session": "o{}".format(i), "event": "start"},
                          home=self.home)
        ledger.append({"repo": "mine", "harness": "codex-cli", "session": "m1",
                       "event": "start"}, home=self.home)
        for i in range(50):
            ledger.append({"repo": "other-repo", "harness": "claude",
                           "session": "p{}".format(i), "event": "start"},
                          home=self.home)
        rows = ledger.read(limit=10, home=self.home, repo_key="mine")
        self.assertEqual([r["session"] for r in rows], ["m1"])

    def test_concurrent_appends_produce_no_partial_records(self):
        pids = []
        for _ in range(8):
            pid = os.fork()
            if pid == 0:
                try:
                    for i in range(200):
                        ledger.append(
                            {"repo": "r", "harness": "h", "event": "start", "n": i},
                            home=self.home,
                        )
                finally:
                    os._exit(0)
            pids.append(pid)
        for pid in pids:
            os.waitpid(pid, 0)
        with open(self._path()) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1600)
        for line in lines:
            json.loads(line)

    def test_corrupt_half_line_is_skipped_not_fatal(self):
        path = self._path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(
                '{"repo":"a","event":"start"}\n'
                '{"repo":"b","ev\n'
                '{"repo":"c","event":"start"}\n'
            )
        rows = ledger.read(home=self.home)
        self.assertEqual([r["repo"] for r in rows], ["a", "c"])


if __name__ == "__main__":
    unittest.main()
