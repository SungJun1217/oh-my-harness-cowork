from __future__ import annotations

import json
import os
import tempfile
import unittest

from omhc import due, gate, ledger

REPO_KEY = "oh-my-harness-cowork-25358bbb"


class TestDue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.state = os.path.join(self.home, ".omhc", REPO_KEY)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, harness, session, epoch, **extra):
        row = {"repo": REPO_KEY, "harness": harness, "session": session,
               "event": "start", "epoch": epoch, "path": "/p/" + session,
               "cwd": "/repo"}
        row.update(extra)
        ledger.append(row, home=self.home)

    def test_missing_ledger_yields_none(self):
        self.assertIsNone(due.due(REPO_KEY, "claude-code", "s1", 100.0, home=self.home))

    def test_only_my_own_harness_yields_none(self):
        self.start("claude-code", "s1", 10.0)
        self.assertIsNone(due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home))

    def test_foreign_harness_yields_a_watermark(self):
        self.start("codex-cli", "cx1", 10.0)
        got = due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.harness, "codex-cli")
        self.assertEqual(got.session_id, "cx1")
        self.assertEqual(got.path, "/p/cx1")

    def test_my_own_session_is_never_the_source(self):
        self.start("codex-cli", "same", 10.0)
        self.assertIsNone(due.due(REPO_KEY, "codex-cli", "same", 100.0, home=self.home))

    def test_other_repo_is_ignored(self):
        ledger.append({"repo": "other-repo", "harness": "codex-cli", "session": "x",
                       "event": "start", "epoch": 10.0}, home=self.home)
        self.assertIsNone(due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home))

    def test_already_delivered_yields_none(self):
        self.start("codex-cli", "cx1", 10.0)
        got = due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home)
        due.mark_delivered(self.state, got, to_harness="claude-code", epoch=100.0)
        self.assertIsNone(due.due(REPO_KEY, "claude-code", "s2", 101.0, home=self.home))

    def test_delivered_to_one_harness_is_still_due_for_another(self):
        self.start("codex-cli", "cx1", 10.0)
        got = due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home)
        due.mark_delivered(self.state, got, to_harness="claude-code", epoch=100.0)
        again = due.due(REPO_KEY, "gajae-code", "g1", 101.0, home=self.home)
        self.assertIsNotNone(again)

    def test_an_ineligible_row_is_skipped_for_the_one_before_it(self):
        """판정은 호출자가 어댑터에게 묻는다(#21). due 가 entrypoint 어휘를 들고
        있으면 같은 규칙이 두 모듈에 살면서 한쪽만 갱신되는 반쪽 필터가 된다.
        부적격 행에서 멈추면 헤드리스 세션 하나가 그 앞의 진짜 세션을 막는다."""
        self.start("codex-cli", "cx1", 10.0)
        self.start("claude-code", "sdk1", 50.0)
        got = due.due(REPO_KEY, "gajae-code", "g1", 100.0, home=self.home,
                      eligible=lambda mark: mark.session_id != "sdk1")
        self.assertEqual(got.session_id, "cx1")

    def test_nothing_eligible_yields_none(self):
        self.start("codex-cli", "cx1", 10.0)
        self.assertIsNone(due.due(REPO_KEY, "claude-code", "s2", 100.0,
                                  home=self.home, eligible=lambda mark: False))

    def test_eligibility_is_not_asked_for_delivered_or_stale_rows(self):
        """이미 전달했거나 너무 오래된 행에서는 그대로 멈춘다 — 판정은 파일을
        여는 일이라 비싸고, 그 앞 행은 더 낡았다."""
        asked = []
        self.start("codex-cli", "cx1", 10.0)
        self.assertIsNone(due.due(
            REPO_KEY, "claude-code", "s2", 10.0 + due.MAX_AGE_SECONDS + 1,
            home=self.home, eligible=lambda mark: asked.append(mark) or True))
        self.assertEqual(asked, [])

    def test_a_legacy_interactive_false_row_is_rechecked(self):
        """예전 mark 가 적은 `interactive:false` 는 더 이상 보지 않는다. 그 행은
        mark 시점에 굳어 OMHC_ALLOW_HEADLESS 를 나중에 켜도 되살릴 수 없었다."""
        self.start("codex-cli", "cx1", 10.0, interactive=False)
        got = due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home,
                      eligible=lambda mark: True)
        self.assertEqual(got.session_id, "cx1")

    def test_a_row_without_a_verdict_is_treated_as_interactive(self):
        """기록이 없으면 사람의 세션으로 본다 — 조용히 잃는 것보다 낫다."""
        self.start("claude-code", "old-row", 50.0)
        got = due.due(REPO_KEY, "codex-cli", "cx9", 100.0, home=self.home)
        self.assertEqual(got.session_id, "old-row")

    def test_due_holds_no_harness_vocabulary(self):
        """하네스별 **어휘 값**이 코어에 있으면 안 된다.

        주석이 그 어휘를 언급하는 것은 정당하다 — 왜 여기 없는지를 설명하는
        문장이 걸리면 가드에서 겪은 것과 같은 거짓 양성이다. 그래서 값만 본다.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(due))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for vocabulary in ("sdk-py", "sdk-cli", "sdk", "isSidechain"):
            self.assertNotIn(vocabulary, literals, vocabulary)

    def test_newest_foreign_session_wins(self):
        self.start("codex-cli", "old", 10.0)
        self.start("codex-cli", "new", 20.0)
        got = due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home)
        self.assertEqual(got.session_id, "new")

    def test_off_switch_env_disables_everything(self):
        self.start("codex-cli", "cx1", 10.0)
        os.environ["OMHC_OFF"] = "1"
        try:
            self.assertIsNone(
                due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home)
            )
        finally:
            del os.environ["OMHC_OFF"]

    def test_off_marker_file_disables_everything(self):
        self.start("codex-cli", "cx1", 10.0)
        os.makedirs(self.state, exist_ok=True)
        open(os.path.join(self.state, "off"), "w").close()
        self.assertIsNone(due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home))

    def test_watermark_is_a_namedtuple_with_the_seam_fields(self):
        self.start("codex-cli", "cx1", 10.0)
        got = due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home)
        self.assertEqual(
            got._fields,
            ("repo_key", "harness", "session_id", "path", "event", "epoch"),
        )

    def test_due_returns_a_single_watermark_in_v1(self):
        """v2 는 반환형을 List[Watermark] 로 바꾼다. 그 seam 이 이 함수 하나다."""
        self.start("codex-cli", "a", 10.0)
        self.start("gajae-code", "b", 20.0)
        got = due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home)
        self.assertFalse(isinstance(got, list))


class TestGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_succeeds_exactly_once(self):
        """실측: SessionStart 훅이 한 세션 안에서 6회 발동했다.

        게이트가 없으면 같은 핸드오프가 한 컨텍스트에 6번 들어간다.
        """
        self.assertTrue(gate.claim(self.state, "claude-code", "sess-A"))
        for _ in range(5):
            self.assertFalse(gate.claim(self.state, "claude-code", "sess-A"))

    def test_different_sessions_each_get_one_claim(self):
        self.assertTrue(gate.claim(self.state, "claude-code", "sess-A"))
        self.assertTrue(gate.claim(self.state, "claude-code", "sess-B"))

    def test_different_harnesses_each_get_one_claim(self):
        self.assertTrue(gate.claim(self.state, "claude-code", "sess-A"))
        self.assertTrue(gate.claim(self.state, "codex-cli", "sess-A"))

    def test_claim_is_process_safe(self):
        results = os.pipe()
        pids = []
        for _ in range(8):
            pid = os.fork()
            if pid == 0:
                got = gate.claim(self.state, "claude-code", "race")
                os.write(results[1], b"1" if got else b"0")
                os._exit(0)
            pids.append(pid)
        for pid in pids:
            os.waitpid(pid, 0)
        os.close(results[1])
        data = os.read(results[0], 64)
        os.close(results[0])
        self.assertEqual(data.count(b"1"), 1, "정확히 한 프로세스만 선점해야 한다")

    def test_missing_session_id_is_not_claimable(self):
        self.assertFalse(gate.claim(self.state, "claude-code", ""))

    def test_unwritable_state_dir_returns_false_not_raises(self):
        self.assertFalse(gate.claim("/proc/nonexistent-omhc", "claude-code", "s"))


class TestSessionIdFromHookPayload(unittest.TestCase):
    def test_reads_session_id_key(self):
        raw = json.dumps({"session_id": "abc", "cwd": "/repo"})
        self.assertEqual(gate.session_id_from_hook_payload(raw), "abc")

    def test_reads_camel_case_variant(self):
        raw = json.dumps({"sessionId": "abc"})
        self.assertEqual(gate.session_id_from_hook_payload(raw), "abc")

    def test_derives_from_transcript_path_when_absent(self):
        raw = json.dumps({"transcript_path": "/h/.claude/projects/slug/uuid-1.jsonl"})
        self.assertEqual(gate.session_id_from_hook_payload(raw), "uuid-1")

    def test_broken_json_returns_none(self):
        self.assertIsNone(gate.session_id_from_hook_payload("{broken"))

    def test_empty_input_returns_none(self):
        self.assertIsNone(gate.session_id_from_hook_payload(""))


if __name__ == "__main__":
    unittest.main()
