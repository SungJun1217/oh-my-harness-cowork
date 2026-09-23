from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from omhc import adapter as A
from omhc.adapters import codex_cli as CX

from . import _repo
from ._repo import MISSING, REPO
from ._repo import CODEX_EXEC as EXEC

have_fixtures = _repo.have_fixtures(EXEC)



def ref_for(path: str) -> A.SessionRef:
    return _repo.ref_for("codex-cli", path, session_id="test-session")


def write_rollout(rows) -> str:
    for i, row in enumerate(rows):
        row.setdefault("ordinal", i)
        row.setdefault("timestamp", "2026-09-22T16:30:0{}.000Z".format(i % 10))
    return _repo.write_jsonl(rows)


def msg(role: str, text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "id": "m-" + role,
            "content": [{"type": "input_text", "text": text}],
        },
    }


class TestDetect(unittest.TestCase):
    def test_detect_never_raises_on_missing_home(self):
        with tempfile.TemporaryDirectory() as home:
            got = CX.CodexCliAdapter(home=home).detect()
            self.assertFalse(got.present)
            self.assertTrue(got.note)

    def test_capabilities_declare_both_halves(self):
        caps = CX.CodexCliAdapter.capabilities
        self.assertIn(A.Capability.READ, caps)
        self.assertIn(A.Capability.WRITE, caps)


class TestListSessions(unittest.TestCase):
    def _make_tree(self, home: str, day_offset: int, cwd: str) -> str:
        stamp = time.gmtime(time.time() - day_offset * 86400)
        directory = os.path.join(
            home, ".codex", "sessions",
            time.strftime("%Y", stamp), time.strftime("%m", stamp),
            time.strftime("%d", stamp),
        )
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "rollout-x{}.jsonl".format(day_offset))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "timestamp": "2026-09-22T00:00:00.000Z", "ordinal": 0,
                "type": "session_meta",
                "payload": {"session_id": "s{}".format(day_offset), "cwd": cwd},
            }) + "\n")
        return path

    def test_only_the_first_line_is_read_for_matching(self):
        with tempfile.TemporaryDirectory() as home:
            path = self._make_tree(home, 0, REPO)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("{broken json that must never be parsed\n")
            refs = CX.CodexCliAdapter(home=home).list_sessions(REPO)
            self.assertEqual(len(refs), 1)

    def test_date_scan_is_bounded_to_recent_days(self):
        with tempfile.TemporaryDirectory() as home:
            self._make_tree(home, 0, REPO)
            self._make_tree(home, 400, REPO)
            refs = CX.CodexCliAdapter(home=home).list_sessions(REPO)
            self.assertEqual(len(refs), 1)

    def test_subdirectory_cwd_matches_equal_or_descendant(self):
        with tempfile.TemporaryDirectory() as home:
            self._make_tree(home, 0, os.path.join(REPO, "docs"))
            refs = CX.CodexCliAdapter(home=home).list_sessions(REPO)
            self.assertEqual(len(refs), 1)

    def test_other_repo_is_not_returned(self):
        with tempfile.TemporaryDirectory() as home:
            self._make_tree(home, 0, "/home/ec2-user/somewhere-else")
            self.assertEqual(CX.CodexCliAdapter(home=home).list_sessions(REPO), [])


@unittest.skipUnless(have_fixtures, MISSING)
class TestReadRealFixture(unittest.TestCase):
    def setUp(self):
        self.read = CX.CodexCliAdapter().read_session(ref_for(EXEC))

    def test_environment_context_envelope_is_not_a_human_turn(self):
        """판별자는 둘이다 — 메타데이터 kind 와 봉투 구조.

        content_item_kinds 는 실재하지만 payload 최상위가 아니라
        payload.internal_chat_message_metadata_passthrough 안에 중첩돼 있다.
        """
        for ev in self.read.events:
            if ev.author == "human":
                self.assertNotIn("<environment_context>", ev.text)

    def test_the_real_human_prompt_is_captured(self):
        humans = [e for e in self.read.events if e.author == "human"]
        self.assertEqual(len(humans), 1)
        self.assertIn("ls docs/superpowers/specs", humans[0].text)

    def test_developer_machinery_is_never_parsed(self):
        joined = "\n".join(e.text for e in self.read.events)
        self.assertNotIn("<skills_instructions>", joined)
        self.assertNotIn("<multi_agent_role>", joined)

    def test_developer_records_are_reported_as_dropped(self):
        self.assertGreater(self.read.dropped.get("role:developer", 0), 0)

    def test_no_exception_on_the_authentication_failed_session(self):
        self.assertEqual(self.read.unparsed, 0)


@unittest.skipUnless(_repo.have_fixtures(_repo.CODEX_TOOLS, _repo.CODEX_EDIT,
                                         _repo.EXPECTED), MISSING)
class TestRealToolCalls(unittest.TestCase):
    """실물(codex-cli 0.156.1, gpt-6-luna) 로 확인한 도구 레코드.

    골든은 harvest.py 가 어댑터를 부르지 않고 독립 계산한다.
    """

    def setUp(self):
        self.golden = _repo.load_expected()["codex_tools"]
        self.tools = CX.CodexCliAdapter().read_session(ref_for(_repo.CODEX_TOOLS))
        self.edit = CX.CodexCliAdapter().read_session(ref_for(_repo.CODEX_EDIT))

    def machine(self, read):
        return [e for e in read.events if e.verb != "said"]

    def test_every_shell_command_becomes_one_event_with_its_real_text(self):
        """이전 매핑은 arguments 가 없는 custom_tool_call 을 읽어 arg 가 비었다."""
        self.assertEqual([e.arg for e in self.machine(self.tools)],
                         self.golden["tools"]["commands"])

    def test_a_nonzero_exit_marks_the_event_failed(self):
        """이전 매핑은 출력 텍스트에서 exit_code 를 찾았고 거기엔 없다 → FAIL 영구 불가."""
        failed = [e.arg for e in self.machine(self.tools) if not e.ok]
        self.assertEqual(failed, self.golden["tools"]["failed"])
        self.assertTrue(failed)

    def test_file_change_becomes_modified_with_its_absolute_path(self):
        modified = [e for e in self.edit.events if e.verb == "modified"]
        self.assertEqual([p for e in modified for p in e.paths],
                         self.golden["edit"]["changed"])
        self.assertTrue(all(e.ok for e in modified))

    def test_read_only_commands_are_inspected_not_ran(self):
        """Codex 에는 읽기 전용 도구가 따로 없고 전부 셸로 간다. parsed_cmd 가 Codex
        자신의 분류이므로 그것을 따른다 — 안 그러면 Codex 세션에 inspected 가 없다."""
        self.assertEqual({e.verb for e in self.machine(self.tools)}, {"inspected"})

    def test_the_js_wrapper_is_bookkeeping_not_an_event(self):
        """custom_tool_call(name=exec) 는 사실의 사본이다. 둘 다 세면 이중 계상된다."""
        self.assertEqual(len(self.machine(self.tools)),
                         len(self.golden["tools"]["commands"]))
        self.assertGreater(self.tools.dropped.get("js_exec", 0), 0)

    def test_nothing_is_unparsed(self):
        self.assertEqual(self.tools.unparsed, 0)
        self.assertEqual(self.edit.unparsed, 0)

    def test_tier_b_offset_points_at_the_command_record(self):
        ev = self.machine(self.tools)[-1]
        with open(_repo.CODEX_TOOLS, "rb") as fh:
            fh.seek(ev.offset)
            row = json.loads(fh.read(ev.length))
        self.assertEqual(row["payload"]["item"]["type"], "CommandExecution")


class TestMeasuredShapes(unittest.TestCase):
    """픽스처 없이도 도는 실측 모양 단위 테스트."""

    def _read(self, rows) -> A.SessionRead:
        path = write_rollout(rows)
        try:
            return CX.CodexCliAdapter().read_session(ref_for(path))
        finally:
            os.unlink(path)

    META = {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}}

    def test_shell_wrapper_is_stripped_from_the_arg(self):
        read = self._read([self.META] + _repo.codex_shell_rows(["pytest", "-q"]))
        self.assertEqual([(e.verb, e.arg) for e in read.events], [("ran", "pytest -q")])

    def test_failed_status_is_a_failure_even_without_an_exit_code(self):
        rows = _repo.codex_shell_rows(["pytest"], failed=True)
        del rows[1]["payload"]["item"]["exit_code"]
        read = self._read([self.META] + rows)
        self.assertFalse(read.events[0].ok)

    def test_parsed_cmd_paths_are_resolved_against_the_item_cwd(self):
        rows = _repo.codex_shell_rows(["cat", "a b.txt"], cwd="/w/my repo")
        item = rows[1]["payload"]["item"]
        item["cwd"] = "file:///w/my%20repo"
        item["parsed_cmd"] = [{"type": "read", "cmd": "cat 'a b.txt'", "path": "a b.txt"}]
        read = self._read([self.META] + rows)
        self.assertEqual(read.events[0].verb, "inspected")
        self.assertEqual(read.events[0].paths, ("/w/my repo/a b.txt",))

    def test_a_mixed_pipeline_is_ran(self):
        rows = _repo.codex_shell_rows(["cat x | python3 -"])
        rows[1]["payload"]["item"]["parsed_cmd"] = [
            {"type": "read", "cmd": "cat x", "path": "x"},
            {"type": "unknown", "cmd": "python3 -"},
        ]
        self.assertEqual(self._read([self.META] + rows).events[0].verb, "ran")

    def test_failed_file_change_is_a_failure(self):
        read = self._read([self.META, _repo.codex_item_row({
            "type": "FileChange", "status": "failed",
            "changes": {"/w/a.py": {"type": "update", "unified_diff": ""}}}, 2)])
        self.assertEqual([(e.verb, e.ok) for e in read.events], [("modified", False)])

    def test_unknown_item_type_is_dropped_by_name(self):
        read = self._read([self.META, _repo.codex_item_row({"type": "Brand2099"}, 2)])
        self.assertEqual(read.events, ())
        self.assertEqual(read.dropped.get("item:Brand2099"), 1)


class TestLegacyToolCallShapes(unittest.TestCase):
    """UNVERIFIED — 0.156.1 은 function_call 을 쓰지 않는다. 이전/다른 버전의
    function_call·local_shell_call 을 위한 경로이고 실물로 확인된 적이 없다.
    """

    def _read(self, rows) -> A.SessionRead:
        path = write_rollout(rows)
        try:
            return CX.CodexCliAdapter().read_session(ref_for(path))
        finally:
            os.unlink(path)

    def test_unverified_function_call_maps_to_a_neutral_verb(self):
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"type": "response_item", "payload": {
                "type": "function_call", "name": "shell", "call_id": "c1",
                "arguments": json.dumps({"command": ["pytest", "-q"]})}},
        ])
        verbs = [e.verb for e in read.events if e.arg]
        self.assertEqual(verbs, ["ran"])
        self.assertEqual(read.unparsed, 0)

    def test_unverified_function_call_output_marks_failure(self):
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"type": "response_item", "payload": {
                "type": "function_call", "name": "shell", "call_id": "c1",
                "arguments": json.dumps({"command": ["pytest"]})}},
            {"type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1",
                "output": json.dumps({"exit_code": 1, "output": "3 failed"})}},
        ])
        ran = [e for e in read.events if e.verb == "ran"]
        self.assertEqual(len(ran), 1)
        self.assertFalse(ran[0].ok)

    def test_unverified_apply_patch_maps_to_modified(self):
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"type": "response_item", "payload": {
                "type": "function_call", "name": "apply_patch", "call_id": "c2",
                "arguments": json.dumps({"input": "*** Update File: omhc/x.py"})}},
        ])
        self.assertEqual([e.verb for e in read.events if e.arg], ["modified"])

    def test_unverified_local_shell_call_maps_to_ran(self):
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"type": "response_item", "payload": {
                "type": "local_shell_call", "call_id": "c3",
                "action": {"type": "exec", "command": ["ls", "-la"]}}},
        ])
        self.assertEqual([e.verb for e in read.events if e.arg], ["ran"])


class TestDefensiveDegradation(unittest.TestCase):
    def test_unknown_response_item_type_is_counted_not_raised(self):
        path = write_rollout([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"type": "response_item", "payload": {"type": "brand_new_2099"}},
        ])
        try:
            read = CX.CodexCliAdapter().read_session(ref_for(path))
            self.assertEqual(read.unparsed, 1)
            self.assertEqual(len(read.events), 0)
        finally:
            os.unlink(path)

    def test_unknown_envelope_type_is_dropped_not_unparsed(self):
        path = write_rollout([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"type": "some_new_envelope", "payload": {}},
        ])
        try:
            read = CX.CodexCliAdapter().read_session(ref_for(path))
            self.assertIn("some_new_envelope", read.dropped)
            self.assertEqual(read.unparsed, 0)
        finally:
            os.unlink(path)

    def test_broken_line_does_not_stop_the_read(self):
        path = write_rollout([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            msg("user", "진짜 사람의 말"),
        ])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{broken\n")
        try:
            read = CX.CodexCliAdapter().read_session(ref_for(path))
            self.assertEqual(len([e for e in read.events if e.author == "human"]), 1)
            self.assertEqual(read.unparsed, 1)
        finally:
            os.unlink(path)

    def test_assistant_output_text_becomes_an_agent_said_event(self):
        path = write_rollout([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "id": "a1",
                "content": [{"type": "output_text", "text": "테스트 3개가 실패했다"}]}},
        ])
        try:
            read = CX.CodexCliAdapter().read_session(ref_for(path))
            agents = [e for e in read.events if e.author == "agent"]
            self.assertEqual(len(agents), 1)
            self.assertEqual(agents[0].verb, "said")
        finally:
            os.unlink(path)


class TestWriteSide(unittest.TestCase):
    def test_native_resume_hint_names_the_session(self):
        hint = CX.CodexCliAdapter().native_resume_hint(ref_for(EXEC) if have_fixtures
                                                       else ref_for(__file__))
        self.assertIn("codex resume", hint)

    def _bundle(self):
        return A.HandoffBundle(body_md="[omhc] hi\n", repo_root=REPO,
                               to_adapter_id="codex-cli")

    def _install_hook(self, home):
        directory = os.path.join(home, ".codex")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "hooks.json"), "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "omhc brief"}]}]}}, fh)

    def test_install_handoff_writes_the_artifact_when_the_hook_exists(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            receipt = CX.CodexCliAdapter(home=home).install_handoff(self._bundle())
            self.assertTrue(receipt.paths_written)
            with open(receipt.paths_written[0], encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "[omhc] hi\n")

    def test_no_hook_means_no_pull_channel(self):
        """훅이 없으면 산출물을 써도 아무도 읽지 않는다 — 그것을 성공으로 보고하면
        AGENTS.md 폴백이 영원히 발동하지 않는다."""
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(A.NoInjectionChannel):
                CX.CodexCliAdapter(home=home).install_handoff(self._bundle())

    def test_fallback_channel_is_declared(self):
        with tempfile.TemporaryDirectory() as home:
            channels = CX.CodexCliAdapter(home=home).fallback_channels()
            self.assertEqual(len(channels), 1)
            self.assertTrue(callable(channels[0]))


class TestRegistryV1(unittest.TestCase):
    def test_both_v1_adapters_are_registered(self):
        from omhc import adapters

        self.assertIn("claude-code", adapters.REGISTRY)
        self.assertIn("codex-cli", adapters.REGISTRY)


if __name__ == "__main__":
    unittest.main()


class TestMetadataKindDiscriminator(unittest.TestCase):
    """content_item_kinds 는 payload 최상위가 아니라 메타데이터 안에 중첩돼 있다."""

    def _read(self, rows):
        path = write_rollout(rows)
        try:
            return CX.CodexCliAdapter().read_session(ref_for(path))
        finally:
            os.unlink(path)

    def _msg(self, role, text, kinds):
        return {"type": "response_item", "payload": {
            "type": "message", "role": role, "id": "m",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": kinds}}}

    def test_environment_context_kind_is_dropped_even_without_an_envelope(self):
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            self._msg("user", "봉투 없이 온 환경 설명",
                      ["environments.environment_context"]),
        ])
        self.assertEqual([e for e in read.events if e.author == "human"], [])
        self.assertIn("kind:environments.environment_context", read.dropped)

    def test_user_text_kind_is_kept(self):
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            self._msg("user", "진짜 사람의 말", ["user.text"]),
        ])
        humans = [e for e in read.events if e.author == "human"]
        self.assertEqual(len(humans), 1)

    def test_a_new_user_prefixed_kind_is_not_lost(self):
        """접두 허용이라 새 user.* kind 가 생겨도 사람의 말을 잃지 않는다."""
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            self._msg("user", "미래의 사람 입력", ["user.voice_2099"]),
        ])
        self.assertEqual(len([e for e in read.events if e.author == "human"]), 1)

    def test_missing_metadata_falls_back_to_the_envelope_test(self):
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "user", "id": "m",
                "content": [{"type": "input_text",
                             "text": "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"}]}},
        ])
        self.assertEqual([e for e in read.events if e.author == "human"], [])

    def test_human_kinds_reads_the_nested_location(self):
        payload = {"internal_chat_message_metadata_passthrough": {
            "content_item_kinds": ["user.text"]}}
        self.assertEqual(CX.human_kinds(payload), ["user.text"])
        self.assertIsNone(CX.human_kinds({"content_item_kinds": ["user.text"]}))
