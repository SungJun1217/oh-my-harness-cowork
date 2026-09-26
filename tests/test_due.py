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
        """The caller asks the adapter for the judgment (#21). If due held
        entrypoint vocabulary, the same rule would live in two modules, and one
        could get updated while the other doesn't — a half-updated filter.
        Stopping at an ineligible row would let one headless session block the
        real session before it."""
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
        """Stops outright at an already-delivered or too-stale row — judging is
        expensive since it opens a file, and the row before it is even older."""
        asked = []
        self.start("codex-cli", "cx1", 10.0)
        self.assertIsNone(due.due(
            REPO_KEY, "claude-code", "s2", 10.0 + due.MAX_AGE_SECONDS + 1,
            home=self.home, eligible=lambda mark: asked.append(mark) or True))
        self.assertEqual(asked, [])

    def test_a_legacy_interactive_false_row_is_rechecked(self):
        """The `interactive:false` an old mark wrote is no longer consulted. That
        row was frozen at mark time and couldn't be revived even by turning
        OMHC_ALLOW_HEADLESS on later."""
        self.start("codex-cli", "cx1", 10.0, interactive=False)
        got = due.due(REPO_KEY, "claude-code", "s2", 100.0, home=self.home,
                      eligible=lambda mark: True)
        self.assertEqual(got.session_id, "cx1")

    def test_a_row_without_a_verdict_is_treated_as_interactive(self):
        """With no record, treat it as a human session — better than silently losing it."""
        self.start("claude-code", "old-row", 50.0)
        got = due.due(REPO_KEY, "codex-cli", "cx9", 100.0, home=self.home)
        self.assertEqual(got.session_id, "old-row")

    def test_due_holds_no_harness_vocabulary(self):
        """No per-harness **vocabulary value** may live in the core.

        A comment mentioning that vocabulary is legitimate — flagging a sentence
        that explains why it's not here would be the same false positive guard
        ran into. So only values are checked.
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
        """v2 changes the return type to List[Watermark]. This one function is that seam."""
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
        """Observed: the SessionStart hook fired 6 times within one session.

        Without a gate, the same handoff would be injected into one context 6 times.
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
        self.assertEqual(data.count(b"1"), 1, "exactly one process must win the claim")

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


class TestReopen(unittest.TestCase):
    """The reopen line resume leaves in delivered.tsv (#22). `codex exec resume`
    appends to the same rollout, and if that session was already delivered,
    already_delivered() would stop due(), so the resumed turn would never go out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.state = os.path.join(self.home, ".omhc", REPO_KEY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reopen_after_delivery_undoes_already_delivered(self):
        wm = due.Watermark(repo_key=REPO_KEY, harness="codex-cli", session_id="cx1",
                           path="/p", event="start", epoch=10.0)
        due.mark_delivered(self.state, wm, to_harness="claude-code", epoch=20.0)
        self.assertTrue(due.already_delivered(self.state, "cx1", "claude-code"))
        due.mark_reopened(self.state, "cx1", "codex-cli", 30.0)
        self.assertFalse(due.already_delivered(self.state, "cx1", "claude-code"))

    def test_a_fresh_delivery_after_reopen_wins_again(self):
        wm = due.Watermark(repo_key=REPO_KEY, harness="codex-cli", session_id="cx1",
                           path="/p", event="start", epoch=10.0)
        due.mark_delivered(self.state, wm, to_harness="claude-code", epoch=20.0)
        due.mark_reopened(self.state, "cx1", "codex-cli", 30.0)
        due.mark_delivered(self.state, wm, to_harness="claude-code", epoch=40.0)
        self.assertTrue(due.already_delivered(self.state, "cx1", "claude-code"))

    def test_reopen_without_a_prior_delivery_is_a_no_op(self):
        due.mark_reopened(self.state, "cx1", "codex-cli", 30.0)
        self.assertFalse(due.already_delivered(self.state, "cx1", "claude-code"))

    def test_reopen_does_not_gate_a_different_target_harness(self):
        wm = due.Watermark(repo_key=REPO_KEY, harness="codex-cli", session_id="cx1",
                           path="/p", event="start", epoch=10.0)
        due.mark_delivered(self.state, wm, to_harness="claude-code", epoch=20.0)
        due.mark_reopened(self.state, "cx1", "codex-cli", 30.0)
        # gajae-code was never delivered to in the first place — False regardless of reopen.
        self.assertFalse(due.already_delivered(self.state, "cx1", "gajae-code"))

    def test_last_delivered_skips_a_trailing_reopen(self):
        wm = due.Watermark(repo_key=REPO_KEY, harness="codex-cli", session_id="cx1",
                           path="/p", event="start", epoch=10.0)
        due.mark_delivered(self.state, wm, to_harness="claude-code", epoch=20.0)
        due.mark_reopened(self.state, "cx1", "codex-cli", 30.0)
        self.assertEqual(due.last_delivered(self.state), "cx1")

    def test_last_delivered_ignores_a_reopen_with_no_delivery_at_all(self):
        due.mark_reopened(self.state, "cx1", "codex-cli", 30.0)
        self.assertIsNone(due.last_delivered(self.state))

    def test_delivered_order_ignores_reopen_lines(self):
        wm1 = due.Watermark(repo_key=REPO_KEY, harness="codex-cli", session_id="cx1",
                            path="/p", event="start", epoch=10.0)
        wm2 = due.Watermark(repo_key=REPO_KEY, harness="codex-cli", session_id="cx2",
                            path="/p", event="start", epoch=15.0)
        due.mark_delivered(self.state, wm1, to_harness="claude-code", epoch=20.0)
        due.mark_delivered(self.state, wm2, to_harness="claude-code", epoch=25.0)
        due.mark_reopened(self.state, "cx1", "codex-cli", 30.0)
        self.assertEqual(due.delivered_order(self.state), ["cx1", "cx2"])


class TestLastDeliveryOffset(unittest.TestCase):
    """#27: preventing redelivery after a reopen needs to know "how much was read"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.state = os.path.join(self.home, ".omhc", REPO_KEY)

    def tearDown(self):
        self.tmp.cleanup()

    def wm(self, session_id):
        return due.Watermark(repo_key=REPO_KEY, harness="codex-cli", session_id=session_id,
                             path="/p", event="start", epoch=10.0)

    def test_no_prior_delivery_yields_none(self):
        self.assertIsNone(due.last_delivery_offset(self.state, "cx1", "claude-code"))

    def test_offset_is_read_back(self):
        due.mark_delivered(self.state, self.wm("cx1"), to_harness="claude-code",
                           epoch=20.0, offset=4096)
        self.assertEqual(due.last_delivery_offset(self.state, "cx1", "claude-code"), 4096)

    def test_legacy_four_column_line_yields_none(self):
        due.mark_delivered(self.state, self.wm("cx1"), to_harness="claude-code", epoch=20.0)
        self.assertIsNone(due.last_delivery_offset(self.state, "cx1", "claude-code"))

    def test_a_later_delivery_wins(self):
        due.mark_delivered(self.state, self.wm("cx1"), to_harness="claude-code",
                           epoch=20.0, offset=100)
        due.mark_reopened(self.state, "cx1", "codex-cli", 30.0)
        due.mark_delivered(self.state, self.wm("cx1"), to_harness="claude-code",
                           epoch=40.0, offset=500)
        self.assertEqual(due.last_delivery_offset(self.state, "cx1", "claude-code"), 500)

    def test_offset_is_scoped_to_the_target_harness(self):
        due.mark_delivered(self.state, self.wm("cx1"), to_harness="claude-code",
                           epoch=20.0, offset=100)
        self.assertIsNone(due.last_delivery_offset(self.state, "cx1", "gajae-code"))


if __name__ == "__main__":
    unittest.main()
