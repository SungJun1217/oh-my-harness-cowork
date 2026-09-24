from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from omhc import brief, deliver, ledger, locate, pin

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

    def test_same_vendor_yields_empty(self):
        self.h.plant_codex_session()
        body = brief.compute(my_harness="codex-cli", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(body, "")


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
            brief._log_failure(home, "pin failed: source missing: /x/\udcff.jsonl")
            with open(os.path.join(locate.omhc_root(home), brief.GUARD_LOG),
                      encoding="utf-8") as fh:
                self.assertIn("\\udcff", fh.read())
