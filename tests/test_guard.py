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
    """Pull a machinery body out of a fixture. Lets the test use genuinely adversarial input."""
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
    raise AssertionError("could not find {}.{} in fixture".format(kind, field))


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
    raise AssertionError("could not find a Codex developer record")


class TestDenylistHygiene(unittest.TestCase):
    def test_no_marker_contains_a_backslash(self):
        """A literal with a stray backslash matches real bytes zero times — a
        denylist entry that's silently dead."""
        for marker in guard.FOREIGN_MARKERS:
            self.assertNotIn("\\", marker, "a marker with a backslash never matches real bytes")

    def test_markers_are_lowercase_ascii_tags(self):
        for marker in guard.FOREIGN_MARKERS:
            self.assertTrue(marker.startswith("<"), marker)
            self.assertTrue(marker.endswith(">"), marker)

    @unittest.skipUnless(have_fixtures, MISSING)
    def test_every_marker_is_witnessed_in_real_bytes(self):
        """Checks each entry individually. A batch assertion that passes on a
        single match would let a bug slip through."""
        blob = raw_bytes()
        for marker in guard.FOREIGN_MARKERS:
            with self.subTest(marker=marker):
                if marker in guard.UNWITNESSED_OK:
                    self.assertTrue(
                        guard.UNWITNESSED_OK[marker],
                        "an UNWITNESSED_OK entry needs a reason",
                    )
                    continue
                self.assertIn(
                    marker.encode("utf-8"),
                    blob,
                    "marker not witnessed in the fixture. If there is no evidence in the wild, list it in UNWITNESSED_OK with a reason",
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
        """A never-before-seen tag must be caught automatically too."""
        self.assertTrue(guard.is_envelope("<never_seen_before_2099>x</never_seen_before_2099>"))


class TestSafeScopedByProvenance(unittest.TestCase):
    def test_human_text_is_kept_even_with_a_foreign_tool_name(self):
        """User decision (a): a human-written sentence is kept even if it
        contains a foreign tool name.

        F2/F3's risk is relaying the harness's imperative instructions, and a
        human's sentence carries that human's authority. And banning keywords
        produces false positives — <system-reminder> appears 146 times in this
        conversation's prose.
        """
        text = "read_codex.py의 function_call_output 파싱이 빈 문자열 반환"
        self.assertTrue(guard.safe(text, "human"))
        self.assertTrue(guard.safe("Bash 로 pytest 돌려줘", "human"))

    def test_human_envelope_is_still_dropped(self):
        """An envelope is a record the harness built, not something a human typed."""
        self.assertFalse(guard.safe("<command-name>/model</command-name>", "human"))

    def test_agent_text_with_a_marker_is_dropped(self):
        self.assertFalse(guard.safe("여기 <system-reminder> 가 있다", "agent"))

    def test_harness_text_is_always_dropped(self):
        self.assertFalse(guard.safe("무해해 보이는 문장", "harness"))

    def test_synthetic_interrupted_for_tool_use_is_dropped(self):
        """A string observed in a real session — identified only by exact text,
        with no structural marker."""
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
        """Pins a fact confirmed in the wild as a regression test.

        The skill_listing body's 29,958 chars contain none of FOREIGN_MARKERS —
        it's a plain bulleted list with no tags. So marker-based detection alone
        would let it through. Evidence that a length cap is needed as a measured
        fact, not a matter of taste.
        """
        body = attachment_text("skill_listing", "content")
        self.assertFalse(
            any(marker in body for marker in guard.FOREIGN_MARKERS),
            "if this assertion breaks, markers now catch it too instead of the length cap, so update the comment",
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
    """#24: if the receiving agent quotes the injected [omhc] block, the
    reverse-direction handoff's PLAN? nests that whole block. Reproduced with
    real mint() output — hand-copying the header wording could drift from what
    mint actually emits.
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
        self.assertTrue(out, "for this test to reproduce anything, mint() must actually emit something")
        return out

    def test_agent_utterance_quoting_a_real_minted_block_is_dropped(self):
        block = self._real_handoff_block()
        quoted = "이전 세션 요약을 받았다:\n\n" + block + "\n\n계속 진행하겠습니다."
        self.assertFalse(guard.safe(quoted, "agent"))

    def test_agent_mentioning_omhc_in_passing_is_kept(self):
        self.assertTrue(guard.safe("the [omhc] tool을 써서 컨텍스트를 넘겼다", "agent"))

    def test_human_pasting_the_block_is_still_human(self):
        """Even if a human pastes the block verbatim, it's still a human utterance."""
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
