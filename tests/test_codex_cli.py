from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from omhc import adapter as A
from omhc import cli
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
    """era A (codex-cli 0.141–0.142) — function_call name=exec_command/
    apply_patch/spawn_agent, 출력은 평문(JSON 아님). 픽스처 없이도 도는 실측
    모양 단위 테스트(_repo.codex_* 빌더가 정확히 그 텍스트 헤더를 쓴다)."""

    def _read(self, rows) -> A.SessionRead:
        path = write_rollout(rows)
        try:
            return CX.CodexCliAdapter().read_session(ref_for(path))
        finally:
            os.unlink(path)

    META = {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}}

    def test_exec_command_exit_0_is_ok(self):
        rows = _repo.codex_exec_command_rows(["pytest", "-q"], code=0)
        read = self._read([self.META] + rows)
        ran = [e for e in read.events if e.verb == "ran"]
        self.assertEqual(len(ran), 1)
        self.assertEqual(ran[0].arg, "pytest -q")
        self.assertTrue(ran[0].ok)

    def test_exec_command_exit_1_is_a_failure(self):
        rows = _repo.codex_exec_command_rows(["pytest", "-q"], code=1)
        read = self._read([self.META] + rows)
        ran = [e for e in read.events if e.verb == "ran"]
        self.assertEqual(len(ran), 1)
        self.assertFalse(ran[0].ok)

    def test_running_then_write_stdin_exit_updates_the_original_event(self):
        rows = (_repo.codex_exec_command_rows(["npm", "run", "dev"], running_sid=42)
               + _repo.codex_write_stdin_rows(42, code=1))
        read = self._read([self.META] + rows)
        ran = [e for e in read.events if e.verb == "ran"]
        self.assertEqual(len(ran), 1)
        self.assertEqual(ran[0].arg, "npm run dev")
        self.assertFalse(ran[0].ok)
        self.assertEqual(read.dropped.get("tool_bookkeeping", 0), 1)

    def test_a_call_with_no_output_stays_ok_abort(self):
        rows = _repo.codex_exec_command_rows(["ls"])  # code/running_sid 둘 다 None
        read = self._read([self.META] + rows)
        ran = [e for e in read.events if e.verb == "ran"]
        self.assertEqual(len(ran), 1)
        self.assertTrue(ran[0].ok)

    def test_aborted_by_user_is_a_failure(self):
        read = self._read([self.META, {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "exec_command",
                       "call_id": "ec9",
                       "arguments": json.dumps({"cmd": "long-running-thing"})}},
            {"type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "ec9",
                        "output": "aborted by user after 12.3s"}},
        ])
        ran = [e for e in read.events if e.verb == "ran"]
        self.assertEqual(len(ran), 1)
        self.assertFalse(ran[0].ok)

    def test_apply_patch_and_filechange_with_the_same_id_merge_into_one_event(self):
        rows = _repo.codex_apply_patch_rows(
            ["*** Update File: /abs/omhc/x.py"], with_filechange=True)
        read = self._read([self.META] + rows)
        modified = [e for e in read.events if e.verb == "modified"]
        self.assertEqual(len(modified), 1)
        self.assertEqual(modified[0].paths, ("/abs/omhc/x.py",))
        self.assertTrue(modified[0].ok)

    def test_apply_patch_alone_resolves_relative_paths_against_workdir(self):
        rows = _repo.codex_apply_patch_rows(
            ["*** Update File: omhc/x.py"], with_filechange=False, workdir="/w/repo")
        read = self._read([self.META] + rows)
        modified = [e for e in read.events if e.verb == "modified"]
        self.assertEqual(len(modified), 1)
        self.assertEqual(modified[0].paths, ("/w/repo/omhc/x.py",))

    def test_spawn_agent_maps_to_delegated_and_never_leaks_the_message(self):
        row = _repo.codex_spawn_agent_row("fix-flaky-test", "여기 비밀 지침이 있다")
        read = self._read([self.META, row])
        delegated = [e for e in read.events if e.verb == "delegated"]
        self.assertEqual(len(delegated), 1)
        self.assertEqual(delegated[0].arg, "fix-flaky-test")
        joined = "\n".join(e.text + e.arg for e in read.events)
        self.assertNotIn("비밀 지침", joined)

    def test_bookkeeping_tools_produce_no_events(self):
        rows = []
        for i, name in enumerate(("wait", "wait_agent", "list_agents",
                                  "interrupt_agent", "send_message",
                                  "followup_task", "request_user_input",
                                  "list_available_plugins_to_install")):
            rows.append({"type": "response_item", "payload": {
                "type": "function_call", "name": name, "call_id": "bk{}".format(i),
                "arguments": "{}"}})
        read = self._read([self.META] + rows)
        self.assertEqual(read.events, ())
        self.assertEqual(read.dropped.get("tool_bookkeeping"), len(rows))

    def test_unknown_tool_name_is_unmapped_not_an_event(self):
        read = self._read([self.META, {
            "type": "response_item", "payload": {
                "type": "function_call", "name": "brand_new_tool_2099",
                "call_id": "u1", "arguments": "{}"}}])
        self.assertEqual(read.events, ())
        self.assertEqual(read.dropped.get("unmapped_tool"), 1)

    def test_agent_message_is_dropped_not_human_or_said(self):
        read = self._read([self.META, {
            "type": "response_item",
            "payload": {"type": "agent_message", "text": "에이전트 간 메시지"}}])
        self.assertEqual(read.events, ())
        self.assertEqual(read.dropped.get("agent_message"), 1)
        self.assertEqual(read.unparsed, 0)


class TestCommandExecutionBenignExitOne(unittest.TestCase):
    """era B: parsed_cmd 가 전부 읽기이고 출력도 비면 exit 1 은 관용구다(실측 2건).
    검증용 grep -q 는 보통 parsed_cmd 가 unknown 이라 여기 안 걸린다."""

    def _read(self, item):
        rows = [{"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
               _repo.codex_item_row(item, 2)]
        path = write_rollout(rows)
        try:
            return CX.CodexCliAdapter().read_session(ref_for(path))
        finally:
            os.unlink(path)

    def _ce(self, exit_code, parsed_cmd, stdout="", stderr=""):
        return {"type": "CommandExecution", "id": "e1",
               "command": ["/bin/bash", "-lc", "grep -r foo ."],
               "cwd": "file://" + REPO, "parsed_cmd": parsed_cmd,
               "status": "failed" if exit_code else "completed",
               "exit_code": exit_code, "stdout": stdout, "stderr": stderr}

    def test_search_with_empty_output_and_exit_1_is_ok(self):
        read = self._read(self._ce(1, [{"type": "search", "cmd": "grep -r foo ."}]))
        self.assertEqual([(e.verb, e.ok) for e in read.events], [("inspected", True)])

    def test_unknown_kind_with_exit_1_is_still_a_failure(self):
        read = self._read(self._ce(1, [{"type": "unknown", "cmd": "grep -q foo ."}]))
        self.assertEqual([(e.verb, e.ok) for e in read.events], [("ran", False)])

    def test_search_with_non_empty_output_and_exit_1_is_still_a_failure(self):
        read = self._read(self._ce(
            1, [{"type": "search", "cmd": "grep -r foo ."}], stdout="1 match"))
        self.assertEqual([(e.verb, e.ok) for e in read.events], [("inspected", False)])


class TestMintShowsRealCodexFacts(unittest.TestCase):
    """era A 모양 세션이라도 FAIL/DID 가 실제 명령·경로를 보여줘야 한다 —
    이전 매핑은 arg 가 비어 있었다."""

    def test_fail_and_did_carry_the_real_command_and_path(self):
        from omhc import mint

        changed = os.path.join(REPO, "omhc", "x.py")
        rows = ([{"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
                msg("user", "테스트를 고쳐줘")]
               + _repo.codex_exec_command_rows(["pytest", "-q"], code=1)
               + _repo.codex_apply_patch_rows(
                   ["*** Update File: {}".format(changed)], with_filechange=True))
        path = write_rollout(rows)
        try:
            read = CX.CodexCliAdapter().read_session(ref_for(path))
            body = mint.mint(read, to_adapter_id="claude-code", now=0.0)
        finally:
            os.unlink(path)
        self.assertIn("pytest -q", body)
        self.assertIn("omhc/x.py", body)
        self.assertLessEqual(len(body.encode("utf-8")), 900)


class TestInteractiveFilter(unittest.TestCase):
    """서브에이전트/헤드리스 exec/프로그래매틱 앱서버 클라이언트는 절대 핸드오프
    원천이 되면 안 된다.

    실물 모양(codex-cli 0.155.1, 이 머신 실측): source={"subagent": {...}},
    thread_source="subagent", parent_thread_id=<uuid> (서브에이전트) /
    originator="codex_exec", source="exec" (헤드리스 exec, 샌드박스 rollout 5개) /
    originator="applecider"(36개)·"splitlane*"(3개), 둘 다 source="vscode" 지만
    role=user 턴이 "User goal: … Current browser URL: …" 형태의 기계 템플릿이다.
    """

    def setUp(self):
        self._env_backup = os.environ.pop("OMHC_ALLOW_HEADLESS", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env_backup is not None:
            os.environ["OMHC_ALLOW_HEADLESS"] = self._env_backup
        else:
            os.environ.pop("OMHC_ALLOW_HEADLESS", None)

    SUBAGENT_SOURCE_DICT = {"source": {"subagent": {"thread_spawn": {
        "parent_thread_id": "p1", "depth": 1, "agent_path": "/root/x",
        "agent_nickname": "n", "agent_role": "worker"}}}}
    SUBAGENT_THREAD_SOURCE = {"thread_source": "subagent"}
    SUBAGENT_PARENT_ID = {"parent_thread_id": "01a0b927-c7b8-7660-a9e2-81e739a81db6"}
    EXEC_SOURCE = {"originator": "codex_exec", "source": "exec"}
    APPLECIDER_SOURCE = {"originator": "applecider", "source": "vscode"}
    SPLITLANE_SOURCE = {"originator": "splitlane-worker", "source": "vscode"}
    INTERACTIVE_CLI = {"originator": "codex-tui", "source": "cli"}
    INTERACTIVE_VSCODE = {"originator": "Codex Desktop", "source": "vscode"}
    INTERACTIVE_STRING_SOURCE = {"originator": "codex-tui", "source": "cli"}
    GARBAGE_SOURCE = {"source": 12345, "thread_source": ["not", "a", "string"]}

    def _meta(self, extra):
        meta = {"session_id": "s", "cwd": REPO}
        meta.update(extra)
        return meta

    def test_subagent_source_dict_is_not_interactive(self):
        self.assertFalse(CX._is_interactive(self._meta(self.SUBAGENT_SOURCE_DICT)))

    def test_subagent_thread_source_is_not_interactive(self):
        self.assertFalse(CX._is_interactive(self._meta(self.SUBAGENT_THREAD_SOURCE)))

    def test_parent_thread_id_alone_is_not_interactive(self):
        self.assertFalse(CX._is_interactive(self._meta(self.SUBAGENT_PARENT_ID)))

    def test_exec_originator_is_not_interactive_by_default(self):
        self.assertFalse(CX._is_interactive(self._meta(self.EXEC_SOURCE)))

    def test_exec_becomes_interactive_under_the_override(self):
        os.environ["OMHC_ALLOW_HEADLESS"] = "1"
        self.assertTrue(CX._is_interactive(self._meta(self.EXEC_SOURCE)))

    def test_applecider_is_not_interactive_by_default(self):
        """앱서버가 얹은 프로그래매틱 클라이언트다 — role=user 턴은 사람이 아니라
        "User goal: … Current browser URL: …" 템플릿이다."""
        self.assertFalse(CX._is_interactive(self._meta(self.APPLECIDER_SOURCE)))

    def test_applecider_becomes_interactive_under_the_override(self):
        os.environ["OMHC_ALLOW_HEADLESS"] = "1"
        self.assertTrue(CX._is_interactive(self._meta(self.APPLECIDER_SOURCE)))

    def test_splitlane_prefixed_originator_is_not_interactive_by_default(self):
        self.assertFalse(CX._is_interactive(self._meta(self.SPLITLANE_SOURCE)))

    def test_splitlane_prefixed_originator_becomes_interactive_under_the_override(self):
        os.environ["OMHC_ALLOW_HEADLESS"] = "1"
        self.assertTrue(CX._is_interactive(self._meta(self.SPLITLANE_SOURCE)))

    def test_subagent_is_never_admitted_even_under_the_override(self):
        os.environ["OMHC_ALLOW_HEADLESS"] = "1"
        self.assertFalse(CX._is_interactive(self._meta(self.SUBAGENT_SOURCE_DICT)))
        self.assertFalse(CX._is_interactive(self._meta(self.SUBAGENT_THREAD_SOURCE)))
        self.assertFalse(CX._is_interactive(self._meta(self.SUBAGENT_PARENT_ID)))

    def test_cli_and_vscode_sources_stay_interactive(self):
        self.assertTrue(CX._is_interactive(self._meta(self.INTERACTIVE_CLI)))
        self.assertTrue(CX._is_interactive(self._meta(self.INTERACTIVE_VSCODE)))

    def test_string_vs_dict_source_is_handled_defensively(self):
        self.assertTrue(CX._is_interactive(self._meta(self.INTERACTIVE_STRING_SOURCE)))
        self.assertFalse(CX._is_interactive(self._meta(self.SUBAGENT_SOURCE_DICT)))

    def test_garbage_metadata_fails_open_to_interactive(self):
        """블록리스트라 모르는/이상한 모양은 대화형으로 남는다."""
        self.assertTrue(CX._is_interactive(self._meta(self.GARBAGE_SOURCE)))

    def test_classify_rejects_a_subagent_rollout(self):
        path = write_rollout([
            {"type": "session_meta",
             "payload": self._meta(self.SUBAGENT_THREAD_SOURCE)},
        ])
        try:
            self.assertFalse(CX.CodexCliAdapter().classify(path))
        finally:
            os.unlink(path)

    def test_classify_rejects_exec_by_default_and_admits_it_under_override(self):
        path = write_rollout([
            {"type": "session_meta", "payload": self._meta(self.EXEC_SOURCE)},
        ])
        try:
            self.assertFalse(CX.CodexCliAdapter().classify(path))
            os.environ["OMHC_ALLOW_HEADLESS"] = "1"
            self.assertTrue(CX.CodexCliAdapter().classify(path))
        finally:
            os.unlink(path)

    def test_list_sessions_skips_a_subagent_rollout(self):
        with tempfile.TemporaryDirectory() as home:
            directory = os.path.join(home, ".codex", "sessions",
                                     time.strftime("%Y/%m/%d", time.gmtime()))
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "rollout-sub.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "session_meta",
                    "payload": self._meta(dict(self.SUBAGENT_THREAD_SOURCE, cwd=REPO)),
                }) + "\n")
            refs = CX.CodexCliAdapter(home=home).list_sessions(REPO)
            self.assertEqual(refs, [])

    def test_list_sessions_admits_exec_only_under_override(self):
        with tempfile.TemporaryDirectory() as home:
            directory = os.path.join(home, ".codex", "sessions",
                                     time.strftime("%Y/%m/%d", time.gmtime()))
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "rollout-exec.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "session_meta",
                    "payload": self._meta(dict(self.EXEC_SOURCE, cwd=REPO)),
                }) + "\n")
            self.assertEqual(CX.CodexCliAdapter(home=home).list_sessions(REPO), [])
            os.environ["OMHC_ALLOW_HEADLESS"] = "1"
            self.assertEqual(len(CX.CodexCliAdapter(home=home).list_sessions(REPO)), 1)

    def test_ref_for_path_rejects_a_subagent_rollout(self):
        path = write_rollout([
            {"type": "session_meta",
             "payload": self._meta(dict(self.SUBAGENT_THREAD_SOURCE, cwd=REPO))},
        ])
        try:
            ref = CX.CodexCliAdapter().ref_for_path(path, "s")
            self.assertIsNone(ref)
        finally:
            os.unlink(path)


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

    def test_omhcs_own_injected_handoff_is_not_a_human_turn(self):
        """실측(codex-cli 0.155.1): omhc 가 주입한 표식이 role=user 로 rollout 에

        실제로 나타난다(content_item_kinds=["hooks.additional_context"]). 이걸
        사람 발화로 잘못 읽으면 다음 핸드오프가 omhc 자신의 표식을 GOAL/NEXT 로
        착각해 되먹임 루프가 생긴다.
        """
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            self._msg("user", "[omhc] GOAL: 리더를 붙여서 양방향으로 만들기",
                      ["hooks.additional_context"]),
            self._msg("user", "진짜 사람의 말", ["user.text"]),
        ])
        self.assertEqual(len([e for e in read.events if e.author == "human"]), 1)
        self.assertIn("kind:hooks.additional_context", read.dropped)

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


class TestHealth(unittest.TestCase):
    """codex-cli 0.155.1 은 신뢰 안 된 훅을 메시지도 원장 행도 없이 건너뛴다 —
    이 진단이 그 상태를 행태 증거로 잡아낸다."""

    INSTALL_EPOCH = 1700000000.0  # 2023-11-14T22:13:20Z

    def _install_hook(self, home: str) -> str:
        directory = os.path.join(home, ".codex")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "hooks.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "omhc brief"}]}]}}, fh)
        os.utime(path, (self.INSTALL_EPOCH, self.INSTALL_EPOCH))
        return path

    def _rollout(self, home: str, session_id: str, iso_ts: str, *,
                 extra_meta=None, raw: bytes = None) -> str:
        stamp = time.gmtime()
        directory = os.path.join(
            home, ".codex", "sessions",
            time.strftime("%Y", stamp), time.strftime("%m", stamp),
            time.strftime("%d", stamp))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "rollout-{}.jsonl".format(session_id))
        if raw is not None:
            with open(path, "wb") as fh:
                fh.write(raw)
            return path
        payload = {"session_id": session_id, "cwd": REPO, "timestamp": iso_ts}
        payload.update(extra_meta or {})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "timestamp": iso_ts, "ordinal": 0, "type": "session_meta",
                "payload": payload,
            }) + "\n")
        return path

    def test_no_hook_installed_reports_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertEqual(rows, ())

    def test_fail_when_a_post_install_session_has_no_ledger_row(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z",
                          extra_meta={"originator": "codex_cli_rs"})
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertEqual(len(rows), 1)
            label, ok, detail = rows[0]
            self.assertIsInstance(label, str)
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(detail, str)
            self.assertEqual(label, "codex hook")
            self.assertFalse(ok)
            self.assertIn("1 consecutive Codex session", detail)
            self.assertIn("newest: codex_cli_rs", detail)
            self.assertIn("no trust entry", detail)

    def test_pass_when_the_ledger_has_a_matching_codex_row(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z")
            rows = CX.CodexCliAdapter(home=home).health(
                REPO, [{"harness": "codex-cli", "session": "s1", "event": "start"}])
            self.assertTrue(rows[0][1])
            self.assertIn("ran for the latest session", rows[0][2])

    def test_pass_when_only_an_older_pre_trust_session_is_missing(self):
        """리뷰 결함: 신뢰는 config.toml 을 바꾸지 hooks.json 을 바꾸지 않는다 —
        신뢰 이전 세션이 안 돈 채 남아 있어도, 신뢰 이후(최신) 세션이 돌았다면
        지금은 신뢰가 성립한 상태이므로 PASS 여야 한다."""
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "before-trust", "2023-11-15T00:00:00.000Z")
            self._rollout(home, "after-trust", "2023-11-16T00:00:00.000Z")
            rows = CX.CodexCliAdapter(home=home).health(
                REPO, [{"harness": "codex-cli", "session": "after-trust", "event": "start"}])
            self.assertTrue(rows[0][1])
            self.assertIn("ran for the latest session", rows[0][2])

    def test_fail_when_only_the_newest_session_is_missing(self):
        """오래된 세션들이 다 돌았어도, 가장 최신이 안 돌았으면 지금은 다시
        신뢰가 깨진 상태이므로 FAIL 이어야 한다."""
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "ran", "2023-11-15T00:00:00.000Z")
            self._rollout(home, "newest", "2023-11-16T00:00:00.000Z",
                          extra_meta={"originator": "codex_work_desktop"})
            rows = CX.CodexCliAdapter(home=home).health(
                REPO, [{"harness": "codex-cli", "session": "ran", "event": "start"}])
            self.assertFalse(rows[0][1])
            self.assertIn("1 consecutive Codex session", rows[0][2])
            self.assertIn("newest: codex_work_desktop", rows[0][2])
            self.assertIn("Claude→Codex is not delivered; Codex→Claude still works",
                          rows[0][2])

    def test_not_judged_when_the_rollout_predates_the_hook_install(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2020-01-01T00:00:00.000Z")
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertIsNone(rows[0][1])
            self.assertIn("not judged yet", rows[0][2])

    def test_a_scan_backfilled_ledger_row_does_not_count_as_ran(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z")
            rows = CX.CodexCliAdapter(home=home).health(
                REPO, [{"harness": "codex-cli", "session": "s1", "via": "scan",
                       "event": "start"}])
            self.assertFalse(rows[0][1])

    def test_a_non_start_ledger_row_does_not_count_as_ran(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z")
            rows = CX.CodexCliAdapter(home=home).health(
                REPO, [{"harness": "codex-cli", "session": "s1", "event": "pull"}])
            self.assertFalse(rows[0][1])

    def test_a_subagent_rollout_is_ignored(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z",
                          extra_meta={"thread_source": "subagent"})
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertIsNone(rows[0][1])
            self.assertIn("not judged yet", rows[0][2])

    def test_no_sessions_at_all_reports_the_install_date(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertIsNone(rows[0][1])
            self.assertIn("not judged yet", rows[0][2])
            self.assertIn(
                time.strftime("%Y-%m-%d", time.localtime(self.INSTALL_EPOCH)), rows[0][2])

    def test_only_headless_sessions_are_not_judged_but_named(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z",
                          extra_meta={"originator": "codex_exec"})
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertIsNone(rows[0][1])
            self.assertIn("only headless", rows[0][2])

    def test_headless_sessions_are_judged_normally_under_the_override(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z",
                          extra_meta={"originator": "codex_exec"})
            with mock.patch.dict(os.environ, {"OMHC_ALLOW_HEADLESS": "1"}):
                rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertFalse(rows[0][1])
            self.assertIn("1 consecutive Codex session", rows[0][2])

            with mock.patch.dict(os.environ, {"OMHC_ALLOW_HEADLESS": "1"}):
                rows = CX.CodexCliAdapter(home=home).health(
                    REPO, [{"harness": "codex-cli", "session": "s1", "event": "start"}])
            self.assertTrue(rows[0][1])

    def test_a_child_repo_under_a_non_git_parent_is_ignored(self):
        with tempfile.TemporaryDirectory() as home, \
             tempfile.TemporaryDirectory() as parent:
            self._install_hook(home)
            child = os.path.join(parent, "child")
            os.makedirs(child)
            _repo.git(child, "init", "-q")
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z",
                          extra_meta={"cwd": child})
            rows = CX.CodexCliAdapter(home=home).health(parent, [])
            self.assertIsNone(rows[0][1])
            self.assertIn("not judged yet", rows[0][2])

    def test_garbage_rollout_and_config_do_not_raise(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z",
                          raw=b"\x00\xff{not json\n\n\x80\x81")
            with open(os.path.join(home, ".codex", "config.toml"), "wb") as fh:
                fh.write(b"\xff\xfe garbage \x80\x81")
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertIsInstance(rows, tuple)

    def test_getmtime_failure_is_not_judged_unknown(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            with mock.patch.object(CX.os.path, "getmtime", side_effect=OSError("boom")):
                rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertIsNone(rows[0][1])
            self.assertIn("unknown", rows[0][2])

    def test_an_unexpected_exception_is_not_judged_unknown(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z")
            with mock.patch.object(CX, "session_meta", side_effect=RuntimeError("boom")):
                rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertIsNone(rows[0][1])
            self.assertIn("unknown", rows[0][2])

    def test_config_trust_entry_drops_the_static_hint(self):
        with tempfile.TemporaryDirectory() as home:
            hooks_path = self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z")
            with open(os.path.join(home, ".codex", "config.toml"), "w",
                     encoding="utf-8") as fh:
                fh.write('[hooks.state."{}:session_start:deadbeef"]\ntrusted = true\n'
                         .format(hooks_path))
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertFalse(rows[0][1])
            self.assertNotIn("no trust entry", rows[0][2])


class TestStatusIntegration(unittest.TestCase):
    """omhc status 가 어댑터 health 행을 실제로 접어 넣는지 — adapters.present()
    는 실제 $HOME 을 본다(AGENTS.md), 그래서 그 발견 자체는 고정시켜 두고
    이 테스트가 만드는 임시 $HOME 으로 health() 를 부르는지만 본다."""

    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.home = os.path.join(self.base.name, "home")
        self.repo = os.path.join(self.base.name, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        _repo.git(self.repo, "init", "-q")
        self.root = os.path.realpath(self.repo)

        # 실제 배포 조각 그대로 심는다(+ 실행 가능한 더미 바이너리) — `codex-cli
        # hooks` 행이 PASS 여야 아래 exit code 단정이 순수하게 health(`codex
        # hook`) 행만 증명한다(리뷰 결함: 예전엔 `omhc brief` 한 줄뿐이라 hooks
        # 행도 함께 FAIL 해서 어느 쪽이 code=1 을 냈는지 이 테스트가 증명하지
        # 못했다).
        _repo.plant_hook_install(self.home, "codex-cli")
        hooks_path = os.path.join(self.home, ".codex", "hooks.json")
        os.utime(hooks_path, (1700000000.0, 1700000000.0))

        sessions_dir = os.path.join(
            self.home, ".codex", "sessions", time.strftime("%Y/%m/%d", time.gmtime()))
        os.makedirs(sessions_dir)
        with open(os.path.join(sessions_dir, "rollout-s1.jsonl"), "w",
                 encoding="utf-8") as fh:
            fh.write(json.dumps({
                "timestamp": "2023-11-15T00:00:00.000Z", "ordinal": 0,
                "type": "session_meta",
                "payload": {"session_id": "s1", "cwd": self.root,
                           "timestamp": "2023-11-15T00:00:00.000Z"},
            }) + "\n")

        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

    def run_status(self, extra_args=()):
        out = io.StringIO()
        with mock.patch.object(cli.adapters, "present", return_value=["codex-cli"]):
            code = cli.cmd_status(
                cli.build_parser().parse_args(["status"] + list(extra_args)),
                home=self.home, out=out)
        return code, out.getvalue()

    def test_status_fails_when_the_codex_hook_never_ran(self):
        code, text = self.run_status()
        self.assertEqual(code, 1)
        line = next(l for l in text.splitlines() if "codex hook" in l)
        self.assertTrue(line.startswith("FAIL"))

    def test_json_carries_the_health_list(self):
        code, text = self.run_status(["--json"])
        payload = json.loads(text)
        self.assertIn("health", payload)
        self.assertEqual(payload["health"][0]["label"], "codex hook")
        self.assertFalse(payload["health"][0]["ok"])


class TestHealthMatchesAcrossNestedGitRoots(unittest.TestCase):
    """리뷰 결함: 세션은 워크트리 루트 자신의 repo 키로 원장에 기록되지만,
    status 는 그걸 감싼 상위 레포 키로 조회된다 — 필터가 레포로 걸리면 세션이
    영원히 "안 돈 것"으로 보인다. 실제 cmd_mark → cmd_status 경로로 재현한다."""

    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.home = os.path.join(self.base.name, "home")
        self.repo = os.path.join(self.base.name, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        _repo.git(self.repo, "init", "-q")
        self.root = os.path.realpath(self.repo)

        # 자기 .git 을 가진 중첩 디렉터리 — 실물 워크트리(.claude/worktrees/*)와
        # 같은 모양: resolve_repo_root 가 여기서 멈추고, 이 경로의 repo 키는
        # 상위 레포의 것과 다르다.
        self.nested = os.path.join(self.root, ".claude", "worktrees", "sub")
        os.makedirs(self.nested)
        _repo.git(self.nested, "init", "-q")

        hooks_dir = os.path.join(self.home, ".codex")
        os.makedirs(hooks_dir)
        hooks_path = os.path.join(hooks_dir, "hooks.json")
        with open(hooks_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "omhc brief"}]}]}}, fh)
        os.utime(hooks_path, (1700000000.0, 1700000000.0))

        sessions_dir = os.path.join(
            self.home, ".codex", "sessions", time.strftime("%Y/%m/%d", time.gmtime()))
        os.makedirs(sessions_dir)
        self.rollout_path = os.path.join(sessions_dir, "rollout-s1.jsonl")
        with open(self.rollout_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "timestamp": "2023-11-15T00:00:00.000Z", "ordinal": 0,
                "type": "session_meta",
                "payload": {"session_id": "s1", "cwd": self.nested,
                           "timestamp": "2023-11-15T00:00:00.000Z"},
            }) + "\n")

        self._cwd = os.getcwd()
        self.addCleanup(os.chdir, self._cwd)

    def _mark(self):
        os.chdir(self.nested)
        stdin = json.dumps({"cwd": self.nested, "transcript_path": self.rollout_path,
                            "session_id": "s1"})
        out = io.StringIO()
        code = cli.cmd_mark(
            cli.build_parser().parse_args(
                ["mark", "--harness", "codex-cli", "--stdin", stdin]),
            home=self.home, out=out)
        self.assertEqual(code, 0)

    def test_a_nested_worktrees_ledger_row_still_counts_as_ran(self):
        """mark 는 워크트리 쪽 cwd 로 불려 자기 repo 키(다른 키)로 기록한다;
        status 는 상위 레포에서 불린다. 필터가 레포로 걸리면 이 행이 사라진다."""
        self._mark()

        from omhc import ledger, locate

        rows = ledger.read(home=self.home)
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["repo"], locate.repo_key(self.root))

        os.chdir(self.root)
        out = io.StringIO()
        with mock.patch.object(cli.adapters, "present", return_value=["codex-cli"]):
            code = cli.cmd_status(
                cli.build_parser().parse_args(["status"]), home=self.home, out=out)
        line = next(l for l in out.getvalue().splitlines() if "codex hook" in l)
        self.assertTrue(line.startswith("PASS"), out.getvalue())


class TestHealthLedgerWindow(unittest.TestCase):
    """리뷰 결함: ledger.read 의 기본 limit(2000, 머신 전체 공유)이 다른 레포의
    행으로 채워지면 이 레포/세션의 행이 창 밖으로 밀려날 수 있다. health 에
    넘기는 원장은 무제한으로 읽어야 한다."""

    def test_more_than_2000_rows_from_another_repo_do_not_hide_a_match(self):
        with tempfile.TemporaryDirectory() as home:
            hooks_dir = os.path.join(home, ".codex")
            os.makedirs(hooks_dir)
            hooks_path = os.path.join(hooks_dir, "hooks.json")
            with open(hooks_path, "w", encoding="utf-8") as fh:
                json.dump({"hooks": {"SessionStart": [
                    {"hooks": [{"type": "command", "command": "omhc brief"}]}]}}, fh)
            os.utime(hooks_path, (1700000000.0, 1700000000.0))

            sessions_dir = os.path.join(
                home, ".codex", "sessions", time.strftime("%Y/%m/%d", time.gmtime()))
            os.makedirs(sessions_dir)
            with open(os.path.join(sessions_dir, "rollout-s1.jsonl"), "w",
                     encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "timestamp": "2023-11-15T00:00:00.000Z", "ordinal": 0,
                    "type": "session_meta",
                    "payload": {"session_id": "s1", "cwd": REPO,
                               "timestamp": "2023-11-15T00:00:00.000Z"},
                }) + "\n")

            from omhc import ledger

            for i in range(2500):
                ledger.append({"repo": "some-other-repo", "harness": "codex-cli",
                               "session": "unrelated-{}".format(i), "event": "start",
                               "epoch": i, "path": "", "cwd": "/nope"}, home=home)
            ledger.append({"repo": "some-other-repo", "harness": "codex-cli",
                           "session": "s1", "event": "start", "epoch": 3000,
                           "path": "", "cwd": "/nope"}, home=home)

            all_rows = ledger.read(home=home, limit=0)
            self.assertEqual(len(all_rows), 2501)
            rows = CX.CodexCliAdapter(home=home).health(REPO, all_rows)
            self.assertTrue(rows[0][1])
            self.assertIn("ran for the latest session", rows[0][2])


class TestExecOutcomeHeaderOnly(unittest.TestCase):
    def test_exit_line_in_the_body_does_not_override_a_running_header(self):
        # 본문(Output: 뒤)은 프로그램 출력이다. 거기 찍힌 "Exit code: 0" 이
        # "running" 헤더를 이기면 write_stdin 의 실패가 원래 이벤트에 닿지 않는다.
        out = CX._parse_exec_outcome(
            "Chunk ID: x\nWall time: 10 seconds\nProcess running with session ID 7\n"
            "Original token count: 5\nOutput:\n[step] Exit code: 0\nExit code: 0\n")
        self.assertIsNone(out.ok)
        self.assertEqual(out.session_id, "7")


class TestSubagentRolloutWithTwoMetaLines(unittest.TestCase):
    """실측(0.155.1 sandbox): 서브에이전트 rollout 은 session_meta 가 두 줄이다 —
    첫 줄이 자기 것(source.subagent·thread_source·parent_thread_id), 둘째 줄이
    부모의 것 — 그리고 부모의 사람 프롬프트를 user.text 로 다시 담는다. 첫 줄의
    서브에이전트 표식만이 그 프롬프트가 GOAL 로 세탁되는 것을 막는다."""

    def test_the_first_meta_line_decides_and_it_is_never_a_source(self):
        with tempfile.TemporaryDirectory() as home:
            day = os.path.join(home, ".codex", "sessions", "2026", "09", "24")
            os.makedirs(day)
            path = os.path.join(day, "rollout-2026-09-24T14-00-00-sub.jsonl")
            rows = [
                {"type": "session_meta", "payload": {
                    "id": "sub", "session_id": "parent", "cwd": REPO,
                    "timestamp": "2026-09-24T05:00:00Z",
                    "source": {"subagent": {"thread_spawn": {}}},
                    "thread_source": "subagent", "parent_thread_id": "parent"}},
                {"type": "session_meta", "payload": {
                    "id": "parent", "session_id": "parent", "cwd": REPO,
                    "timestamp": "2026-09-24T04:59:00Z", "source": "cli",
                    "originator": "codex-tui"}},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "PARENT_GOAL 이거 고쳐줘"}],
                    "internal_chat_message_metadata_passthrough": {
                        "content_item_kinds": ["user.text"]}}},
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            adapter = CX.CodexCliAdapter(home=home)
            self.assertFalse(adapter.classify(path))
            self.assertIsNone(adapter.ref_for_path(path, "parent"))
            self.assertEqual(adapter.list_sessions(REPO), [])
