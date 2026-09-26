"""`omhc mark`'s ledger backfill. Even when the untrusted Codex hook leaves
that session entirely out of the ledger, Claude-side mark finds and fills it
in via discover() so due() can still see Codex→Claude. due() itself is
untouched.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from omhc import adapters, brief, cli, due, ledger, locate

from . import _repo


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


class Harness:
    def __init__(self):
        self.t = _repo.TempRepo()
        self.home = self.t.home
        self.root = self.t.root
        self.key = self.t.key
        self.state = self.t.state

    def close(self):
        self.t.close()

    def plant(self, session_id, epoch, human="필드 경로부터 다시 확인해줘",
             meta_extra=None, ledger_home=""):
        extra = {"timestamp": _iso(epoch)}
        if meta_extra:
            extra.update(meta_extra)
        return self.t.plant_codex(session_id=session_id, human=human, when=epoch,
                                  meta_extra=extra, ledger_home=ledger_home)

    def mark(self, harness="claude-code", session_id="me1", env=None, source=None):
        payload = {"cwd": self.root, "session_id": session_id}
        if source is not None:
            payload["source"] = source
        stdin = json.dumps(payload)
        args = cli.build_parser().parse_args(
            ["mark", "--harness", harness, "--stdin", stdin])
        out = io.StringIO()
        patched = mock.patch.dict(os.environ, env or {})
        with patched:
            code = cli.cmd_mark(args, home=self.home, out=out)
        return code, out.getvalue()

    def scan_rows(self, harness="codex-cli"):
        # #28: a rebase marker is also via=="scan" but has no session — this
        # helper has always meant a scan row "for a specific session", so exclude markers.
        rows = ledger.read(repo_key=self.key, home=self.home)
        return [r for r in rows if r.get("harness") == harness and r.get("via") == "scan"
               and r.get("session")]

    def marker_rows(self, harness="codex-cli"):
        rows = ledger.read(repo_key=self.key, home=self.home)
        return [r for r in rows if r.get("harness") == harness
               and r.get("event") == cli.REBASE_EVENT]


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_claude_mark_backfills_an_unledgered_codex_session(self):
        """Even a session missing from the ledger because the Codex hook isn't trusted gets filled in via discover()."""
        now = time.time()
        self.h.plant("cx1", now - 600)
        code, _ = self.h.mark()
        self.assertEqual(code, 0)
        rows = self.h.scan_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "cx1")

    def test_due_then_sees_the_backfilled_session(self):
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark()
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.harness, "codex-cli")
        self.assertEqual(got.session_id, "cx1")

    def test_brief_produces_a_handoff_from_the_backfilled_session(self):
        from omhc import brief

        now = time.time()
        self.h.plant("cx1", now - 600, human="리더를 붙여서 양방향으로 만들기")
        self.h.mark()
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.root, home=self.h.home, now=now)
        self.assertIn("[omhc]", body)
        self.assertIn("리더를 붙여서", body)

    def test_older_unledgered_session_never_overrides_a_newer_ledgered_one(self):
        """due() picks the "most recent" by ledger append order. If an
        older session gets appended later, it would be mistaken for the
        newest — so an older one must be filtered out."""
        now = time.time()
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "cx-new",
                      "event": "start", "epoch": now - 100, "path": "/nope",
                      "cwd": self.h.root}, home=self.h.home)
        self.h.plant("cx-old", now - 500)  # older than the ledger's cx-new
        self.h.mark()
        self.assertEqual(self.h.scan_rows(), [])
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-new")

    def test_a_newer_unledgered_session_is_appended_after_the_ledgered_one(self):
        now = time.time()
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "cx-old",
                      "event": "start", "epoch": now - 500, "path": "/nope",
                      "cwd": self.h.root}, home=self.h.home)
        self.h.plant("cx-new", now - 100)
        self.h.mark()
        rows = self.h.scan_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "cx-new")
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-new")

    def test_six_session_starts_append_the_same_session_only_once(self):
        """Mimics the measured case of SessionStart firing 6 times in one session."""
        now = time.time()
        self.h.plant("cx1", now - 600)
        for _ in range(6):
            self.h.mark()
        self.assertEqual(len(self.h.scan_rows()), 1)

    def test_mark_still_exits_0_with_empty_stdout_when_its_own_row_is_refused(self):
        """#22: even in an extreme where transcript_path can't meet the cap
        (e.g. a PATH_MAX-scale path), the hook path (mark) must never raise
        — empty stdout, exit 0 (invariant 2). The refusal only leaves a
        trace somewhere visible (ledger.rejected)."""
        from omhc import ledger

        payload = {"cwd": self.h.root, "session_id": "me1",
                   "transcript_path": "/p" * 500}
        args = cli.build_parser().parse_args(
            ["mark", "--harness", "claude-code", "--stdin", json.dumps(payload)])
        out = io.StringIO()
        code = cli.cmd_mark(args, home=self.h.home, out=out)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        self.assertEqual(rows, [])
        rejected = ledger.read_rejected(home=self.h.home, repo_key=self.h.key)
        self.assertEqual(len(rejected), 1)

    def test_omhc_off_disables_the_scan(self):
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark(env={"OMHC_OFF": "1"})
        self.assertEqual(self.h.scan_rows(), [])

    def test_subagent_rollout_is_never_backfilled(self):
        now = time.time()
        self.h.plant("cx-sub", now - 600,
                     meta_extra={"thread_source": "subagent"})
        self.h.mark()
        self.assertEqual(self.h.scan_rows(), [])

    def test_exec_rollout_is_not_backfilled_by_default(self):
        now = time.time()
        self.h.plant("cx-exec", now - 600,
                     meta_extra={"source": "exec", "originator": "codex_exec"})
        self.h.mark()
        self.assertEqual(self.h.scan_rows(), [])

    def test_exec_rollout_is_backfilled_under_the_headless_override(self):
        now = time.time()
        self.h.plant("cx-exec", now - 600,
                     meta_extra={"source": "exec", "originator": "codex_exec"})
        self.h.mark(env={"OMHC_ALLOW_HEADLESS": "1"})
        self.assertEqual(len(self.h.scan_rows()), 1)

    def test_a_garbage_rollout_leaves_mark_exiting_zero_with_empty_stdout(self):
        directory = os.path.join(self.h.home, ".codex", "sessions",
                                 time.strftime("%Y/%m/%d", time.gmtime()))
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "rollout-garbage.jsonl"), "wb") as fh:
            fh.write(b"\x00\xff{not json\n\n\x80\x81")
        code, out = self.h.mark()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_the_per_mark_cap_holds(self):
        now = time.time()
        for i in range(cli.BACKFILL_CAP + 3):
            self.h.plant("cx{}".format(i), now - 1000 + i)
        self.h.mark()
        self.assertEqual(len(self.h.scan_rows()), cli.BACKFILL_CAP)

    def test_cap_exceeded_still_delivers_the_newest_session(self):
        """Review defect: walking in ascending order and cutting off at the
        cap leaves the oldest 5 behind. due() picks the last one in the
        ledger, so when only 5 of 8 fit, cx4 must not come out instead of
        cx7 (the newest)."""
        now = time.time()
        total = cli.BACKFILL_CAP + 3
        for i in range(total):
            self.h.plant("cx{}".format(i), now - 1000 + i)
        self.h.mark()
        newest = "cx{}".format(total - 1)
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, newest)
        rows = self.h.scan_rows()
        self.assertIn(newest, {r["session"] for r in rows})
        oldest = "cx0"
        self.assertNotIn(oldest, {r["session"] for r in rows})

    def test_two_sessions_starting_in_the_same_whole_second_both_land(self):
        """#22: session_meta.timestamp is second-granularity. After the first
        mark fills in one session and its epoch becomes newest_start, a
        **different** session that started in the same second must not
        disappear from the `<=` comparison in the second mark — a different
        id is not an already-known session."""
        now = time.time()
        same_second = now - 500
        self.h.plant("cx-a", same_second)
        self.h.mark()
        self.assertEqual([r["session"] for r in self.h.scan_rows()], ["cx-a"])

        self.h.plant("cx-b", same_second + 0.4)  # same second -> same ISO string
        self.h.mark()
        sessions = {r["session"] for r in self.h.scan_rows()}
        self.assertEqual(sessions, {"cx-a", "cx-b"})

    def test_sessions_from_a_nested_child_repo_are_not_backfilled(self):
        """If Claude opens in a parent directory with no `.git`, discover()'s
        equal-or-descendant judgment also passes through sessions of a
        **child** repo below it that has its own `.git` (the same shape as a
        nested worktree or submodule). Writing that session to the ledger
        under the parent's repo key would leak another repo's GOAL — it
        must be filtered out when the repo keys differ."""
        now = time.time()
        outer = os.path.dirname(self.h.t.repo)  # repo's parent. No .git
        self.h.plant("cx-child", now - 600)  # cwd defaults to the child repo, self.h.root

        stdin = json.dumps({"cwd": outer, "session_id": "me1"})
        args = cli.build_parser().parse_args(
            ["mark", "--harness", "claude-code", "--stdin", stdin])
        out = io.StringIO()
        code = cli.cmd_mark(args, home=self.h.home, out=out)
        self.assertEqual(code, 0)

        rows = ledger.read(home=self.h.home)
        leaked = [r for r in rows if r.get("session") == "cx-child"
                 and r.get("via") == "scan"]
        self.assertEqual(leaked, [], rows)

    def test_backfill_accepts_a_subdirectory_session_under_an_omhc_root_parent(self):
        """#12: even a non-git project backfills a subdirectory session into
        the same repo via the `.omhc-root` marker."""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            sub = os.path.join(root, "sub")
            os.makedirs(sub)
            open(os.path.join(root, ".omhc-root"), "w").close()
            now = time.time()
            _repo.plant_codex(self.h.home, cwd=sub, session_id="cx-sub",
                              when=now - 600,
                              meta_extra={"timestamp": _iso(now - 600)})

            stdin = json.dumps({"cwd": root, "session_id": "me1"})
            args = cli.build_parser().parse_args(
                ["mark", "--harness", "claude-code", "--stdin", stdin])
            out = io.StringIO()
            code = cli.cmd_mark(args, home=self.h.home, out=out)
            self.assertEqual(code, 0)

            key = locate.repo_key(os.path.realpath(root))
            rows = [r for r in ledger.read(repo_key=key, home=self.h.home)
                   if r.get("harness") == "codex-cli" and r.get("via") == "scan"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session"], "cx-sub")

    def test_backfill_still_rejects_a_nested_git_child_under_an_omhc_root_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            child = os.path.join(root, "child")
            os.makedirs(child)
            open(os.path.join(root, ".omhc-root"), "w").close()
            _repo.git(child, "init", "-q")
            now = time.time()
            _repo.plant_codex(self.h.home, cwd=child, session_id="cx-child",
                              when=now - 600,
                              meta_extra={"timestamp": _iso(now - 600)})

            stdin = json.dumps({"cwd": root, "session_id": "me1"})
            args = cli.build_parser().parse_args(
                ["mark", "--harness", "claude-code", "--stdin", stdin])
            out = io.StringIO()
            code = cli.cmd_mark(args, home=self.h.home, out=out)
            self.assertEqual(code, 0)

            key = locate.repo_key(os.path.realpath(root))
            rows = [r for r in ledger.read(repo_key=key, home=self.h.home)
                   if r.get("session") == "cx-child"]
            self.assertEqual(rows, [])

    def test_already_delivered_session_is_not_redelivered_after_a_backfill(self):
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark()
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        # Even if mark runs again (refires), an already-delivered session doesn't reappear.
        self.h.mark()
        self.assertIsNone(due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home))

    def test_status_codex_hook_still_fails_with_only_scan_rows(self):
        """If a via:scan row created by backfill disguised itself as evidence
        that 'the hook actually ran', it would mask an untrusted hook —
        health() already ignores scan rows."""
        now = time.time()
        hooks_dir = os.path.join(self.h.home, ".codex")
        os.makedirs(hooks_dir, exist_ok=True)
        hooks_path = os.path.join(hooks_dir, "hooks.json")
        with open(hooks_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "omhc brief --harness codex-cli"}]}]}}, fh)
        install_epoch = now - 3600
        os.utime(hooks_path, (install_epoch, install_epoch))
        self.h.plant("cx1", now - 600)  # started after install
        self.h.mark()
        self.assertEqual(len(self.h.scan_rows()), 1)

        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.h.root)
        try:
            with mock.patch.object(cli.adapters, "present", return_value=["codex-cli"]):
                cli.cmd_status(cli.build_parser().parse_args(["status"]),
                               home=self.h.home, out=out)
        finally:
            os.chdir(cwd)
        line = next(l for l in out.getvalue().splitlines() if "codex hook" in l)
        self.assertTrue(line.startswith("FAIL"), out.getvalue())


class TestResumeReopensDelivery(unittest.TestCase):
    """When `source:"resume"` arrives in the SessionStart payload (#22,
    measured on both Claude Code and Codex), due() must pick up an
    already-delivered session again — `codex exec resume` appends to the
    same rollout and doesn't create a new rollout (session_meta)."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def _deliver(self, now, human):
        from omhc import brief

        path = self.h.plant("cx1", now - 600, human=human)
        self.h.mark()
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        self.assertIsNone(
            due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home))
        return brief, path

    def _resume_with_new_turn(self, path, text):
        # `codex exec resume` doesn't create a new rollout, it appends to
        # the same file (measured) — it never writes session_meta again.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row(text, ordinal=50),
                                ensure_ascii=False) + "\n")

    def test_resume_after_delivery_makes_it_due_again(self):
        now = time.time()
        _, path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._resume_with_new_turn(path, "이제 두 번째 턴도 반영해줘")
        self.h.mark(harness="codex-cli", session_id="cx1", source="resume")
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")

    def test_next_brief_of_the_other_harness_contains_the_resumed_turn(self):
        now = time.time()
        brief, path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._resume_with_new_turn(path, "이제 두 번째 턴도 반영해줘")
        self.h.mark(harness="codex-cli", session_id="cx1", source="resume")
        body = brief.compute(my_harness="claude-code", my_session_id="me2",
                             repo_root=self.h.root, home=self.h.home, now=now)
        self.assertIn("이제 두 번째 턴도 반영해줘", body)

    def test_compact_after_delivery_does_not_reopen(self):
        now = time.time()
        self._deliver(now, "필드 경로부터 다시 확인해줘")
        self.h.mark(harness="codex-cli", session_id="cx1", source="compact")
        self.assertIsNone(
            due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home))

    def test_fork_after_delivery_does_not_reopen(self):
        """source:"fork" arrives with a new session_id, so cmd_mark just
        leaves a plain start row (#34) — unlike resume, it doesn't reopen an
        already-delivered session. Fork eligibility on the Claude side is
        judged by the adapter's classify()."""
        now = time.time()
        self._deliver(now, "필드 경로부터 다시 확인해줘")
        code, out = self.h.mark(harness="codex-cli", session_id="cx1", source="fork")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIsNone(
            due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home))

    def test_claude_receiving_side_with_source_fork_is_unaffected(self):
        """cmd_mark's source branch doesn't distinguish harnesses — even when
        Claude's own fork emits SessionStart as source:"fork", it's just a
        plain new start row, not leaking into resume treatment."""
        now = time.time()
        code, out = self.h.mark(harness="claude-code", session_id="fork1", source="fork")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        starts = [r for r in rows if r.get("harness") == "claude-code"
                 and r.get("session") == "fork1" and r.get("event") == "start"]
        self.assertEqual(len(starts), 1)

    def test_resume_of_a_never_delivered_session_is_unchanged(self):
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark(harness="codex-cli", session_id="cx1", source="resume")
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")


class TestReactivateGrownSessions(unittest.TestCase):
    """#22's last hole: under an untrusted Codex hook, `codex exec resume`
    appends to the same rollout file and never writes `session_meta` again
    — if the hook doesn't run, discover()/first-line start time can never
    see this resume. Caught via file-size growth + read_session_since,
    which reads only from an offset onward."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        # The growth check runs inside the hook budget (80ms). Timed with
        # the real clock, it tripped the budget only under the full suite on
        # a loaded machine and no grew row was produced (same cause as
        # TestRebaseMarker). The budget-exceeded path is checked separately.
        patcher = mock.patch.object(cli, "BACKFILL_TIME_BUDGET", 60.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _append_human_turn(self, path, text, ordinal=90):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row(text, ordinal=ordinal),
                                ensure_ascii=False) + "\n")

    def _deliver(self, now, human):
        path = self.h.plant("cx1", now - 600, human=human)
        self.h.mark()  # backfill leaves the baseline size in the ledger
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        self.assertIsNone(
            due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home))
        return path

    def _grew_rows(self, session="cx1"):
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        return [r for r in rows if r.get("harness") == "codex-cli"
               and r.get("session") == session and r.get("grew")]

    def test_growth_with_a_human_turn_is_reactivated_without_any_hook(self):
        """Even if the hook never runs (just repeating plain mark, no
        source:"resume"), due() picks it up again once a new human turn appears."""
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._append_human_turn(path, "이제 두 번째 턴도 반영해줘")
        self.h.mark()  # a plain mark with no source — mimics an untrusted hook
        self.assertEqual(len(self._grew_rows()), 1)
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")

    def test_next_brief_contains_the_reactivated_turn_even_after_delivery(self):
        from omhc import brief

        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._append_human_turn(path, "이제 두 번째 턴도 반영해줘")
        self.h.mark()
        body = brief.compute(my_harness="claude-code", my_session_id="me2",
                             repo_root=self.h.root, home=self.h.home, now=now)
        self.assertIn("이제 두 번째 턴도 반영해줘", body)

    def test_a_first_baseline_taken_mid_long_record_does_not_redeliver_old_content(self):
        """Even if a >64KB record is being written at the moment of first
        observation, the baseline must not be set to 0 — at 0, the next
        check would read from the start and falsely reactivate on the
        **original** human turn, redelivering old content (review t7 repro)."""
        now = time.time()
        path = self.h.plant("cx1", now - 600, human="필드 경로부터 다시 확인해줘")
        big = json.dumps({"timestamp": "2026-09-24T00:00:00Z", "type": "response_item",
                          "payload": {"type": "function_call_output", "call_id": "c9",
                                      "output": "x" * 80000}}) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(big[:70000])  # writing past 64KB — no newline yet
        self.h.mark()
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(big[70000:])  # record is done — no new human turn
        self.h.mark()
        self.assertEqual(self._grew_rows(), [])
        self.assertIsNone(
            due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home))

    def test_growth_without_a_human_turn_is_not_reactivated(self):
        """Growth with only agent soliloquy (shell execution) appended is not
        seen as a resume — only the baseline size is updated (seen row)."""
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        _repo.append_codex_turn(path, ordinal=90)  # pure agent shell execution
        self.h.mark()
        self.assertEqual(self._grew_rows(), [])
        self.assertIsNone(
            due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home))
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        seen = [r for r in rows if r.get("harness") == "codex-cli"
               and r.get("session") == "cx1" and r.get("event") == "seen"]
        self.assertEqual(len(seen), 1, rows)

    def test_a_torn_last_line_does_not_lose_the_completed_human_turn(self):
        """Review (round 2) #2: if os.stat catches a record mid-write (the
        last line ends with no newline), the baseline must not include those
        partial bytes — if that record finishes being written later and gets
        read, the skip-to-newline logic would skip over the whole completed
        record and lose the human turn for good."""
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        # One complete agent turn line (ends with a newline) — this alone isn't a resume.
        agent_row = {"timestamp": _iso(now), "ordinal": 90, "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "id": "a90",
                                "content": [{"type": "output_text",
                                            "text": "에이전트 혼잣말"}]}}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(agent_row, ensure_ascii=False) + "\n")

        human_row = _repo.codex_user_row("사람의 완성된 재개 턴", ordinal=91)
        full_line = json.dumps(human_row, ensure_ascii=False).encode("utf-8")
        torn = full_line[:30]  # cut off mid-record with no newline — mid-write
        with open(path, "ab") as fh:
            fh.write(torn)

        self.h.mark()  # the baseline must not include this incomplete line
        self.assertEqual(self._grew_rows(), [])

        with open(path, "ab") as fh:
            fh.write(full_line[30:] + b"\n")  # finish writing the rest

        self.h.mark()  # now complete — must be caught as a resume
        self.assertEqual(len(self._grew_rows()), 1)
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")

    def test_the_existing_dedupe_test_still_holds(self):
        """#22's existing dedupe guarantee holds even with size recording added."""
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark()
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        self.h.mark()
        self.assertIsNone(due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home))
        self.assertEqual(self._grew_rows(), [])

    def test_a_newer_session_in_the_same_interval_wins_over_a_grown_older_one(self):
        """Condition (b): if the same mark call just filled in a newer
        session (B), it must not stack a stale resume (A) on top and make
        due() pick A instead of B."""
        now = time.time()
        path_a = self._deliver(now, "A 세션 첫 턴")
        self._append_human_turn(path_a, "A 세션 재개 턴")
        self.h.plant("cx-b", now - 100, human="B 세션 첫 턴")

        self.h.mark()  # backfill(B) and reactivate(A) candidates overlap within one call
        self.assertEqual(self._grew_rows(), [], "A must not be reactivated in the call that fills in B")
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx-b")

        # In the next call too, A stays superseded by B (condition a: there's
        # another session's start row after the baseline).
        self.h.mark()
        self.assertEqual(self._grew_rows(), [])
        got = due.due_one(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-b")

    def test_a_grows_again_after_b_is_delivered_and_is_reactivated(self):
        """Review #1: a session skipped as `superseded` must not stay blocked
        forever — if A **grows again** (a new human turn) after B, it must
        be caught again. The moment B just arrived
        (`_rebaseline_after_fresh_start`), A's baseline already moves past
        B, so growth after that gets a fresh verdict right away."""
        now = time.time()
        path_a = self._deliver(now, "A 세션 첫 턴")
        self._append_human_turn(path_a, "B 이전의 재개 턴")
        self.h.plant("cx-b", now - 100, human="B 세션 첫 턴")

        self.h.mark()  # backfill(B) also moves A's baseline past B at this moment
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-b")
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)

        self.h.mark()  # no growth since the rebaseline point — not a resume
        self.assertEqual(self._grew_rows(), [])

        self._append_human_turn(path_a, "B 이후의 진짜 재개 턴")
        self.h.mark()  # baseline is past B, so this growth gets a fresh verdict right away
        self.assertEqual(len(self._grew_rows()), 1)
        got2 = due.due_one(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertIsNotNone(got2)
        self.assertEqual(got2.session_id, "cx1")

    def test_a_resume_that_starts_only_after_b_is_delivered_is_reactivated(self):
        """Review (round 2) #1 exact repro: A never grows at all before B —
        it's resumed exactly once, only **after** B has already been
        delivered. Round 1's fix (absorbing superseded only at growth time)
        left the baseline stuck before B in this case, so it swallowed even
        this one and only resume — calling mark any number of times more
        left due() permanently None."""
        now = time.time()
        path_a = self._deliver(now, "A 세션 첫 턴")
        self.h.plant("cx-b", now - 100, human="B 세션 첫 턴")

        self.h.mark()  # backfill(B) — A hasn't grown yet at this moment
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-b")
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)

        self._append_human_turn(path_a, "B 전달 후 A 의 유일한 재개 턴")
        for _ in range(3):
            self.h.mark()
        self.assertEqual(len(self._grew_rows()), 1)
        got2 = due.due_one(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertIsNotNone(got2)
        self.assertEqual(got2.session_id, "cx1")

    def test_t5_b_started_via_its_own_trusted_hook_still_rebaselines_a(self):
        """Review (round 3) #1, repro t5: if B leaves its start row directly
        via **its own trusted Codex hook** rather than backfill,
        `_backfill_foreign_sessions` treats that session as "already known"
        and doesn't fill it in again (`fresh` is empty), so the
        backfill-time rebaseline (`_rebaseline_after_fresh_start`) never
        runs at all. Even so, the next Claude mark's lazy rebaseline
        (ledger's `_reactivate_grown_sessions`) must still rescue A —
        moving the stale baseline past B regardless of whether it grew."""
        now = time.time()
        path_a = self._deliver(now, "A 세션 첫 턴")

        # B's own trusted hook leaves the start row directly — not a backfill.
        self.h.mark(harness="codex-cli", session_id="cx-b")

        self.h.mark()  # Claude mark — learns about B, lazily moves A's baseline
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx-b")
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)

        # Keep typing into A with no hook (no SessionStart firing).
        self._append_human_turn(path_a, "훅 없이 A 를 계속 타이핑")
        for _ in range(3):
            self.h.mark()
        self.assertEqual(len(self._grew_rows()), 1)
        got2 = due.due_one(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertIsNotNone(got2)
        self.assertEqual(got2.session_id, "cx1")

    def test_an_adapter_that_cannot_tell_never_gets_a_growth_row(self):
        """Review #2: an adapter where `read_session_since` (per contract)
        returns None must leave no seen/grew rows at all even if there's
        growth — the contract is "this adapter can't tell", not "recheck
        every time and pile up seen rows while still not knowing". Measured
        (before the fix): 4 marks → 4 seen rows. Both real adapters now
        implement read_session_since (#42), so this patches the method
        directly to keep exercising the "can't tell" contract in isolation."""
        now = time.time()
        fake_claude_path = os.path.join(self.h.home, "fake-claude-session.jsonl")
        with open(fake_claude_path, "w", encoding="utf-8") as fh:
            fh.write("x" * 100 + "\n")
        ledger.append({
            "repo": self.h.key, "harness": "claude-code", "session": "cl1",
            "event": "start", "epoch": now - 600, "path": fake_claude_path,
            "cwd": self.h.root, "via": "scan", "size": os.path.getsize(fake_claude_path),
        }, home=self.h.home)

        from omhc.adapters import claude_code as CC

        patcher = mock.patch.object(CC.ClaudeCodeAdapter, "read_session_since",
                                    return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

        for _ in range(4):
            with open(fake_claude_path, "a", encoding="utf-8") as fh:
                fh.write("y" * 100 + "\n")
            code, _ = self.h.mark(harness="codex-cli", session_id="cx1")
            self.assertEqual(code, 0)

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        claude_side = [r for r in rows if r.get("harness") == "claude-code"
                      and r.get("session") == "cl1"]
        self.assertEqual(len(claude_side), 1, claude_side)  # only the one originally planted line

    def test_a_baseline_older_than_the_discover_window_is_still_reactivated(self):
        """discover() only scans a 14-day window (SCAN_DAYS), but resume
        detection stats only the path already in the ledger, so it catches
        an older baseline too."""
        for days in (10, 20):
            with self.subTest(days=days):
                h = Harness()
                self.addCleanup(h.close)
                now = time.time()
                when = now - days * 86400
                sid = "cx-old{}".format(days)
                path = h.plant(sid, when)
                ledger.append({
                    "repo": h.key, "harness": "codex-cli", "session": sid,
                    "event": "start", "epoch": when, "path": path,
                    "cwd": h.root, "via": "scan", "size": os.path.getsize(path),
                }, home=h.home)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(
                        _repo.codex_user_row("오래된 세션의 재개 턴", ordinal=90),
                        ensure_ascii=False) + "\n")

                code, _ = h.mark()
                self.assertEqual(code, 0)
                got = due.due_one(h.key, "claude-code", "me-old", now, home=h.home)
                self.assertIsNotNone(got)
                self.assertEqual(got.session_id, sid)

    def test_six_repeated_marks_reactivate_exactly_once(self):
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._append_human_turn(path, "이제 두 번째 턴도 반영해줘")
        for _ in range(6):
            self.h.mark()
        self.assertEqual(len(self._grew_rows()), 1)

    def test_deadline_exceeded_writes_no_rows(self):
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._append_human_turn(path, "이제 두 번째 턴도 반영해줘")
        with mock.patch.object(cli, "BACKFILL_TIME_BUDGET", -1000.0):
            self.h.mark()
        self.assertEqual(self._grew_rows(), [])
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        seen = [r for r in rows if r.get("harness") == "codex-cli"
               and r.get("event") == "seen"]
        self.assertEqual(seen, [])
        self.assertIsNone(
            due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home))

    def test_a_garbage_size_field_does_not_break_mark(self):
        now = time.time()
        path = self.h.plant("cx1", now - 600)
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "cx1",
                      "event": "start", "epoch": now - 600, "path": path,
                      "cwd": self.h.root, "via": "scan", "size": "not-a-number"},
                     home=self.h.home)
        code, out = self.h.mark()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


def _iso_claude(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def _plant_claude(home: str, cwd: str, session_id: str, human: str, epoch: float) -> str:
    """One real Claude Code session file — mirrors `Harness.plant`'s Codex
    rollout, but under `~/.claude/projects/<slug>/<session_id>.jsonl` (#42)."""
    from omhc.adapters import claude_code as CC

    root = os.path.realpath(cwd)
    directory = os.path.join(home, ".claude", "projects", CC.claude_slug(root))
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "{}.jsonl".format(session_id))
    row = {"type": "user", "cwd": root, "entrypoint": "cli", "sessionId": session_id,
          "timestamp": _iso_claude(epoch), "message": {"content": human}}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _append_claude_human_turn(path: str, text: str, epoch: float) -> None:
    row = {"type": "user", "timestamp": _iso_claude(epoch), "message": {"content": text}}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _append_claude_agent_turn(path: str, text: str, epoch: float) -> None:
    row = {"type": "assistant", "timestamp": _iso_claude(epoch),
          "message": {"content": [{"type": "text", "text": text}]}}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class TestClaudeReactivateGrownSessions(unittest.TestCase):
    """#42 (v2 phase 2 prerequisite): the same growth-detection path as
    TestReactivateGrownSessions, but with Claude as the foreign, growing
    session and Codex's own `mark` doing the reactivation — the direction
    docs/limits.md used to call an unfixed limit ("a live-continue into a
    Claude session isn't detected")."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        patcher = mock.patch.object(cli, "BACKFILL_TIME_BUDGET", 60.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, now, human):
        """A ledger row with `size` already set to the planted content — the
        same shape `_backfill_foreign_sessions` leaves for Codex (baseline
        included from the start), so the very next mark call judges growth
        directly instead of needing an extra "first observation" round."""
        path = _plant_claude(self.h.home, self.h.root, "cl1", human, now - 600)
        ledger.append({
            "repo": self.h.key, "harness": "claude-code", "session": "cl1",
            "event": "start", "epoch": now - 600, "path": path, "cwd": self.h.root,
            "via": "scan", "size": os.path.getsize(path),
        }, home=self.h.home)
        return path

    def _grew_rows(self, session="cl1"):
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        return [r for r in rows if r.get("harness") == "claude-code"
               and r.get("session") == session and r.get("grew")]

    def test_growth_with_a_human_turn_is_reactivated(self):
        now = time.time()
        path = self._seed(now, "필드 경로부터 다시 확인해줘")
        _append_claude_human_turn(path, "이제 두 번째 턴도 반영해줘", now)
        self.h.mark(harness="codex-cli", session_id="cx1")
        self.assertEqual(len(self._grew_rows()), 1)
        got = due.due_one(self.h.key, "codex-cli", "cx1", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cl1")

    def test_next_brief_contains_the_reactivated_turn_even_after_delivery(self):
        now = time.time()
        path = self._seed(now, "필드 경로부터 다시 확인해줘")
        got = due.due_one(self.h.key, "codex-cli", "cx1", now, home=self.h.home)
        due.mark_delivered(self.h.state, got, to_harness="codex-cli", epoch=now)
        self.assertIsNone(due.due_one(self.h.key, "codex-cli", "cx1", now, home=self.h.home))

        _append_claude_human_turn(path, "이제 두 번째 턴도 반영해줘", now)
        self.h.mark(harness="codex-cli", session_id="cx1")
        self.assertEqual(len(self._grew_rows()), 1)
        body = brief.compute(my_harness="codex-cli", my_session_id="cx2",
                             repo_root=self.h.root, home=self.h.home, now=now)
        self.assertIn("이제 두 번째 턴도 반영해줘", body)

    def test_growth_without_a_human_turn_is_not_reactivated(self):
        now = time.time()
        path = self._seed(now, "필드 경로부터 다시 확인해줘")
        got = due.due_one(self.h.key, "codex-cli", "cx1", now, home=self.h.home)
        due.mark_delivered(self.h.state, got, to_harness="codex-cli", epoch=now)
        _append_claude_agent_turn(path, "에이전트 혼잣말", now)
        self.h.mark(harness="codex-cli", session_id="cx1")
        self.assertEqual(self._grew_rows(), [])
        self.assertIsNone(
            due.due_one(self.h.key, "codex-cli", "cx1", now, home=self.h.home))
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        seen = [r for r in rows if r.get("harness") == "claude-code"
               and r.get("session") == "cl1" and r.get("event") == "seen"]
        self.assertEqual(len(seen), 1, rows)

    def test_a_fork_needs_its_own_turn_to_be_reactivated(self):
        """#34/#42: a fork copies the parent's chain under a new session_id —
        even once its own trusted hook has recorded a baseline, that
        baseline already includes the whole copied section (the common
        case, see the adapter's `_read` docstring), so only a genuinely new
        turn of the fork's own reactivates it."""
        now = time.time()
        path = _plant_claude(self.h.home, self.h.root, "fork1",
                            "부모의 목표", now - 600)
        ledger.append({
            "repo": self.h.key, "harness": "claude-code", "session": "fork1",
            "event": "start", "epoch": now - 600, "path": path, "cwd": self.h.root,
        }, home=self.h.home)
        self.h.mark(harness="codex-cli", session_id="cx1")  # baseline covers this whole file

        with open(path, "a", encoding="utf-8") as fh:
            copied = {"type": "user", "timestamp": _iso_claude(now),
                     "message": {"content": "포크가 복사한 오래된 턴"},
                     "forkedFrom": {"sessionId": "parent1", "messageUuid": "u9"}}
            fh.write(json.dumps(copied, ensure_ascii=False) + "\n")
        self.h.mark(harness="codex-cli", session_id="cx1")
        self.assertEqual(self._grew_rows("fork1"), [])

        _append_claude_human_turn(path, "포크 자신의 새 지시", now)
        self.h.mark(harness="codex-cli", session_id="cx1")
        self.assertEqual(len(self._grew_rows("fork1")), 1)
        got = due.due_one(self.h.key, "codex-cli", "cx1", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "fork1")


@contextlib.contextmanager
def _deadline_trips_after_first_stat(target_name):
    """#28 review repro: makes `time.time()` return a value well past the
    deadline **starting right after** the first `os.stat` call inside
    `cli.<target_name>` — only while that function is running (armed is
    never turned on before entering that function). The first session's
    stat finishes normally, and it's the deadline check right before
    processing the next session that trips and causes a `break`."""
    orig_fn = getattr(cli, target_name)
    orig_stat = os.stat
    orig_time = time.time
    state = {"armed": False}

    def fake_stat(path, *a, **kw):
        result = orig_stat(path, *a, **kw)
        state["armed"] = True
        return result

    def fake_time():
        return (orig_time() + 10 ** 6) if state["armed"] else orig_time()

    def wrapped(*args, **kwargs):
        with mock.patch("os.stat", side_effect=fake_stat), \
             mock.patch.object(time, "time", side_effect=fake_time):
            return orig_fn(*args, **kwargs)

    with mock.patch.object(cli, target_name, side_effect=wrapped):
        yield


class TestRebaseMarker(unittest.TestCase):
    """#28: in both backfill and lazy rebaseline, other sessions that didn't
    grow are absorbed into a single `rebase` marker per (repo, harness)
    instead of individual seen rows."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        # The marker count is only meaningful once a verdict has run to
        # completion. Timed with the real clock, the hook budget (80ms) got
        # tripped judging 60 sessions on a loaded machine (load 7), no
        # marker got written, and the test failed 2 out of 3 runs. The
        # budget-exceeded case is checked separately, with
        # _deadline_trips_after_first_stat skipping the clock ahead.
        patcher = mock.patch.object(cli, "BACKFILL_TIME_BUDGET", 60.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_known_unchanged(self, n, base):
        """Directly plants n already-known sessions that won't grow in this
        verdict into the ledger (reproducing only the "known session" state
        without going through a real backfill)."""
        sids = []
        for i in range(n):
            sid = "old{}".format(i)
            path = self.h.plant(sid, base - 10000 + i)
            ledger.append({
                "repo": self.h.key, "harness": "codex-cli", "session": sid,
                "event": "start", "epoch": base - 10000 + i, "path": path,
                "cwd": self.h.root, "via": "scan", "size": os.path.getsize(path),
            }, home=self.h.home)
            sids.append(sid)
        return sids

    def test_twenty_unchanged_sessions_yield_markers_not_per_session_seen_rows(self):
        """Even with 20 (exactly REACTIVATE_SCAN_CAP) + 4 new sessions coming
        in, still one marker per round, no individual seen rows (#28 review
        round 2: complete is independent of the cap — the
        test_beyond_the_cap_* tests below handle scale beyond the cap)."""
        now = time.time()
        self._seed_known_unchanged(20, now)

        for i in range(4):
            self.h.plant("new{}".format(i), now - 1000 + i * 10)
            self.h.mark()

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        markers = [r for r in codex_rows if r.get("event") == cli.REBASE_EVENT]
        old_seen = [r for r in codex_rows if r.get("event") == "seen"
                   and str(r.get("session", "")).startswith("old")]
        self.assertLessEqual(len(markers), 4, codex_rows)
        self.assertEqual(old_seen, [])
        # A marker has no session/path — known_sessions/newest_start/log
        # ranking must never mistake it for a session (#28).
        for m in markers:
            self.assertNotIn("session", m)
            self.assertNotIn("path", m)

    def test_a_session_that_grew_before_b_still_gets_its_own_seen_row(self):
        """Ambiguous pre-growth (before B, agent-only) still gets its own
        seen row — the marker must not swallow it and falsely move its position up."""
        now = time.time()
        sids = self._seed_known_unchanged(3, now)
        grown_sid = sids[0]
        grown_path = None
        for r in ledger.read(repo_key=self.h.key, home=self.h.home):
            if r.get("session") == grown_sid:
                grown_path = r.get("path")
        _repo.append_codex_turn(grown_path, ordinal=90)  # pure agent shell execution

        self.h.plant("new-b", now - 500)
        self.h.mark()  # backfill(B) — old0 has already grown at this moment

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        grown_seen = [r for r in codex_rows if r.get("session") == grown_sid
                     and r.get("event") == "seen"]
        self.assertEqual(len(grown_seen), 1, codex_rows)
        markers = [r for r in codex_rows if r.get("event") == cli.REBASE_EVENT]
        self.assertEqual(len(markers), 1, codex_rows)

    def test_lazy_path_writes_one_marker_under_a_trusted_hook_b(self):
        """If B arrives via its own trusted hook (not a backfill), the lazy
        rebaseline (`_reactivate_grown_sessions`) runs — it absorbs several
        non-growing A's into one marker, and A's later human turn is still reactivated."""
        now = time.time()
        sids = self._seed_known_unchanged(5, now)

        self.h.mark(harness="codex-cli", session_id="new-b")  # B's own trusted hook

        self.h.mark()  # Claude mark — lazy rebaseline absorbs the 5 A's into one marker
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        markers = [r for r in codex_rows if r.get("event") == cli.REBASE_EVENT]
        old_seen = [r for r in codex_rows if r.get("event") == "seen"
                   and str(r.get("session", "")).startswith("old")]
        self.assertEqual(len(markers), 1, codex_rows)
        self.assertEqual(old_seen, [])

        # After B (new-b) is delivered, if A's (=sids[0]) human turn
        # continues, it's still reactivated.
        got_b = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got_b)
        self.assertEqual(got_b.session_id, "new-b")
        due.mark_delivered(self.h.state, got_b, to_harness="claude-code", epoch=now)

        reactivated_sid = sids[0]
        reactivated_path = None
        for r in rows:
            if r.get("session") == reactivated_sid:
                reactivated_path = r.get("path")
        with open(reactivated_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("B 이후 A 의 재개 턴", ordinal=90),
                                ensure_ascii=False) + "\n")
        self.h.mark()
        got = due.due_one(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, reactivated_sid)

    def test_beyond_the_cap_backfill_path_still_yields_one_marker_per_round(self):
        """[review round 2] `complete` is independent of exceeding the cap
        (`REACTIVATE_SCAN_CAP`) — even when known sessions vastly exceed the
        cap (only the top-N are candidates and the rest are never touched at
        all), it's still one marker per round with no individual seen rows
        for checked sessions. Reverting the round-1 mistake of tying the cap
        to `complete` reproduces the same row explosion as HEAD at n=25/60
        (measured: 84 rows/80 seen)."""
        for n in (cli.REACTIVATE_SCAN_CAP + 5, cli.REACTIVATE_SCAN_CAP + 40):
            with self.subTest(n=n):
                h = Harness()
                self.addCleanup(h.close)
                now = time.time()
                for i in range(n):
                    sid = "old{}".format(i)
                    path = h.plant(sid, now - 100000 + i)
                    ledger.append({
                        "repo": h.key, "harness": "codex-cli", "session": sid,
                        "event": "start", "epoch": now - 100000 + i, "path": path,
                        "cwd": h.root, "via": "scan", "size": os.path.getsize(path),
                    }, home=h.home)

                for r in range(4):
                    h.plant("new-b{}".format(r), now - 1000 + r * 10)
                    h.mark()

                rows = ledger.read(repo_key=h.key, home=h.home)
                codex_rows = [row for row in rows if row.get("harness") == "codex-cli"]
                markers = [row for row in codex_rows if row.get("event") == cli.REBASE_EVENT]
                old_seen = [row for row in codex_rows if row.get("event") == "seen"
                           and str(row.get("session", "")).startswith("old")]
                self.assertEqual(len(markers), 4, codex_rows)
                self.assertEqual(old_seen, [], codex_rows)

    def test_beyond_the_cap_lazy_path_still_yields_one_marker_per_round(self):
        """Same as above, but B arrives each time via its own trusted hook rather than backfill (the lazy rebaseline path)."""
        for n in (cli.REACTIVATE_SCAN_CAP + 5, cli.REACTIVATE_SCAN_CAP + 40):
            with self.subTest(n=n):
                h = Harness()
                self.addCleanup(h.close)
                now = time.time()
                for i in range(n):
                    sid = "old{}".format(i)
                    path = h.plant(sid, now - 100000 + i)
                    ledger.append({
                        "repo": h.key, "harness": "codex-cli", "session": sid,
                        "event": "start", "epoch": now - 100000 + i, "path": path,
                        "cwd": h.root, "via": "scan", "size": os.path.getsize(path),
                    }, home=h.home)

                for r in range(4):
                    h.mark(harness="codex-cli", session_id="new-b{}".format(r))
                    h.mark()  # Claude mark — lazy rebaseline

                rows = ledger.read(repo_key=h.key, home=h.home)
                codex_rows = [row for row in rows if row.get("harness") == "codex-cli"]
                markers = [row for row in codex_rows if row.get("event") == cli.REBASE_EVENT]
                old_seen = [row for row in codex_rows if row.get("event") == "seen"
                           and str(row.get("session", "")).startswith("old")]
                self.assertEqual(len(markers), 4, codex_rows)
                self.assertEqual(old_seen, [], codex_rows)

    def test_x_far_reentering_via_its_own_hook_is_not_falsely_reactivated(self):
        """[review round 2, "x-far" repro] A session outside the cap already
        grew before B (including a human turn) without ever being checked by
        a marker; later it re-enters via its own trusted hook (a new start
        row with a path but no size) and falls within this round's top-N —
        the marker must not disguise that ambiguous pre-growth as a clear
        resume — `marker_covers` must remember only the top-N **at the time
        the marker was written** (reconstructed by slicing to the segment before the marker)."""
        now = time.time()
        n = cli.REACTIVATE_SCAN_CAP + 5
        xfar_sid = "old0"  # planted first, so the oldest — pushed outside the top-N (cap)
        xfar_path = None
        for i in range(n):
            sid = "old{}".format(i)
            path = self.h.plant(sid, now - 100000 + i)
            if sid == xfar_sid:
                xfar_path = path
            ledger.append({
                "repo": self.h.key, "harness": "codex-cli", "session": sid,
                "event": "start", "epoch": now - 100000 + i, "path": path,
                "cwd": self.h.root, "via": "scan", "size": os.path.getsize(path),
            }, home=self.h.home)
        # x-far already grew with a human turn before B arrives — it's
        # outside the cap so it isn't checked in this round.
        with open(xfar_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("B 전, cap 밖에서의 사전 성장",
                                                       ordinal=90),
                                ensure_ascii=False) + "\n")

        self.h.plant("new-b", now - 500, human="B 세션 첫 턴")
        self.h.mark()  # backfill(B) — only the top-N (cap) gets checked and a marker is written

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        self.assertEqual(
            len([r for r in codex_rows if r.get("event") == cli.REBASE_EVENT]), 1,
            codex_rows)
        # x-far was not checked this round — no row of any kind is left.
        self.assertEqual(
            [r for r in codex_rows if r.get("session") == xfar_sid
             and r.get("via") == "scan" and r is not codex_rows[0]], [])

        # x-far re-enters via its own trusted hook — the path is real (same
        # rollout) but there's no size (the exact shape #28's round-2 review reproduces).
        stdin = json.dumps({"cwd": self.h.root, "session_id": xfar_sid,
                            "transcript_path": xfar_path})
        args = cli.build_parser().parse_args(
            ["mark", "--harness", "codex-cli", "--stdin", stdin])
        out = io.StringIO()
        code = cli.cmd_mark(args, home=self.h.home, out=out)
        self.assertEqual(code, 0)

        self.h.mark()  # Claude mark — lazy rebaseline now sees x-far, which is inside the top-N

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        xfar_grew = [r for r in codex_rows if r.get("session") == xfar_sid and r.get("grew")]
        self.assertEqual(xfar_grew, [], codex_rows)

        # If a genuinely newer session (C) comes in, due() must still return
        # it — not x-far. x-far's re-entry row's epoch is the **real** wall
        # clock time at that mark call (cmd_mark uses round(time.time(),0)),
        # so C also has to be planted with a real later time to avoid
        # tripping the newest_start filter.
        after_reentry = time.time()
        self.h.plant("new-c", after_reentry + 10, human="C 세션 첫 턴")
        self.h.mark()
        got = due.due_one(self.h.key, "claude-code", "me-final", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "new-c")

    def _seed_two_known(self, now, grown_sid_human_turn):
        """Plants a-stale (already grown with a human turn before B) and
        a-unch (unchanged) into the ledger — the shared setup step for the three review repros below."""
        path_stale = self.h.plant("a-stale", now - 5000, human="원래 턴")
        ledger.append({
            "repo": self.h.key, "harness": "codex-cli", "session": "a-stale",
            "event": "start", "epoch": now - 5000, "path": path_stale,
            "cwd": self.h.root, "via": "scan", "size": os.path.getsize(path_stale),
        }, home=self.h.home)
        path_unch = self.h.plant("a-unch", now - 4000)
        ledger.append({
            "repo": self.h.key, "harness": "codex-cli", "session": "a-unch",
            "event": "start", "epoch": now - 4000, "path": path_unch,
            "cwd": self.h.root, "via": "scan", "size": os.path.getsize(path_unch),
        }, home=self.h.home)
        if grown_sid_human_turn:
            with open(path_stale, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(_repo.codex_user_row("B 전의 사람 턴", ordinal=90),
                                    ensure_ascii=False) + "\n")
        return path_stale, path_unch

    def test_deadline_trip_mid_backfill_rebaseline_does_not_mark_unverified_session(self):
        """[high, review repro] A marker applies to the whole harness ("every
        known session for this harness was checked and didn't grow") — if
        the deadline cuts things off after checking only a-unch, never
        seeing a-stale (already grown before B), a marker must not be
        written based on a-unch alone. Otherwise, on the next round a-stale
        would be wrongly pushed behind the marker, its ambiguous pre-growth
        misjudged as a clear resume, and due() would return a-stale instead of B."""
        now = time.time()
        self._seed_two_known(now, grown_sid_human_turn=True)
        self.h.plant("new-b", now - 100, human="B 세션 첫 턴")

        with _deadline_trips_after_first_stat("_rebaseline_after_fresh_start"):
            self.h.mark()  # backfill(B) — only a-unch gets checked, a-stale is never seen

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        self.assertEqual([r for r in codex_rows if r.get("event") == cli.REBASE_EVENT],
                         [], "must not write a marker without an exhaustive check")

        self.h.mark()  # a normal round — a-stale's pre-growth should be absorbed, not treated as a resume
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "new-b")

    def test_deadline_trip_mid_lazy_reactivate_does_not_mark_unverified_session(self):
        """[high, review repro] Same repro as above on the lazy path — where
        B arrives via its own trusted hook rather than backfill."""
        now = time.time()
        self._seed_two_known(now, grown_sid_human_turn=True)
        self.h.mark(harness="codex-cli", session_id="new-b")  # B's own trusted hook

        with _deadline_trips_after_first_stat("_reactivate_grown_sessions"):
            self.h.mark()  # Claude mark — lazy rebaseline, cut off after only checking a-unch

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        self.assertEqual([r for r in codex_rows if r.get("event") == cli.REBASE_EVENT],
                         [], "must not write a marker without an exhaustive check")

        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "new-b")

    def test_transient_stat_error_on_one_session_does_not_mark_the_others(self):
        """[medium, review repro] Even if a-stale's stat fails transiently
        (e.g. PermissionError), a marker must not be written just because
        a-unch was confirmed unchanged — that marker would apply equally to
        the a-stale that was never checked."""
        now = time.time()
        path_stale, _ = self._seed_two_known(now, grown_sid_human_turn=True)
        self.h.plant("new-b", now - 100, human="B 세션 첫 턴")

        orig_stat = os.stat

        def flaky_stat(path, *a, **kw):
            if path == path_stale:
                raise PermissionError("transient")
            return orig_stat(path, *a, **kw)

        with mock.patch("os.stat", side_effect=flaky_stat):
            self.h.mark()

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        self.assertEqual([r for r in codex_rows if r.get("event") == cli.REBASE_EVENT],
                         [], "must not write a marker when a-stale could not be checked")

        self.h.mark()  # stat is back to normal — a-stale's pre-growth is absorbed
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "new-b")


class TestLiveContinueWithoutASessionStart(unittest.TestCase):
    """#22's newly discovered second hole: after A is handed off, if the
    human keeps typing into the **still-alive** Codex session A and then
    switches to a new Claude session, that Claude session's SessionStart
    fires (mark runs) but A's SessionStart never fires at all — growth
    detection + read_session_since must still catch it (that mark re-stats
    codex-cli sessions as scan targets, independent of the Codex hook)."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_live_continue_is_delivered_to_a_new_claude_session(self):
        now = time.time()
        path = self.h.plant("cx1", now - 600, human="첫 턴")  # first turn
        self.h.mark(session_id="me1")
        got = due.due_one(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)

        # The human keeps typing into the still-alive Codex session A — the
        # Codex side never fires SessionStart at all.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("A 에서 계속 타이핑",
                                                      ordinal=90),
                                ensure_ascii=False) + "\n")

        # Only the new Claude session's SessionStart fires.
        code, _ = self.h.mark(session_id="me2")
        self.assertEqual(code, 0)
        got = due.due_one(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")


class TestCompactSessionStart(unittest.TestCase):
    """#30: after auto-compaction, SessionStart(source=compact) fires again
    in the same session. If the session is already in the ledger, it must not leave another start row."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def _starts(self, harness, session):
        return [r for r in ledger.read(repo_key=self.h.key, home=self.h.home)
                if r.get("event") == "start" and r.get("harness") == harness
                and r.get("session") == session]

    def test_compact_after_startup_adds_no_start_row(self):
        self.h.mark(harness="codex-cli", session_id="cx1", source="startup")
        self.h.mark(harness="codex-cli", session_id="cx1", source="compact")
        self.h.mark(harness="codex-cli", session_id="cx1", source="compact")
        self.assertEqual(len(self._starts("codex-cli", "cx1")), 1)

    def test_compact_for_an_unknown_session_is_still_recorded(self):
        """For a session that missed startup (installed midway), compact is the first record."""
        self.h.mark(harness="codex-cli", session_id="cx9", source="compact")
        self.assertEqual(len(self._starts("codex-cli", "cx9")), 1)

    def test_compact_refreshes_age_for_a_long_lived_session(self):
        """due() judges age by the most recent start row. A session used
        over multiple days must not drop out past MAX_AGE just because it
        only ever repeats compact (#30 review)."""
        old = time.time() - 8 * 86400
        ledger.append({"repo": self.h.key, "harness": "claude-code",
                       "session": "cc1", "event": "start", "epoch": old,
                       "path": "", "cwd": self.h.root}, home=self.h.home)
        self.h.mark(harness="claude-code", session_id="cc1", source="compact")
        self.assertEqual(len(self._starts("claude-code", "cc1")), 2)

    def test_a_scan_row_alone_does_not_suppress_the_hook_row(self):
        """If there's only a row backfill left in its place, it must leave evidence that the hook ran."""
        ledger.append({"repo": self.h.key, "harness": "codex-cli",
                       "session": "cx2", "event": "start", "epoch": time.time(),
                       "path": "", "cwd": self.h.root, "via": "scan"},
                      home=self.h.home)
        self.h.mark(harness="codex-cli", session_id="cx2", source="compact")
        rows = self._starts("codex-cli", "cx2")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[-1].get("via"), "scan")

    def test_resume_still_adds_a_start_row(self):
        self.h.mark(harness="codex-cli", session_id="cx1", source="startup")
        self.h.mark(harness="codex-cli", session_id="cx1", source="resume")
        self.assertEqual(len(self._starts("codex-cli", "cx1")), 2)


class TestOnSessionStartMarkCli(unittest.TestCase):
    """#36: `omhc mark` collapses an "already read" AGENTS.md block on
    startup/resume so the next Codex session can't read it — checks that the
    adapter's optional method actually gets called through cmd_mark (the
    unit test is test_codex_cli.py::TestOnSessionStartMark)."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def _captured_at(self):
        from omhc import agents_md, managed_block

        return managed_block.installed_captured_at(agents_md.path_for(self.h.root))

    def test_old_block_collapses_on_codex_startup_mark(self):
        from omhc import agents_md, managed_block

        managed_block.splice(agents_md.path_for(self.h.root), "[omhc] old\n",
                             captured_at=time.time() - 3600)
        self.h.mark(harness="codex-cli", session_id="cx1", source="startup")
        self.assertIsNone(self._captured_at())

    def test_old_block_is_kept_on_codex_resume_mark(self):
        """Review #2: the "reads before the hook" ordering was only measured
        on startup — since it's unknown when Codex computes the AGENTS.md
        diff on resume, resume never collapses the block."""
        from omhc import agents_md, managed_block

        managed_block.splice(agents_md.path_for(self.h.root), "[omhc] old\n",
                             captured_at=time.time() - 3600)
        self.h.mark(harness="codex-cli", session_id="cx1", source="resume")
        self.assertIsNotNone(self._captured_at())

    def test_block_written_by_this_same_burst_is_kept(self):
        """Mimics brief (Path B) having just written this session's share of
        the block in parallel — if mark mistook it for a "stale block" and
        wiped it, even this session couldn't read it."""
        from omhc import agents_md, managed_block

        managed_block.splice(agents_md.path_for(self.h.root), "[omhc] just written\n",
                             captured_at=time.time())
        self.h.mark(harness="codex-cli", session_id="cx1", source="startup")
        self.assertIsNotNone(self._captured_at())

    def test_shared_with_claude_is_kept(self):
        from omhc import agents_md, managed_block

        agents_path = agents_md.path_for(self.h.root)
        claude_path = os.path.join(self.h.root, "CLAUDE.md")
        managed_block.splice(agents_path, "[omhc] old\n", captured_at=time.time() - 3600)
        os.symlink(agents_path, claude_path)
        self.h.mark(harness="codex-cli", session_id="cx1", source="startup")
        self.assertIsNotNone(self._captured_at())

    def test_claude_mark_never_collapses_via_this_path(self):
        from omhc import agents_md, managed_block

        managed_block.splice(agents_md.path_for(self.h.root), "[omhc] old\n",
                             captured_at=time.time() - 3600)
        self.h.mark(harness="claude-code", session_id="cc1", source="startup")
        self.assertIsNotNone(self._captured_at())

    def test_compact_source_never_collapses(self):
        from omhc import agents_md, managed_block

        managed_block.splice(agents_md.path_for(self.h.root), "[omhc] old\n",
                             captured_at=time.time() - 3600)
        self.h.mark(harness="codex-cli", session_id="cx1", source="compact")
        self.assertIsNotNone(self._captured_at())


if __name__ == "__main__":
    unittest.main()
