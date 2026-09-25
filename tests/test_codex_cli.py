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
    """era B: parsed_cmd 가 전부 읽기이고 출력도 비면 exit 1 은 관용구다(실측 3건).
    검증용 grep -q 는 보통 parsed_cmd 가 unknown 이라 여기 안 걸린다. 더 넓히지
    않는 이유는 _item_fact 의 주석(#11)."""

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

    def test_several_inspect_entries_with_empty_output_are_ok(self):
        # rg … && sed … && sed … 처럼 전부 읽기이고 출력이 없으면 관용구다.
        read = self._read(self._ce(1, [{"type": "search", "cmd": "rg foo"},
                                       {"type": "read", "cmd": "sed -n 1p a"},
                                       {"type": "read", "cmd": "sed -n 1p b"}]))
        self.assertEqual([e.ok for e in read.events], [True])

    def test_missing_file_before_a_search_stays_a_failure(self):
        # sed P && rg … — && 가 끊겨 sed 의 실패가 종료 코드다. 출력이 있으니 실패.
        read = self._read(self._ce(
            1, [{"type": "read", "cmd": "sed -n 1p missing"},
                {"type": "search", "cmd": "rg foo"}],
            stdout="sed: missing: No such file or directory"))
        self.assertEqual([e.ok for e in read.events], [False])

    def test_list_then_search_with_output_stays_a_failure(self):
        # ls -la; rg --files -g … — ls 의 출력이 있어 무해한지 구조만으로는 가를 수
        # 없다. 알면서 두는 누락이다(#11).
        read = self._read(self._ce(
            1, [{"type": "list_files", "cmd": "ls -la"},
                {"type": "list_files", "cmd": "rg --files -g x"}],
            stdout="total 0\ndrwxr-xr-x  2 u  g  64 .\n"))
        self.assertEqual([e.ok for e in read.events], [False])

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
                {"hooks": [{"type": "command",
                            "command": "omhc brief --harness codex-cli"}]}]}}, fh)

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

    def test_install_handoff_collapses_a_stale_agents_md_block(self):
        """#36: Path A(훅) 가 성공하면 그 옆의 낡은 Path B 구간을 즉시
        붕괴시킨다 — 안 그러면 다음 Codex 세션이 신선한 훅 핸드오프와 낡은
        AGENTS.md 지시를 동시에 읽는다."""
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = os.path.join(base, "proj")
            os.makedirs(root)
            _repo.git(root, "init", "-q")
            self._install_hook(home)
            managed_block.splice(agents_md.path_for(root), "[omhc] stale handoff\n",
                                 captured_at=1000.0)

            bundle = A.HandoffBundle(body_md="[omhc] fresh\n", repo_root=root,
                                     to_adapter_id="codex-cli")
            CX.CodexCliAdapter(home=home).install_handoff(bundle)

            self.assertIsNone(managed_block.installed_captured_at(agents_md.path_for(root)))

    def test_install_handoff_never_touches_agents_md_shared_with_claude(self):
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = os.path.join(base, "proj")
            os.makedirs(root)
            _repo.git(root, "init", "-q")
            self._install_hook(home)
            agents_path = agents_md.path_for(root)
            claude_path = os.path.join(root, "CLAUDE.md")
            managed_block.splice(agents_path, "[omhc] stale handoff\n", captured_at=1000.0)
            os.symlink(agents_path, claude_path)

            bundle = A.HandoffBundle(body_md="[omhc] fresh\n", repo_root=root,
                                     to_adapter_id="codex-cli")
            CX.CodexCliAdapter(home=home).install_handoff(bundle)

            self.assertIsNotNone(managed_block.installed_captured_at(agents_path))


class TestInlineTomlHooks(unittest.TestCase):
    """#32: config.toml 의 인라인 `[[hooks.SessionStart]]` 도 hooks.json 과
    같은 자격으로 훅 설치로 친다."""

    def _install_toml(self, home: str) -> None:
        directory = os.path.join(home, ".codex")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(
                '[[hooks.SessionStart]]\n'
                '\n'
                '[[hooks.SessionStart.hooks]]\n'
                'type = "command"\n'
                'command = "omhc brief --harness codex-cli"\n'
            )

    def _install_json(self, home: str) -> None:
        directory = os.path.join(home, ".codex")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "hooks.json"), "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command",
                            "command": "omhc brief --harness codex-cli"}]}]}}, fh)

    def test_inline_only_is_installed(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_toml(home)
            self.assertTrue(CX.CodexCliAdapter(home=home).hook_is_installed())

    def test_inline_only_install_handoff_succeeds(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_toml(home)
            receipt = CX.CodexCliAdapter(home=home).install_handoff(
                A.HandoffBundle(body_md="[omhc] hi\n", repo_root=REPO,
                               to_adapter_id="codex-cli"))
            self.assertTrue(receipt.paths_written)

    def test_neither_is_not_installed(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertFalse(CX.CodexCliAdapter(home=home).hook_is_installed())

    def _make_bin(self, home: str) -> None:
        bin_path = os.path.join(home, ".local", "bin", "omhc")
        os.makedirs(os.path.dirname(bin_path), exist_ok=True)
        with open(bin_path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(bin_path, 0o755)

    def _shipped_hooks(self, home: str):
        # hooks/codex-hooks.json 이 배포하는 정확한 모양(mark + brief --wire
        # claude) — hooks_status()/inspect 는 이 전체와 비교하지, has_runnable_call
        # 처럼 brief 하나만 보지 않는다.
        bin_path = os.path.join(home, ".local", "bin", "omhc")
        return [
            {"type": "command", "command": "{} mark --harness codex-cli".format(bin_path)},
            {"type": "command",
             "command": "{} brief --harness codex-cli --wire claude".format(bin_path)},
        ]

    def _install_full_json(self, home: str) -> None:
        directory = os.path.join(home, ".codex")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "hooks.json"), "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": self._shipped_hooks(home)}]}}, fh)

    def _install_full_toml(self, home: str) -> None:
        directory = os.path.join(home, ".codex")
        os.makedirs(directory, exist_ok=True)
        bin_path = os.path.join(home, ".local", "bin", "omhc")
        with open(os.path.join(directory, "config.toml"), "a", encoding="utf-8") as fh:
            fh.write(
                '[[hooks.SessionStart]]\n'
                '\n'
                '[[hooks.SessionStart.hooks]]\n'
                'type = "command"\n'
                'command = "{bin} mark --harness codex-cli"\n'
                '\n'
                '[[hooks.SessionStart.hooks]]\n'
                'type = "command"\n'
                'command = "{bin} brief --harness codex-cli --wire claude"\n'
                .format(bin=bin_path))

    def test_hooks_status_pass_via_hooks_json_only(self):
        with tempfile.TemporaryDirectory() as home:
            self._make_bin(home)
            self._install_full_json(home)
            ok, detail = CX.CodexCliAdapter(home=home).hooks_status()
            self.assertTrue(ok, detail)
            self.assertIn("hooks.json", detail)

    def test_hooks_status_pass_via_inline_only(self):
        with tempfile.TemporaryDirectory() as home:
            self._make_bin(home)
            self._install_full_toml(home)
            ok, detail = CX.CodexCliAdapter(home=home).hooks_status()
            self.assertTrue(ok, detail)
            self.assertIn("config.toml", detail)

    def test_hooks_status_warns_when_both_present(self):
        with tempfile.TemporaryDirectory() as home:
            self._make_bin(home)
            self._install_full_json(home)
            self._install_full_toml(home)
            ok, detail = CX.CodexCliAdapter(home=home).hooks_status()
            self.assertIsNone(ok)
            self.assertIn("both", detail)

    def test_hooks_status_fail_when_neither_present(self):
        with tempfile.TemporaryDirectory() as home:
            ok, detail = CX.CodexCliAdapter(home=home).hooks_status()
            self.assertFalse(ok)

    def test_garbage_config_toml_fails_open(self):
        with tempfile.TemporaryDirectory() as home:
            directory = os.path.join(home, ".codex")
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write("not { valid toml at all !!!\n[[[broken\n")
            self.assertFalse(CX.CodexCliAdapter(home=home).hook_is_installed())

    def _write_projects_trust(self, home: str, repo: str, trust_level: str) -> None:
        directory = os.path.join(home, ".codex")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "config.toml"), "a", encoding="utf-8") as fh:
            fh.write('\n[projects."{}"]\ntrust_level = "{}"\n'.format(
                os.path.realpath(repo), trust_level))

    def test_project_trust_body_end_is_not_fooled_by_a_multiline_array_bracket(self):
        # 리뷰 #3 재현: 예전엔 project 본문의 끝을 `^[ \t]*\[` 로 다시 찾았는데,
        # 이건 #31 이 이미 걸러낸 "여러 줄 배열 값의 원소도 줄 맨 앞에 `[`
        # 로 올 수 있다" 문제를 그대로 반복한다 — 공유 스캐너를 쓰면
        # trust_level 이 (가짜 헤더로 오인된 배열 원소 앞이 아니라) 진짜
        # 다음 헤더 전까지 온전히 본문으로 잡혀야 한다.
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as repo:
            directory = os.path.join(home, ".codex")
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write(
                    '[projects."{}"]\n'
                    'ignored = [\n'
                    '  "a",\n'
                    ']\n'
                    'trust_level = "trusted"\n'.format(os.path.realpath(repo)))
            self.assertEqual(
                CX.CodexCliAdapter(home=home)._project_trust_level(repo), "trusted")

    def test_project_level_hooks_json_counts_when_trusted(self):
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as repo:
            self._write_projects_trust(home, repo, "trusted")
            project_dir = os.path.join(repo, ".codex")
            os.makedirs(project_dir, exist_ok=True)
            with open(os.path.join(project_dir, "hooks.json"), "w", encoding="utf-8") as fh:
                json.dump({"hooks": {"SessionStart": [
                    {"hooks": [{"type": "command",
                                "command": "omhc brief --harness codex-cli"}]}]}}, fh)
            self.assertTrue(CX.CodexCliAdapter(home=home).hook_is_installed(repo))

    def test_project_level_hooks_json_ignored_when_untrusted(self):
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as repo:
            self._write_projects_trust(home, repo, "untrusted")
            project_dir = os.path.join(repo, ".codex")
            os.makedirs(project_dir, exist_ok=True)
            with open(os.path.join(project_dir, "hooks.json"), "w", encoding="utf-8") as fh:
                json.dump({"hooks": {"SessionStart": [
                    {"hooks": [{"type": "command",
                                "command": "omhc brief --harness codex-cli"}]}]}}, fh)
            self.assertFalse(CX.CodexCliAdapter(home=home).hook_is_installed(repo))

    def test_project_level_ignored_when_trust_unknown(self):
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as repo:
            # ~/.codex/config.toml 에 이 레포에 대한 [projects."..."] 항목이 아예 없다
            project_dir = os.path.join(repo, ".codex")
            os.makedirs(project_dir, exist_ok=True)
            with open(os.path.join(project_dir, "hooks.json"), "w", encoding="utf-8") as fh:
                json.dump({"hooks": {"SessionStart": [
                    {"hooks": [{"type": "command",
                                "command": "omhc brief --harness codex-cli"}]}]}}, fh)
            self.assertFalse(CX.CodexCliAdapter(home=home).hook_is_installed(repo))

    def test_hooks_install_skips_duplicate_when_inline_already_installed(self):
        with tempfile.TemporaryDirectory() as home:
            self._make_bin(home)
            self._install_full_toml(home)
            out = io.StringIO()
            code = cli.main(["hooks", "install", "--harness", "codex-cli"], home=home, out=out)
            self.assertEqual(code, 0)
            self.assertIn("already up to date", out.getvalue())
            self.assertFalse(os.path.exists(os.path.join(home, ".codex", "hooks.json")))

    def test_hooks_status_reports_differs_not_not_found_for_a_partial_inline_install(self):
        # 리뷰 #1 재현: brief 는 있지만 mark 도 --wire claude 도 없는 인라인
        # 설치 — "존재하지만 배포 조각과 다르다" 이지 "없다" 가 아니다.
        with tempfile.TemporaryDirectory() as home:
            self._install_toml(home)  # brief 만, mark 없음, --wire claude 없음
            ok, detail = CX.CodexCliAdapter(home=home).hooks_status()
            self.assertFalse(ok)
            self.assertNotIn("not found", detail)
            self.assertIn("differs from shipped fragment", detail)

    def test_hooks_install_does_not_duplicate_a_partial_inline_install(self):
        # 리뷰 #1 재현: 부분 인라인 설치 위에 `omhc hooks install` 이 hooks.json
        # 을 겹쳐 쓰면 Codex 가 두 층을 다 읽고 경고하며, 인라인 쪽은 여전히
        # 매 세션 실패한다 — 대신 hooks.json 을 쓰지 않고 실패로 보고해야
        # 한다.
        with tempfile.TemporaryDirectory() as home:
            self._make_bin(home)
            self._install_toml(home)  # brief 만 있는 부분 인라인 설치
            out = io.StringIO()
            code = cli.main(["hooks", "install", "--harness", "codex-cli"], home=home, out=out)
            self.assertNotEqual(code, 0)
            self.assertIn("differs", out.getvalue())
            self.assertFalse(os.path.exists(os.path.join(home, ".codex", "hooks.json")))
            # 재확인해도 여전히 "설치 안 됨" 이 아니라 "다르다" 로 보고돼야 한다.
            ok, detail = CX.CodexCliAdapter(home=home).hooks_status()
            self.assertFalse(ok)
            self.assertNotIn("not found", detail)


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
                {"hooks": [{"type": "command", "command": "omhc brief --harness codex-cli"}]}]}}, fh)
        os.utime(path, (self.INSTALL_EPOCH, self.INSTALL_EPOCH))
        return path

    def _install_toml_hook(self, home: str) -> str:
        directory = os.path.join(home, ".codex")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "config.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                '[[hooks.SessionStart]]\n'
                '\n'
                '[[hooks.SessionStart.hooks]]\n'
                'type = "command"\n'
                'command = "omhc brief --harness codex-cli"\n'
            )
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
            self.assertIn("no hooks.state trust hash", detail)

    def test_pass_when_the_ledger_has_a_matching_codex_row(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z")
            rows = CX.CodexCliAdapter(home=home).health(
                REPO, [{"harness": "codex-cli", "session": "s1", "event": "start"}])
            self.assertTrue(rows[0][1])
            self.assertIn("ran for the latest session", rows[0][2])

    def test_inline_only_install_is_judged_from_config_toml_mtime(self):
        # #32 리뷰 1 재현: 예전엔 install_epoch 을 언제나 hooks.json 의 mtime
        # 으로 삼아서, 인라인 전용 설치(hooks.json 자체가 없다)에서 이 stat
        # 이 ENOENT 로 죽어 이 행이 매번 `----(unknown)` 으로만 남았다 —
        # 신뢰 안 된 인라인 훅이 조용히 스킵되는 걸 잡아야 할 행이 제 역할을
        # 못 했다. 이제 실제로 설치된 파일(config.toml)의 mtime 을 쓴다.
        with tempfile.TemporaryDirectory() as home:
            self._install_toml_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z")
            rows = CX.CodexCliAdapter(home=home).health(
                REPO, [{"harness": "codex-cli", "session": "s1", "event": "start"}])
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0][1], rows[0][2])
            self.assertIn("ran for the latest session", rows[0][2])

    def test_inline_only_install_fails_when_a_later_session_has_no_hook_row(self):
        with tempfile.TemporaryDirectory() as home:
            self._install_toml_hook(home)
            self._rollout(home, "s1", "2023-11-15T00:00:00.000Z",
                          extra_meta={"originator": "codex_cli_rs"})
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertEqual(len(rows), 1)
            label, ok, detail = rows[0]
            self.assertEqual(label, "codex hook")
            self.assertFalse(ok)
            self.assertIn("1 consecutive Codex session", detail)
            # 인라인 설치는 hooks.json 의 hooks.state 신뢰 해시와 다른 메커니즘
            # 이다 — 그 사실이 힌트로만 남아야지 확정 진단으로 말하면 안 된다.
            self.assertIn("hint, not a diagnosis", detail)

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
            label, ok, detail = next(r for r in rows if r[0] == "codex hook")
            self.assertIsNone(ok)
            self.assertIn("not judged yet", detail)

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
            self.assertNotIn("no hooks.state trust hash", rows[0][2])


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
        row = next(h for h in payload["health"] if h["label"] == "codex hook")
        self.assertFalse(row["ok"])


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
                {"hooks": [{"type": "command", "command": "omhc brief --harness codex-cli"}]}]}}, fh)
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


class TestHealthMatchesAcrossOmhcRootMarker(unittest.TestCase):
    """#12: git 이 아닌 프로젝트(`.omhc-root`)에서도 서브디렉터리에서 시작한
    세션이 `codex hook` 행에서 "안 돈 것"으로 사라지면 안 된다."""

    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.home = os.path.join(self.base.name, "home")
        self.root = os.path.join(self.base.name, "proj")
        os.makedirs(self.home)
        os.makedirs(self.root)
        open(os.path.join(self.root, ".omhc-root"), "w").close()
        self.sub = os.path.join(self.root, "sub")
        os.makedirs(self.sub)

        hooks_dir = os.path.join(self.home, ".codex")
        os.makedirs(hooks_dir)
        hooks_path = os.path.join(hooks_dir, "hooks.json")
        with open(hooks_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "omhc brief --harness codex-cli"}]}]}}, fh)
        os.utime(hooks_path, (1700000000.0, 1700000000.0))

        sessions_dir = os.path.join(
            self.home, ".codex", "sessions", time.strftime("%Y/%m/%d", time.gmtime()))
        os.makedirs(sessions_dir)
        self.rollout_path = os.path.join(sessions_dir, "rollout-s1.jsonl")
        with open(self.rollout_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "timestamp": "2023-11-15T00:00:00.000Z", "ordinal": 0,
                "type": "session_meta",
                "payload": {"session_id": "s1", "cwd": self.sub,
                           "timestamp": "2023-11-15T00:00:00.000Z"},
            }) + "\n")

        self._cwd = os.getcwd()
        self.addCleanup(os.chdir, self._cwd)

    def test_a_subdirectory_session_still_counts_as_ran(self):
        os.chdir(self.sub)
        stdin = json.dumps({"cwd": self.sub, "transcript_path": self.rollout_path,
                            "session_id": "s1"})
        out = io.StringIO()
        code = cli.cmd_mark(
            cli.build_parser().parse_args(
                ["mark", "--harness", "codex-cli", "--stdin", stdin]),
            home=self.home, out=out)
        self.assertEqual(code, 0)

        os.chdir(self.root)
        out = io.StringIO()
        with mock.patch.object(cli.adapters, "present", return_value=["codex-cli"]):
            code = cli.cmd_status(
                cli.build_parser().parse_args(["status"]), home=self.home, out=out)
        line = next(l for l in out.getvalue().splitlines() if "codex hook" in l)
        self.assertTrue(line.startswith("PASS"), out.getvalue())


class TestFirstTableHeader(unittest.TestCase):
    """리뷰: 줄 맨 앞의 `[` 가 모두 섹션 머리는 아니다."""

    def test_headers_and_non_headers(self):
        F = CX._first_table_header
        self.assertGreaterEqual(F('a = 1\n[tui]\n'), 0)
        self.assertGreaterEqual(F('a = 1\n  [projects."/a b"]\n'), 0)
        self.assertGreaterEqual(F('[[mcp]]\n'), 0)
        self.assertGreaterEqual(F('x = [\n  ".git",\n]\n[t]\n'), 0)
        self.assertEqual(F('other = [\n  ["a", "b"],\n]\nk = 1\n'), -1)
        self.assertEqual(F('s = """\n[x]\n"""\n'), -1)
        self.assertEqual(F('# [c]\nk = 1\n'), -1)
        # basic 여러 줄 문자열 안의 \""" 는 끝이 아니다.
        self.assertGreaterEqual(F('a = """x \\""" y"""\n[t]\n'), 0)

    def test_a_nested_array_value_is_unparseable(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "c.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('project_root_markers = [\n  [".omhc-root"],\n]\n')
            state, _ = CX.CodexCliAdapter(home=home)._codex_root_markers(path)
            self.assertEqual(state, "unparseable")

    def test_a_nested_array_element_does_not_hide_a_later_top_level_key(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".codex"))
            path = os.path.join(home, ".codex", "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('other = [\n  ["a", "b"],\n]\n'
                         'project_root_markers = [".git", ".omhc-root"]\n')
            state, markers = CX.CodexCliAdapter(home=home)._codex_root_markers(path)
            self.assertEqual(state, "ok")
            self.assertIn(".omhc-root", markers)


class TestRootMarkerHealth(unittest.TestCase):
    """#31: `.omhc-root` 로만 정해진 프로젝트(=`.git` 없음)에서 Codex 의
    `project_root_markers` 설정이 `.omhc-root` 를 포함하는지 진단한다."""

    def _omhc_root_repo(self, base: str) -> str:
        root = os.path.join(base, "proj")
        os.makedirs(root)
        open(os.path.join(root, ".omhc-root"), "w").close()
        return root

    def _row(self, rows):
        return next((r for r in rows if r[0] == "codex root markers"), None)

    def test_git_repo_gets_no_row(self):
        with tempfile.TemporaryDirectory() as home:
            rows = CX.CodexCliAdapter(home=home).health(REPO, [])
            self.assertIsNone(self._row(rows))

    def test_no_repo_root_gets_no_row(self):
        with tempfile.TemporaryDirectory() as home:
            rows = CX.CodexCliAdapter(home=home).health(None, [])
            self.assertIsNone(self._row(rows))

    def _write_config(self, home: str, text: str, *, encoding: str = "utf-8") -> None:
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        with open(os.path.join(home, ".codex", "config.toml"), "w",
                 encoding=encoding) as fh:
            fh.write(text)

    def test_missing_config_is_uninformative_not_a_fail(self):
        """리뷰 #1: Path A(훅 설치)가 정상인 보통 설정에서 이 설정은 아무 효과가
        없으므로 게이팅하면 안 된다 — PASS 아니면 언제나 `----`."""
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertIsNone(ok)
            self.assertIn("not found", detail)
            self.assertIn('project_root_markers = [".git", ".omhc-root"]', detail)

    def test_config_without_the_key_is_uninformative(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            self._write_config(home, 'model = "gpt-5"\n')
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertIsNone(ok)
            self.assertIn("lacks", detail)

    def test_key_without_the_marker_is_uninformative(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            self._write_config(home, 'project_root_markers = [".git"]\n')
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertIsNone(ok)
            self.assertIn("lacks", detail)

    def test_multi_line_array_with_the_marker_passes(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            self._write_config(
                home, 'project_root_markers = [\n  ".git",\n  ".omhc-root",\n]\n')
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertTrue(ok)
            self.assertIn(".omhc-root", detail)

    def test_garbage_config_cannot_be_judged(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            self._write_config(home, 'project_root_markers = [1, 2, {nested = true}]\n')
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertIsNone(ok)
            self.assertIn("cannot judge", detail)

    def test_key_inside_a_table_is_not_top_level(self):
        """리뷰 #2: `[table]` 뒤의 키는 그 테이블에 스코프돼 최상위 키가
        아니다 — 예를 들어 신뢰 테이블 뒤에 사람이 실수로 이어 붙인 경우."""
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            self._write_config(
                home,
                '[projects."/some/path"]\n'
                'trusted = true\n'
                'project_root_markers = [".git", ".omhc-root"]\n')
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertIsNone(ok)
            self.assertIn("[section]", detail)
            self.assertIn("above the first [section]", detail)

    def test_bom_prefixed_config_is_still_read(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            self._write_config(
                home, 'project_root_markers = [".git", ".omhc-root"]\n',
                encoding="utf-8-sig")
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertTrue(ok)

    def test_quoted_key_and_single_quoted_strings_are_recognized(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            self._write_config(home, "\"project_root_markers\" = ['.git', '.omhc-root']\n")
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertTrue(ok)

    def test_unreadable_config_is_distinct_from_missing(self):
        """디렉터리를 그 자리에 두면 open() 이 IsADirectoryError(OSError) 를
        낸다 — FileNotFoundError 와 다른 사유로 구분돼야 한다."""
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            os.makedirs(os.path.join(home, ".codex"))
            os.makedirs(os.path.join(home, ".codex", "config.toml"))
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertIsNone(ok)
            self.assertIn("cannot read", detail)
            self.assertNotIn("not found", detail)

    def test_an_ancestor_git_repo_makes_the_row_disappear(self):
        """리뷰 #4: `repo_root` 위에 `.git` 조상이 있으면 Codex 기본값으로도
        그 조상에서부터 AGENTS.md 를 cwd 까지 읽으므로 마커를 더할 필요가
        없다."""
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            _repo.git(base, "init", "-q")
            root = self._omhc_root_repo(base)
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            self.assertIsNone(self._row(rows))

    def test_missing_config_notes_it_is_the_only_channel_when_the_hook_is_not_installed(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            _label, _ok, detail = self._row(rows)
            self.assertIn("isn't installed", detail)
            self.assertIn("only channel", detail)

    def test_missing_config_notes_path_a_covers_it_when_the_hook_is_installed(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            os.makedirs(os.path.join(home, ".codex"))
            with open(os.path.join(home, ".codex", "hooks.json"), "w",
                     encoding="utf-8") as fh:
                json.dump({"hooks": {"SessionStart": [
                    {"hooks": [{"type": "command",
                                "command": "omhc brief --harness codex-cli"}]}]}}, fh)
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            _label, _ok, detail = self._row(rows)
            self.assertIn("Path A currently delivers", detail)

    def test_status_json_key_set_is_unchanged(self):
        """새 행은 `health` 리스트 안의 원소일 뿐, status --json 의 최상위 키
        집합을 늘리지 않는다(#19 의 계약)."""
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._omhc_root_repo(base)
            out_root = io.StringIO()
            cwd = os.getcwd()
            os.chdir(root)
            try:
                with mock.patch.object(cli.adapters, "present",
                                       return_value=["codex-cli"]):
                    cli.cmd_status(
                        cli.build_parser().parse_args(["status", "--json"]),
                        home=home, out=out_root)
            finally:
                os.chdir(cwd)
            payload = json.loads(out_root.getvalue())
            self.assertEqual(set(payload), set(cli._status_json_empty()))
            self.assertTrue(any(h["label"] == "codex root markers"
                                for h in payload["health"]))


class TestAgentsMdBudget(unittest.TestCase):
    """#33: Codex 는 AGENTS.md 를 `project_doc_max_bytes` 만큼만 머리부터
    읽는다 — 예산을 넘겨 설치하려는 Path B 는 claim 하지 말고 outbox 로
    떨어뜨려야 하고, status 는 이미 넘겨 설치된 구간을 알려야 한다."""

    def _write_config(self, home: str, text: str) -> None:
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        with open(os.path.join(home, ".codex", "config.toml"), "w",
                 encoding="utf-8") as fh:
            fh.write(text)

    def _repo(self, base: str) -> str:
        root = os.path.join(base, "proj")
        os.makedirs(root)
        _repo.git(root, "init", "-q")
        return root

    def test_default_limit_when_config_is_missing(self):
        with tempfile.TemporaryDirectory() as home:
            adapter = CX.CodexCliAdapter(home=home)
            self.assertEqual(adapter._project_doc_max_bytes(adapter.toml_config_path()),
                             CX.DEFAULT_PROJECT_DOC_MAX_BYTES)

    def test_reads_the_configured_limit(self):
        with tempfile.TemporaryDirectory() as home:
            self._write_config(home, "project_doc_max_bytes = 4096\n")
            adapter = CX.CodexCliAdapter(home=home)
            self.assertEqual(adapter._project_doc_max_bytes(adapter.toml_config_path()), 4096)

    def test_key_inside_a_table_is_not_top_level(self):
        with tempfile.TemporaryDirectory() as home:
            self._write_config(
                home, '[projects."/x"]\nproject_doc_max_bytes = 4096\n')
            adapter = CX.CodexCliAdapter(home=home)
            self.assertEqual(adapter._project_doc_max_bytes(adapter.toml_config_path()),
                             CX.DEFAULT_PROJECT_DOC_MAX_BYTES)

    def test_install_declines_and_falls_to_outbox_when_over_budget(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            self._write_config(home, "project_doc_max_bytes = 10\n")
            bundle = A.HandoffBundle(body_md="[omhc] handoff\nGOAL  x\n",
                                     repo_root=root, to_adapter_id="codex-cli")
            adapter = CX.CodexCliAdapter(home=home)
            with self.assertRaises(A.NoInjectionChannel):
                adapter._install_agents_md(bundle)
            self.assertFalse(os.path.exists(os.path.join(root, "AGENTS.md")))
            log_path = os.path.join(home, ".omhc", "guard.log")
            with open(log_path, encoding="utf-8") as fh:
                self.assertIn("project_doc_max_bytes", fh.read())

    def test_deliver_falls_all_the_way_to_the_outbox_when_over_budget(self):
        """라우터(deliver)까지 통째로 — install_handoff 도 없고(훅 미설치) Path
        B 도 예산 초과로 거절되면 보편 바닥(outbox)에 떨어져야 한다."""
        from omhc import deliver

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            self._write_config(home, "project_doc_max_bytes = 10\n")
            bundle = A.HandoffBundle(body_md="[omhc] handoff\nGOAL  x\n",
                                     repo_root=root, to_adapter_id="codex-cli")
            receipt = deliver.deliver(bundle, home=home, now=1000.0)
            self.assertEqual(receipt.channel, "file-drop")
            self.assertFalse(os.path.exists(os.path.join(root, "AGENTS.md")))

    def test_install_succeeds_under_budget(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            bundle = A.HandoffBundle(body_md="[omhc] handoff\nGOAL  x\n",
                                     repo_root=root, to_adapter_id="codex-cli")
            adapter = CX.CodexCliAdapter(home=home)
            receipt = adapter._install_agents_md(bundle)
            self.assertEqual(receipt.channel, "agents-md")
            self.assertTrue(os.path.exists(os.path.join(root, "AGENTS.md")))

    def _row(self, rows):
        return next((r for r in rows if r[0] == "codex agents.md budget"), None)

    def test_status_row_is_uninformative_without_an_installed_block(self):
        """AGENTS.md 의 status 관례: 판정 가능한 진단은 판정할 것이 없어도
        `----` 로 행을 낸다(SKIP 이 아니다) — 아예 무의미한 레포(공유됨)만
        행을 생략한다(아래 test_status_row_is_absent_when_shared_with_claude)."""
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertIsNone(ok)
            self.assertIn("no omhc block", detail)

    def test_status_row_is_absent_when_shared_with_claude(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            agents = os.path.join(root, "AGENTS.md")
            claude = os.path.join(root, "CLAUDE.md")
            with open(agents, "w", encoding="utf-8") as fh:
                fh.write("neutral instructions")
            os.symlink(agents, claude)
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            self.assertIsNone(self._row(rows))

    def test_status_row_fails_when_the_installed_block_is_over_budget(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            self._write_config(home, "project_doc_max_bytes = 10\n")
            from omhc import agents_md

            managed_block_path = agents_md.path_for(root)
            from omhc import managed_block

            managed_block.splice(managed_block_path, "[omhc] handoff\n",
                                 captured_at=1000.0)
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertFalse(ok)
            self.assertIn("byte", detail)
            self.assertIn("10", detail)

    def test_status_row_passes_when_the_installed_block_is_under_budget(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            from omhc import agents_md, managed_block

            managed_block.splice(agents_md.path_for(root), "[omhc] handoff\n",
                                 captured_at=1000.0)
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertTrue(ok)

    def test_declining_over_budget_also_clears_a_stale_installed_block(self):
        """리뷰 결함: 예산 초과로 거절만 하고 낡은 구간을 그대로 두면, Codex 는
        outbox 로 떨어진 새 핸드오프 대신 그 낡은 구간을 계속 읽는다."""
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            agents = agents_md.path_for(root)
            with open(agents, "w", encoding="utf-8") as fh:
                fh.write("# user content\n")
            managed_block.splice(agents, "[omhc] stale handoff\n", captured_at=1000.0)
            self._write_config(home, "project_doc_max_bytes = 10\n")

            bundle = A.HandoffBundle(body_md="[omhc] fresh handoff\nGOAL  x\n",
                                     repo_root=root, to_adapter_id="codex-cli")
            adapter = CX.CodexCliAdapter(home=home)
            with self.assertRaises(A.NoInjectionChannel):
                adapter._install_agents_md(bundle)

            self.assertIsNone(managed_block.installed_captured_at(agents))
            with open(agents, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("# user content", text)
            self.assertNotIn("stale handoff", text)

    def test_declining_over_budget_never_touches_agents_md_shared_with_claude(self):
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            agents = agents_md.path_for(root)
            claude = os.path.join(root, "CLAUDE.md")
            managed_block.splice(agents, "[omhc] stale handoff\n", captured_at=1000.0)
            os.symlink(agents, claude)
            self._write_config(home, "project_doc_max_bytes = 10\n")

            bundle = A.HandoffBundle(body_md="[omhc] fresh handoff\nGOAL  x\n",
                                     repo_root=root, to_adapter_id="codex-cli")
            adapter = CX.CodexCliAdapter(home=home)
            with self.assertRaises(A.NoInjectionChannel):
                adapter._install_agents_md(bundle)

            # 예산 초과 거절이 shared_with_claude 가드를 우회해 공유 파일을
            # 건드리면 안 된다 — 낡은 구간이 그대로 남아 있어야 한다.
            self.assertIsNotNone(managed_block.installed_captured_at(agents))

    def test_zero_limit_is_read_as_is_not_folded_to_default(self):
        with tempfile.TemporaryDirectory() as home:
            self._write_config(home, "project_doc_max_bytes = 0\n")
            adapter = CX.CodexCliAdapter(home=home)
            self.assertEqual(adapter._project_doc_max_bytes(adapter.toml_config_path()), 0)

    def test_negative_limit_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as home:
            self._write_config(home, "project_doc_max_bytes = -1\n")
            adapter = CX.CodexCliAdapter(home=home)
            self.assertEqual(adapter._project_doc_max_bytes(adapter.toml_config_path()),
                             CX.DEFAULT_PROJECT_DOC_MAX_BYTES)

    def test_install_declines_when_the_limit_is_zero(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            self._write_config(home, "project_doc_max_bytes = 0\n")
            bundle = A.HandoffBundle(body_md="[omhc] handoff\nGOAL  x\n",
                                     repo_root=root, to_adapter_id="codex-cli")
            adapter = CX.CodexCliAdapter(home=home)
            with self.assertRaises(A.NoInjectionChannel) as ctx:
                adapter._install_agents_md(bundle)
            self.assertIn("project_doc_max_bytes=0", str(ctx.exception))
            self.assertFalse(os.path.exists(os.path.join(root, "AGENTS.md")))

    def test_status_row_fails_when_the_limit_is_zero(self):
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            self._write_config(home, "project_doc_max_bytes = 0\n")
            managed_block.splice(agents_md.path_for(root), "[omhc] handoff\n",
                                 captured_at=1000.0)
            rows = CX.CodexCliAdapter(home=home).health(root, [])
            label, ok, detail = self._row(rows)
            self.assertFalse(ok)
            self.assertIn("project_doc_max_bytes=0", detail)


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
                    {"hooks": [{"type": "command", "command": "omhc brief --harness codex-cli"}]}]}}, fh)
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


class TestReadSessionSince(unittest.TestCase):
    """#22: `codex exec resume` 은 같은 파일에 이어 쓰고 session_meta 를 다시
    안 쓴다 — offset 부터만 읽는 이 경로가 훅 예산에서 그 재개를 잡는 유일한
    길이다(전체 read_session 은 13.8MB 에서 593ms 실측)."""

    def _rollout(self):
        rows = [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": REPO,
                                                  "timestamp": "2026-09-22T16:30:00Z"}},
            msg("user", "첫 턴"),
            msg("assistant", "첫 응답"),
            msg("user", "두 번째 턴"),
        ]
        return write_rollout(rows)

    def test_matches_read_session_restricted_to_a_line_aligned_offset(self):
        path = self._rollout()
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            full = adapter.read_session(ref)
            self.assertGreaterEqual(len(full.events), 3)
            mid = full.events[1]  # 첫 응답
            since = adapter.read_session_since(ref, mid.offset)
            self.assertIsNotNone(since)
            # seq 는 이 부분 읽기가 처음부터 다시 매긴다(색인은 이 경로를
            # 쓰지 않으므로 구현 자유) — 나머지 필드만 비교한다.
            expected = tuple(e._replace(seq=0) for e in full.events
                             if e.offset >= mid.offset)
            got = tuple(e._replace(seq=0) for e in since.events)
            self.assertEqual(got, expected)
            self.assertIn("두 번째 턴", [e.text for e in since.events])
            self.assertNotIn("첫 턴", [e.text for e in since.events])
        finally:
            os.unlink(path)

    def test_mid_line_offset_skips_the_truncated_record(self):
        """줄 중간에서 시작하면 그 레코드는 버리고 다음 개행부터 읽는다."""
        path = self._rollout()
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            full = adapter.read_session(ref)
            second_user = next(e for e in full.events if e.text == "두 번째 턴")
            # 그 레코드 줄 중간(오프셋+5)에서 시작한다 — 그 레코드는 나오면 안 된다.
            since = adapter.read_session_since(ref, second_user.offset + 5)
            self.assertIsNotNone(since)
            self.assertNotIn("두 번째 턴", [e.text for e in since.events])
            self.assertEqual(since.events, ())
        finally:
            os.unlink(path)

    def test_offset_at_eof_returns_no_events(self):
        path = self._rollout()
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            size = os.path.getsize(path)
            since = adapter.read_session_since(ref, size)
            self.assertIsNotNone(since)
            self.assertEqual(since.events, ())
            self.assertEqual(since.end_offset, size)
        finally:
            os.unlink(path)

    def test_end_offset_reaches_eof_by_default(self):
        path = self._rollout()
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, 0)
            self.assertEqual(since.end_offset, os.path.getsize(path))
        finally:
            os.unlink(path)

    def test_missing_file_never_raises(self):
        adapter = CX.CodexCliAdapter()
        ref = A.SessionRef(adapter_id="codex-cli", session_id="gone",
                           source_path="/nope/missing.jsonl", cwd=REPO,
                           epoch=0.0, size=0)
        since = adapter.read_session_since(ref, 10)
        self.assertIsNotNone(since)
        self.assertEqual(since.events, ())

    def test_garbage_offset_never_raises(self):
        path = self._rollout()
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, "not-an-int")
            self.assertIsNotNone(since)
        finally:
            os.unlink(path)

    def test_stop_at_human_turn_returns_immediately_after_the_first_match(self):
        """리뷰 #3: 존재 여부만 필요한 호출자는 첫 사람 턴에서 멈춰야 한다 —
        그 뒤에 남은 레코드는 읽지 않는다."""
        rows = [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": REPO,
                                                  "timestamp": "2026-09-22T16:30:00Z"}},
            msg("user", "첫 사람 턴"),
            msg("assistant", "그 뒤에 오는 응답 — 안 읽혀야 한다"),
            msg("user", "그 뒤에 오는 두 번째 사람 턴 — 안 읽혀야 한다"),
        ]
        path = write_rollout(rows)
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            self.assertEqual(len(since.events), 1)
            self.assertEqual(since.events[0].text, "첫 사람 턴")
            self.assertLess(since.end_offset, os.path.getsize(path))
        finally:
            os.unlink(path)

    def test_stop_at_human_turn_with_no_human_turn_reads_to_eof(self):
        rows = [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": REPO,
                                                  "timestamp": "2026-09-22T16:30:00Z"}},
            msg("assistant", "에이전트 혼잣말"),
        ]
        path = write_rollout(rows)
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            # 에이전트 혼잣말은 사람 턴이 아니므로 멈추지 않는다 — 끝까지
            # 읽되(agent 의 said 이벤트는 여전히 나온다), 사람 턴은 없다.
            self.assertFalse(any(e.author == "human" for e in since.events))
            self.assertEqual(since.end_offset, os.path.getsize(path))
        finally:
            os.unlink(path)

    def test_max_bytes_caps_the_tail_read_and_end_offset_stays_line_aligned(self):
        """리뷰 #3: 꼬리 크기에 비례해 비용이 늘던 것을 캡으로 막는다 — 캡을
        넘는 레코드는 아예 안 읽고, end_offset 은 캡 안의 마지막 완전한 줄
        끝에 멈춘다(레코드 중간이 아니다)."""
        rows = [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": REPO,
                                                  "timestamp": "2026-09-22T16:30:00Z"}},
        ]
        for i in range(50):
            rows.append(msg("assistant", "패딩 " * 50))
        rows.append(msg("user", "캡 밖의 사람 턴"))
        path = write_rollout(rows)
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            full_size = os.path.getsize(path)
            since = adapter.read_session_since(ref, 0, max_bytes=200)
            self.assertLess(since.end_offset, full_size)
            self.assertNotIn("캡 밖의 사람 턴", [e.text for e in since.events])
            # end_offset 직전 바이트는 개행이다(줄 경계 — 레코드 중간이 아니다).
            with open(path, "rb") as fh:
                content = fh.read()
            self.assertTrue(since.end_offset == 0
                           or content[since.end_offset - 1:since.end_offset] == b"\n")
        finally:
            os.unlink(path)

    def test_stop_at_human_turn_ignores_a_complete_but_unterminated_human_line(self):
        """리뷰(3차) #2: 개행이 아직 안 붙은(쓰는 도중일 수 있는) 완전한 JSON
        사람 턴은 stop_at_human_turn 의 트리거로도, 이벤트로도 세지 않는다 —
        세면 개행이 마저 붙은 뒤 다음 라운드가 baseline 을 그 줄 앞에 둔 채로
        같은 턴을 또 찾아 두 번 재전달한다. stop_at_human_turn 이 아닌 호출
        (read_session 포함)은 이 분기를 안 타므로 영향이 없다."""
        rows = [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": REPO,
                                                  "timestamp": "2026-09-22T16:30:00Z"}},
        ]
        path = write_rollout(rows)
        try:
            boundary = os.path.getsize(path)  # session_meta 줄 끝(개행 포함)

            human_row = {"timestamp": "2026-09-22T16:30:01Z", "ordinal": 1,
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "id": "u1",
                                    "content": [{"type": "input_text",
                                                "text": "완전하지만 개행 없는 턴"}]}}
            line = json.dumps(human_row, ensure_ascii=False).encode("utf-8")
            with open(path, "ab") as fh:
                fh.write(line)  # 개행 없이 — 쓰는 도중을 흉내

            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            self.assertEqual(since.events, ())
            self.assertEqual(since.end_offset, boundary)

            # 영향 없음 — stop_at_human_turn 이 아닌 전체 읽기는 그대로 본다.
            full = adapter.read_session(ref)
            self.assertEqual(len(full.events), 1)
            self.assertEqual(full.events[0].text, "완전하지만 개행 없는 턴")

            with open(path, "ab") as fh:
                fh.write(b"\n")  # 개행이 마저 붙는다
            since2 = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            self.assertEqual(len(since2.events), 1)
            self.assertEqual(since2.end_offset, os.path.getsize(path))
        finally:
            os.unlink(path)
