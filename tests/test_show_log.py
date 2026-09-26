"""#10's defect where `omhc show '#N'` opens another session's event, and
#15a/#15c's log/show usability items. All three are covered here (an older
accumulated item was already fixed in 9495f87)."""
from __future__ import annotations

import io
import os
import subprocess
import unittest

from omhc import cli, due, index
from omhc.event import Event

from ._repo import REPO, TempRepo


def _write_idx(state: str, session_id: str, rows) -> None:
    """rows are (seq, verb, arg) tuples. author/ok/offset/length are pinned
    to values that don't matter for the test."""
    idx_dir = os.path.join(state, "index")
    os.makedirs(idx_dir, exist_ok=True)
    events = [
        Event(seq=seq, epoch=1700000000.0 + seq, author="human", verb=verb,
              ok=True, text=arg, arg=arg, paths=(), offset=seq * 10, length=5)
        for seq, verb, arg in rows
    ]
    index.append_rows(os.path.join(idx_dir, session_id + ".idx"), events)


def _mark_delivered(state: str, session_id: str) -> None:
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, due.DELIVERED_NAME), "a", encoding="utf-8") as fh:
        fh.write("\t".join((session_id, "claude-code", "codex-cli", "1700000000")) + "\n")


class TestShowSeqRef(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        cwd = os.getcwd()
        os.chdir(self.t.root)
        self.addCleanup(os.chdir, cwd)

    def _show(self, target, **kw):
        out = io.StringIO()
        err = io.StringIO()
        code = cli.cmd_show(
            cli.build_parser().parse_args(["show", target] + (["--full"] if kw.get("full") else [])),
            home=self.t.home, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_bare_seq_ref_resolves_to_the_log_default_session(self):
        """Both sessions have a #3 — the most recently delivered session's should open."""
        _write_idx(self.t.state, "aaaaaaaa1111", [(3, "said", "old-session-text")])
        _write_idx(self.t.state, "bbbbbbbb2222", [(3, "said", "new-session-text")])
        _mark_delivered(self.t.state, "aaaaaaaa1111")
        _mark_delivered(self.t.state, "bbbbbbbb2222")

        # The original bytes are located by offset/length — this fails without a
        # pinned original, so plant source.jsonl under the state dir and use the fallback path.
        pin_dir = os.path.join(self.t.state, "pinned", "bbbbbbbb2222")
        os.makedirs(pin_dir, exist_ok=True)
        with open(os.path.join(pin_dir, "source.jsonl"), "wb") as fh:
            fh.write(b"0" * 30 + b"new!!" + b"0" * 30)

        code, out, err = self._show("#3")
        self.assertEqual(code, 0)
        # stdout is the raw original bytes — a pipe like `show '#3' | jq .`
        # must not break (review defect). Which session was chosen only goes to stderr.
        self.assertEqual(out, "new!!\n")
        self.assertIn("bbbbbbbb", err)
        self.assertNotIn("aaaaaaaa", err)

        from omhc import ledger
        pulls = [r for r in ledger.read(repo_key=self.t.key, home=self.t.home)
                 if r.get("event") == "pull"]
        self.assertEqual(pulls[-1]["session"], "bbbbbbbb2222")

    def test_explicit_prefix_selects_that_session(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(3, "said", "old")])
        _write_idx(self.t.state, "bbbbbbbb2222", [(3, "said", "new")])
        _mark_delivered(self.t.state, "bbbbbbbb2222")

        pin_dir = os.path.join(self.t.state, "pinned", "aaaaaaaa1111")
        os.makedirs(pin_dir, exist_ok=True)
        with open(os.path.join(pin_dir, "source.jsonl"), "wb") as fh:
            fh.write(b"0" * 30 + b"old!!" + b"0" * 30)

        code, out, err = self._show("aaaaaaaa#3")
        self.assertEqual(code, 0)
        # No need to state the reason for selection in the header since the prefix is
        # explicit, but confirm via pull bookkeeping that it read from the specified
        # session, not the wrong (most-recently-delivered) one.
        from omhc import ledger
        pulls = [r for r in ledger.read(repo_key=self.t.key, home=self.t.home)
                 if r.get("event") == "pull"]
        self.assertEqual(pulls[-1]["session"], "aaaaaaaa1111")

    def test_ambiguous_prefix_errors_with_candidates(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "said", "x")])
        _write_idx(self.t.state, "aaaaaaaa2222", [(1, "said", "y")])
        code, out, err = self._show("aaaaaaaa#1")
        self.assertEqual(code, 1)
        # The error goes to stderr, not stdout (#19) — exit code stays 1.
        self.assertEqual(out, "")
        self.assertIn("ambiguous", err)
        # Candidates are shown as full ids, not abbreviated — shortening to a prefix
        # could itself become ambiguous again (review defect).
        self.assertIn("aaaaaaaa1111", err)
        self.assertIn("aaaaaaaa2222", err)

    def test_ambiguous_prefix_lists_full_ids_even_when_they_share_13_chars(self):
        """13 chars is just a display preference — shortening two ids that don't
        differ within it down to candidates creates a defect where the candidate
        list itself can't tell them apart (review defect)."""
        long_a = "aaaaaaaaaaaaa1111"  # the first 13 chars "aaaaaaaaaaaaa" are identical
        long_b = "aaaaaaaaaaaaa2222"
        _write_idx(self.t.state, long_a, [(1, "said", "x")])
        _write_idx(self.t.state, long_b, [(1, "said", "y")])
        code, out, err = self._show("aaaaaaaaaaaaa#1")
        self.assertEqual(code, 1)
        self.assertIn(long_a, err)
        self.assertIn(long_b, err)

    def test_no_default_session_gives_a_hint_not_a_crash(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "said", "x")])
        code, out, err = self._show("#1")
        self.assertEqual(code, 1)
        self.assertIn("no default session", err)

    def test_default_session_delivered_but_not_indexed_gives_a_distinct_hint(self):
        """The #N hint must not conflate "nothing was ever delivered" with
        "it was delivered but there's no index yet" (review defect)."""
        _mark_delivered(self.t.state, "not-indexed-yet")
        code, out, err = self._show("#1")
        self.assertEqual(code, 1)
        self.assertIn("not-indexed-yet", err)
        self.assertNotIn("no default session", err)


class TestShowRealStdoutIsRawBytes(unittest.TestCase):
    """#19: real stdout (a stream with `.buffer`) gets the raw original bytes
    written as-is — invalid UTF-8 isn't replaced with U+FFFD, and no missing
    newline is appended. io.StringIO can't verify this path, so the real binary
    is run as a subprocess. Errors go to stderr, exit code is 1."""

    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)

    def _run(self, target, extra=()):
        return subprocess.run(
            [os.path.join(REPO, "bin", "omhc"), "show", target] + list(extra),
            cwd=self.t.root, env=self.t.env, capture_output=True)

    def test_invalid_utf8_and_missing_trailing_newline_survive_unchanged(self):
        os.makedirs(self.t.state, exist_ok=True)
        body = b'{"broken":"\xff\xfe no newline here"}'
        source = os.path.join(self.t.state, "source.jsonl")
        with open(source, "wb") as fh:
            fh.write(body)
        with open(os.path.join(self.t.state, index.REFS_NAME), "w", encoding="utf-8") as fh:
            fh.write("\t".join(("E1", "s1", source, "0", str(len(body)), "1")) + "\n")

        proc = self._run("E1")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, body)

    def test_error_goes_to_stderr_and_stdout_stays_empty(self):
        proc = self._run("E404")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(b"unknown reference", proc.stderr)


class TestLogRefsAndSaidPreview(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        cwd = os.getcwd()
        os.chdir(self.t.root)
        self.addCleanup(os.chdir, cwd)

    def _log(self, extra=()):
        out = io.StringIO()
        code = cli.cmd_log(
            cli.build_parser().parse_args(["log"] + list(extra)), home=self.t.home, out=out)
        return code, out.getvalue()

    def test_log_refs_are_directly_passable_to_show(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "ran", "pytest")])
        code, out = self._log()
        self.assertEqual(code, 0)
        line = out.strip().splitlines()[0]
        ref = line.split()[0]
        self.assertRegex(ref, r"^[0-9a-f]+#1$")

    def test_said_row_without_text_shows_a_hint(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "said", "")])
        code, out = self._log()
        self.assertEqual(code, 0)
        self.assertIn("omhc show", out)

    def test_unique_prefix_grows_past_13_chars_when_still_colliding(self):
        """13 chars is just a display preference — if it still doesn't disambiguate,
        it must grow further. Otherwise show rejects as ambiguous a ref that log
        printed (review defect)."""
        _write_idx(self.t.state, "aaaaaaaaaaaaa1111", [(1, "ran", "x")])
        _write_idx(self.t.state, "aaaaaaaaaaaaa2222", [(1, "ran", "y")])
        code, out = self._log()
        self.assertEqual(code, 0)
        refs = [line.split()[0] for line in out.strip().splitlines()]
        sessions = [r.split("#")[0] for r in refs]
        self.assertEqual(len(sessions), len(set(sessions)))

    def test_unique_prefix_grows_when_ids_share_the_first_8_chars(self):
        """Codex UUIDv7's first 8 chars overlap by time period (#15c) — log must
        still print distinct refs."""
        _write_idx(self.t.state, "0199aaaa1111", [(1, "ran", "x")])
        _write_idx(self.t.state, "0199aaaa2222", [(1, "ran", "y")])
        code, out = self._log()
        self.assertEqual(code, 0)
        refs = [line.split()[0] for line in out.strip().splitlines()]
        sessions = [r.split("#")[0] for r in refs]
        self.assertEqual(len(sessions), len(set(sessions)))

    def test_rows_within_a_session_order_by_seq_even_when_epoch_goes_backwards(self):
        """Invariant 6: timestamps are not an ordering source (#18). Even if
        epoch goes backwards within the same session, the seq order recorded in
        the index must be honored."""
        idx_dir = os.path.join(self.t.state, "index")
        os.makedirs(idx_dir, exist_ok=True)
        events = [
            Event(seq=1, epoch=1700000500.0, author="human", verb="said",
                  ok=True, text="first", arg="first", paths=(), offset=0, length=5),
            Event(seq=2, epoch=1700000100.0, author="human", verb="said",
                  ok=True, text="second", arg="second", paths=(), offset=5, length=6),
        ]
        index.append_rows(os.path.join(idx_dir, "aaaaaaaa1111.idx"), events)
        code, out = self._log()
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertIn("first", lines[0])
        self.assertIn("second", lines[1])

    def test_sessions_order_by_ledger_append_order_not_epoch(self):
        """Session order is the order `start` rows were written to the ledger —
        even if one session's epoch reads as starting later than another's, the
        ledger's appearance order is followed."""
        from omhc import ledger

        _write_idx(self.t.state, "bbbbbbbb2222", [(1, "said", "second-session")])
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "said", "first-session")])
        # aaaaaaaa1111 was written to the ledger first, but its epoch is larger (a backwards timestamp).
        ledger.append({"repo": self.t.key, "harness": "codex-cli",
                       "session": "aaaaaaaa1111", "event": "start",
                       "epoch": 2000000000.0, "path": "x", "cwd": self.t.root},
                      home=self.t.home)
        ledger.append({"repo": self.t.key, "harness": "claude-code",
                       "session": "bbbbbbbb2222", "event": "start",
                       "epoch": 1000000000.0, "path": "y", "cwd": self.t.root},
                      home=self.t.home)
        code, out = self._log()
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertIn("first-session", lines[0])
        self.assertIn("second-session", lines[1])

    def test_log_ends_on_the_last_delivered_session_despite_mark_then_backfill_ledger_order(self):
        """Review defect (#18): `cmd_mark` writes its own session (C) to the ledger
        first, and `_backfill_foreign_sessions` later writes a foreign session (X)
        that actually started earlier, as `via:"scan"` — ledger appearance order is
        C, X, but the actual delivery order (delivered.tsv) is X, C. Ranking by
        ledger appearance order flips every C/X pair, so log's tail disagrees with
        `show '#N'`'s default session (due.last_delivered), and `--last N` keeps
        X's old line instead of C's newest one."""
        from omhc import ledger

        _write_idx(self.t.state, "cccccccc1111", [(1, "said", "c-old"), (2, "said", "c-new")])
        _write_idx(self.t.state, "xxxxxxxx2222", [(1, "said", "x-event")])

        # mark: writes C to the ledger first.
        ledger.append({"repo": self.t.key, "harness": "claude-code",
                       "session": "cccccccc1111", "event": "start",
                       "epoch": 2000000000.0, "path": "c", "cwd": self.t.root},
                      home=self.t.home)
        # backfill: X started earlier but is written to the ledger later, as via:"scan".
        ledger.append({"repo": self.t.key, "harness": "codex-cli",
                       "session": "xxxxxxxx2222", "event": "start",
                       "epoch": 1000000000.0, "path": "x", "cwd": self.t.root,
                       "via": "scan"}, home=self.t.home)

        # The actual delivery order is the reverse of ledger appearance order: X first, C most recent.
        _mark_delivered(self.t.state, "xxxxxxxx2222")
        _mark_delivered(self.t.state, "cccccccc1111")

        code, out = self._log()
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertIn("x-event", lines[0])
        self.assertIn("c-old", lines[1])
        self.assertIn("c-new", lines[2])

        from omhc import due
        self.assertEqual(due.last_delivered(self.t.state), "cccccccc1111")

        code, out = self._log(["--last", "2"])
        self.assertEqual(code, 0)
        lines2 = out.strip().splitlines()
        self.assertEqual(len(lines2), 2)
        self.assertNotIn("x-event", out)
        self.assertIn("c-old", lines2[0])
        self.assertIn("c-new", lines2[1])

    def test_sessions_without_a_ledger_row_sort_after_ledgered_ones(self):
        """A session that has an index but no `start` row in the ledger (e.g.
        before backfill) has no basis for ordering — it's placed deterministically
        after ledgered sessions, sorted by filename."""
        from omhc import ledger

        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "said", "ledgered")])
        _write_idx(self.t.state, "zzzzzzzz9999", [(1, "said", "no-ledger-row")])
        ledger.append({"repo": self.t.key, "harness": "codex-cli",
                       "session": "aaaaaaaa1111", "event": "start",
                       "epoch": 1000000000.0, "path": "x", "cwd": self.t.root},
                      home=self.t.home)
        code, out = self._log()
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertIn("ledgered", lines[0])
        self.assertIn("no-ledger-row", lines[1])


class TestDeliveredOrder(unittest.TestCase):
    def test_a_session_redelivered_later_ranks_by_its_last_delivery(self):
        """Must use the same criterion as last_delivered so log's tail and show's default session match."""
        import tempfile
        from omhc import due
        with tempfile.TemporaryDirectory() as state:
            with open(os.path.join(state, due.DELIVERED_NAME), "w", encoding="utf-8") as fh:
                fh.write("S\ta\tx\t1\nT\tb\tx\t2\nS\tc\tx\t3\n")
            self.assertEqual(due.delivered_order(state), ["T", "S"])
            self.assertEqual(due.last_delivered(state), "S")


if __name__ == "__main__":
    unittest.main()
