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
        """There are two discriminators — the metadata kind and the envelope structure.

        content_item_kinds really exists, but nested under
        payload.internal_chat_message_metadata_passthrough rather than at the payload's top level.
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
    """Tool records confirmed on real data (codex-cli 0.156.1, gpt-6-luna).

    The golden values are computed independently by harvest.py, without calling the adapter.
    """

    def setUp(self):
        self.golden = _repo.load_expected()["codex_tools"]
        self.tools = CX.CodexCliAdapter().read_session(ref_for(_repo.CODEX_TOOLS))
        self.edit = CX.CodexCliAdapter().read_session(ref_for(_repo.CODEX_EDIT))

    def machine(self, read):
        return [e for e in read.events if e.verb != "said"]

    def test_every_shell_command_becomes_one_event_with_its_real_text(self):
        """The old mapping read the argument-less custom_tool_call, so arg came up empty."""
        self.assertEqual([e.arg for e in self.machine(self.tools)],
                         self.golden["tools"]["commands"])

    def test_a_nonzero_exit_marks_the_event_failed(self):
        """The old mapping looked for exit_code in the output text, where it never appears -> FAIL was permanently unreachable."""
        failed = [e.arg for e in self.machine(self.tools) if not e.ok]
        self.assertEqual(failed, self.golden["tools"]["failed"])
        self.assertTrue(failed)

    def test_file_change_becomes_modified_with_its_absolute_path(self):
        modified = [e for e in self.edit.events if e.verb == "modified"]
        self.assertEqual([p for e in modified for p in e.paths],
                         self.golden["edit"]["changed"])
        self.assertTrue(all(e.ok for e in modified))

    def test_read_only_commands_are_inspected_not_ran(self):
        """Codex has no separate read-only tool — everything goes through the
        shell. parsed_cmd is Codex's own classification, so we follow it —
        otherwise a Codex session would never have inspected."""
        self.assertEqual({e.verb for e in self.machine(self.tools)}, {"inspected"})

    def test_the_js_wrapper_is_bookkeeping_not_an_event(self):
        """custom_tool_call(name=exec) is a duplicate of the fact. Counting both would double-count."""
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
    """Unit tests of measured shapes that run without fixtures."""

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
    apply_patch/spawn_agent, output is plain text (not JSON). Unit tests of
    measured shapes that run without fixtures (the _repo.codex_* builders write that exact text header)."""

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
        rows = _repo.codex_exec_command_rows(["ls"])  # code/running_sid both None
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
        row = _repo.codex_spawn_agent_row("fix-flaky-test", "여기 비밀 지침이 있다")  # "there's a secret instruction here"
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
            "payload": {"type": "agent_message", "text": "에이전트 간 메시지"}}])  # "inter-agent message"
        self.assertEqual(read.events, ())
        self.assertEqual(read.dropped.get("agent_message"), 1)
        self.assertEqual(read.unparsed, 0)


class TestCommandExecutionBenignExitOne(unittest.TestCase):
    """era B: if parsed_cmd is entirely reads and the output is also empty,
    exit 1 is idiomatic (3 measured cases). A validation `grep -q` usually
    has parsed_cmd=unknown so it doesn't fall in here. The reason we don't
    broaden this further is in _item_fact's comment (#11)."""

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
        # Like `rg ... && sed ... && sed ...` — all reads with no output is idiomatic.
        read = self._read(self._ce(1, [{"type": "search", "cmd": "rg foo"},
                                       {"type": "read", "cmd": "sed -n 1p a"},
                                       {"type": "read", "cmd": "sed -n 1p b"}]))
        self.assertEqual([e.ok for e in read.events], [True])

    def test_missing_file_before_a_search_stays_a_failure(self):
        # `sed P && rg ...` — the `&&` chain breaks, so sed's failure is the
        # exit code. There's output, so it's a failure.
        read = self._read(self._ce(
            1, [{"type": "read", "cmd": "sed -n 1p missing"},
                {"type": "search", "cmd": "rg foo"}],
            stdout="sed: missing: No such file or directory"))
        self.assertEqual([e.ok for e in read.events], [False])

    def test_list_then_search_with_output_stays_a_failure(self):
        # `ls -la; rg --files -g ...` — ls has output, so structure alone
        # can't tell whether this is benign. A known, deliberate gap (#11).
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
    """Even for an era A-shaped session, FAIL/DID must show the real
    command/path — the old mapping left arg empty."""

    def test_fail_and_did_carry_the_real_command_and_path(self):
        from omhc import mint

        changed = os.path.join(REPO, "omhc", "x.py")
        rows = ([{"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
                msg("user", "테스트를 고쳐줘")]  # "fix the test"
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
    """A subagent, headless exec, or programmatic app-server client must
    never become a source for a handoff.

    Real shapes (codex-cli 0.155.1, measured on this machine): source={"subagent": {...}},
    thread_source="subagent", parent_thread_id=<uuid> (subagent) /
    originator="codex_exec", source="exec" (headless exec, 5 sandbox rollouts) /
    originator="applecider" (36) / "splitlane*" (3), both source="vscode" but
    the role=user turn is a machine template shaped like "User goal: ... Current browser URL: ...".
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
        """A programmatic client the app server put on top — the role=user
        turn isn't a human, it's a "User goal: ... Current browser URL: ..." template."""
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
        """It's a blocklist, so an unknown/weird shape stays interactive."""
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
            msg("user", "진짜 사람의 말"),  # "the real human's words"
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
                "content": [{"type": "output_text", "text": "테스트 3개가 실패했다"}]}},  # "3 tests failed"
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
        """With no hook, no one reads the artifact even if it's written —
        reporting that as success would mean the AGENTS.md fallback never fires."""
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(A.NoInjectionChannel):
                CX.CodexCliAdapter(home=home).install_handoff(self._bundle())

    def test_fallback_channel_is_declared(self):
        with tempfile.TemporaryDirectory() as home:
            channels = CX.CodexCliAdapter(home=home).fallback_channels()
            self.assertEqual(len(channels), 1)
            self.assertTrue(callable(channels[0]))

    def test_install_handoff_collapses_a_stale_agents_md_block(self):
        """#36: when Path A (the hook) succeeds, it must immediately collapse
        the stale Path B section next to it — otherwise the next Codex
        session would read both a fresh hook handoff and stale AGENTS.md instructions at once."""
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


class TestOnSessionStartMark(unittest.TestCase):
    """#36: this session's mark(startup) collapses an "already read"
    AGENTS.md block so the next Codex session can't read it. resume leaves
    it untouched per review #2 — the "reads before the hook" ordering was
    only measured on startup."""

    def _bare_repo(self, base: str) -> str:
        root = os.path.join(base, "proj")
        os.makedirs(root)
        _repo.git(root, "init", "-q")
        return root

    def test_block_captured_before_this_startup_mark_is_collapsed(self):
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._bare_repo(base)
            managed_block.splice(agents_md.path_for(root), "[omhc] old\n",
                                 captured_at=1000.0)

            CX.CodexCliAdapter(home=home).on_session_start_mark(
                root, source="startup", epoch=1000.0 + 3600)

            self.assertIsNone(managed_block.installed_captured_at(agents_md.path_for(root)))

    def test_block_captured_after_this_mark_epoch_is_kept(self):
        """The case where brief (Path B) has already written this session's
        share of the block in parallel within the same SessionStart (a race)
        — if mark wiped it, even this session couldn't read the handoff."""
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._bare_repo(base)
            managed_block.splice(agents_md.path_for(root), "[omhc] just written\n",
                                 captured_at=2000.0)

            CX.CodexCliAdapter(home=home).on_session_start_mark(
                root, source="startup", epoch=2000.0)

            self.assertIsNotNone(managed_block.installed_captured_at(agents_md.path_for(root)))

    def test_shared_with_claude_is_never_collapsed(self):
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._bare_repo(base)
            agents_path = agents_md.path_for(root)
            claude_path = os.path.join(root, "CLAUDE.md")
            managed_block.splice(agents_path, "[omhc] old\n", captured_at=1000.0)
            os.symlink(agents_path, claude_path)

            CX.CodexCliAdapter(home=home).on_session_start_mark(
                root, source="startup", epoch=1000.0 + 3600)

            self.assertIsNotNone(managed_block.installed_captured_at(agents_path))

    def test_compact_source_never_collapses(self):
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._bare_repo(base)
            managed_block.splice(agents_md.path_for(root), "[omhc] old\n",
                                 captured_at=1000.0)

            CX.CodexCliAdapter(home=home).on_session_start_mark(
                root, source="compact", epoch=1000.0 + 3600)

            self.assertIsNotNone(managed_block.installed_captured_at(agents_md.path_for(root)))

    def test_resume_source_never_collapses(self):
        """Review #2: it's not yet been measured when Codex computes the
        AGENTS.md diff on resume — only startup collapses it."""
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._bare_repo(base)
            managed_block.splice(agents_md.path_for(root), "[omhc] old\n",
                                 captured_at=1000.0)

            CX.CodexCliAdapter(home=home).on_session_start_mark(
                root, source="resume", epoch=1000.0 + 3600)

            self.assertIsNotNone(managed_block.installed_captured_at(agents_md.path_for(root)))

    def test_a_splice_between_marks_judgment_and_the_strip_call_is_not_lost(self):
        """Review #1 repro: right after mark judges the block "stale",
        another process (e.g. brief running in parallel within the same
        SessionStart) writes a fresh handoff Y in its place — Y must survive."""
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._bare_repo(base)
            path = agents_md.path_for(root)
            managed_block.splice(path, "[omhc] old\n", captured_at=1000.0)

            real = managed_block.installed_captured_at
            calls = {"n": 0}

            def fake(p):
                calls["n"] += 1
                value = real(p)
                if calls["n"] == 1:
                    # Right after mark's judgment (the first call) finishes,
                    # mimic another process having already written a new block for this session.
                    managed_block.splice(p, "[omhc] concurrent Y\n", captured_at=9999.0)
                return value

            with mock.patch.object(managed_block, "installed_captured_at", side_effect=fake):
                CX.CodexCliAdapter(home=home).on_session_start_mark(
                    root, source="startup", epoch=1000.0 + 3600)

            self.assertEqual(managed_block.installed_captured_at(path), 9999.0)

    def test_a_splice_between_strips_read_and_write_is_not_lost(self):
        """Review #1 repro: even if a new handoff Y sneaks in between
        strip_if_captured's own first read and the write that actually
        erases it (the recheck point), it must survive."""
        from omhc import agents_md, managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._bare_repo(base)
            path = agents_md.path_for(root)
            managed_block.splice(path, "[omhc] old\n", captured_at=1000.0)

            real = managed_block.installed_captured_at
            calls = {"n": 0}

            def fake(p):
                calls["n"] += 1
                if calls["n"] == 2:
                    # The recheck right before strip_if_captured's write (the
                    # second call) — mimic another process having already
                    # written new content before that value is read.
                    managed_block.splice(p, "[omhc] concurrent Y\n", captured_at=9999.0)
                return real(p)

            with mock.patch.object(managed_block, "installed_captured_at", side_effect=fake):
                CX.CodexCliAdapter(home=home).on_session_start_mark(
                    root, source="startup", epoch=1000.0 + 3600)

            self.assertEqual(managed_block.installed_captured_at(path), 9999.0)
            self.assertEqual(calls["n"], 2)

    def test_no_block_is_a_noop_and_never_raises(self):
        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._bare_repo(base)
            CX.CodexCliAdapter(home=home).on_session_start_mark(
                root, source="startup", epoch=1000.0)

    def test_an_internal_failure_is_swallowed(self):
        """Called from the hook path (invariant 2), so nothing must raise no matter what breaks."""
        from omhc import managed_block

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home, \
             mock.patch.object(managed_block, "installed_captured_at",
                               side_effect=RuntimeError("boom")):
            root = self._bare_repo(base)
            CX.CodexCliAdapter(home=home).on_session_start_mark(
                root, source="startup", epoch=1000.0)


class TestInlineTomlHooks(unittest.TestCase):
    """#32: an inline `[[hooks.SessionStart]]` in config.toml counts as a hook install on equal footing with hooks.json."""

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
        # The exact shape shipped by hooks/codex-hooks.json (mark + brief
        # --wire claude) — hooks_status()/inspect compares against this
        # whole thing, not just brief alone like has_runnable_call.
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
        # Review #3 repro: it used to re-find the end of the project body
        # with `^[ \t]*\[`, which repeats exactly the problem #31 already
        # filtered out — "a multi-line array value's element can also start
        # a line with `[`". Using the shared scanner, trust_level must be
        # captured as body text all the way to the real next header (not cut
        # off before an array element mistaken for a fake header).
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
            # ~/.codex/config.toml has no [projects."..."] entry for this repo at all
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
        # Review #1 repro: an inline install with brief but no mark and no
        # --wire claude — this is "exists but differs from the shipped
        # fragment", not "not found".
        with tempfile.TemporaryDirectory() as home:
            self._install_toml(home)  # brief only, no mark, no --wire claude
            ok, detail = CX.CodexCliAdapter(home=home).hooks_status()
            self.assertFalse(ok)
            self.assertNotIn("not found", detail)
            self.assertIn("differs from shipped fragment", detail)

    def test_hooks_install_does_not_duplicate_a_partial_inline_install(self):
        # Review #1 repro: if `omhc hooks install` overlaid hooks.json on top
        # of a partial inline install, Codex would read both layers and
        # warn, and the inline side would keep failing every session —
        # instead it must not write hooks.json and must report failure.
        with tempfile.TemporaryDirectory() as home:
            self._make_bin(home)
            self._install_toml(home)  # a partial inline install with brief only
            out = io.StringIO()
            code = cli.main(["hooks", "install", "--harness", "codex-cli"], home=home, out=out)
            self.assertNotEqual(code, 0)
            self.assertIn("differs", out.getvalue())
            self.assertFalse(os.path.exists(os.path.join(home, ".codex", "hooks.json")))
            # Rechecking must still report "differs", not "not installed".
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
    """content_item_kinds is nested inside the metadata, not the payload's top level."""

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
            self._msg("user", "봉투 없이 온 환경 설명",  # "environment description with no envelope"
                      ["environments.environment_context"]),
        ])
        self.assertEqual([e for e in read.events if e.author == "human"], [])
        self.assertIn("kind:environments.environment_context", read.dropped)

    def test_user_text_kind_is_kept(self):
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            self._msg("user", "진짜 사람의 말", ["user.text"]),  # "the real human's words"
        ])
        humans = [e for e in read.events if e.author == "human"]
        self.assertEqual(len(humans), 1)

    def test_omhcs_own_injected_handoff_is_not_a_human_turn(self):
        """Measured (codex-cli 0.155.1): a marker injected by omhc really

        shows up in the rollout as role=user
        (content_item_kinds=["hooks.additional_context"]). Misreading it as
        human speech would let the next handoff mistake omhc's own marker
        for GOAL/NEXT, creating a feedback loop.
        """
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            self._msg("user", "[omhc] GOAL: 리더를 붙여서 양방향으로 만들기",
                      ["hooks.additional_context"]),
            self._msg("user", "진짜 사람의 말", ["user.text"]),
        ])
        self.assertEqual(len([e for e in read.events if e.author == "human"]), 1)
        self.assertIn("kind:hooks.additional_context", read.dropped)

    def test_turn_hooks_additional_context_developer_message_is_never_a_human_turn(self):
        """v2 phase 2 (#42) measured fact: `omhc turn`'s own UserPromptSubmit
        note lands in the rollout as a separate **developer** message tagged
        content_item_kinds=["hooks.additional_context"] — one role lower on
        the trust ladder than SessionStart's role=user injection (already
        pinned above). role=developer is outside _PARSED_ROLES regardless of
        kind, so this is dropped by role alone before the kind is even
        looked at — invariant 4."""
        read = self._read([
            {"type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            self._msg("developer", "[omhc] codex-cli 01a0d2e1 (running) modified files "
                      "you touched, since your last turn:",
                      ["hooks.additional_context"]),
            self._msg("user", "진짜 사람의 말", ["user.text"]),
        ])
        self.assertEqual(len([e for e in read.events if e.author == "human"]), 1)
        self.assertIn("role:developer", read.dropped)

    def test_a_new_user_prefixed_kind_is_not_lost(self):
        """Prefix allowance means the human's words aren't lost even when a new user.* kind appears."""
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
    """codex-cli 0.155.1 skips an untrusted hook with no message and no
    ledger row — this diagnosis catches that state through behavioral evidence."""

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
        # #32 review 1 repro: install_epoch used to always be hooks.json's
        # mtime, so under an inline-only install (hooks.json doesn't even
        # exist) this stat died with ENOENT and the row stayed
        # `----(unknown)` every time — the row meant to catch a silently
        # skipped untrusted inline hook failed at its job. Now it uses the
        # mtime of whichever file is actually installed (config.toml).
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
            # Inline install is a different mechanism from hooks.json's
            # hooks.state trust hash — that fact must only surface as a hint,
            # never stated as a firm diagnosis.
            self.assertIn("hint, not a diagnosis", detail)

    def test_pass_when_only_an_older_pre_trust_session_is_missing(self):
        """Review defect: trust changes config.toml, not hooks.json — even if
        a pre-trust session never ran, if a post-trust (newer) session ran,
        trust holds right now, so it must be PASS."""
        with tempfile.TemporaryDirectory() as home:
            self._install_hook(home)
            self._rollout(home, "before-trust", "2023-11-15T00:00:00.000Z")
            self._rollout(home, "after-trust", "2023-11-16T00:00:00.000Z")
            rows = CX.CodexCliAdapter(home=home).health(
                REPO, [{"harness": "codex-cli", "session": "after-trust", "event": "start"}])
            self.assertTrue(rows[0][1])
            self.assertIn("ran for the latest session", rows[0][2])

    def test_fail_when_only_the_newest_session_is_missing(self):
        """Even if all the older sessions ran, if the newest one didn't,
        trust is broken again right now, so it must be FAIL."""
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
    """Whether omhc status actually folds in an adapter's health row —
    adapters.present() looks at the real $HOME (AGENTS.md), so that
    discovery itself is pinned down, and this only checks whether health()
    is called with the temp $HOME this test builds."""

    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.home = os.path.join(self.base.name, "home")
        self.repo = os.path.join(self.base.name, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        _repo.git(self.repo, "init", "-q")
        self.root = os.path.realpath(self.repo)

        # Plant the exact shipped fragment (+ an executable dummy binary) —
        # the `codex-cli hooks` row must PASS so the exit-code assertion
        # below proves purely the health(`codex hook`) row (review defect:
        # it used to be just one `omhc brief` line, so the hooks row also
        # FAILed and this test couldn't prove which side produced code=1).
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
    """Review defect: a session is recorded in the ledger under its own
    worktree root's repo key, but status queries under the enclosing parent
    repo's key — if the filter goes by repo, the session looks like it
    "never ran" forever. Reproduced via the real cmd_mark -> cmd_status path."""

    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.home = os.path.join(self.base.name, "home")
        self.repo = os.path.join(self.base.name, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        _repo.git(self.repo, "init", "-q")
        self.root = os.path.realpath(self.repo)

        # A nested directory with its own .git — the same shape as a real
        # worktree (.claude/worktrees/*): resolve_repo_root stops here, and
        # this path's repo key differs from the parent repo's.
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
        """mark is called with the worktree-side cwd and records under its
        own (different) repo key; status is called from the parent repo. If
        the filter goes by repo, this row disappears."""
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
    """#12: even in a non-git project (`.omhc-root`), a session started from
    a subdirectory must not disappear from the `codex hook` row as "never ran"."""

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
    """Review: not every `[` at the start of a line is a section header."""

    def test_headers_and_non_headers(self):
        F = CX._first_table_header
        self.assertGreaterEqual(F('a = 1\n[tui]\n'), 0)
        self.assertGreaterEqual(F('a = 1\n  [projects."/a b"]\n'), 0)
        self.assertGreaterEqual(F('[[mcp]]\n'), 0)
        self.assertGreaterEqual(F('x = [\n  ".git",\n]\n[t]\n'), 0)
        self.assertEqual(F('other = [\n  ["a", "b"],\n]\nk = 1\n'), -1)
        self.assertEqual(F('s = """\n[x]\n"""\n'), -1)
        self.assertEqual(F('# [c]\nk = 1\n'), -1)
        # An escaped \""" inside a basic multi-line string is not the end.
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
    """#31: for a project defined only by `.omhc-root` (no `.git`), diagnose
    whether Codex's `project_root_markers` setting includes `.omhc-root`."""

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
        """Review #1: under a normal setup where Path A (hook install) is
        fine, this setting has no effect, so it must not gate — always
        `----` unless PASS."""
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
        """Review #2: a key after `[table]` is scoped to that table, not a
        top-level key — e.g. when a human accidentally appends after the
        trust table."""
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
        """Putting a directory in that spot makes open() raise
        IsADirectoryError (an OSError) — it must be distinguished from FileNotFoundError."""
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
        """Review #4: if there's a `.git` ancestor above `repo_root`, even
        Codex's default reads AGENTS.md from that ancestor down to cwd, so
        there's no need to add the marker."""
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
        """The new row is just an element inside the `health` list — it must
        not grow status --json's top-level key set (#19's contract)."""
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
    """#33: Codex only reads AGENTS.md from the head, up to
    `project_doc_max_bytes` — a Path B install that would exceed the budget
    must not claim, and must fall to the outbox instead, while status must
    report a section that's already installed past budget."""

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
        """All the way through the router (deliver) — with no
        install_handoff (hook not installed) and Path B also refused for
        exceeding budget, it must fall to the universal floor (outbox)."""
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
        """AGENTS.md's status convention: a judgeable diagnosis emits a
        `----` row even with nothing to judge (not SKIP) — only a repo where
        the row is entirely meaningless (shared) omits the row (see
        test_status_row_is_absent_when_shared_with_claude below)."""
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
        """Review defect: if it only refuses for exceeding budget and leaves
        the stale section in place, Codex keeps reading that stale section
        instead of the new handoff that fell to the outbox."""
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

            # A budget-exceeded refusal must not bypass the
            # shared_with_claude guard and touch the shared file — the stale
            # section must stay untouched.
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
    """Review defect: if ledger.read's default limit (2000, shared
    machine-wide) fills up with another repo's rows, this repo/session's row
    can get pushed out of the window. The ledger passed into health must be read unbounded."""

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
        # The body (after Output:) is program output. If the "Exit code: 0"
        # printed there overrides the "running" header, write_stdin's
        # failure never reaches the original event.
        out = CX._parse_exec_outcome(
            "Chunk ID: x\nWall time: 10 seconds\nProcess running with session ID 7\n"
            "Original token count: 5\nOutput:\n[step] Exit code: 0\nExit code: 0\n")
        self.assertIsNone(out.ok)
        self.assertEqual(out.session_id, "7")


class TestSubagentRolloutWithTwoMetaLines(unittest.TestCase):
    """Measured (0.155.1 sandbox): a subagent rollout has two session_meta
    lines — the first is its own (source.subagent / thread_source /
    parent_thread_id), the second is the parent's — and it re-carries the
    parent's human prompt as user.text. Only the subagent marker on the
    first line stops that prompt from being laundered into GOAL."""

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
                    "content": [{"type": "input_text", "text": "PARENT_GOAL 이거 고쳐줘"}],  # "fix this"
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
    """#22: `codex exec resume` appends to the same file and never writes
    session_meta again — this path that reads only from an offset onward is
    the only way to catch that resume within the hook budget (a full
    read_session measured at 593ms on 13.8MB)."""

    def _rollout(self):
        rows = [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": REPO,
                                                  "timestamp": "2026-09-22T16:30:00Z"}},
            msg("user", "첫 턴"),  # "first turn"
            msg("assistant", "첫 응답"),  # "first response"
            msg("user", "두 번째 턴"),  # "second turn"
        ]
        return write_rollout(rows)

    def test_matches_read_session_restricted_to_a_line_aligned_offset(self):
        path = self._rollout()
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            full = adapter.read_session(ref)
            self.assertGreaterEqual(len(full.events), 3)
            mid = full.events[1]  # first response
            since = adapter.read_session_since(ref, mid.offset)
            self.assertIsNotNone(since)
            # seq is renumbered from scratch by this partial read (the index
            # never uses this path, so it's the implementation's own choice)
            # — compare only the remaining fields.
            expected = tuple(e._replace(seq=0) for e in full.events
                             if e.offset >= mid.offset)
            got = tuple(e._replace(seq=0) for e in since.events)
            self.assertEqual(got, expected)
            self.assertIn("두 번째 턴", [e.text for e in since.events])
            self.assertNotIn("첫 턴", [e.text for e in since.events])
        finally:
            os.unlink(path)

    def test_mid_line_offset_skips_the_truncated_record(self):
        """Starting mid-line discards that record and reads from the next newline onward."""
        path = self._rollout()
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            full = adapter.read_session(ref)
            second_user = next(e for e in full.events if e.text == "두 번째 턴")
            # Starts in the middle of that record's line (offset+5) — that record must not appear.
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
        """Review #3: a caller who only needs existence must stop at the
        first human turn — records after that must not be read."""
        rows = [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": REPO,
                                                  "timestamp": "2026-09-22T16:30:00Z"}},
            msg("user", "첫 사람 턴"),  # "first human turn"
            msg("assistant", "그 뒤에 오는 응답 — 안 읽혀야 한다"),  # "response that follows — must not be read"
            msg("user", "그 뒤에 오는 두 번째 사람 턴 — 안 읽혀야 한다"),  # "second human turn that follows — must not be read"
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
            msg("assistant", "에이전트 혼잣말"),  # "agent soliloquy"
        ]
        path = write_rollout(rows)
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            # Agent soliloquy is not a human turn, so it doesn't stop — reads
            # to the end (the agent's said event still comes out), but there's no human turn.
            self.assertFalse(any(e.author == "human" for e in since.events))
            self.assertEqual(since.end_offset, os.path.getsize(path))
        finally:
            os.unlink(path)

    def test_max_bytes_caps_the_tail_read_and_end_offset_stays_line_aligned(self):
        """Review #3: a cap stops cost growing proportional to tail size — a
        record beyond the cap is never read at all, and end_offset stops at
        the end of the last complete line inside the cap (never mid-record)."""
        rows = [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": REPO,
                                                  "timestamp": "2026-09-22T16:30:00Z"}},
        ]
        for i in range(50):
            rows.append(msg("assistant", "패딩 " * 50))  # "padding"
        rows.append(msg("user", "캡 밖의 사람 턴"))  # "human turn outside the cap"
        path = write_rollout(rows)
        try:
            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            full_size = os.path.getsize(path)
            since = adapter.read_session_since(ref, 0, max_bytes=200)
            self.assertLess(since.end_offset, full_size)
            self.assertNotIn("캡 밖의 사람 턴", [e.text for e in since.events])
            # The byte right before end_offset is a newline (a line boundary — never mid-record).
            with open(path, "rb") as fh:
                content = fh.read()
            self.assertTrue(since.end_offset == 0
                           or content[since.end_offset - 1:since.end_offset] == b"\n")
        finally:
            os.unlink(path)

    def test_stop_at_human_turn_ignores_a_complete_but_unterminated_human_line(self):
        """Review (round 3) #2: a complete JSON human turn with no trailing
        newline yet (possibly mid-write) doesn't count either as a
        stop_at_human_turn trigger or as an event — counting it would mean
        that once the newline finally lands, the next round finds the same
        turn again with the baseline still before that line, and redelivers
        it twice. Calls that aren't stop_at_human_turn (including
        read_session) don't take this branch, so they're unaffected."""
        rows = [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": REPO,
                                                  "timestamp": "2026-09-22T16:30:00Z"}},
        ]
        path = write_rollout(rows)
        try:
            boundary = os.path.getsize(path)  # end of the session_meta line (newline included)

            human_row = {"timestamp": "2026-09-22T16:30:01Z", "ordinal": 1,
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "id": "u1",
                                    "content": [{"type": "input_text",
                                                "text": "완전하지만 개행 없는 턴"}]}}  # "complete but newline-less turn"
            line = json.dumps(human_row, ensure_ascii=False).encode("utf-8")
            with open(path, "ab") as fh:
                fh.write(line)  # no newline — mimics being mid-write

            adapter = CX.CodexCliAdapter()
            ref = ref_for(path)
            since = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            self.assertEqual(since.events, ())
            self.assertEqual(since.end_offset, boundary)

            # Unaffected — a full read that isn't stop_at_human_turn still sees it.
            full = adapter.read_session(ref)
            self.assertEqual(len(full.events), 1)
            self.assertEqual(full.events[0].text, "완전하지만 개행 없는 턴")

            with open(path, "ab") as fh:
                fh.write(b"\n")  # the newline finally lands
            since2 = adapter.read_session_since(ref, 0, stop_at_human_turn=True)
            self.assertEqual(len(since2.events), 1)
            self.assertEqual(since2.end_offset, os.path.getsize(path))
        finally:
            os.unlink(path)


class TestHookProvesPathA(unittest.TestCase):
    """#38: inside the hook, stdout already delivered — never also write Path B."""

    def _repo(self, base: str) -> str:
        root = os.path.join(base, "proj")
        os.makedirs(root)
        _repo.git(root, "init", "-q")
        return root

    def _deliver(self, from_hook: bool):
        from omhc import deliver

        with tempfile.TemporaryDirectory() as base, \
             tempfile.TemporaryDirectory() as home:
            root = self._repo(base)
            # No hooks.json at all: the config check alone says "no hook".
            bundle = A.HandoffBundle(body_md="[omhc] handoff\nGOAL  x\n", repo_root=root,
                                     to_adapter_id="codex-cli", from_hook=from_hook)
            receipt = deliver.deliver(bundle, home=home, now=1000.0)
            return receipt, os.path.exists(os.path.join(root, "AGENTS.md"))

    def test_a_running_hook_counts_as_path_a_even_if_its_config_is_unrecognized(self):
        receipt, wrote_agents_md = self._deliver(from_hook=True)
        self.assertEqual(receipt.channel, "sessionstart-hook")
        self.assertFalse(wrote_agents_md, "stdout already delivered; Path B would duplicate it")

    def test_a_manual_call_still_falls_back_to_agents_md(self):
        receipt, wrote_agents_md = self._deliver(from_hook=False)
        self.assertEqual(receipt.channel, "agents-md")
        self.assertTrue(wrote_agents_md)
