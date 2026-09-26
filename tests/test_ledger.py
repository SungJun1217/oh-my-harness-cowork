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

    def test_lines_stay_under_the_line_cap(self):
        """Shortens a decorative field (cwd) to fit under the cap."""
        ok = ledger.append(
            {"repo": "k", "harness": "claude", "session": "s", "event": "start",
             "path": "/p" * 60, "cwd": "/very/long/cwd" * 20},
            home=self.home,
        )
        self.assertTrue(ok)
        with open(self._path(), "rb") as fh:
            self.assertLessEqual(len(fh.readline()), ledger.MAX_LINE)

    def test_a_realistic_long_home_row_now_fits(self):
        """#22: with a long HOME (observed ~80-100 chars), the Claude backfill path
        exceeded the old 400-byte cap and was silently dropped. It must fit under the 800-byte cap."""
        home = "/Users/" + "u" * 70
        root = os.path.join(home, "Projects", "some-repo-name")
        slug = "-" + root.strip("/").replace("/", "-")
        path = os.path.join(home, ".claude", "projects", slug,
                            "550e8400-e29b-41d4-a716-446655440000.jsonl")
        ok = ledger.append(
            {"repo": "some-repo-name-abcdef01", "harness": "claude-code",
             "session": "550e8400-e29b-41d4-a716-446655440000", "event": "start",
             "epoch": 1700000000, "path": path, "cwd": root},
            home=self.home,
        )
        self.assertTrue(ok)
        row = ledger.read(home=self.home)[0]
        self.assertEqual(row["path"], path)

    def test_identity_fields_are_never_truncated(self):
        """Truncating path or session yields a line that's syntactically valid
        but points to nothing, so the handoff fails silently and doesn't even
        show up in guard.log."""
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
             "event": "start", "path": "/p" * 400},
            home=self.home,
        )
        self.assertFalse(ok, "if it can't fit, must return False without writing")
        self.assertEqual(ledger.read(home=self.home), [])

    def test_a_refusal_is_recorded_for_status_to_show(self):
        """#22: even if the caller discards append()'s return value, the refusal
        itself remains in ledger.rejected for `omhc status` to show."""
        ok = ledger.append(
            {"repo": "k", "harness": "codex-cli", "session": "s" * 36,
             "event": "start", "path": "/p" * 400},
            home=self.home,
        )
        self.assertFalse(ok)
        rejected = ledger.read_rejected(home=self.home, repo_key="k")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["harness"], "codex-cli")
        self.assertGreater(rejected[0]["bytes"], ledger.MAX_LINE)
        self.assertEqual(ledger.read_rejected(home=self.home, repo_key="other"), [])

    def test_retrying_the_same_unfittable_session_does_not_pile_up_rejects(self):
        """#22 review: known_sessions counts only rows that actually made it into
        the ledger, so a session that didn't make it is retried on every mark. If
        every retry left a new reject line, a single session would inflate into
        "dropped N times" and the file would grow without bound."""
        record = {"repo": "k", "harness": "codex-cli", "session": "s" * 36,
                  "event": "start", "path": "/p" * 400}
        for _ in range(5):
            ok = ledger.append(dict(record), home=self.home)
            self.assertFalse(ok)
        rejected = ledger.read_rejected(home=self.home, repo_key="k")
        self.assertEqual(len(rejected), 1)

    def test_a_different_session_still_gets_its_own_reject_row(self):
        for session in ("a" * 36, "b" * 36):
            ledger.append(
                {"repo": "k", "harness": "codex-cli", "session": session,
                 "event": "start", "path": "/p" * 400},
                home=self.home,
            )
        rejected = ledger.read_rejected(home=self.home, repo_key="k")
        self.assertEqual({r["session"] for r in rejected}, {"a" * 36, "b" * 36})

    def test_clear_rejected_removes_only_the_given_repo(self):
        ledger.append({"repo": "mine", "harness": "codex-cli", "session": "s" * 36,
                       "event": "start", "path": "/p" * 400}, home=self.home)
        ledger.append({"repo": "other", "harness": "codex-cli", "session": "t" * 36,
                       "event": "start", "path": "/p" * 400}, home=self.home)
        removed = ledger.clear_rejected("mine", home=self.home)
        self.assertEqual(removed, 1)
        self.assertEqual(ledger.read_rejected(home=self.home, repo_key="mine"), [])
        self.assertEqual(len(ledger.read_rejected(home=self.home, repo_key="other")), 1)

    def test_repo_filter_is_applied_before_the_line_limit(self):
        """The ledger is one file shared by the whole machine. Truncating first
        would push this repo's line out of the window, silently stalling the handoff."""
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
