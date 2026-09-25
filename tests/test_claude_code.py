from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock

from omhc import adapter as A
from omhc import brief, due, guard, ledger, locate
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


class TestHeadlessOverride(unittest.TestCase):
    """OMHC_ALLOW_HEADLESS 는 헤드리스(sdk-cli 등)를 되살리지만 사이드체인은
    절대 되살리지 않는다 — 발화자가 다른 문제라서 오버라이드로 풀 대상이 아니다."""

    def setUp(self):
        self._backup = os.environ.pop("OMHC_ALLOW_HEADLESS", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._backup is not None:
            os.environ["OMHC_ALLOW_HEADLESS"] = self._backup
        else:
            os.environ.pop("OMHC_ALLOW_HEADLESS", None)

    def _plant(self, home: str, cwd: str, *, entrypoint="sdk-cli", sidechain=False):
        root = os.path.realpath(cwd)
        directory = os.path.join(home, ".claude", "projects", CC.claude_slug(root))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "s1.jsonl")
        row = {"type": "user", "cwd": root, "entrypoint": entrypoint,
              "sessionId": "s1", "message": {"content": "hi"}}
        if sidechain:
            row["isSidechain"] = True
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return path

    def test_without_override_a_headless_session_is_excluded(self):
        with tempfile.TemporaryDirectory() as home:
            self._plant(home, REPO)
            self.assertEqual(CC.ClaudeCodeAdapter(home=home).list_sessions(REPO), [])

    def test_override_admits_a_headless_sdk_cli_session(self):
        with tempfile.TemporaryDirectory() as home:
            self._plant(home, REPO)
            os.environ["OMHC_ALLOW_HEADLESS"] = "1"
            refs = CC.ClaudeCodeAdapter(home=home).list_sessions(REPO)
            self.assertEqual(len(refs), 1)
            self.assertTrue(CC.ClaudeCodeAdapter(home=home).classify(refs[0].source_path))

    def test_override_never_admits_a_sidechain(self):
        with tempfile.TemporaryDirectory() as home:
            self._plant(home, REPO, entrypoint="cli", sidechain=True)
            os.environ["OMHC_ALLOW_HEADLESS"] = "1"
            self.assertEqual(CC.ClaudeCodeAdapter(home=home).list_sessions(REPO), [])


def _write_jsonl(path: str, rows) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _copied(row: dict, *, fork_id="fork1", parent_id="parent1", uuid="u1") -> dict:
    """/branch, --fork-session 등이 복사한 레코드의 모양(#34, 분석 근거).
    uuid/parentUuid/timestamp/type/message 는 원본 그대로고 나머지를 덮어쓴다."""
    row = dict(row)
    row["sessionId"] = fork_id
    row["isSidechain"] = False
    row["sessionKind"] = None
    row["forkedFrom"] = {"sessionId": parent_id, "messageUuid": uuid}
    return row


_TS = "2026-09-25T00:00:00.000Z"


class TestForkClassify(unittest.TestCase):
    """#34: 포크는 부모의 사슬을 복사해 새 session_id 로 시작한다. 포크 자신의
    사람 턴이 없으면 이미 전달된 부모 턴을 또 전달하게 되므로 적격이 아니다."""

    def test_fork_with_no_own_turn_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                {"type": "history-suppression", "cause": "fork_inherit"},
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
                _copied({"type": "assistant", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": [{"type": "text", "text": "부모의 답"}]}},
                       uuid="u2"),
            ])
            self.assertFalse(CC.ClaudeCodeAdapter().classify(path))

    def test_fork_with_its_own_human_turn_is_eligible(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                {"type": "history-suppression", "cause": "fork_inherit"},
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
                {"type": "user", "cwd": REPO, "timestamp": _TS, "sessionId": "fork1",
                 "message": {"content": "포크 자신의 새 지시"}},
            ])
            self.assertTrue(CC.ClaudeCodeAdapter().classify(path))

    def test_fork_with_only_new_assistant_records_is_not_eligible(self):
        """새 구간이 있어도 사람의 말이 아니면 여전히 적격이 아니다."""
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
                {"type": "assistant", "cwd": REPO, "timestamp": _TS, "sessionId": "fork1",
                 "message": {"content": [{"type": "text", "text": "포크가 혼자 계속함"}]}},
            ])
            self.assertFalse(CC.ClaudeCodeAdapter().classify(path))

    def test_non_fork_session_is_unaffected(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                {"type": "user", "cwd": REPO, "timestamp": _TS,
                 "message": {"content": "평범한 세션의 첫 말"}},
            ])
            self.assertTrue(CC.ClaudeCodeAdapter().classify(path))

    def test_bound_hit_fails_open_to_eligible(self):
        """own tail(복사 구간을 벗어난 뒤)이 상한을 넘도록 크면(사람 턴을 못
        찾으면) 예전 동작으로 연다 — 판정 포기가 세션을 영영 못 여는 것보다
        싸다. 복사 구간 자체의 바이트는 상한에 넣지 않으므로(리뷰 지적) own
        tail 을 상한보다 크게 채워야 한다."""
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            rows = [_copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                            "message": {"content": "부모의 목표"}}, uuid="u1")]
            rows += [{"type": "assistant", "cwd": REPO, "timestamp": _TS,
                      "sessionId": "fork1",
                      "message": {"content": [{"type": "text", "text": "x" * 200}]}}
                     for _ in range(50)]
            _write_jsonl(path, rows)
            old_limit = CC._FORK_SCAN_BYTE_LIMIT
            CC._FORK_SCAN_BYTE_LIMIT = 256
            try:
                self.assertTrue(CC.ClaudeCodeAdapter().classify(path))
            finally:
                CC._FORK_SCAN_BYTE_LIMIT = old_limit

    def test_time_bound_hit_fails_open_to_eligible(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                {"type": "history-suppression", "cause": "fork_inherit"},
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
            ])
            old_limit = CC._FORK_SCAN_TIME_LIMIT
            CC._FORK_SCAN_TIME_LIMIT = -1
            try:
                self.assertTrue(CC.ClaudeCodeAdapter().classify(path))
            finally:
                CC._FORK_SCAN_TIME_LIMIT = old_limit

    def test_list_sessions_excludes_a_fork_with_no_own_turn(self):
        with tempfile.TemporaryDirectory() as home:
            directory = os.path.join(home, ".claude", "projects", CC.claude_slug(REPO))
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "fork1.jsonl")
            _write_jsonl(path, [
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
            ])
            self.assertEqual(CC.ClaudeCodeAdapter(home=home).list_sessions(REPO), [])

    def test_message_as_a_string_in_the_own_tail_fails_open_not_raises(self):
        """리뷰 재현: own tail 의 user 레코드가 message 를 문자열로 갖고 있으면
        `message.get("content")` 가 AttributeError 를 낸다 — classify() 밖으로
        새면 watch 가 그 레포의 Claude ref 를 전부 잃는다."""
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
                {"type": "user", "cwd": REPO, "timestamp": _TS, "sessionId": "fork1",
                 "message": "그냥 문자열"},
            ])
            self.assertTrue(CC.ClaudeCodeAdapter().classify(path))

    def test_non_string_text_block_in_the_own_tail_fails_open_not_raises(self):
        """리뷰 재현: text 블록의 text 가 문자열이 아니면(예: 5) `_text_of` 의
        "".join 이 TypeError 를 낸다."""
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
                {"type": "user", "cwd": REPO, "timestamp": _TS, "sessionId": "fork1",
                 "message": {"content": [{"type": "text", "text": 5}]}},
            ])
            self.assertTrue(CC.ClaudeCodeAdapter().classify(path))

    def test_guard_dropped_own_turn_does_not_make_a_fork_eligible(self):
        """envelope 전용·tool_result 전용·합성 인터럽트 문자열은 read_session
        도 사람의 말로 세지 않는다 — 포크의 own tail 에 이런 레코드만 있으면
        여전히 적격이 아니어야 한다."""
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
                {"type": "user", "cwd": REPO, "timestamp": _TS, "sessionId": "fork1",
                 "message": {"content": "<local-command-stdout>echo hi</local-command-stdout>"}},
                {"type": "user", "cwd": REPO, "timestamp": _TS, "sessionId": "fork1",
                 "message": {"content": [
                     {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
                {"type": "user", "cwd": REPO, "timestamp": _TS, "sessionId": "fork1",
                 "message": {"content": "[Request interrupted by user]"}},
            ])
            self.assertFalse(CC.ClaudeCodeAdapter().classify(path))

    def test_a_deeply_nested_line_does_not_raise(self):
        """json 이 RecursionError 를 내는 줄도 깨진 줄처럼 건너뛴다(리뷰)."""
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
            ])
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("[" * 200000 + "\n")
            self.assertFalse(CC.ClaudeCodeAdapter().classify(path))

    def test_cache_key_includes_the_headless_override(self):
        """OMHC_ALLOW_HEADLESS 가 판정을 바꾸므로 캐시가 옛 판정을 돌려주면 안 된다."""
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "s.jsonl")
            _write_jsonl(path, [{"type": "user", "cwd": REPO, "timestamp": _TS,
                                 "entrypoint": "sdk-cli", "message": {"content": "hi"}}])
            adapter = CC.ClaudeCodeAdapter()
            with unittest.mock.patch.dict(os.environ, {"OMHC_ALLOW_HEADLESS": ""}):
                self.assertFalse(adapter.classify(path))
            with unittest.mock.patch.dict(os.environ, {"OMHC_ALLOW_HEADLESS": "1"}):
                self.assertTrue(adapter.classify(path))

    def test_classify_result_is_cached_per_path_size_and_mtime(self):
        """리뷰 지적: ref_for_path 가 한 번, brief.eligible 이 다시 한 번
        adapter.classify(mark.path) 를 부를 수 있다 — 두 번째 호출은 파일을
        다시 스캔하지 않아야 한다. 파일이 자라면(size/mtime 이 바뀌면) 캐시가
        무효화돼 다시 스캔해야 한다."""
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "f.jsonl")
            _write_jsonl(path, [
                _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": "부모의 목표"}}, uuid="u1"),
            ])
            calls = []
            real = CC._forked_lacks_own_turn

            def counting(p):
                calls.append(p)
                return real(p)

            adapter = CC.ClaudeCodeAdapter()
            with unittest.mock.patch.object(CC, "_forked_lacks_own_turn", counting):
                self.assertFalse(adapter.classify(path))
                self.assertFalse(adapter.classify(path))
                self.assertEqual(len(calls), 1)

                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "user", "cwd": REPO, "timestamp": _TS,
                                        "sessionId": "fork1",
                                        "message": {"content": "새 지시"}}) + "\n")
                self.assertTrue(adapter.classify(path))
                self.assertEqual(len(calls), 2)


NOW = 1758500000.0


class TestForkBrief(unittest.TestCase):
    """#34 end-to-end: 부모가 이미 codex-cli 로 전달된 뒤 그 세션이 포크되면,
    포크 자신의 새 턴이 없는 한 아무것도 다시 나가면 안 된다."""

    def setUp(self):
        self.t = _repo.TempRepo()
        self.addCleanup(self.t.close)
        self.home = self.t.home
        self.root = self.t.root
        self.key = self.t.key
        self.state = self.t.state
        self.directory = os.path.join(self.home, ".claude", "projects",
                                      CC.claude_slug(self.root))
        os.makedirs(self.directory, exist_ok=True)

    def _plant(self, session_id, rows, epoch):
        path = os.path.join(self.directory, session_id + ".jsonl")
        _write_jsonl(path, rows)
        ledger.append({"repo": self.key, "harness": "claude-code",
                       "session": session_id, "event": "start",
                       "epoch": epoch, "path": path, "cwd": self.root},
                      home=self.home)
        return path

    def _deliver_parent_to_codex(self):
        path = self._plant("parent1", [
            {"type": "user", "cwd": self.root, "timestamp": _TS,
             "message": {"content": "부모의 목표: 필드 경로부터 다시 확인해줘"}},
        ], NOW - 1200)
        wm = due.Watermark(repo_key=self.key, harness="claude-code",
                           session_id="parent1", path=path, event="start",
                           epoch=NOW - 1200)
        due.mark_delivered(self.state, wm, to_harness="codex-cli", epoch=NOW - 1100)
        return path

    def test_fork_with_no_own_turn_delivers_nothing_and_leaves_delivered_unchanged(self):
        self._deliver_parent_to_codex()
        delivered_path = os.path.join(self.state, due.DELIVERED_NAME)
        with open(delivered_path, encoding="utf-8") as fh:
            before = fh.read()
        self._plant("fork1", [
            {"type": "history-suppression", "cause": "fork_inherit"},
            _copied({"type": "user", "cwd": self.root, "timestamp": _TS,
                    "message": {"content": "부모의 목표: 필드 경로부터 다시 확인해줘"}},
                   fork_id="fork1", parent_id="parent1", uuid="u1"),
        ], NOW - 600)
        body = brief.compute(my_harness="codex-cli", my_session_id="cxnow",
                             repo_root=self.root, home=self.home, now=NOW)
        self.assertEqual(body, "")
        with open(delivered_path, encoding="utf-8") as fh:
            after = fh.read()
        self.assertEqual(before, after)
        self.assertFalse(due.already_delivered(self.state, "fork1", "codex-cli"))

    def test_fork_with_a_new_human_turn_inherits_goal_and_carries_the_new_turn_as_next(self):
        self._deliver_parent_to_codex()
        self._plant("fork1", [
            {"type": "history-suppression", "cause": "fork_inherit"},
            _copied({"type": "user", "cwd": self.root, "timestamp": _TS,
                    "message": {"content": "부모의 목표: 필드 경로부터 다시 확인해줘"}},
                   fork_id="fork1", parent_id="parent1", uuid="u1"),
            {"type": "user", "cwd": self.root, "timestamp": _TS, "sessionId": "fork1",
             "message": {"content": "포크에서 새로 시킨 일: 로그를 확인해줘"}},
        ], NOW - 600)
        body = brief.compute(my_harness="codex-cli", my_session_id="cxnow",
                             repo_root=self.root, home=self.home, now=NOW)
        self.assertIn("필드 경로부터 다시 확인해줘", body)
        self.assertIn("포크에서 새로 시킨 일", body)
        self.assertTrue(due.already_delivered(self.state, "fork1", "codex-cli"))


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
        for synthetic in guard.SYNTHETIC_HUMAN:
            for ev in humans:
                self.assertNotEqual(ev.text, synthetic)

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

    def test_synthetic_assistant_record_is_dropped(self):
        """Claude Code 2.1.281 자신의 스킵 판정과 같은 레코드다.

        <synthetic> 모델이나 isApiErrorMessage 는 로그인 안내 같은 하네스 자체
        발화이지 에이전트의 말이 아니다 — PLAN? 으로도 새어나가면 안 된다.
        """
        read = self._read([
            {"type": "assistant", "cwd": REPO, "isApiErrorMessage": True,
             "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"model": "claude-opus-4",
                         "content": [{"type": "text",
                                      "text": "Not logged in · Please run /login"}]}},
            {"type": "assistant", "cwd": REPO,
             "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"model": "<synthetic>",
                         "content": [{"type": "text", "text": "synthetic push"}]}},
        ])
        self.assertEqual(len(read.events), 0)
        self.assertEqual(read.dropped.get("synthetic"), 2)

    def test_interrupted_for_tool_use_yields_no_human_event_and_no_next_slot(self):
        rows = [
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 목표 진술"}},
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"content": "[Request interrupted by user for tool use]"}},
        ]
        read = self._read(rows)
        self.assertEqual(len(read.events), 1)
        from omhc import mint
        out = mint.mint(read, to_adapter_id="codex-cli", budget=900, now=1758500000.0)
        self.assertNotIn("interrupted by user for tool use", out)

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
