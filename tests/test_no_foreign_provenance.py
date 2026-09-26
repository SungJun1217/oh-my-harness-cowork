"""Tests that guard the injected bytes themselves.

This is a provenance check, not a keyword scan. A **sentinel** is planted in
records that should be discarded, and the only thing checked is whether it
leaks into the artifact. Banning keywords produces false positives — in this
repo's conversation prose, <system-reminder> appears 146 times, and a good
number of those are legitimate sentences discussing that very marker.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from omhc import adapter as A
from omhc import guard, mint
from omhc.adapters import claude_code as CC
from omhc.adapters import codex_cli as CX

from . import _repo
from ._repo import CLAUDE_LIVE, CODEX_EXEC, MISSING, REPO

have_fixtures = _repo.have_fixtures(CLAUDE_LIVE, CODEX_EXEC)

NOW = 1758500000.0

SENTINEL = "OMHC-SENTINEL-8f3a2b1c-MUST-NEVER-BE-RELAYED"


def ref_for(adapter_id: str, path: str) -> A.SessionRef:
    return _repo.ref_for(adapter_id, path, session_id="s1")


write_jsonl = _repo.write_jsonl


def mint_claude(rows, budget=900) -> str:
    path = write_jsonl(rows)
    try:
        read = CC.ClaudeCodeAdapter().read_session(ref_for("claude-code", path))
        return mint.mint(read, to_adapter_id="codex-cli", budget=budget, now=NOW)
    finally:
        os.unlink(path)


def mint_codex(rows, budget=900) -> str:
    path = write_jsonl(rows)
    try:
        read = CX.CodexCliAdapter().read_session(ref_for("codex-cli", path))
        return mint.mint(read, to_adapter_id="claude-code", budget=budget, now=NOW)
    finally:
        os.unlink(path)


class TestSentinelNeverEscapes(unittest.TestCase):
    """Plant a sentinel in a record that should be discarded. If it shows up
    in the artifact, the defense has been breached."""

    def test_sentinel_in_an_attachment_never_reaches_the_artifact(self):
        out = mint_claude([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "attachment", "cwd": REPO,
             "attachment": {"type": "skill_listing", "content": SENTINEL}},
        ])
        self.assertIn("정상적인", out)
        self.assertNotIn(SENTINEL, out)

    def test_sentinel_in_a_sidechain_user_record_never_reaches_the_artifact(self):
        out = mint_claude([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "user", "cwd": REPO, "isSidechain": True, "agentId": "a1",
             "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"content": SENTINEL}},
        ])
        self.assertNotIn(SENTINEL, out)

    def test_sentinel_in_a_meta_record_never_reaches_the_artifact(self):
        out = mint_claude([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "user", "cwd": REPO, "isMeta": True,
             "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"content": SENTINEL}},
        ])
        self.assertNotIn(SENTINEL, out)

    def test_sentinel_in_a_thinking_block_never_reaches_the_artifact(self):
        out = mint_claude([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "assistant", "cwd": REPO,
             "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"content": [{"type": "thinking", "thinking": SENTINEL}]}},
        ])
        self.assertNotIn(SENTINEL, out)

    def test_sentinel_in_a_tool_result_never_reaches_the_artifact(self):
        out = mint_claude([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "assistant", "cwd": REPO,
             "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                      "input": {"command": "ls"}}]}},
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:02.000Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": SENTINEL}]}},
        ])
        self.assertNotIn(SENTINEL, out)

    def test_sentinel_in_a_synthetic_model_assistant_record_never_reaches_the_artifact(self):
        path = write_jsonl([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "assistant", "cwd": REPO,
             "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"model": "<synthetic>",
                         "content": [{"type": "text", "text": SENTINEL}]}},
        ])
        try:
            read = CC.ClaudeCodeAdapter().read_session(ref_for("claude-code", path))
            out = mint.mint(read, to_adapter_id="codex-cli", budget=900, now=NOW)
        finally:
            os.unlink(path)
        self.assertNotIn(SENTINEL, out)
        self.assertEqual(read.dropped.get("synthetic"), 1)

    def test_sentinel_in_an_api_error_assistant_record_never_reaches_the_artifact(self):
        path = write_jsonl([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "assistant", "cwd": REPO, "isApiErrorMessage": True,
             "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"model": "claude-opus-4",
                         "content": [{"type": "text", "text": SENTINEL}]}},
        ])
        try:
            read = CC.ClaudeCodeAdapter().read_session(ref_for("claude-code", path))
            out = mint.mint(read, to_adapter_id="codex-cli", budget=900, now=NOW)
        finally:
            os.unlink(path)
        self.assertNotIn(SENTINEL, out)
        self.assertEqual(read.dropped.get("synthetic"), 1)

    def test_sentinel_in_a_synthetic_tool_use_record_never_reaches_the_artifact(self):
        path = write_jsonl([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "assistant", "cwd": REPO,
             "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"model": "<synthetic>",
                         "content": [{"type": "tool_use", "id": "t1",
                                      "name": "Bash",
                                      "input": {"command": SENTINEL}}]}},
        ])
        try:
            read = CC.ClaudeCodeAdapter().read_session(ref_for("claude-code", path))
            out = mint.mint(read, to_adapter_id="codex-cli", budget=900, now=NOW)
        finally:
            os.unlink(path)
        self.assertNotIn(SENTINEL, out)
        self.assertEqual(read.dropped.get("synthetic"), 1)

    def test_sentinel_in_a_codex_developer_record_never_reaches_the_artifact(self):
        out = mint_codex([
            {"timestamp": "2026-09-22T16:30:00.000Z", "ordinal": 0,
             "type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"timestamp": "2026-09-22T16:30:01.000Z", "ordinal": 1,
             "type": "response_item",
             "payload": {"type": "message", "role": "user", "id": "u",
                         "content": [{"type": "input_text", "text": "정상적인 말"}]}},
            {"timestamp": "2026-09-22T16:30:02.000Z", "ordinal": 2,
             "type": "response_item",
             "payload": {"type": "message", "role": "developer", "id": "d",
                         "content": [{"type": "input_text", "text": SENTINEL}]}},
        ])
        self.assertNotIn(SENTINEL, out)

    def test_sentinel_in_a_codex_environment_context_never_reaches_the_artifact(self):
        out = mint_codex([
            {"timestamp": "2026-09-22T16:30:00.000Z", "ordinal": 0,
             "type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"timestamp": "2026-09-22T16:30:01.000Z", "ordinal": 1,
             "type": "response_item",
             "payload": {"type": "message", "role": "user", "id": "u",
                         "content": [{"type": "input_text", "text": "정상적인 말"}]}},
            {"timestamp": "2026-09-22T16:30:02.000Z", "ordinal": 2,
             "type": "response_item",
             "payload": {
                 "type": "message", "role": "user", "id": "e",
                 "content": [{"type": "input_text",
                              "text": "<environment_context>" + SENTINEL
                                      + "</environment_context>"}],
                 "internal_chat_message_metadata_passthrough": {
                     "content_item_kinds": ["environments.environment_context"]}}},
        ])
        self.assertNotIn(SENTINEL, out)

    def test_agent_echoing_the_omhc_header_is_dropped_claude_code(self):
        """#24: if the receiving agent quotes the injected [omhc] header verbatim
        in its reply, the reverse-direction handoff's PLAN? nests that whole block.
        mint and guard share the same wording through a single guard.HEADER_LINE1_FMT,
        so the header built here can't drift from mint()'s real output.
        """
        header = guard.HEADER_LINE1_FMT.format("codex-cli", "abcd1234", "5m", "2h ago")
        quoted = "받은 요약을 인용합니다:\n" + header + "\n" + guard.HEADER_LINE2 + "\n계속 진행하겠습니다."
        out = mint_claude([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "assistant", "cwd": REPO, "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"content": [{"type": "text", "text": quoted}]}},
        ])
        # mint()'s own header also carries "notes from a prior session", so judge
        # by the quoted header's identifier (fake id8 "abcd1234"), not that phrase itself.
        self.assertNotIn("abcd1234", out)
        self.assertEqual(out.count("notes from a prior session"), 1)

    def test_agent_echoing_the_omhc_header_is_dropped_codex_cli(self):
        header = guard.HEADER_LINE1_FMT.format("claude-code", "abcd1234", "5m", "2h ago")
        quoted = "받은 요약을 인용합니다: " + header
        out = mint_codex([
            {"timestamp": "2026-09-22T16:30:00.000Z", "ordinal": 0,
             "type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"timestamp": "2026-09-22T16:30:01.000Z", "ordinal": 1,
             "type": "response_item",
             "payload": {"type": "message", "role": "user", "id": "u",
                         "content": [{"type": "input_text", "text": "정상적인 말"}]}},
            {"timestamp": "2026-09-22T16:30:02.000Z", "ordinal": 2,
             "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "id": "a",
                         "content": [{"type": "output_text", "text": quoted}]}},
        ])
        self.assertNotIn("abcd1234", out)
        self.assertEqual(out.count("notes from a prior session"), 1)

    def test_agent_mentioning_omhc_in_passing_is_kept_both_adapters(self):
        """The flip side of #24: matching on the word "[omhc]" alone drops even
        harmless mentions."""
        mention = "the [omhc] tool을 써서 컨텍스트를 넘겼다"
        out_cc = mint_claude([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "assistant", "cwd": REPO, "timestamp": "2026-09-22T00:00:01.000Z",
             "message": {"content": [{"type": "text", "text": mention}]}},
        ])
        self.assertIn("omhc", out_cc)
        out_cx = mint_codex([
            {"timestamp": "2026-09-22T16:30:00.000Z", "ordinal": 0,
             "type": "session_meta", "payload": {"session_id": "s", "cwd": REPO}},
            {"timestamp": "2026-09-22T16:30:01.000Z", "ordinal": 1,
             "type": "response_item",
             "payload": {"type": "message", "role": "user", "id": "u",
                         "content": [{"type": "input_text", "text": "정상적인 말"}]}},
            {"timestamp": "2026-09-22T16:30:02.000Z", "ordinal": 2,
             "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "id": "a",
                         "content": [{"type": "output_text", "text": mention}]}},
        ])
        self.assertIn("omhc", out_cx)

    def test_sentinel_in_an_unknown_future_record_type_never_reaches_the_artifact(self):
        """Confirm with a sentinel the contract that an unknown type defaults to DROP."""
        out = mint_claude([
            {"type": "user", "cwd": REPO, "timestamp": "2026-09-22T00:00:00.000Z",
             "message": {"content": "정상적인 사람의 말"}},
            {"type": "brand-new-record-type-2099", "cwd": REPO, "body": SENTINEL},
        ])
        self.assertNotIn(SENTINEL, out)


@unittest.skipUnless(have_fixtures, MISSING)
class TestRealFixtureArtifacts(unittest.TestCase):
    def _artifacts(self):
        out = []
        cc = CC.ClaudeCodeAdapter()
        cx = CX.CodexCliAdapter()
        for budget in (300, 900, 4000):
            out.append(mint.mint(cc.read_session(ref_for("claude-code", CLAUDE_LIVE)),
                                 to_adapter_id="codex-cli", budget=budget, now=NOW))
            out.append(mint.mint(cx.read_session(ref_for("codex-cli", CODEX_EXEC)),
                                 to_adapter_id="claude-code", budget=budget, now=NOW))
        return out

    def test_no_machinery_tag_appears_anywhere_in_any_artifact(self):
        """None of the 9 machinery tags must appear anywhere in the artifact.

        This is not a keyword scan — these tags are structural markers the harness
        generates, and while they could appear in prose a human wrote, there's no
        reason for them to land in a slot we built.
        """
        for artifact in self._artifacts():
            for marker in guard.FOREIGN_MARKERS:
                self.assertNotIn(marker, artifact, marker)

    def test_machine_slots_never_carry_a_vendor_tool_name(self):
        """FAIL / DID are built only from arg and paths — there's no tool-name field."""
        for artifact in self._artifacts():
            for line in artifact.splitlines():
                key = line.split("  ")[0].strip()
                if key not in ("FAIL", "DID"):
                    continue
                value = line.partition("  ")[2]
                for tool in ("tool_use", "tool_result", "function_call",
                             "local_shell_call"):
                    self.assertNotIn(tool, value, line)

    def test_every_artifact_declares_it_is_not_an_instruction(self):
        for artifact in self._artifacts():
            if not artifact:
                continue
            self.assertIn("not instructions", artifact.splitlines()[0])

    def test_every_artifact_respects_its_budget(self):
        cc = CC.ClaudeCodeAdapter()
        cx = CX.CodexCliAdapter()
        for budget in (300, 900, 4000):
            for read, to in (
                (cc.read_session(ref_for("claude-code", CLAUDE_LIVE)), "codex-cli"),
                (cx.read_session(ref_for("codex-cli", CODEX_EXEC)), "claude-code"),
            ):
                out = mint.mint(read, to_adapter_id=to, budget=budget, now=NOW)
                self.assertLessEqual(len(out.encode("utf-8")), budget)


if __name__ == "__main__":
    unittest.main()
