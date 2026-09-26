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
        """A value measured on this machine. Skipped under a different path."""
        if REPO != "/home/ec2-user/capstone/oh-my-harness-cowork":
            self.skipTest("different checkout path: {}".format(REPO))
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
        """Never look at the real home. CI runners have no Claude Code, so relying on the real home would always fail."""
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".claude", "projects"))
            got = CC.ClaudeCodeAdapter(home=home).detect()
            self.assertTrue(got.present)
            self.assertEqual(got.note, os.path.join(home, ".claude", "projects"))


@unittest.skipUnless(have_fixtures, MISSING)
class TestListSessions(unittest.TestCase):
    def test_glob_is_depth_one_only(self):
        """If 137 nested subagent files leaked into the result, it would read another agent's speech."""
        refs = CC.ClaudeCodeAdapter().list_sessions(REPO)
        self.assertTrue(refs)
        for ref in refs:
            rest = os.path.relpath(ref.source_path, os.path.dirname(refs[0].source_path))
            self.assertNotIn(os.sep, rest, "path deeper than depth 1: {}".format(ref.source_path))

    def test_sessions_outside_this_repo_are_not_returned(self):
        refs = CC.ClaudeCodeAdapter().list_sessions(REPO)
        for ref in refs:
            self.assertEqual(ref.adapter_id, "claude-code")

    def test_unknown_repo_returns_empty_not_an_exception(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(CC.ClaudeCodeAdapter().list_sessions(d), [])

    def test_non_interactive_sdk_sessions_are_excluded(self):
        """Measured: 30 of this repo's 31 top-level sessions have entrypoint=sdk-py.

        Without filtering, a non-interactive session left by someone else's tool gets mistaken for human work.
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
        """An allowlist would lose a real session whenever a new interactive entrypoint appears."""
        self.assertIn("sdk-py", CC.NON_INTERACTIVE_ENTRYPOINTS)
        self.assertNotIn("cli", CC.NON_INTERACTIVE_ENTRYPOINTS)


class TestHeadlessOverride(unittest.TestCase):
    """OMHC_ALLOW_HEADLESS revives headless sessions (sdk-cli etc.) but never
    a sidechain — that's a different problem (who the speaker is), not something the override is meant to solve."""

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
    """The shape of a record copied by /branch, --fork-session, etc. (#34,
    basis of the analysis). uuid/parentUuid/timestamp/type/message stay as
    in the original; everything else is overwritten."""
    row = dict(row)
    row["sessionId"] = fork_id
    row["isSidechain"] = False
    row["sessionKind"] = None
    row["forkedFrom"] = {"sessionId": parent_id, "messageUuid": uuid}
    return row


_TS = "2026-09-25T00:00:00.000Z"


class TestForkClassify(unittest.TestCase):
    """#34: a fork copies the parent's chain and starts under a new
    session_id. With no human turn of its own, it would redeliver the
    already-delivered parent turn, so it's not eligible."""

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
        """Even with a new section, still not eligible if it's not a human's words."""
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
        """If the own tail (past the copied section) is large enough to
        exceed the bound (a human turn can't be found), fall back to the old
        behavior — giving up on the judgment is cheaper than never opening
        the session at all. The copied section's own bytes don't count
        toward the bound (review point), so the own tail must be filled
        larger than the bound."""
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
        """Review repro: if a user record in the own tail holds message as a
        string, `message.get("content")` raises AttributeError — if that
        leaks out of classify(), watch loses every Claude ref for that repo."""
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
        """Review repro: if a text block's text is not a string (e.g. 5),
        `_text_of`'s "".join raises TypeError."""
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
        """envelope-only, tool_result-only, and synthetic interrupt strings
        aren't counted as human speech by read_session either — if a fork's
        own tail has only these records, it must still not be eligible."""
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
        """A line where json raises RecursionError is skipped like a broken line too (review)."""
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
        """OMHC_ALLOW_HEADLESS changes the verdict, so the cache must not return a stale one."""
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
        """Review point: adapter.classify(mark.path) can be called once by
        ref_for_path and again by brief.eligible — the second call must not
        rescan the file. If the file grows (size/mtime changes), the cache
        must invalidate and rescan."""
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
    """#34 end-to-end: once a parent has already been delivered to codex-cli
    and that session is forked, nothing must go out again unless the fork
    has a new turn of its own."""

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
        """Claude Code reflects tool_result back as type:\"user\". 67 of them do."""
        for ev in self.read.events:
            if ev.author == "human":
                self.assertNotIn("tool_use_id", ev.text)

    def test_subagent_file_yields_no_events(self):
        read = self.adapter.read_session(ref_for(SUB))
        self.assertEqual(len(read.events), 0)
        self.assertGreater(sum(read.dropped.values()), 0)

    def test_real_tool_names_map_to_neutral_verbs(self):
        verbs = {e.verb for e in self.read.events}
        self.assertIn("ran", verbs)       # Bash x52
        self.assertIn("modified", verbs)  # Write 18 / Edit 6
        self.assertIn("said", verbs)

    def test_verb_map_covers_every_tool_this_machine_actually_used(self):
        """Unmapped tools must be 0.

        Never assert "no tool_use in agent text" via a keyword scan —
        legitimate prose where the agent explains a tool_use pairing would
        get caught. Vocabulary leakage is prevented by the schema (no
        tool-name field) and exhaustive mapping, not by censoring the body text.
        """
        self.assertNotIn("unmapped_tool", self.read.dropped)

    def test_tool_events_carry_arg_but_never_a_tool_name_field(self):
        with_arg = [e for e in self.read.events if e.arg]
        self.assertTrue(with_arg)
        for ev in with_arg:
            self.assertEqual(ev.author, "agent")
            self.assertLessEqual(len(ev.arg), 120)

    def test_thinking_blocks_are_not_events(self):
        """The model's private reasoning never crosses vendors. 79 of them in the fixture."""
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
    """A branch absent from real fixtures is covered with a forged record — there's no observed is_error=True case."""

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
        """The same record shape as Claude Code 2.1.281's own skip judgment.

        A <synthetic> model or isApiErrorMessage is the harness's own speech
        (e.g. a login prompt), not the agent's words — it must not leak out
        even as PLAN?.
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


class TestReadSessionSince(unittest.TestCase):
    """#42 (v2 phase 2 prerequisite): a per-turn check can't afford a full
    read_session (measured 112ms on a 24MB transcript) — read_session_since
    must match read_session's own record classification exactly (invariant 4:
    never a second, drifting copy of the whitelist)."""

    def _rows(self):
        return [
            {"type": "user", "cwd": REPO, "timestamp": _TS,
             "message": {"content": "첫 턴"}},
            {"type": "assistant", "cwd": REPO, "timestamp": _TS,
             "message": {"content": [{"type": "text", "text": "첫 응답"}]}},
            {"type": "user", "cwd": REPO, "timestamp": _TS,
             "message": {"content": "두 번째 턴"}},
        ]

    def _write(self, rows) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            return fh.name

    def test_matches_read_session_restricted_to_a_line_aligned_offset(self):
        path = self._write(self._rows())
        try:
            adapter = CC.ClaudeCodeAdapter()
            ref = ref_for(path)
            full = adapter.read_session(ref)
            self.assertGreaterEqual(len(full.events), 3)
            mid = full.events[1]  # first response
            since = adapter.read_session_since(ref, mid.offset)
            self.assertIsNotNone(since)
            expected = tuple(e._replace(seq=0) for e in full.events
                             if e.offset >= mid.offset)
            got = tuple(e._replace(seq=0) for e in since.events)
            self.assertEqual(got, expected)
            self.assertIn("두 번째 턴", [e.text for e in since.events])
            self.assertNotIn("첫 턴", [e.text for e in since.events])
        finally:
            os.unlink(path)

    def test_offset_zero_matches_read_session_in_full(self):
        path = self._write(self._rows())
        try:
            adapter = CC.ClaudeCodeAdapter()
            ref = ref_for(path)
            full = adapter.read_session(ref)
            since = adapter.read_session_since(ref, 0)
            self.assertEqual(since.events, full.events)
            self.assertEqual(since.end_offset, os.path.getsize(path))
        finally:
            os.unlink(path)

    def test_mid_line_offset_skips_the_truncated_record(self):
        path = self._write(self._rows())
        try:
            adapter = CC.ClaudeCodeAdapter()
            ref = ref_for(path)
            full = adapter.read_session(ref)
            second_user = next(e for e in full.events if e.text == "두 번째 턴")
            since = adapter.read_session_since(ref, second_user.offset + 3)
            self.assertIsNotNone(since)
            self.assertEqual(since.events, ())
        finally:
            os.unlink(path)

    def test_max_bytes_caps_the_tail_and_end_offset_stays_line_aligned(self):
        rows = self._rows()
        for i in range(50):
            rows.append({"type": "assistant", "cwd": REPO, "timestamp": _TS,
                        "message": {"content": [{"type": "text",
                                                 "text": "패딩 " * 50}]}})
        rows.append({"type": "user", "cwd": REPO, "timestamp": _TS,
                    "message": {"content": "캡 밖의 사람 턴"}})
        path = self._write(rows)
        try:
            adapter = CC.ClaudeCodeAdapter()
            ref = ref_for(path)
            full_size = os.path.getsize(path)
            since = adapter.read_session_since(ref, 0, max_bytes=200)
            self.assertLess(since.end_offset, full_size)
            self.assertNotIn("캡 밖의 사람 턴", [e.text for e in since.events])
            with open(path, "rb") as fh:
                content = fh.read()
            self.assertTrue(since.end_offset == 0
                           or content[since.end_offset - 1:since.end_offset] == b"\n")
        finally:
            os.unlink(path)

    def test_stop_at_human_turn_returns_immediately_after_the_first_match(self):
        rows = [
            {"type": "user", "cwd": REPO, "timestamp": _TS,
             "message": {"content": "첫 사람 턴"}},
            {"type": "assistant", "cwd": REPO, "timestamp": _TS,
             "message": {"content": [{"type": "text",
                                      "text": "안 읽혀야 한다"}]}},
            {"type": "user", "cwd": REPO, "timestamp": _TS,
             "message": {"content": "안 읽혀야 하는 두 번째 사람 턴"}},
        ]
        path = self._write(rows)
        try:
            adapter = CC.ClaudeCodeAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            self.assertEqual(len(since.events), 1)
            self.assertEqual(since.events[0].text, "첫 사람 턴")
            self.assertLess(since.end_offset, os.path.getsize(path))
        finally:
            os.unlink(path)

    def test_stop_at_human_turn_ignores_a_complete_but_unterminated_human_line(self):
        """A human turn whose trailing newline hasn't landed yet (may be
        mid-write) doesn't count as a match — counting it would let the next
        round, once the newline lands, find the same line again and hand it
        off twice. Calls that aren't stop_at_human_turn are unaffected."""
        path = self._write([])
        try:
            row = {"type": "user", "cwd": REPO, "timestamp": _TS,
                  "message": {"content": "완전하지만 개행 없는 턴"}}
            line = json.dumps(row, ensure_ascii=False).encode("utf-8")
            with open(path, "ab") as fh:
                fh.write(line)  # no trailing newline — mimics mid-write

            adapter = CC.ClaudeCodeAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            self.assertEqual(since.events, ())
            self.assertEqual(since.end_offset, 0)

            full = adapter.read_session(ref)
            self.assertEqual(len(full.events), 1)
            self.assertEqual(full.events[0].text, "완전하지만 개행 없는 턴")
        finally:
            os.unlink(path)

    def test_fork_copied_records_never_produce_a_human_turn_in_a_tail_read(self):
        """#34/#42: a forked transcript's copied section is never new work —
        if a growth check's baseline ever landed inside it (the one race
        this guards; the common case never sees it, see cli.py's
        `_reactivate_grown_sessions`), `stop_at_human_turn` must not mistake
        the copy for a fresh human turn."""
        rows = [
            {"type": "history-suppression", "cause": "fork_inherit"},
            _copied({"type": "user", "cwd": REPO, "timestamp": _TS,
                    "message": {"content": "부모의 목표"}}, uuid="u1"),
            _copied({"type": "assistant", "cwd": REPO, "timestamp": _TS,
                    "message": {"content": [{"type": "text", "text": "부모의 답"}]}},
                   uuid="u2"),
        ]
        path = self._write(rows)
        try:
            adapter = CC.ClaudeCodeAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            self.assertFalse(any(e.author == "human" for e in since.events))
            self.assertEqual(since.end_offset, os.path.getsize(path))
            # Unaffected — a full/plain read still inherits the copied GOAL.
            full = adapter.read_session(ref)
            self.assertEqual(len([e for e in full.events if e.author == "human"]), 1)
        finally:
            os.unlink(path)

    def test_missing_file_never_raises(self):
        adapter = CC.ClaudeCodeAdapter()
        ref = A.SessionRef(adapter_id="claude-code", session_id="gone",
                           source_path="/nope/missing.jsonl", cwd=REPO,
                           epoch=0.0, size=0)
        since = adapter.read_session_since(ref, 10)
        self.assertIsNotNone(since)
        self.assertEqual(since.events, ())

    def test_garbage_offset_never_raises(self):
        path = self._write(self._rows())
        try:
            adapter = CC.ClaudeCodeAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, "not-an-int")
            self.assertIsNotNone(since)
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
