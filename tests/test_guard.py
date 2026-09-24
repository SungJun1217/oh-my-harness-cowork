from __future__ import annotations

import json
import os
import unittest

from omhc import guard

from . import _repo

from ._repo import CLAUDE_LIVE, CODEX_EXEC, MISSING

have_fixtures = _repo.have_fixtures(CLAUDE_LIVE, CODEX_EXEC)


def raw_bytes() -> bytes:
    blobs = []
    for path in (CLAUDE_LIVE, CODEX_EXEC):
        if os.path.exists(path):
            with open(path, "rb") as fh:
                blobs.append(fh.read())
    return b"".join(blobs)


def attachment_text(kind: str, field: str) -> str:
    """픽스처에서 기계장치 본문을 꺼낸다. 테스트가 진짜 적대적 입력을 쓰게 한다."""
    with open(CLAUDE_LIVE, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "attachment":
                continue
            att = row.get("attachment") or {}
            if att.get("type") != kind:
                continue
            value = att.get(field)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return "\n".join(str(v) for v in value)
    raise AssertionError("픽스처에서 {}.{} 를 찾지 못했다".format(kind, field))


def codex_developer_text() -> str:
    with open(CODEX_EXEC, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "response_item":
                continue
            payload = row.get("payload") or {}
            if payload.get("role") != "developer":
                continue
            return "".join(
                b.get("text", "")
                for b in payload.get("content") or []
                if isinstance(b, dict)
            )
    raise AssertionError("Codex developer 레코드를 찾지 못했다")


class TestDenylistHygiene(unittest.TestCase):
    def test_no_marker_contains_a_backslash(self):
        """백슬래시가 섞인 리터럴은 실물과 0회 매칭한다 — 조용히 무효인 차단목록."""
        for marker in guard.FOREIGN_MARKERS:
            self.assertNotIn("\\", marker, "마커에 백슬래시가 있으면 실물과 매칭되지 않는다")

    def test_markers_are_lowercase_ascii_tags(self):
        for marker in guard.FOREIGN_MARKERS:
            self.assertTrue(marker.startswith("<"), marker)
            self.assertTrue(marker.endswith(">"), marker)

    @unittest.skipUnless(have_fixtures, MISSING)
    def test_every_marker_is_witnessed_in_real_bytes(self):
        """항목마다 개별 검사한다. 하나만 매칭돼도 통과하는 묶음 단정이면 버그를 놓친다."""
        blob = raw_bytes()
        for marker in guard.FOREIGN_MARKERS:
            with self.subTest(marker=marker):
                if marker in guard.UNWITNESSED_OK:
                    self.assertTrue(
                        guard.UNWITNESSED_OK[marker],
                        "UNWITNESSED_OK 항목에는 이유가 있어야 한다",
                    )
                    continue
                self.assertIn(
                    marker.encode("utf-8"),
                    blob,
                    "픽스처에서 목격되지 않은 마커. 실물 근거가 없으면 UNWITNESSED_OK 에 이유와 함께 등재하라",
                )


class TestEnvelope(unittest.TestCase):
    def test_whole_content_envelope_is_detected(self):
        self.assertTrue(guard.is_envelope("<command-name>/model</command-name>"))
        self.assertTrue(
            guard.is_envelope("<environment_context>\n  <cwd>/x</cwd>\n</environment_context>")
        )

    def test_multiple_top_level_envelopes_are_detected(self):
        text = "<a>1</a>\n<b>2</b>"
        self.assertTrue(guard.is_envelope(text))

    def test_prose_containing_a_tag_is_not_an_envelope(self):
        self.assertFalse(guard.is_envelope("안녕 <b>x</b> 하세요"))
        self.assertFalse(guard.is_envelope("이 마커 <system-reminder> 가 왜 위험한지 설명한다"))

    def test_plain_text_is_not_an_envelope(self):
        self.assertFalse(guard.is_envelope("계속 진행해"))
        self.assertFalse(guard.is_envelope(""))

    def test_envelope_detection_is_structural_not_a_tag_list(self):
        """처음 보는 태그도 자동으로 걸려야 한다."""
        self.assertTrue(guard.is_envelope("<never_seen_before_2099>x</never_seen_before_2099>"))


class TestSafeScopedByProvenance(unittest.TestCase):
    def test_human_text_is_kept_even_with_a_foreign_tool_name(self):
        """사용자 결정 (a): 사람이 쓴 문장은 외래 툴 이름을 포함해도 유지한다.

        F2/F3 의 위험은 하네스의 명령형 지시를 중계하는 것이고, 사람의 문장은
        그 사람의 권위다. 그리고 키워드 금지는 거짓 양성을 낸다 — 이 대화의
        산문에 <system-reminder> 가 146회 등장한다.
        """
        text = "read_codex.py의 function_call_output 파싱이 빈 문자열 반환"
        self.assertTrue(guard.safe(text, "human"))
        self.assertTrue(guard.safe("Bash 로 pytest 돌려줘", "human"))

    def test_human_envelope_is_still_dropped(self):
        """봉투는 사람이 타이핑한 것이 아니라 하네스가 만든 레코드다."""
        self.assertFalse(guard.safe("<command-name>/model</command-name>", "human"))

    def test_agent_text_with_a_marker_is_dropped(self):
        self.assertFalse(guard.safe("여기 <system-reminder> 가 있다", "agent"))

    def test_harness_text_is_always_dropped(self):
        self.assertFalse(guard.safe("무해해 보이는 문장", "harness"))

    def test_synthetic_interrupted_for_tool_use_is_dropped(self):
        """실물 세션에서 목격된 문자열이다 — 구조적 마커 없이 정확한 텍스트로만 식별된다."""
        self.assertFalse(
            guard.safe("[Request interrupted by user for tool use]", "human")
        )

    def test_agent_plain_text_is_kept(self):
        self.assertTrue(guard.safe("테스트 3개가 실패했다", "agent"))

    @unittest.skipUnless(have_fixtures, MISSING)
    def test_real_skill_listing_body_is_dropped(self):
        body = attachment_text("skill_listing", "content")
        self.assertGreater(len(body), 20000)
        self.assertFalse(guard.safe(body, "agent"))

    @unittest.skipUnless(have_fixtures, MISSING)
    def test_marker_detection_alone_would_have_missed_the_biggest_machinery_blob(self):
        """실물로 확인된 사실을 회귀 테스트로 고정한다.

        skill_listing 본문 29,958자에는 FOREIGN_MARKERS 중 어느 것도 없다 — 태그가
        없는 평범한 불릿 목록이다. 그래서 마커 기반 탐지만으로는 통과시킨다.
        길이 상한이 필요한 이유가 취향이 아니라 측정이라는 근거.
        """
        body = attachment_text("skill_listing", "content")
        self.assertFalse(
            any(marker in body for marker in guard.FOREIGN_MARKERS),
            "이 단정이 깨지면 길이 상한 대신 마커로도 잡힌다는 뜻이니 주석을 갱신하라",
        )
        self.assertGreater(len(body), guard.MAX_DERIVED_CHARS)

    @unittest.skipUnless(have_fixtures, MISSING)
    def test_real_prompt_snapshot_is_dropped(self):
        body = attachment_text("prompt_snapshot", "systemPrompt")
        self.assertFalse(guard.safe(body, "agent"))

    @unittest.skipUnless(have_fixtures, MISSING)
    def test_real_codex_developer_block_is_dropped(self):
        body = codex_developer_text()
        self.assertIn("<skills_instructions>", body)
        self.assertFalse(guard.safe(body, "agent"))


class TestHandoffEcho(unittest.TestCase):
    """#24: 받는 에이전트가 주입된 [omhc] 블록을 인용하면 반대 방향 핸드오프의
    PLAN? 에 그 블록이 통째로 중첩된다. 진짜 mint() 산출물로 재현한다 — 헤더
    문구를 손으로 베껴 쓰면 mint 가 실제로 내는 것과 어긋날 수 있다.
    """

    def _real_handoff_block(self) -> str:
        from omhc import adapter as A
        from omhc import mint
        from omhc.event import Event

        ref = A.SessionRef(
            adapter_id="claude-code", session_id="01a0c9f4-06aa-72d0",
            source_path="/x.jsonl", cwd="/repo", epoch=1700000000.0, size=100,
        )
        events = [
            Event(seq=1, epoch=1700000000.0, author="human", verb="said", ok=True,
                  text="로그인 버그 고쳐줘", arg="", paths=(), offset=0, length=10),
        ]
        read = A.SessionRead(ref=ref, events=tuple(events), unparsed=0, dropped={})
        out = mint.mint(read, to_adapter_id="codex-cli", budget=900, now=1700000900.0)
        self.assertTrue(out, "테스트가 재현하려면 mint() 가 실제로 뭔가를 내야 한다")
        return out

    def test_agent_utterance_quoting_a_real_minted_block_is_dropped(self):
        block = self._real_handoff_block()
        quoted = "이전 세션 요약을 받았다:\n\n" + block + "\n\n계속 진행하겠습니다."
        self.assertFalse(guard.safe(quoted, "agent"))

    def test_agent_mentioning_omhc_in_passing_is_kept(self):
        self.assertTrue(guard.safe("the [omhc] tool을 써서 컨텍스트를 넘겼다", "agent"))

    def test_human_pasting_the_block_is_still_human(self):
        """사람이 블록을 그대로 붙여 넣어도 사람의 발화라는 사실은 바뀌지 않는다."""
        block = self._real_handoff_block()
        self.assertTrue(guard.safe(block, "human"))


class TestRedaction(unittest.TestCase):
    def test_long_base64_runs_are_redacted(self):
        blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5" * 4
        out = guard.redact_b64("key=" + blob)
        self.assertNotIn(blob, out)
        self.assertIn("[b64", out)

    def test_short_words_are_not_redacted(self):
        self.assertEqual(guard.redact_b64("hello world"), "hello world")


if __name__ == "__main__":
    unittest.main()
