from __future__ import annotations

import json
import os
import unittest

from ._repo import (
    CLAUDE_LIVE,
    CLAUDE_SUB,
    CODEX_EXEC,
    MISSING,
    REPO,
    iter_json,
    load_expected,
)

have_fixtures = __import__("tests._repo", fromlist=["have_fixtures"]).have_fixtures()


@unittest.skipUnless(have_fixtures, MISSING)
class TestFixtureGolden(unittest.TestCase):
    """Checks the golden is self-consistent with the frozen fixture.

    Values like record count aren't hardcoded — the live transcript is this
    very session and keeps growing (798 -> 1022 observed). Only structurally
    stable values are hardcoded.
    """

    def setUp(self):
        self.exp = load_expected()

    def test_golden_record_count_matches_the_frozen_fixture(self):
        actual = sum(1 for _ in iter_json(CLAUDE_LIVE))
        self.assertEqual(actual, self.exp["claude"]["records"])

    def test_first_cwd_bearing_record_is_not_index_zero(self):
        """'reading cwd from the first line' fails on v1's primary harness."""
        self.assertEqual(self.exp["claude"]["first_cwd_index"], 3)
        self.assertGreater(self.exp["claude"]["no_cwd"], 0)

    def test_machinery_is_present_so_guard_tests_have_real_adversarial_input(self):
        subs = self.exp["claude"]["attachment_subtypes"]
        for needed in (
            "skill_listing",
            "prompt_snapshot",
            "deferred_tools_delta",
            "mcp_instructions_delta",
        ):
            self.assertIn(needed, subs)

    def test_hook_additional_context_is_observed(self):
        """Whether the injection surface we'll use is observed in the wild."""
        self.assertIn("hook_additional_context", self.exp["claude"]["attachment_subtypes"])

    def test_nested_subagent_files_exist(self):
        self.assertGreater(self.exp["claude"]["nested_subagent_files"], 0)

    def test_codex_fixture_is_for_this_repo(self):
        self.assertEqual(
            self.exp["codex"]["cwd"], REPO
        )


@unittest.skipUnless(have_fixtures, MISSING)
class TestFixtureStructure(unittest.TestCase):
    """Structural facts that must hold regardless of session length."""

    def test_tool_results_come_back_as_user_records(self):
        """Claude Code reports tool_result as type:"user" too.

        Evidence that type alone is not enough to judge something as human speech.
        """
        tool_result_users = 0
        for _i, row in iter_json(CLAUDE_LIVE):
            if row.get("type") != "user" or row.get("isMeta"):
                continue
            content = (row.get("message") or {}).get("content")
            if isinstance(content, list):
                kinds = {b.get("type") for b in content if isinstance(b, dict)}
                if kinds == {"tool_result"}:
                    tool_result_users += 1
        self.assertGreater(tool_result_users, 0)

    def test_subagent_fixture_user_records_are_all_sidechain(self):
        flags = set()
        for _i, row in iter_json(CLAUDE_SUB):
            if row.get("type") == "user":
                flags.add(bool(row.get("isSidechain")))
        self.assertEqual(flags, {True})

    def test_codex_envelope_shape(self):
        for _i, row in iter_json(CODEX_EXEC):
            self.assertEqual(set(row) & {"timestamp", "ordinal", "type", "payload"},
                             {"timestamp", "ordinal", "type", "payload"})
            break

    def test_codex_session_meta_carries_cwd_and_base_instructions(self):
        _i, first = next(iter_json(CODEX_EXEC))
        self.assertEqual(first["type"], "session_meta")
        self.assertIn("cwd", first["payload"])
        self.assertIn("base_instructions", first["payload"])

    def _codex_user_texts(self):
        out = []
        for _i, row in iter_json(CODEX_EXEC):
            if row.get("type") != "response_item":
                continue
            payload = row.get("payload") or {}
            if payload.get("type") != "message" or payload.get("role") != "user":
                continue
            blocks = payload.get("content")
            if isinstance(blocks, list):
                out.append(
                    "".join(
                        b.get("text", "")
                        for b in blocks
                        if isinstance(b, dict) and b.get("type") == "input_text"
                    )
                )
        return out

    def test_codex_relays_its_environment_prompt_as_a_user_role_record(self):
        """A role-based filter alone relays Codex's environment prompt as human speech.

        There are two discriminators — envelope structure and the metadata kind.
        content_item_kinds does exist, but nested under
        payload.internal_chat_message_metadata_passthrough.
        """
        texts = self._codex_user_texts()
        self.assertGreaterEqual(len(texts), 2)
        wrapped = [t for t in texts if t.strip().startswith("<environment_context>")]
        self.assertEqual(len(wrapped), 1, "exactly one role=user record carrying the environment prompt is expected")

    def test_codex_developer_records_are_pure_machinery(self):
        heads = []
        for _i, row in iter_json(CODEX_EXEC):
            if row.get("type") != "response_item":
                continue
            payload = row.get("payload") or {}
            if payload.get("role") != "developer":
                continue
            for b in payload.get("content") or []:
                if isinstance(b, dict) and b.get("text"):
                    heads.append(b["text"][:40])
        self.assertTrue(heads, "could not find a developer record")
        self.assertTrue(
            any(h.startswith("<skills_instructions>") for h in heads),
            "the fixture needs a machinery tag so guard tests get real adversarial input",
        )

    def test_subagent_user_records_carry_agent_id(self):
        """Why author has three values. These records are not human speech."""
        seen = 0
        for _i, row in iter_json(CLAUDE_SUB):
            if row.get("type") != "user":
                continue
            seen += 1
            self.assertTrue(row.get("isSidechain"))
            self.assertIsNotNone(row.get("agentId"))
        self.assertGreater(seen, 0)


if __name__ == "__main__":
    unittest.main()
