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
        ledger.append(
            {"repo": "r" * 500, "harness": "claude", "event": "start", "path": "p" * 500},
            home=self.home,
        )
        with open(self._path(), "rb") as fh:
            self.assertLessEqual(len(fh.readline()), 400)

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

    def test_newest_filters_by_repo_and_excludes_harness(self):
        ledger.append({"repo": "r", "harness": "claude", "event": "start", "epoch": 1}, home=self.home)
        ledger.append({"repo": "r", "harness": "codex", "event": "start", "epoch": 2}, home=self.home)
        ledger.append({"repo": "other", "harness": "codex", "event": "start", "epoch": 3}, home=self.home)
        row = ledger.newest("r", exclude_harness="claude", home=self.home)
        self.assertIsNotNone(row)
        self.assertEqual(row["harness"], "codex")
        self.assertEqual(row["epoch"], 2)

    def test_newest_returns_none_when_ledger_missing(self):
        self.assertIsNone(ledger.newest("r", home=self.home))

    def test_newest_ignores_non_start_events(self):
        ledger.append({"repo": "r", "harness": "codex", "event": "pull", "epoch": 9}, home=self.home)
        self.assertIsNone(ledger.newest("r", home=self.home))


if __name__ == "__main__":
    unittest.main()
