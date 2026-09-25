from __future__ import annotations

import os
import tempfile
import unittest

from omhc import managed_block as MB


class TestSplice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "AGENTS.md")

    def tearDown(self):
        self.tmp.cleanup()

    def read(self) -> str:
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_creates_the_file_when_absent(self):
        MB.splice(self.path, "hello", captured_at=1000.0)
        self.assertTrue(os.path.exists(self.path))
        self.assertIn("hello", self.read())

    def test_markers_wrap_the_body(self):
        MB.splice(self.path, "hello", captured_at=1000.0)
        text = self.read()
        self.assertIn(MB.BEGIN_PREFIX, text)
        self.assertIn(MB.END, text)

    def test_second_splice_replaces_rather_than_appends(self):
        MB.splice(self.path, "first", captured_at=1000.0)
        MB.splice(self.path, "second", captured_at=2000.0)
        text = self.read()
        self.assertEqual(text.count(MB.END), 1)
        self.assertIn("second", text)
        self.assertNotIn("first", text)

    def test_surrounding_content_is_preserved(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("# My project\n\n사람이 쓴 지침\n")
        MB.splice(self.path, "handoff", captured_at=1000.0)
        text = self.read()
        self.assertIn("# My project", text)
        self.assertIn("사람이 쓴 지침", text)
        self.assertIn("handoff", text)

    def test_strip_leaves_no_marker_and_restores_original(self):
        original = "# My project\n\n사람이 쓴 지침\n"
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(original)
        MB.splice(self.path, "handoff", captured_at=1000.0)
        self.assertTrue(MB.strip(self.path))
        self.assertEqual(self.read(), original)

    def test_strip_deletes_a_file_that_only_held_the_block(self):
        MB.splice(self.path, "handoff", captured_at=1000.0)
        self.assertTrue(MB.strip(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_strip_on_a_file_without_a_block_returns_false(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("no block here\n")
        self.assertFalse(MB.strip(self.path))
        self.assertEqual(self.read(), "no block here\n")

    def test_strip_on_a_missing_file_returns_false(self):
        self.assertFalse(MB.strip(self.path))

    def test_captured_at_is_readable_from_the_begin_marker(self):
        MB.splice(self.path, "handoff", captured_at=1758500000.0)
        self.assertEqual(MB.installed_captured_at(self.path), 1758500000.0)

    def test_captured_at_of_missing_block_is_none(self):
        self.assertIsNone(MB.installed_captured_at(self.path))

    def test_write_is_atomic_leaving_no_tmp_file(self):
        MB.splice(self.path, "handoff", captured_at=1000.0)
        leftovers = [n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_file_header_is_written_only_on_creation(self):
        MB.splice(self.path, "a", captured_at=1000.0, file_header="# AGENTS\n")
        MB.splice(self.path, "b", captured_at=2000.0, file_header="# AGENTS\n")
        self.assertEqual(self.read().count("# AGENTS"), 1)

    def test_is_stale_uses_the_captured_stamp(self):
        MB.splice(self.path, "handoff", captured_at=1000.0)
        self.assertFalse(MB.is_stale(self.path, now=1000.0 + 10))
        self.assertTrue(
            MB.is_stale(self.path, now=1000.0 + MB.STALE_AFTER_SECONDS + 1)
        )

    def test_body_containing_marker_like_text_does_not_break_parsing(self):
        MB.splice(self.path, "논의: " + MB.END + " 라는 마커", captured_at=1000.0)
        # 본문이 END 를 담으면 구조가 깨진다 → splice 가 중화해야 한다.
        self.assertEqual(self.read().count(MB.END), 1)
        self.assertTrue(MB.strip(self.path))
        self.assertFalse(os.path.exists(self.path))


class TestTopPlacementRoundTrip(unittest.TestCase):
    """#33: 구간은 이제 파일 맨 앞이다 — splice/strip 이 사용자 콘텐츠를 있는
    그대로(개행 방식·트레일링 개행 유무까지) 되돌리는지 확인한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "AGENTS.md")

    def tearDown(self):
        self.tmp.cleanup()

    def readb(self) -> bytes:
        with open(self.path, "rb") as fh:
            return fh.read()

    def test_new_block_is_at_the_top_of_an_existing_file(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("# user content\n")
        MB.splice(self.path, "handoff", captured_at=1000.0)
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith(MB.BEGIN_PREFIX))
        self.assertLess(text.index(MB.END), text.index("# user content"))

    def test_round_trip_on_an_empty_file(self):
        open(self.path, "w").close()
        MB.splice(self.path, "handoff", captured_at=1000.0)
        self.assertTrue(MB.strip(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_round_trip_preserves_content_without_a_trailing_newline(self):
        original = b"# no trailing newline here"
        with open(self.path, "wb") as fh:
            fh.write(original)
        MB.splice(self.path, "handoff", captured_at=1000.0)
        self.assertTrue(MB.strip(self.path))
        self.assertEqual(self.readb(), original)

    def test_round_trip_preserves_crlf_content(self):
        original = b"# title\r\n\r\nbody line\r\n"
        with open(self.path, "wb") as fh:
            fh.write(original)
        MB.splice(self.path, "handoff", captured_at=1000.0)
        self.assertTrue(MB.strip(self.path))
        self.assertEqual(self.readb(), original)

    def test_legacy_end_block_is_moved_to_the_top_on_next_splice(self):
        original = "# user content\n\nmore\n"
        # 예전(#33 이전) 배치를 손으로 흉내낸다: 사용자 콘텐츠 + 구분용 빈 줄 +
        # 구간(파일 끝).
        legacy_block = "{}\nold\n{}\n".format(MB._begin(1000.0), MB.END)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(original + "\n" + legacy_block)

        MB.splice(self.path, "new", captured_at=2000.0)
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith(MB.BEGIN_PREFIX))
        self.assertIn("new", text)
        self.assertNotIn("old", text)
        self.assertIn(original, text)
        self.assertEqual(text.count(MB.END), 1)

        self.assertTrue(MB.strip(self.path))
        with open(self.path, encoding="utf-8") as fh:
            # 예전 배치가 끼워 넣었던 구분용 빈 줄 하나는 이제 사용자 콘텐츠로
            # 취급돼 그대로 남는다(무해한 흔적 한 바이트) — 위치로 "이게
            # omhc 가 넣은 빈 줄인지" 추측하다 실제 사용자 콘텐츠를 통째로
            # 버리는 쪽보다 안전하다(리뷰 결함 #1).
            self.assertEqual(fh.read(), original + "\n")

    def test_strip_keeps_a_line_the_user_added_above_the_block(self):
        """리뷰 결함 #1 (A): 블록이 맨 앞이어도, 그 위에 사용자가 줄을 더하면
        `before` 가 더 이상 비지 않는다 — 이전 버전은 그러면 `after`(원래
        블록 뒤 콘텐츠) 전체를 버렸다."""
        MB.splice(self.path, "handoff", captured_at=1000.0)
        with open(self.path, encoding="utf-8") as fh:
            with_block = fh.read()
        edited = "# user heading added on top\n" + with_block
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(edited)

        self.assertTrue(MB.strip(self.path))
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# user heading added on top", text)
        self.assertNotIn(MB.END, text)

    def test_strip_keeps_content_that_trails_a_legacy_end_block(self):
        """리뷰 결함 #1 (B): 예전 배치의 구간 뒤에 사용자 콘텐츠가 더 있으면
        (구간이 파일의 진짜 끝이 아니면) 이전 버전은 그 뒤쪽 콘텐츠를 버렸다."""
        legacy_block = "{}\nold\n{}\n".format(MB._begin(1000.0), MB.END)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("# content\n\n" + legacy_block + "user appended after\n")

        self.assertTrue(MB.strip(self.path))
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# content", text)
        self.assertIn("user appended after", text)
        self.assertNotIn(MB.END, text)

    def test_splice_keeps_content_on_both_sides_of_a_block_in_the_middle(self):
        legacy_block = "{}\nold\n{}\n".format(MB._begin(1000.0), MB.END)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("before text\n" + legacy_block + "after text\n")

        MB.splice(self.path, "new", captured_at=2000.0)
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("before text", text)
        self.assertIn("after text", text)
        self.assertIn("new", text)
        self.assertNotIn("old", text)
        self.assertEqual(text.count(MB.END), 1)

    def test_no_non_block_byte_of_the_input_is_ever_lost(self):
        """속성 테스트: 블록 구간만 빠지고 나머지 바이트는 그대로 순서대로 남는다
        — 위치(맨 앞/중간/맨 끝)나 줄바꿈 모양과 상관없이(리뷰: 조각 순서만 보던
        옛 검사는 줄바꿈·\r 손실을 못 잡았다)."""
        block = "{}\nold\n{}\n".format(MB._begin(1000.0), MB.END)
        shapes = [
            "",
            "only before, no block",
            block,
            "before\n" + block,
            block + "after\n",
            "before\n" + block + "after\n",
            "line1\nline2\n" + block + "line3\nline4\n",
            "a\r\nb\r\n" + block + "c\r\nd",
            "\ufeff# t\n" + block + "tail",
            "\n\nlead\n" + block,
            "top\n" + block + "mid\n" + block + "tail\n",
            "한글 앞\n" + block + "한글 뒤",
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                self.assertEqual(MB._without_block(shape), shape.replace(block, "", 1))

    def test_file_round_trip_is_byte_identical_for_every_shape(self):
        """splice → splice → strip 을 실제 파일로 돌리면 원래 바이트가 그대로다."""
        shapes = [b"x", b"a\r\nb\r\n", b"a\r\nb", b"\xef\xbb\xbf# t\n",
                  b"\n\nlead", "한글\n본문\n".encode("utf-8")]
        for raw in shapes:
            with self.subTest(raw=raw):
                with open(self.path, "wb") as fh:
                    fh.write(raw)
                MB.splice(self.path, "first", captured_at=1000.0)
                MB.splice(self.path, "second", captured_at=2000.0)
                MB.strip(self.path)
                with open(self.path, "rb") as fh:
                    self.assertEqual(fh.read(), raw)

    def test_prospective_block_end_bytes_matches_the_actual_write(self):
        predicted = MB.prospective_block_end_bytes(
            self.path, "handoff", captured_at=1000.0)
        MB.splice(self.path, "handoff", captured_at=1000.0)
        actual = MB.installed_block_end_bytes(self.path)
        self.assertEqual(predicted, actual)

    def test_prospective_block_end_bytes_is_independent_of_existing_size(self):
        small = MB.prospective_block_end_bytes(
            self.path, "handoff", captured_at=1000.0)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("x" * 100_000)
        big = MB.prospective_block_end_bytes(
            self.path, "handoff", captured_at=1000.0)
        self.assertEqual(small, big)


if __name__ == "__main__":
    unittest.main()
