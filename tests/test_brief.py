from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from omhc import brief, deliver, due, ledger, locate, pin

from . import _repo
from omhc.adapter import Capability, HandoffBundle

NOW = 1758500000.0


class Harness:
    """Temp home + temp repo. Planting sessions is owned by tests/_repo.plant_codex."""

    def __init__(self):
        self.t = _repo.TempRepo()
        self.home = self.t.home
        self.repo = self.t.repo
        self.repo_root = self.t.root
        self.key = self.t.key
        self.state = self.t.state

    def close(self):
        self.t.close()

    def plant_codex_session(self, session_id="cx1",
                            human="필드 경로부터 다시 확인해줘"):
        return self.t.plant_codex(session_id=session_id, human=human,
                                  shell_turns=1, failing_shell=True,
                                  ledger_home=self.home, when=NOW)


class TestCompute(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_handoff_body_is_produced_for_a_foreign_session(self):
        self.h.plant_codex_session()
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertIn("[omhc]", body)
        self.assertIn("필드 경로", body)
        self.assertLessEqual(len(body.encode("utf-8")), 900)

    def test_failed_command_appears_as_fail(self):
        self.h.plant_codex_session()
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertIn("FAIL", body)
        self.assertIn("pytest", body)

    def test_nothing_to_send_yields_empty(self):
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(body, "")

    def test_a_subagent_codex_session_never_becomes_a_handoff(self):
        """A parent agent's prompt must never disguise itself as a human's
        words and become GOAL/NEXT (invariant 3). Both ref_for_path and the
        list_sessions fallback must filter it out for due() to come back empty."""
        self.h.t.plant_codex(
            session_id="sub1", human="부모 에이전트가 시킨 일",
            ledger_home=self.h.home, when=NOW,
            meta_extra={"thread_source": "subagent",
                       "parent_thread_id": "parent-thread-id"},
        )
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(body, "")

    def test_an_applecider_templated_turn_never_reaches_goal(self):
        """originator=applecider has source=vscode, so there's no subagent marker.

        If not filtered out, the app server's machine-filled template gets
        disguised as the human's GOAL.
        """
        self.h.t.plant_codex(
            session_id="app1",
            human="User goal: 브라우저 자동화 작업 Current browser URL: about:blank",
            ledger_home=self.h.home, when=NOW,
            meta_extra={"originator": "applecider", "source": "vscode"},
        )
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(body, "")

    def test_second_call_in_the_same_session_yields_empty(self):
        """The SessionStart hook fires multiple times within one session."""
        self.h.plant_codex_session()
        first = brief.compute(my_harness="claude-code", my_session_id="me1",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        second = brief.compute(my_harness="claude-code", my_session_id="me1",
                               repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertTrue(first)
        self.assertEqual(second, "")

    def test_delivered_session_is_not_resent_to_a_new_session(self):
        self.h.plant_codex_session()
        brief.compute(my_harness="claude-code", my_session_id="me1",
                      repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        again = brief.compute(my_harness="claude-code", my_session_id="me2",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(again, "")

    def test_archive_is_built_as_a_side_effect(self):
        path = self.h.plant_codex_session()
        brief.compute(my_harness="claude-code", my_session_id="me1",
                      repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        pinned = os.path.join(self.h.state, "pinned", "cx1", "source.jsonl")
        idx = os.path.join(self.h.state, "index", "cx1.idx")
        self.assertTrue(os.path.exists(pinned), "no hardlink")
        self.assertEqual(os.stat(path).st_ino, os.stat(pinned).st_ino)
        self.assertTrue(os.path.exists(idx), "no index")

    def test_pin_failure_is_logged_but_the_hook_still_succeeds(self):
        """Review defect: silently swallowing a pin failure leaves `omhc
        status`'s archive row falsely PASS-ing with no trace. Since this is
        the hook path (invariant 2), only a log should be left — exit 0 and
        the handoff body must stay unaffected."""
        self.h.plant_codex_session()
        broken = pin.PinResult(None, False, 0, "mocked pin failure")
        stdin = json.dumps({"session_id": "me1", "cwd": self.h.repo_root})
        with mock.patch.object(pin, "pin_session_result", return_value=broken):
            out = io.StringIO()
            code = brief.emit(harness="claude-code", stdin_text=stdin,
                              home=self.h.home, now=NOW, out=out)
        self.assertEqual(code, 0)
        self.assertIn("[omhc]", out.getvalue())

        guard_log = os.path.join(locate.omhc_root(self.h.home), brief.GUARD_LOG)
        with open(guard_log, encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("pin failed: mocked pin failure", content)

    def test_notes_are_included(self):
        self.h.plant_codex_session()
        os.makedirs(self.h.state, exist_ok=True)
        with open(os.path.join(self.h.state, "notes.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("rollout 이 source of truth\n")
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertIn("source of truth", body)

    def test_old_stamped_notes_expire_but_legacy_lines_stay(self):
        """#36: a note older than 7 days doesn't ride along in the handoff. Legacy timestampless lines still stay."""
        from omhc import due
        self.h.plant_codex_session()
        os.makedirs(self.h.state, exist_ok=True)
        with open(os.path.join(self.h.state, "notes.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("옛 형식 메모\n")
            fh.write("{:.0f}\t지난달 메모\n".format(NOW - due.MAX_AGE_SECONDS - 60))
            fh.write("{:.0f}\t어제 메모\n".format(NOW - 86400))
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertIn("어제 메모", body)
        self.assertIn("옛 형식 메모", body)
        self.assertNotIn("지난달 메모", body)
        self.assertNotIn("\t", body)

    def test_omhc_note_writes_a_stamp_that_the_reader_expires(self):
        """Pins down that the writer and reader agree on the same format (review)."""
        import time as _time
        from omhc import cli, due
        cwd = os.getcwd()
        os.chdir(self.h.repo_root)
        self.addCleanup(os.chdir, cwd)
        out, err = io.StringIO(), io.StringIO()
        code = cli.cmd_note(cli.build_parser().parse_args(["note", "탭\t포함 메모"]),
                            home=self.h.home, out=out, err=err)
        self.assertEqual(code, 0)
        with open(os.path.join(self.h.state, "notes.txt"), encoding="utf-8") as fh:
            line = fh.read().strip("\n")
        m = brief._NOTE_STAMP.match(line)
        self.assertIsNotNone(m, line)
        self.assertEqual(m.group(2), "탭 포함 메모")
        self.assertEqual(brief._notes(self.h.state, now=_time.time()), ["탭 포함 메모"])
        self.assertEqual(brief._notes(self.h.state,
                                      now=_time.time() + due.MAX_AGE_SECONDS + 60), [])

    def test_same_vendor_yields_empty(self):
        self.h.plant_codex_session()
        body = brief.compute(my_harness="codex-cli", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(body, "")


class TestEligibilityAtBriefTime(unittest.TestCase):
    """Whether a session was a human conversation is judged by the adapter at brief time (#21)."""

    EXEC = {"source": "exec", "originator": "codex_exec"}

    def setUp(self):
        self.h = Harness()
        self._backup = os.environ.pop("OMHC_ALLOW_HEADLESS", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._backup is not None:
            os.environ["OMHC_ALLOW_HEADLESS"] = self._backup
        else:
            os.environ.pop("OMHC_ALLOW_HEADLESS", None)

    def tearDown(self):
        self.h.close()

    def compute(self):
        return brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)

    def test_a_later_headless_session_does_not_block_the_interactive_one(self):
        self.h.t.plant_codex(session_id="cx1", human="사람이 한 말",
                             ledger_home=self.h.home, when=NOW)
        self.h.t.plant_codex(session_id="cx2", human="exec 가 받은 프롬프트",
                             ledger_home=self.h.home, when=NOW,
                             meta_extra=self.EXEC)
        body = self.compute()
        self.assertIn("사람이 한 말", body)
        self.assertNotIn("exec 가 받은 프롬프트", body)

    def test_a_newer_session_whose_file_is_gone_stops_the_search(self):
        """A vanished file is not evidence of headless. Skipping past it would
        surface the previous day's session as if it just happened, instead
        of the one the user actually continued — a stale handoff is worse
        than none."""
        self.h.t.plant_codex(session_id="cx1", human="월요일에 하던 옛 작업",
                             ledger_home=self.h.home, when=NOW)
        cx2 = self.h.t.plant_codex(session_id="cx2", human="화요일에 이어서 한 작업",
                                   ledger_home=self.h.home, when=NOW)
        os.remove(cx2)
        self.assertEqual(self.compute(), "")

    def test_a_newer_session_in_an_unknown_shape_stops_the_search(self):
        """An empty file or a rollout in a changed format is not evidence of
        headless. Skipping past it would skip every new session from the
        day the format changed onward and surface a stale one instead
        (invariant 7: fail open)."""
        for content in ("", '{"type": "session_start_v2", "payload": {}}\n'):
            with self.subTest(content=content):
                self.h.close()
                self.h = Harness()
                self.h.t.plant_codex(session_id="cx1", human="월요일에 하던 옛 작업",
                                     ledger_home=self.h.home, when=NOW)
                cx2 = self.h.t.plant_codex(session_id="cx2", human="x",
                                           ledger_home=self.h.home, when=NOW)
                with open(cx2, "w", encoding="utf-8") as fh:
                    fh.write(content)
                self.assertEqual(self.compute(), "")

    def test_rows_with_missing_files_cost_at_most_one_scan(self):
        from omhc.adapters import codex_cli
        self.h.t.plant_codex(session_id="cx1", human="사람이 한 말",
                             ledger_home=self.h.home, when=NOW)
        for sid in ("gone1", "gone2", "gone3"):
            os.remove(self.h.t.plant_codex(session_id=sid, human="x",
                                           ledger_home=self.h.home, when=NOW))
        with mock.patch.object(codex_cli.CodexCliAdapter, "list_sessions",
                               autospec=True, return_value=[]) as scan:
            self.assertEqual(self.compute(), "")
        self.assertLessEqual(scan.call_count, 1)

    def test_the_override_applies_to_sessions_marked_before_it_was_set(self):
        self.h.t.plant_codex(session_id="cx2", human="exec 가 받은 프롬프트",
                             ledger_home=self.h.home, when=NOW,
                             meta_extra=self.EXEC)
        self.assertEqual(self.compute(), "")
        os.environ["OMHC_ALLOW_HEADLESS"] = "1"
        self.assertIn("exec 가 받은 프롬프트", self.compute())

    def test_mark_records_no_verdict_even_before_the_rollout_exists(self):
        """Even if the rollout doesn't exist yet at mark time, it must not be locked in as non-interactive in the ledger."""
        from omhc import cli
        path = os.path.join(self.h.home, ".codex", "sessions", "rollout-cx1.jsonl")
        stdin = json.dumps({"cwd": self.h.repo_root, "session_id": "cx1",
                            "transcript_path": path})
        cli.cmd_mark(cli.build_parser().parse_args(
            ["mark", "--harness", "codex-cli", "--stdin", stdin]), home=self.h.home)
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        self.assertTrue(rows)
        self.assertNotIn("interactive", rows[-1])


class TestRunHostileInputs(unittest.TestCase):
    """Spec §16-5: empty stdout + exit 0 across 5 kinds of adversarial input."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def _run(self, stdin_text, harness="claude-code"):
        out = io.StringIO()
        code = brief.emit(harness=harness, stdin_text=stdin_text,
                          home=self.h.home, now=NOW, out=out)
        return code, out.getvalue()

    def test_missing_ledger(self):
        code, text = self._run(json.dumps({"cwd": self.h.repo_root,
                                           "session_id": "me1"}))
        self.assertEqual((code, text), (0, ""))

    def test_ledger_with_a_corrupt_half_line(self):
        self.h.plant_codex_session()
        path = os.path.join(self.h.home, ".omhc", ledger.LEDGER_NAME)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"repo":"x","harn\n')
        code, text = self._run(json.dumps({"cwd": self.h.repo_root,
                                           "session_id": "me1"}))
        self.assertEqual(code, 0)
        self.assertIn("[omhc]", text)

    def test_nonexistent_transcript_path(self):
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "gone",
                       "event": "start", "epoch": NOW - 10,
                       "path": "/nope/missing.jsonl", "cwd": self.h.repo_root},
                      home=self.h.home)
        code, text = self._run(json.dumps({"cwd": self.h.repo_root,
                                           "session_id": "me1"}))
        self.assertEqual((code, text), (0, ""))

    def test_dev_null_transcript_path(self):
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "null",
                       "event": "start", "epoch": NOW - 10, "path": "/dev/null",
                       "cwd": self.h.repo_root}, home=self.h.home)
        code, text = self._run(json.dumps({"cwd": self.h.repo_root,
                                           "session_id": "me1"}))
        self.assertEqual((code, text), (0, ""))

    def test_broken_stdin_json(self):
        self.h.plant_codex_session()
        code, text = self._run("{not json at all")
        self.assertEqual(code, 0)

    def test_empty_stdin(self):
        self.h.plant_codex_session()
        code, _text = self._run("")
        self.assertEqual(code, 0)

    def test_missing_harness_argument_is_a_noop(self):
        self.h.plant_codex_session()
        code, text = self._run(json.dumps({"cwd": self.h.repo_root}), harness="")
        self.assertEqual((code, text), (0, ""))

    def test_refused_root_yields_empty_stdout_and_exit_0(self):
        """Meaningless unless non-empty — plant a real session under repo_key("/")
        to reproduce the situation where compute() would have made a real
        handoff had the refusal not happened. With nothing at all in the
        ledger, this test would still pass with the refusal branch deleted
        (review defect)."""
        self.h.t.plant_codex(cwd="/", session_id="cx-root",
                             human="루트 세션은 절대 새면 안 된다",
                             ledger_home=self.h.home, when=NOW)
        code, text = self._run(json.dumps({"cwd": "/", "session_id": "me1"}))
        self.assertEqual((code, text), (0, ""))

    def test_unwritable_home_does_not_raise(self):
        self.h.plant_codex_session()
        out = io.StringIO()
        code = brief.emit(
            harness="claude-code",
            stdin_text=json.dumps({"cwd": self.h.repo_root, "session_id": "me1"}),
            home="/proc/omhc-nonexistent", now=NOW, out=out)
        self.assertEqual(code, 0)


class TestRunWireShape(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_stdout_is_the_hook_wire_json(self):
        self.h.plant_codex_session()
        out = io.StringIO()
        code = brief.emit(
            harness="claude-code",
            stdin_text=json.dumps({"cwd": self.h.repo_root, "session_id": "me1"}),
            home=self.h.home, now=NOW, out=out)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("[omhc]", payload["hookSpecificOutput"]["additionalContext"])

    def test_text_mode_prints_the_body_only(self):
        self.h.plant_codex_session()
        out = io.StringIO()
        brief.emit(
            harness="claude-code", as_text=True,
            stdin_text=json.dumps({"cwd": self.h.repo_root, "session_id": "me1"}),
            home=self.h.home, now=NOW, out=out)
        self.assertTrue(out.getvalue().startswith("[omhc]"))

    def test_budget_flag_is_respected(self):
        self.h.plant_codex_session()
        out = io.StringIO()
        brief.emit(
            harness="claude-code", as_text=True, budget=400,
            stdin_text=json.dumps({"cwd": self.h.repo_root, "session_id": "me1"}),
            home=self.h.home, now=NOW, out=out)
        self.assertLessEqual(len(out.getvalue().encode("utf-8")), 400)

    def test_second_run_in_the_same_session_prints_nothing(self):
        self.h.plant_codex_session()
        payload = json.dumps({"cwd": self.h.repo_root, "session_id": "me1"})
        first, second = io.StringIO(), io.StringIO()
        brief.emit(harness="claude-code", stdin_text=payload, home=self.h.home,
                   now=NOW, out=first)
        brief.emit(harness="claude-code", stdin_text=payload, home=self.h.home,
                   now=NOW, out=second)
        self.assertTrue(first.getvalue())
        self.assertEqual(second.getvalue(), "")


class TestDryRun(unittest.TestCase):
    """--dry-run shows the body only and writes nothing (#17). If one manual
    check consumed that session's delivery, the next real SessionStart would
    get nothing."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def _tree(self):
        found = {}
        for base in (self.h.home, self.h.repo_root):
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    path = os.path.join(dirpath, name)
                    st = os.stat(path)
                    found[path] = (st.st_size, st.st_mtime_ns)
        return found

    def test_dry_run_writes_nothing(self):
        self.h.plant_codex_session()
        before = self._tree()
        out = io.StringIO()
        code = brief.emit(
            harness="claude-code", dry_run=True,
            stdin_text=json.dumps({"cwd": self.h.repo_root, "session_id": "me1"}),
            home=self.h.home, now=NOW, out=out)
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("[omhc]"))
        self.assertEqual(self._tree(), before)

    def test_dry_run_does_not_consume_the_real_delivery(self):
        self.h.plant_codex_session()
        payload = json.dumps({"cwd": self.h.repo_root, "session_id": "me1"})
        dry, real = io.StringIO(), io.StringIO()
        brief.emit(harness="claude-code", dry_run=True, stdin_text=payload,
                   home=self.h.home, now=NOW, out=dry)
        brief.emit(harness="claude-code", stdin_text=payload,
                   home=self.h.home, now=NOW, out=real)
        self.assertTrue(dry.getvalue())
        payload = json.loads(real.getvalue())
        self.assertIn("[omhc]", payload["hookSpecificOutput"]["additionalContext"])

    def test_dry_run_works_without_a_session_id(self):
        """A manual call has no hook payload. Since the gate isn't used, no session id is needed either."""
        self.h.plant_codex_session()
        out = io.StringIO()
        brief.emit(harness="claude-code", dry_run=True,
                   stdin_text=json.dumps({"cwd": self.h.repo_root}),
                   home=self.h.home, now=NOW, out=out)
        self.assertTrue(out.getvalue().startswith("[omhc]"))

    def test_dry_run_with_an_open_non_tty_stdin_does_not_hang(self):
        """`omhc brief --dry-run` must not hang when stdin is open but not a
        TTY (the other end of a pipe stays open and writes nothing) (#27
        side discovery). A dry-run with no explicit --stdin must not read
        stdin at all."""
        # A real process, so brief uses the real time.time() — the NOW
        # constant is a fixed past time and would trip the too-old filter.
        # Only here do we plant with the real current time.
        self.h.t.plant_codex(session_id="cx1", human="필드 경로부터 다시 확인해줘",
                             ledger_home=self.h.home, when=time.time())
        from tests._repo import REPO
        r_fd, w_fd = os.pipe()
        try:
            proc = subprocess.run(
                [os.path.join(REPO, "bin", "omhc"), "brief",
                 "--harness", "claude-code", "--dry-run"],
                stdin=r_fd, capture_output=True, text=True,
                cwd=self.h.repo_root, env=self.h.t.env,
                timeout=10,
            )
        finally:
            os.close(r_fd)
            os.close(w_fd)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("[omhc]", proc.stdout)

    def test_dry_run_reads_a_piped_payload_from_a_different_cwd(self):
        """Review (#27 round 1): one documented use is piping the payload from
        a different cwd (`echo '{"cwd": R}' | omhc brief --dry-run`). Never
        reading stdin at all would break this use — here it must actually read."""
        self.h.t.plant_codex(session_id="cx1", human="필드 경로부터 다시 확인해줘",
                             ledger_home=self.h.home, when=time.time())
        from tests._repo import REPO
        r_fd, w_fd = os.pipe()
        os.write(w_fd, json.dumps({"cwd": self.h.repo_root}).encode("utf-8"))
        os.close(w_fd)  # send like echo and close right away — EOF.
        try:
            proc = subprocess.run(
                [os.path.join(REPO, "bin", "omhc"), "brief",
                 "--harness", "claude-code", "--dry-run"],
                stdin=r_fd, capture_output=True, text=True,
                cwd=tempfile.gettempdir(), env=self.h.t.env,
                timeout=10,
            )
        finally:
            os.close(r_fd)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("[omhc]", proc.stdout)

    def test_dry_run_returns_after_a_write_even_if_the_pipe_stays_open(self):
        """The case where the writer sends one line and keeps the pipe open
        (#27 review) — no EOF arrives, but no more data arrives either, so
        it must stop within the timeout."""
        self.h.t.plant_codex(session_id="cx1", human="필드 경로부터 다시 확인해줘",
                             ledger_home=self.h.home, when=time.time())
        from tests._repo import REPO
        r_fd, w_fd = os.pipe()
        os.write(w_fd, json.dumps({"cwd": self.h.repo_root}).encode("utf-8"))
        try:
            proc = subprocess.run(
                [os.path.join(REPO, "bin", "omhc"), "brief",
                 "--harness", "claude-code", "--dry-run"],
                stdin=r_fd, capture_output=True, text=True,
                cwd=tempfile.gettempdir(), env=self.h.t.env,
                timeout=10,
            )
        finally:
            os.close(r_fd)
            os.close(w_fd)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("[omhc]", proc.stdout)

    def test_cli_dry_run_flag_reaches_compute(self):
        from omhc import cli
        self.h.plant_codex_session()
        before = self._tree()
        out = io.StringIO()
        args = cli.build_parser().parse_args(
            ["brief", "--harness", "claude-code", "--dry-run",
             "--stdin", json.dumps({"cwd": self.h.repo_root})])
        with mock.patch("omhc.brief.time.time", return_value=NOW):
            code = cli.cmd_brief(args, home=self.h.home, out=out)
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("[omhc]"))
        self.assertEqual(self._tree(), before)


class TestReopenRedeliveryGuard(unittest.TestCase):
    """#27: reopen is only a hint — a new human turn must actually exist before resending."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def _deliver_once(self):
        path = self.h.plant_codex_session()
        first = brief.compute(my_harness="claude-code", my_session_id="me1",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertTrue(first)
        return path

    def test_empty_prompt_resume_yields_empty(self):
        """`codex exec resume <id> ""` — only a user message with `"text": ""`
        gets appended to the rollout (measured). Not a new human turn, so it
        must not be resent."""
        path = self._deliver_once()
        due.mark_reopened(self.h.state, "cx1", "codex-cli", NOW + 10)
        from tests._repo import append_codex_user_turn
        append_codex_user_turn(path, "", ordinal=90)
        again = brief.compute(my_harness="claude-code", my_session_id="me2",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW + 20)
        self.assertEqual(again, "")

    def test_a_real_new_human_turn_is_delivered_again(self):
        path = self._deliver_once()
        due.mark_reopened(self.h.state, "cx1", "codex-cli", NOW + 10)
        from tests._repo import append_codex_user_turn
        append_codex_user_turn(path, "이어서 로그 포맷도 고쳐줘", ordinal=90)
        again = brief.compute(my_harness="claude-code", my_session_id="me2",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW + 20)
        self.assertIn("이어서 로그 포맷도 고쳐줘", again)

    def test_race_delivery_then_reopen_with_no_new_turn_yields_empty(self):
        """A measured mark/brief concurrency race: in the exact same second
        brief delivers, mark's growth check sees the just-delivered turn and
        appends a reopen right after it."""
        self._deliver_once()
        due.mark_reopened(self.h.state, "cx1", "codex-cli", NOW + 1)
        again = brief.compute(my_harness="claude-code", my_session_id="me2",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW + 2)
        self.assertEqual(again, "")

    def test_legacy_four_column_delivery_is_still_redelivered(self):
        """A reopen after a legacy 4-column delivered line (no offset) keeps
        today's behavior as-is — with no known offset, resend unconditionally."""
        path = self.h.plant_codex_session()
        wm = due.Watermark(repo_key=self.h.key, harness="codex-cli", session_id="cx1",
                           path=path, event="start", epoch=NOW - 600)
        due.mark_delivered(self.h.state, wm, to_harness="claude-code", epoch=NOW)
        due.mark_reopened(self.h.state, "cx1", "codex-cli", NOW + 10)
        again = brief.compute(my_harness="claude-code", my_session_id="me2",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW + 20)
        self.assertTrue(again)

    def test_dry_run_applies_the_same_guard(self):
        path = self._deliver_once()
        due.mark_reopened(self.h.state, "cx1", "codex-cli", NOW + 10)
        from tests._repo import append_codex_user_turn
        append_codex_user_turn(path, "", ordinal=90)
        again = brief.compute(my_harness="claude-code", my_session_id="me2",
                              repo_root=self.h.repo_root, home=self.h.home,
                              now=NOW + 20, dry_run=True)
        self.assertEqual(again, "")


class TestDeliver(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def bundle(self, to="claude-code"):
        return HandoffBundle(body_md="[omhc] hi\n", repo_root=self.h.repo_root,
                             to_adapter_id=to)

    def test_same_vendor_produces_no_handoff(self):
        """F5 is handled by mint alone — keeping the same rule in two modules
        means only one gets updated. deliver had a duplicate declaration and
        only the test exercised it."""
        self.h.plant_codex_session()
        body = brief.compute(my_harness="codex-cli", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(body, "")

    def test_claude_delivery_writes_the_state_artifact(self):
        receipt = deliver.deliver(self.bundle(), home=self.h.home, now=NOW)
        self.assertEqual(receipt.channel, "sessionstart-hook")
        self.assertTrue(os.path.exists(receipt.paths_written[0]))

    def test_unknown_adapter_falls_through_to_the_file_drop(self):
        receipt = deliver.deliver(self.bundle(to="nope-cli"), home=self.h.home,
                                  now=NOW)
        self.assertEqual(receipt.channel, "file-drop")
        self.assertTrue(os.path.exists(receipt.paths_written[0]))

    def test_file_drop_path_shape(self):
        receipt = deliver.file_drop(self.bundle(), "because", now=NOW)
        path = receipt.paths_written[0]
        self.assertIn(os.path.join(".omhc", "outbox"), path)
        self.assertTrue(path.endswith("-to-claude-code.md"))
        with open(path, encoding="utf-8") as fh:
            self.assertIn("because", fh.read())

    def test_file_drop_header_carries_a_readable_utc_stamp_next_to_the_epoch(self):
        """#36: the body's relative age ("3m ago") freezes at mint time — the
        header needs an absolute timestamp so a later reader can judge whether it's stale."""
        receipt = deliver.file_drop(self.bundle(), "because", now=NOW)
        with open(receipt.paths_written[0], encoding="utf-8") as fh:
            header = fh.readline()
        self.assertIn('captured="{:.0f}"'.format(NOW), header)
        self.assertIn("captured_utc=\"2025-09-22T", header)

    def test_file_drop_neutralizes_a_comment_terminator_in_why(self):
        """Review #3: if `why` contains `-->`, the comment ends there and the
        following captured=/captured_utc= leaks into the body."""
        receipt = deliver.file_drop(
            self.bundle(), "boom --> <script>evil</script>", now=NOW)
        with open(receipt.paths_written[0], encoding="utf-8") as fh:
            header = fh.readline()
        self.assertNotIn("-->", header[:-len(" -->\n")])
        self.assertTrue(header.rstrip("\n").endswith(" -->"))
        self.assertIn('captured="{:.0f}"'.format(NOW), header)
        self.assertTrue(deliver._is_own_outbox_file(receipt.paths_written[0]))

    def test_file_drop_collapses_newlines_in_why(self):
        receipt = deliver.file_drop(self.bundle(), "line1\nline2\r\nline3", now=NOW)
        with open(receipt.paths_written[0], encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertTrue(lines[0].startswith(deliver.FILE_DROP_HEADER_PREFIX))
        self.assertIn("line1 line2  line3", lines[0])

    def test_file_drop_registers_omhc_dir_in_git_info_exclude_once(self):
        deliver.file_drop(self.bundle(), "because", now=NOW)
        deliver.file_drop(self.bundle(), "because again", now=NOW + 1)
        exclude_path = os.path.join(self.h.repo_root, ".git", "info", "exclude")
        with open(exclude_path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count(".omhc/"), 1)

    def test_file_drop_registers_a_pre_existing_omhc_dir_too(self):
        """Review #1: it used to only try registering when `.omhc/` was newly
        created, so an existing user who already had an outbox never got registered."""
        os.makedirs(os.path.join(self.h.repo_root, deliver.OUTBOX_DIR), exist_ok=True)
        deliver.file_drop(self.bundle(), "because", now=NOW)
        exclude_path = os.path.join(self.h.repo_root, ".git", "info", "exclude")
        with open(exclude_path, encoding="utf-8") as fh:
            self.assertIn(".omhc/", fh.read())

    def test_second_drop_does_not_spawn_a_subprocess_once_excluded(self):
        """Review #1: once already registered, reading `.git/info/exclude`
        once must be enough — must not spawn a git check-ignore subprocess again."""
        deliver.file_drop(self.bundle(), "first", now=NOW)
        with mock.patch("subprocess.run") as run:
            deliver.file_drop(self.bundle(), "second", now=NOW + 1)
            run.assert_not_called()

    def test_prune_outbox_deletes_own_files_older_than_the_ttl_and_keeps_fresh_ones(self):
        old = deliver.file_drop(self.bundle(), "old", now=NOW).paths_written[0]
        fresh = deliver.file_drop(self.bundle(), "fresh", now=NOW + 1).paths_written[0]
        old_time = NOW - deliver.OUTBOX_TTL_SECONDS - 10
        os.utime(old, (old_time, old_time))
        removed = deliver.prune_outbox(self.h.repo_root, now=NOW)
        self.assertEqual(removed, [old])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_prune_outbox_never_deletes_a_file_it_did_not_write(self):
        directory = os.path.join(self.h.repo_root, deliver.OUTBOX_DIR)
        os.makedirs(directory, exist_ok=True)
        stray = os.path.join(directory, "2020-to-somewhere.md")
        with open(stray, "w", encoding="utf-8") as fh:
            fh.write("not omhc's file\n")
        old_time = NOW - deliver.OUTBOX_TTL_SECONDS - 10
        os.utime(stray, (old_time, old_time))
        removed = deliver.prune_outbox(self.h.repo_root, now=NOW)
        self.assertEqual(removed, [])
        self.assertTrue(os.path.exists(stray))

    def test_prune_outbox_with_force_removes_fresh_files_too(self):
        fresh = deliver.file_drop(self.bundle(), "fresh", now=NOW).paths_written[0]
        removed = deliver.prune_outbox(self.h.repo_root, now=NOW, force=True)
        self.assertEqual(removed, [fresh])
        self.assertFalse(os.path.exists(fresh))

    def test_read_only_adapter_falls_through_to_the_file_drop(self):
        from omhc import adapters

        class ReadOnly:
            adapter_id = "ro-cli"
            capabilities = frozenset({Capability.READ})

            def __init__(self, *, home=None, now=None):
                pass

        adapters.REGISTRY["ro-cli"] = ReadOnly
        try:
            receipt = deliver.deliver(self.bundle(to="ro-cli"), home=self.h.home,
                                      now=NOW)
            self.assertEqual(receipt.channel, "file-drop")
            with open(receipt.paths_written[0], encoding="utf-8") as fh:
                self.assertIn("read-only", fh.read())
        finally:
            del adapters.REGISTRY["ro-cli"]

    def test_every_path_ends_in_a_receipt(self):
        for to in ("claude-code", "codex-cli", "nope-cli"):
            receipt = deliver.deliver(self.bundle(to=to), home=self.h.home, now=NOW)
            self.assertTrue(receipt.channel)
            self.assertTrue(receipt.paths_written)


if __name__ == "__main__":
    unittest.main()


class TestWireFormat(unittest.TestCase):
    """Emitting all three formats at once means Claude Code reads both with
    no deduplication and injects twice — confirmed from a comment in an installed superpowers hook."""

    def test_claude_wire_is_nested_only(self):
        payload = json.loads(brief.hook_wire("x", "claude"))
        self.assertEqual(list(payload), ["hookSpecificOutput"])
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "x")

    def test_cursor_wire_is_snake_case_only(self):
        payload = json.loads(brief.hook_wire("x", "cursor"))
        self.assertEqual(list(payload), ["additional_context"])

    def test_sdk_wire_is_top_level_only(self):
        payload = json.loads(brief.hook_wire("x", "sdk"))
        self.assertEqual(list(payload), ["additionalContext"])

    def test_no_wire_format_emits_more_than_one_field(self):
        for wire in ("claude", "cursor", "sdk"):
            payload = json.loads(brief.hook_wire("x", wire))
            self.assertEqual(len(payload), 1, wire)

    def test_codex_cli_wire_is_the_nested_shape(self):
        """Measured (codex-cli 0.155.1): a top-level additionalContext is

        rejected and nothing is injected. Only the nested hookSpecificOutput
        shape actually shows up in the rollout
        (content_item_kinds=["hooks.additional_context"]). If the adapter
        regresses back to "sdk", this test must fail loudly, not break silently.
        """
        from omhc import adapters

        adapter = adapters.get("codex-cli")
        self.assertEqual(adapter.wire, "claude")
        payload = json.loads(brief.hook_wire("x", adapter.wire))
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "SessionStart"
        )
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "x")

    def test_every_adapter_declares_its_own_wire(self):
        """The wire format is a property of the adapter, not a lookup table in the core.

        If the core held the table, a new adapter would have to modify the
        core, and if it didn't, it would silently emit a field its own
        harness ignores — with no receipt left behind either.
        """
        from omhc import adapters

        for adapter_id in adapters.REGISTRY:
            wire = getattr(adapters.get(adapter_id), "wire", None)
            self.assertIn(wire, ("claude", "cursor", "sdk"), adapter_id)
            payload = json.loads(brief.hook_wire("x", wire))
            self.assertEqual(len(payload), 1, adapter_id)


class TestLogFailureNeverRaises(unittest.TestCase):
    def test_a_non_utf8_filename_in_the_detail_is_logged_not_raised(self):
        with tempfile.TemporaryDirectory() as home:
            brief.log_failure(home, "pin failed: source missing: /x/\udcff.jsonl")
            with open(os.path.join(locate.omhc_root(home), brief.GUARD_LOG),
                      encoding="utf-8") as fh:
                self.assertIn("\\udcff", fh.read())

    def test_timestamp_is_stamped_with_gmtime_not_the_bare_local_call(self):
        """Review: using bare `time.strftime(fmt)` (no struct) wrongly tacks a
        `Z` (UTC) suffix onto local time — `time.gmtime()` must be passed explicitly."""
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.object(brief.time, "gmtime", side_effect=time.gmtime) as spy:
            brief.log_failure(home, "x")
        spy.assert_called_once_with()


class TestOutboxHygieneIntegration(unittest.TestCase):
    """How `omhc mark` (hook path) and `omhc clear` clean up the outbox."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def bundle(self, to="claude-code"):
        return HandoffBundle(body_md="[omhc] hi\n", repo_root=self.h.repo_root,
                             to_adapter_id=to)

    def test_cmd_mark_prunes_outbox_files_older_than_24h(self):
        from omhc import cli

        old = deliver.file_drop(self.bundle(), "old", now=NOW).paths_written[0]
        old_time = NOW - deliver.OUTBOX_TTL_SECONDS - 10
        os.utime(old, (old_time, old_time))

        args = cli.build_parser().parse_args(
            ["mark", "--harness", "claude-code", "--stdin",
             json.dumps({"cwd": self.h.repo_root, "session_id": "me1"})])
        cli.cmd_mark(args, home=self.h.home, out=io.StringIO())

        self.assertFalse(os.path.exists(old))

    def test_cmd_clear_removes_every_omhc_outbox_file(self):
        from omhc import cli

        fresh = deliver.file_drop(self.bundle(), "fresh", now=NOW).paths_written[0]

        cwd = os.getcwd()
        os.chdir(self.h.repo_root)
        try:
            args = cli.build_parser().parse_args(["clear"])
            cli.cmd_clear(args, home=self.h.home, out=io.StringIO())
        finally:
            os.chdir(cwd)

        self.assertFalse(os.path.exists(fresh))
