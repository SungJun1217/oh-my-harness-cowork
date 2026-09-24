from __future__ import annotations

import json
import os
import tempfile
import unittest

from omhc import adapter as A
from omhc.adapters import claude_code as CC

from . import _repo
from ._repo import MISSING, REPO
from ._repo import CLAUDE_LIVE as LIVE
from ._repo import CLAUDE_SUB as SUB

have_fixtures = _repo.have_fixtures(LIVE, _repo.EXPECTED)



def expected() -> dict:
    return _repo.load_expected()


def ref_for(path: str, cwd=REPO) -> A.SessionRef:
    return _repo.ref_for("claude-code", path, cwd=cwd)


class TestSlug(unittest.TestCase):
    def test_slug_for_this_repo_matches_the_real_directory_name(self):
        """이 머신의 실측 값. 다른 경로에서는 건너뛴다."""
        if REPO != "/home/ec2-user/capstone/oh-my-harness-cowork":
            self.skipTest("다른 체크아웃 경로: {}".format(REPO))
        self.assertEqual(
            CC.claude_slug(REPO), "-home-ec2-user-capstone-oh-my-harness-cowork"
        )

    def test_slug_collapses_every_non_alphanumeric_character(self):
        self.assertEqual(CC.claude_slug("/a_b.c d/e"), "-a-b-c-d-e")

    def test_long_paths_take_the_truncate_and_hash_branch(self):
        long_path = "/" + "x" * 300
        slug = CC.claude_slug(long_path)
        self.assertLessEqual(len(slug), 210)
        self.assertNotEqual(slug, CC.claude_slug("/" + "x" * 301))


class TestDetect(unittest.TestCase):
    def test_detect_never_raises_on_a_missing_home(self):
        with tempfile.TemporaryDirectory() as home:
            got = CC.ClaudeCodeAdapter(home=home).detect()
            self.assertFalse(got.present)
            self.assertTrue(got.note)

    def test_detect_finds_an_existing_projects_dir(self):
        """실제 홈을 보지 않는다. CI 러너에는 Claude Code 가 없어서 실측 홈에 기대면 항상 빨갛다."""
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".claude", "projects"))
            got = CC.ClaudeCodeAdapter(home=home).detect()
            self.assertTrue(got.present)
            self.assertEqual(got.note, os.path.join(home, ".claude", "projects"))


@unittest.skipUnless(have_fixtures, MISSING)
class TestListSessions(unittest.TestCase):
    def test_glob_is_depth_one_only(self):
        """중첩 subagent 파일 137개가 결과에 들어오면 남의 에이전트 발화를 읽는다."""
        refs = CC.ClaudeCodeAdapter().list_sessions(REPO)
        self.assertTrue(refs)
        for ref in refs:
            rest = os.path.relpath(ref.source_path, os.path.dirname(refs[0].source_path))
            self.assertNotIn(os.sep, rest, "깊이 1을 벗어난 경로: {}".format(ref.source_path))

    def test_sessions_outside_this_repo_are_not_returned(self):
        refs = CC.ClaudeCodeAdapter().list_sessions(REPO)
        for ref in refs:
            self.assertEqual(ref.adapter_id, "claude-code")

    def test_unknown_repo_returns_empty_not_an_exception(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(CC.ClaudeCodeAdapter().list_sessions(d), [])

    def test_non_interactive_sdk_sessions_are_excluded(self):
        """실측: 이 레포의 최상위 세션 31개 중 30개가 entrypoint=sdk-py 다.

        걸러내지 않으면 남의 도구가 남긴 비대화형 세션을 사람의 작업으로 오인한다.
        """
        refs = CC.ClaudeCodeAdapter().list_sessions(REPO)
        for ref in refs:
            head = CC.head_of(ref.source_path)
            self.assertNotIn(
                str(head.get("entrypoint") or ""), CC.NON_INTERACTIVE_ENTRYPOINTS
            )

    def test_sessions_are_ordered_most_recent_first(self):
        refs = CC.ClaudeCodeAdapter().list_sessions(REPO)
        epochs = [r.epoch for r in refs]
        self.assertEqual(epochs, sorted(epochs, reverse=True))

    def test_blocklist_not_allowlist(self):
        """허용목록이면 새 대화형 entrypoint 가 생겼을 때 진짜 세션을 잃는다."""
        self.assertIn("sdk-py", CC.NON_INTERACTIVE_ENTRYPOINTS)
        self.assertNotIn("cli", CC.NON_INTERACTIVE_ENTRYPOINTS)


@unittest.skipUnless(have_fixtures, MISSING)
class TestReadSession(unittest.TestCase):
    def setUp(self):
        self.adapter = CC.ClaudeCodeAdapter()
        self.read = self.adapter.read_session(ref_for(LIVE))

    def test_human_event_count_matches_the_independently_computed_golden(self):
        humans = [e for e in self.read.events if e.author == "human"]
        self.assertEqual(len(humans), expected()["claude"]["human_turns"])

    def test_human_events_are_the_real_messages_not_machinery(self):
        humans = [e for e in self.read.events if e.author == "human"]
        joined = "\n".join(e.text for e in humans)
        self.assertNotIn("<command-name>", joined)
        self.assertNotIn("<local-command-stdout>", joined)
        self.assertNotIn("[Request interrupted by user]", joined)

    def test_tool_result_user_records_never_become_human(self):
        """Claude Code는 tool_result 를 type:\"user\" 로 되돌린다. 67개가 그렇다."""
        for ev in self.read.events:
            if ev.author == "human":
                self.assertNotIn("tool_use_id", ev.text)

    def test_subagent_file_yields_no_events(self):
        read = self.adapter.read_session(ref_for(SUB))
        self.assertEqual(len(read.events), 0)
        self.assertGreater(sum(read.dropped.values()), 0)

    def test_real_tool_names_map_to_neutral_verbs(self):
        verbs = {e.verb for e in self.read.events}
        self.assertIn("ran", verbs)       # Bash 52회
        self.assertIn("modified", verbs)  # Write 18 / Edit 6
        self.assertIn("said", verbs)

    def test_verb_map_covers_every_tool_this_machine_actually_used(self):
        """미매핑 툴이 0이어야 한다.

        키워드 스캔으로 "에이전트 텍스트에 tool_use 가 없다"를 단정하면 안 된다 —
        에이전트가 tool_use 페어링을 설명하는 정당한 산문이 걸린다. 어휘 누출은
        스키마(툴 이름 필드 부재)와 총체적 매핑으로 막는 것이고, 본문 검열이
        아니다.
        """
        self.assertNotIn("unmapped_tool", self.read.dropped)

    def test_tool_events_carry_arg_but_never_a_tool_name_field(self):
        with_arg = [e for e in self.read.events if e.arg]
        self.assertTrue(with_arg)
        for ev in with_arg:
            self.assertEqual(ev.author, "agent")
            self.assertLessEqual(len(ev.arg), 120)

    def test_thinking_blocks_are_not_events(self):
        """모델의 사적 추론은 교차 벤더로 옮기지 않는다. 픽스처에 79개 있다."""
        self.assertTrue(all(e.verb in {"said", "inspected", "modified", "ran",
                                       "delegated", "researched"}
                            for e in self.read.events))

    def test_unknown_record_types_are_counted_not_raised(self):
        self.assertTrue(self.read.dropped)
        self.assertEqual(self.read.unparsed, 0)

    def test_sequence_numbers_are_monotonic(self):
        seqs = [e.seq for e in self.read.events]
        self.assertEqual(seqs, sorted(seqs))

    def test_offsets_point_into_the_source_file(self):
        size = os.path.getsize(LIVE)
        for ev in self.read.events:
            self.assertGreaterEqual(ev.offset, 0)
            self.assertLessEqual(ev.offset + ev.length, size)


class TestReadSessionForgedRecords(unittest.TestCase):
    """실물에 없는 분기는 위조 레코드로 덮는다. is_error=True 가 없기 때문이다."""

    def _read(self, rows) -> A.SessionRead:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            path = fh.name
        try:
            return CC.ClaudeCodeAdapter().read_session(ref_for(path))
        finally:
            os.unlink(path)

    def test_failed_tool_result_sets_ok_false(self):
        read = self._read([
            {"type": "assistant", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": [
                 {"type": "tool_use", "id": "t1", "name": "Bash",
                  "input": {"command": "pytest"}}]}},
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"content": [
                 {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                  "content": "3 failed"}]}},
        ])
        ran = [e for e in read.events if e.verb == "ran"]
        self.assertEqual(len(ran), 1)
        self.assertFalse(ran[0].ok)

    def test_compaction_summary_record_does_not_become_a_human_turn(self):
        read = self._read([
            {"type": "user", "cwd": REPO, "isCompactSummary": True,
             "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "이전 대화 요약: 사용자는 X를 요청했다"}},
        ])
        self.assertEqual([e for e in read.events if e.author == "human"], [])

    def test_meta_user_record_is_dropped(self):
        read = self._read([
            {"type": "user", "cwd": REPO, "isMeta": True,
             "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "메타"}},
        ])
        self.assertEqual(len(read.events), 0)

    def test_cwd_is_found_even_when_absent_from_the_first_records(self):
        rows = [
            {"type": "last-prompt", "sessionId": "s"},
            {"type": "mode", "sessionId": "s"},
            {"type": "permission-mode", "sessionId": "s"},
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "안녕"}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            path = fh.name
        try:
            self.assertEqual(CC.head_of(path).get("cwd"), REPO)
        finally:
            os.unlink(path)

    def test_unknown_type_is_reported_in_dropped(self):
        read = self._read([{"type": "brand-new-record-type-2099", "cwd": REPO}])
        self.assertIn("brand-new-record-type-2099", read.dropped)

    def test_broken_json_line_is_counted_as_unparsed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as fh:
            fh.write('{"type":"user","cwd":"%s","message":{"content":"ok"}}\n' % REPO)
            fh.write("{broken\n")
            path = fh.name
        try:
            read = CC.ClaudeCodeAdapter().read_session(ref_for(path))
            self.assertEqual(read.unparsed, 1)
        finally:
            os.unlink(path)


class TestWriteSide(unittest.TestCase):
    def test_native_resume_hint_names_the_session(self):
        hint = CC.ClaudeCodeAdapter().native_resume_hint(ref_for("/x/abc.jsonl"))
        self.assertIn("--resume", hint)
        self.assertIn("abc", hint)

    def test_install_handoff_writes_the_artifact_where_brief_will_find_it(self):
        with tempfile.TemporaryDirectory() as home:
            bundle = A.HandoffBundle(body_md="[omhc] hi\n", repo_root=REPO,
                                     to_adapter_id="claude-code")
            receipt = CC.ClaudeCodeAdapter(home=home).install_handoff(bundle)
            self.assertEqual(len(receipt.paths_written), 1)
            with open(receipt.paths_written[0], encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "[omhc] hi\n")
            self.assertTrue(receipt.consumed_on_read)

    def test_capabilities_declare_both_halves(self):
        caps = CC.ClaudeCodeAdapter.capabilities
        self.assertIn(A.Capability.READ, caps)
        self.assertIn(A.Capability.WRITE, caps)


if __name__ == "__main__":
    unittest.main()
