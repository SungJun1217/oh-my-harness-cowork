"""#50: both harnesses start every SessionStart hook at (measured) the same
instant, so `mark` can no longer be assumed to run before `brief` in the same
start — the omhc hook fragments' "mark, then brief" ordering assumption is
false. `brief.compute` now also runs the foreign-session collection
(`collect.collect_foreign_state`) that used to live only in `cmd_mark`, so a
handoff still goes out even when `brief` is the one that runs first.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from omhc import brief, cli, collect, due, ledger

from . import _repo

# Real, freshly-computed time — not a fixed historical epoch. codex_cli's
# discover() windows its date-directory scan off the real wall clock
# (SCAN_DAYS, `time.time` by default), so a stale fixed constant would drift
# out of that window as the calendar moves on and silently stop being
# backfillable (unlike due()'s own age check, which compares against
# whatever `now` a caller injects).
NOW = time.time()
OMHC = os.path.join(_repo.REPO, "bin", "omhc")


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def _claude_row(kind, **extra):
    row = {"type": kind, "timestamp": "2026-09-25T00:00:00.000Z"}
    row.update(extra)
    return row


def _write_claude_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


NOT_LOGGED_IN = [
    _claude_row("assistant", isApiErrorMessage=True,
               message={"model": "<synthetic>", "content": [], "usage": {}}),
]


class Harness:
    def __init__(self):
        self.t = _repo.TempRepo()
        self.home = self.t.home
        self.root = self.t.root
        self.key = self.t.key
        self.state = self.t.state

    def close(self):
        self.t.close()

    def mark(self, harness, session_id, transcript_path=None):
        payload = {"cwd": self.root, "session_id": session_id}
        if transcript_path is not None:
            payload["transcript_path"] = transcript_path
        args = cli.build_parser().parse_args(
            ["mark", "--harness", harness, "--stdin", json.dumps(payload)])
        out = io.StringIO()
        code = cli.cmd_mark(args, home=self.home, out=out)
        return code, out.getvalue()

    def plant_codex(self, session_id, human="필드 경로부터 다시 확인해줘", when=NOW,
                    ledger_home=""):
        # discover()'s ordering (and the age/window checks in due()/backfill)
        # read session_meta's own `timestamp` field, not the row's — must be
        # set explicitly (test_mark_backfill.py's Harness.plant does the
        # same) or discover() silently skips the session (`if not started:
        # continue`).
        return self.t.plant_codex(session_id=session_id, human=human, when=when,
                                  meta_extra={"timestamp": _iso(when)},
                                  ledger_home=ledger_home)

    def claude_path(self, session_id):
        return os.path.join(self.home, ".claude", "projects", "p", session_id + ".jsonl")

    def deliver(self, session_id, **kw):
        return brief.compute(my_harness="claude-code", my_session_id=session_id,
                             repo_root=self.root, home=self.home, now=NOW, **kw)


class TestBriefRunsFirst(unittest.TestCase):
    """Simulates `brief` winning the SessionStart race — `cli.cmd_mark` is
    never called for the receiving session at all, only `brief.compute`."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_backfill_only_foreign_session_is_delivered(self):
        """(a) A Codex session that only exists on disk (no ledger row at
        all, as if its own hook never ran) still gets backfilled and
        delivered when `brief` is the first thing to run this start."""
        self.h.plant_codex("X1")  # ledger_home="" -> no ledger row planted
        body = self.h.deliver("C1")
        self.assertIn("필드 경로", body)

    def test_grown_delivered_session_is_reactivated(self):
        """(b) The ordinary "keep typing in a delivered session, then
        switch" case (_reactivate_grown_sessions), triggered from brief's
        own collection instead of a prior mark call."""
        self.h.plant_codex("X1", ledger_home=self.h.home)
        # Establishes a real pre-growth baseline the same way a genuine
        # earlier mark would (collect_foreign_state's first observation).
        self.h.mark("claude-code", "seed")
        first = self.h.deliver("C1")
        self.assertIn("필드 경로", first)

        rollout = os.path.join(self.h.home, ".codex", "sessions",
                               time.strftime("%Y/%m/%d", time.gmtime(NOW)),
                               "rollout-X1.jsonl")
        _repo.append_codex_user_turn(rollout, "새로 추가된 지시사항")

        # No mark call for C2 at all — only brief's own collection may see the growth.
        second = self.h.deliver("C2")
        self.assertIn("새로 추가된 지시사항", second)

    def test_bounced_group_is_redelivered(self):
        """(c) #51's bounce check, run from brief's own collection instead
        of a `cmd_mark` call."""
        self.h.plant_codex("X1")
        c1 = self.h.claude_path("C1")
        self.h.mark("claude-code", "C1", transcript_path=c1)
        first = self.h.deliver("C1")
        self.assertIn("필드 경로", first)
        _write_claude_jsonl(c1, NOT_LOGGED_IN)

        # No mark call for C2 — brief alone must run the bounce check and
        # redeliver X1 within the same compute() call.
        second = self.h.deliver("C2")
        self.assertIn("필드 경로", second)


class TestCollectionIsIdempotent(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_running_collection_twice_in_one_start_writes_no_duplicate_rows(self):
        for i in range(3):
            self.h.plant_codex("X{}".format(i), when=NOW - 100 * (i + 1))
        self.h.mark("claude-code", "C1")
        rows_after_mark = ledger.read(repo_key=self.h.key, home=self.h.home)
        starts_after_mark = [(r["harness"], r["session"]) for r in rows_after_mark
                             if r.get("event") == "start" and r.get("session")
                             and r.get("harness") == "codex-cli"]
        self.assertEqual(sorted(starts_after_mark),
                         [("codex-cli", "X0"), ("codex-cli", "X1"), ("codex-cli", "X2")])

        # brief runs again in the "same start" — its own collection call
        # must find everything already known (no new `start` rows for the
        # same sessions), even though an unchanged round still legitimately
        # folds into one more `rebase` marker (#28) — that bookkeeping row
        # isn't a duplicate of anything, it happens every round regardless
        # of #50.
        brief.compute(my_harness="claude-code", my_session_id="C2",
                      repo_root=self.h.root, home=self.h.home, now=NOW)
        rows_after_brief = ledger.read(repo_key=self.h.key, home=self.h.home)
        starts_after_brief = [(r["harness"], r["session"]) for r in rows_after_brief
                              if r.get("event") == "start" and r.get("session")
                              and r.get("harness") == "codex-cli"]
        self.assertEqual(sorted(starts_after_mark), sorted(starts_after_brief))

    def test_mark_then_brief_does_not_double_bounce(self):
        self.h.plant_codex("X1")
        c1 = self.h.claude_path("C1")
        self.h.mark("claude-code", "C1", transcript_path=c1)
        self.h.deliver("C1")
        _write_claude_jsonl(c1, NOT_LOGGED_IN)

        c2 = self.h.claude_path("C2")
        self.h.mark("claude-code", "C2", transcript_path=c2)  # mark runs first this time
        self.h.deliver("C2")  # brief's own collection call races right after

        lines = ledger.read(repo_key=self.h.key, home=self.h.home)
        # delivered.tsv, not ledger, holds bounce lines — checked there.
        with open(os.path.join(self.h.state, due.DELIVERED_NAME), encoding="utf-8") as fh:
            delivered_lines = [ln.rstrip("\n").split("\t") for ln in fh if ln.strip()]
        bounce_lines = [p for p in delivered_lines if len(p) >= 2 and p[1] == due.BOUNCE_MARKER]
        self.assertEqual(len(bounce_lines), 1)


class TestLockTimeout(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_brief_proceeds_without_raising_when_the_lock_is_held(self):
        """brief skips collecting this round on a lock timeout (fails open)
        rather than blocking or raising — the handoff just lags, per #50's
        design."""
        os.makedirs(self.h.state, exist_ok=True)
        lock_path = os.path.join(self.h.state, collect.LOCK_NAME)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.h.plant_codex("X1")  # never backfilled if collection is skipped
            body = self.h.deliver("C1")  # must not raise, must not hang
            self.assertEqual(body, "")  # nothing to send: X1 was never collected
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_mark_still_collects_when_the_lock_is_held(self):
        """Unlike brief, mark collects anyway on a lock timeout — a
        session's own hook is the only place an untrusted foreign harness's
        backfill/bounce detection ever runs."""
        os.makedirs(self.h.state, exist_ok=True)
        lock_path = os.path.join(self.h.state, collect.LOCK_NAME)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.h.plant_codex("X1")
            code, out = self.h.mark("claude-code", "C1")
            self.assertEqual(code, 0)
            self.assertEqual(out, "")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        got = due.due_one(self.h.key, "claude-code", "C2", NOW, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "X1")

    def test_mark_skips_reactivation_but_still_backfills_when_lock_is_held(self):
        """Review finding 1: two concurrent `_reactivate_grown_sessions`
        runs can each independently append their own `grew` start row for
        the same session, reproducing the #49 duplicate-row bug. When mark
        doesn't get the lock (the holder — brief, or another mark — is
        presumably reactivating this round already), mark must still
        backfill a brand-new foreign session and run the #51 bounce check
        (redundant with the holder's own work, but idempotent and therefore
        harmless), while leaving reactivation to whoever does hold the
        lock."""
        self.h.plant_codex("X1", ledger_home=self.h.home)
        # A genuine, uncontended mark establishes X1's real pre-growth
        # baseline (the lock is free here, so this one both backfills and
        # reactivates normally).
        self.h.mark("claude-code", "seed")
        rollout = os.path.join(self.h.home, ".codex", "sessions",
                               time.strftime("%Y/%m/%d", time.gmtime(NOW)),
                               "rollout-X1.jsonl")
        _repo.append_codex_user_turn(rollout, "락 보유 중 성장한 턴")

        # A second, brand-new foreign session that was never backfilled —
        # proves backfill/bounce still run even though the lock isn't held.
        self.h.plant_codex("X2")

        os.makedirs(self.h.state, exist_ok=True)
        lock_path = os.path.join(self.h.state, collect.LOCK_NAME)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            code, out = self.h.mark("claude-code", "C1")
            self.assertEqual(code, 0)
            self.assertEqual(out, "")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        grew_rows = [r for r in rows if r.get("session") == "X1" and r.get("grew")]
        self.assertEqual(grew_rows, [], "reactivation must not run without the lock")
        x2_rows = [r for r in rows if r.get("session") == "X2" and r.get("event") == "start"]
        self.assertEqual(len(x2_rows), 1, "backfill must still run without the lock")

    def test_missing_fcntl_never_raises(self):
        """fcntl is POSIX-only — a platform without it must fall back to
        'never locked' rather than breaking the hook path."""
        with mock.patch.object(collect, "fcntl", None):
            with collect.try_lock(self.h.state) as got:
                self.assertFalse(got)


class TestDryRunCollectsNothing(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_dry_run_skips_collection_entirely(self):
        with mock.patch.object(collect, "collect_foreign_state") as fake:
            brief.compute(my_harness="claude-code", my_session_id="C1",
                         repo_root=self.h.root, home=self.h.home, now=NOW,
                         dry_run=True)
            fake.assert_not_called()

    def test_a_real_call_does_collect(self):
        with mock.patch.object(collect, "collect_foreign_state") as fake:
            brief.compute(my_harness="claude-code", my_session_id="C1",
                         repo_root=self.h.root, home=self.h.home, now=NOW)
            fake.assert_called_once()


class TestConcurrencySmoke(unittest.TestCase):
    """Real subprocesses, not mocks — spawns `bin/omhc mark` and
    `bin/omhc brief` at the same time, repeatedly, on the same state, and
    checks the handoff is delivered exactly once with no duplicate rows."""

    def setUp(self):
        self.t = _repo.TempRepo()
        self.addCleanup(self.t.close)

    def _spawn(self, harness_args):
        return subprocess.Popen(
            [OMHC] + harness_args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.t.env, cwd=self.t.repo, text=True)

    def test_mark_and_brief_race_N_times_without_duplicate_delivery(self):
        """N rounds, each a fresh Claude SessionStart racing `mark` and
        `brief` as two real processes against the same on-disk state. Each
        round leaves the Codex session with one new human turn to hand off,
        so N rounds must yield exactly N head deliveries — never 0 (lost,
        the race breaking the handoff) and never more than N (duplicated)."""
        N = 5
        path = self.t.plant_codex(session_id="cx1", human="동시성 테스트용 0",
                                  meta_extra={"timestamp": _iso(time.time())})

        for i in range(N):
            if i:
                # One new human turn per round, appended right before that
                # round's race — incremental growth, not delivered until
                # `_reactivate_grown_sessions` sees it this round.
                _repo.append_codex_user_turn(path, "동시성 테스트용 {}".format(i), ordinal=90 + i)
            payload = json.dumps({"cwd": self.t.root, "session_id": "me{}".format(i)})
            procs = [
                self._spawn(["mark", "--harness", "claude-code"]),
                self._spawn(["brief", "--harness", "claude-code", "--wire", "claude"]),
            ]
            outputs = [p.communicate(input=payload, timeout=30) for p in procs]
            for p, (_out, err) in zip(procs, outputs):
                self.assertEqual(p.returncode, 0, err)

        delivered_path = os.path.join(self.t.state, due.DELIVERED_NAME)
        with open(delivered_path, encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        head_lines = [ln for ln in lines if ln.split("\t")[1] == "claude-code"]
        self.assertEqual(len(head_lines), N, "expected exactly {} deliveries: {}".format(N, lines))


if __name__ == "__main__":
    unittest.main()
