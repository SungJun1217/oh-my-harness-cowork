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


if __name__ == "__main__":
    unittest.main()
