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
    """골든이 얼린 픽스처와 자기 일관되는지 본다.

    레코드 수 같은 값은 하드코딩하지 않는다 — 라이브 트랜스크립트가 이 세션이라
    계속 자라기 때문이다(798 → 1022 실측). 구조적으로 안정한 값만 하드코딩한다.
    """

    def setUp(self):
        self.exp = load_expected()

    def test_golden_record_count_matches_the_frozen_fixture(self):
        actual = sum(1 for _ in iter_json(CLAUDE_LIVE))
        self.assertEqual(actual, self.exp["claude"]["records"])

    def test_first_cwd_bearing_record_is_not_index_zero(self):
        """'첫 줄에서 cwd 읽기' 는 v1 주력 하네스에서 실패한다."""
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
        """우리가 쓸 주입 표면이 실물로 관측되는지."""
        self.assertIn("hook_additional_context", self.exp["claude"]["attachment_subtypes"])

    def test_nested_subagent_files_exist(self):
        self.assertGreater(self.exp["claude"]["nested_subagent_files"], 0)

    def test_codex_fixture_is_for_this_repo(self):
        self.assertEqual(
            self.exp["codex"]["cwd"], REPO
        )


@unittest.skipUnless(have_fixtures, MISSING)
class TestFixtureStructure(unittest.TestCase):
    """세션 길이와 무관하게 성립해야 하는 구조적 사실."""

    def test_tool_results_come_back_as_user_records(self):
        """Claude Code는 tool_result 를 type:"user" 로 되돌린다.

        타입만 보고 사람의 말로 판정하면 안 된다는 근거.
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
        """role 기반 필터만으로는 Codex 환경 프롬프트가 사람의 말로 중계된다.

        판별자는 둘이다 — 봉투 구조와 메타데이터 kind. content_item_kinds 는
        실재하지만 payload.internal_chat_message_metadata_passthrough 안에
        중첩돼 있다.
        """
        texts = self._codex_user_texts()
        self.assertGreaterEqual(len(texts), 2)
        wrapped = [t for t in texts if t.strip().startswith("<environment_context>")]
        self.assertEqual(len(wrapped), 1, "환경 프롬프트를 실은 role=user 레코드가 정확히 1개여야 한다")

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
        self.assertTrue(heads, "developer 레코드를 찾지 못했다")
        self.assertTrue(
            any(h.startswith("<skills_instructions>") for h in heads),
            "기계장치 태그가 픽스처에 있어야 가드 테스트가 진짜 적대적 입력을 갖는다",
        )

    def test_subagent_user_records_carry_agent_id(self):
        """author 를 3값으로 두는 이유. 이 레코드들은 사람의 말이 아니다."""
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
