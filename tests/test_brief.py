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
    """임시 홈 + 임시 레포. 세션 심기는 tests/_repo.plant_codex 가 소유한다."""

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
        """부모 에이전트의 프롬프트가 사람의 말로 둔갑해 GOAL/NEXT 가 되면 안 된다
        (invariant 3). ref_for_path 와 list_sessions 폴백 둘 다 걸러야 due 가
        빈 몸으로 돌아온다."""
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
        """originator=applecider 는 source=vscode 라 서브에이전트 표식이 없다.

        걸러지지 않으면 앱서버가 채운 기계 템플릿이 사람의 GOAL 로 둔갑한다.
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
        """SessionStart 훅은 한 세션에서 여러 번 발동한다."""
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
        self.assertTrue(os.path.exists(pinned), "하드링크가 없다")
        self.assertEqual(os.stat(path).st_ino, os.stat(pinned).st_ino)
        self.assertTrue(os.path.exists(idx), "색인이 없다")

    def test_pin_failure_is_logged_but_the_hook_still_succeeds(self):
        """리뷰 결함: pin 실패를 조용히 넘기면 `omhc status` 의 archive 행이
        아무 흔적 없이 거짓 PASS 를 낸다. 훅 경로(invariant 2)이므로 로그만
        남기고 exit 0/핸드오프 본문은 그대로여야 한다."""
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
        """#36: 7일이 지난 메모는 핸드오프에 붙지 않는다. 시각 없는 옛 줄은 남는다."""
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
        """쓰는 쪽과 읽는 쪽이 같은 형식을 쓰는지 고정한다(리뷰)."""
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
    """사람이 대화한 세션인지는 brief 시점에 어댑터가 판정한다(#21)."""

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
        """사라진 파일은 헤드리스가 아니다. 건너뛰면 사용자가 이어서 작업한 세션을
        두고 그 전날 세션이 방금 일처럼 나간다 — 낡은 표식은 없는 표식보다 나쁘다."""
        self.h.t.plant_codex(session_id="cx1", human="월요일에 하던 옛 작업",
                             ledger_home=self.h.home, when=NOW)
        cx2 = self.h.t.plant_codex(session_id="cx2", human="화요일에 이어서 한 작업",
                                   ledger_home=self.h.home, when=NOW)
        os.remove(cx2)
        self.assertEqual(self.compute(), "")

    def test_a_newer_session_in_an_unknown_shape_stops_the_search(self):
        """빈 파일이나 포맷이 바뀐 rollout 은 헤드리스라는 증거가 아니다. 건너뛰면
        포맷이 바뀐 날부터 새 세션이 전부 건너뛰어지고 낡은 세션이 나간다
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
        """rollout 이 mark 시점에 아직 없어도 원장에 비대화형으로 굳지 않는다."""
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
    """스펙 §16-5: 적대적 입력 5종에서 빈 stdout + exit 0."""

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
        """비어 있지 않아야 의미가 있다 — repo_key("/") 로 실제 세션을 심어,
        거부가 없었다면 compute() 가 진짜 핸드오프를 만들었을 상황을 재현한다.
        원장에 아무것도 없어 무조건 빈 손인 상태에서는 이 테스트가 거부
        분기를 지우고도 통과한다(리뷰 결함)."""
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
    """--dry-run 은 본문만 보이고 아무것도 쓰지 않는다(#17). 한 번의 수동 확인이
    그 세션의 전달을 소비하면 다음 실제 SessionStart 에 아무것도 가지 않는다."""

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
        """수동 호출에는 훅 payload 가 없다. 게이트를 쓰지 않으므로 세션 id 도
        필요 없다."""
        self.h.plant_codex_session()
        out = io.StringIO()
        brief.emit(harness="claude-code", dry_run=True,
                   stdin_text=json.dumps({"cwd": self.h.repo_root}),
                   home=self.h.home, now=NOW, out=out)
        self.assertTrue(out.getvalue().startswith("[omhc]"))

    def test_dry_run_with_an_open_non_tty_stdin_does_not_hang(self):
        """`omhc brief --dry-run` 이 stdin 이 TTY 가 아닌 채 열려 있으면(파이프의
        다른 쪽 끝이 열려만 있고 아무것도 안 씀) 멈추면 안 된다(#27 부수 발견).
        --stdin 을 명시하지 않은 dry-run 은 stdin 을 아예 읽지 않아야 한다."""
        # 실제 프로세스라 brief 가 진짜 time.time() 을 쓴다 — NOW 상수는 고정된
        # 과거 시각이라 too-old 필터에 걸린다. 여기서만 실제 현재 시각을 심는다.
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
        """리뷰(#27 라운드 1): 문서화된 쓰임 하나가 다른 cwd 에서 payload 를
        파이프로 넘기는 것이다(`echo '{"cwd": R}' | omhc brief --dry-run`).
        stdin 을 아예 안 읽으면 이 쓰임이 깨진다 — 여기서는 실제로 읽어야 한다."""
        self.h.t.plant_codex(session_id="cx1", human="필드 경로부터 다시 확인해줘",
                             ledger_home=self.h.home, when=time.time())
        from tests._repo import REPO
        r_fd, w_fd = os.pipe()
        os.write(w_fd, json.dumps({"cwd": self.h.repo_root}).encode("utf-8"))
        os.close(w_fd)  # echo 처럼 보내고 바로 닫는다 — EOF.
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
        """쓰개가 한 줄 보내고 파이프를 계속 열어 두는 경우(#27 리뷰) — EOF 는
        안 오지만 다음 데이터도 안 오므로 타임아웃 안에 멈춰야 한다."""
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
    """#27: reopen 은 힌트일 뿐이다 — 새 사람 턴이 실제로 있어야 다시 보낸다."""

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
        """`codex exec resume <id> ""` — rollout 에 `"text": ""` 인 user 메시지만
        붙는다(실측). 새 사람 턴이 아니므로 다시 보내면 안 된다."""
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
        """mark 와 brief 의 동시 실행 경합(실측): brief 가 전달한 바로 그 초에
        mark 의 성장 판정이 이미 전달된 턴을 보고 reopen 을 그 뒤에 붙인다."""
        self._deliver_once()
        due.mark_reopened(self.h.state, "cx1", "codex-cli", NOW + 1)
        again = brief.compute(my_harness="claude-code", my_session_id="me2",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW + 2)
        self.assertEqual(again, "")

    def test_legacy_four_column_delivery_is_still_redelivered(self):
        """옛 4열 delivered 줄(offset 없음) 뒤 reopen 은 오늘까지의 동작 그대로
        — offset 을 모르면 조건 없이 다시 보낸다."""
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
        """F5 는 mint 가 한 곳에서 처리한다 — 같은 규칙을 두 모듈에 두면
        한쪽만 바뀐다. deliver 에 중복 선언이 있었고 테스트만 그것을 썼다."""
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
        """#36: 본문의 상대 나이("3m ago")는 mint 시점에 얼어붙는다 — 헤더에
        절대시각이 있어야 나중에 읽는 사람이 그게 낡았는지 가늠할 수 있다."""
        receipt = deliver.file_drop(self.bundle(), "because", now=NOW)
        with open(receipt.paths_written[0], encoding="utf-8") as fh:
            header = fh.readline()
        self.assertIn('captured="{:.0f}"'.format(NOW), header)
        self.assertIn("captured_utc=\"2025-09-22T", header)

    def test_file_drop_neutralizes_a_comment_terminator_in_why(self):
        """리뷰 #3: `why` 에 `-->` 가 섞이면 주석이 거기서 끝나고 그 뒤의
        captured=/captured_utc= 가 본문으로 새 버린다."""
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
        """리뷰 #1: 예전엔 `.omhc/` 를 새로 만들 때만 등재를 시도해서, 이미
        outbox 가 있던 기존 사용자는 영영 등재되지 않았다."""
        os.makedirs(os.path.join(self.h.repo_root, deliver.OUTBOX_DIR), exist_ok=True)
        deliver.file_drop(self.bundle(), "because", now=NOW)
        exclude_path = os.path.join(self.h.repo_root, ".git", "info", "exclude")
        with open(exclude_path, encoding="utf-8") as fh:
            self.assertIn(".omhc/", fh.read())

    def test_second_drop_does_not_spawn_a_subprocess_once_excluded(self):
        """리뷰 #1: 이미 등재됐으면 `.git/info/exclude` 파일 한 번 읽는 것만으로
        끝나야 한다 — git check-ignore subprocess 를 또 부르면 안 된다."""
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
    """세 형식을 동시에 내보내면 Claude Code 가 중복 제거 없이 둘 다 읽어 두 번
    주입된다 — 설치된 superpowers 훅의 주석에서 확인한 사실이다."""

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
        """실측(codex-cli 0.155.1): 최상위 additionalContext 는 거부되고 아무것도

        주입되지 않는다. hookSpecificOutput 중첩 형식만 rollout 에 실제로 나타난다
        (content_item_kinds=["hooks.additional_context"]). 어댑터가 다시 "sdk" 로
        회귀하면 이 테스트가 조용히 깨지지 않고 실패해야 한다.
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
        """와이어 형식은 코어의 조회표가 아니라 어댑터의 속성이다.

        코어가 표를 들고 있으면 새 어댑터가 코어를 고쳐야 하고, 고치지 않으면
        자기 하네스가 무시하는 필드를 조용히 내보낸다 — receipt 도 남지 않는다.
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
        """리뷰: `time.strftime(fmt)` 하나만 쓰면(구조체 없이) 로컬 시각에
        `Z`(UTC) 접미가 잘못 붙는다 — `time.gmtime()` 을 명시로 넘겨야 한다."""
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.object(brief.time, "gmtime", side_effect=time.gmtime) as spy:
            brief.log_failure(home, "x")
        spy.assert_called_once_with()


class TestOutboxHygieneIntegration(unittest.TestCase):
    """`omhc mark`(훅 경로) 와 `omhc clear` 가 outbox 를 어떻게 청소하는지."""

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
