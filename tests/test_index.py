from __future__ import annotations

import os
import tempfile
import unittest

from omhc import index
from omhc.event import Event


def mk(seq: int, **kw) -> Event:
    base = dict(
        seq=seq, epoch=1700000000.0 + seq, author="agent", verb="ran", ok=True,
        text="", arg="pytest -q", paths=("omhc/x.py",), offset=seq * 100,
        length=100,
    )
    base.update(kw)
    return Event(**base)


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "s.idx")

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_then_read_roundtrip(self):
        index.append_rows(self.path, [mk(1), mk(2)])
        rows = index.rows(self.path)
        self.assertEqual([r.seq for r in rows], [1, 2])
        self.assertEqual(rows[0].arg, "pytest -q")
        self.assertEqual(rows[0].paths, ("omhc/x.py",))
        self.assertTrue(rows[0].ok)

    def test_row_size_is_small(self):
        index.append_rows(self.path, [mk(i) for i in range(1, 51)])
        per_row = os.path.getsize(self.path) / 50.0
        self.assertLess(per_row, 120, "행당 크기가 커지면 색인이 아카이브가 된다")

    def test_append_is_additive_not_rewriting(self):
        index.append_rows(self.path, [mk(1)])
        first = os.path.getsize(self.path)
        index.append_rows(self.path, [mk(2)])
        self.assertGreater(os.path.getsize(self.path), first)
        self.assertEqual([r.seq for r in index.rows(self.path)], [1, 2])

    def test_watermark_is_last_offset_plus_length(self):
        index.append_rows(self.path, [mk(1), mk(2)])
        self.assertEqual(index.watermark(self.path), 200 + 100)

    def test_watermark_of_missing_file_is_zero(self):
        self.assertEqual(index.watermark(os.path.join(self.tmp.name, "nope.idx")), 0)

    def test_truncated_last_row_is_skipped_and_watermark_backs_off(self):
        index.append_rows(self.path, [mk(1), mk(2)])
        with open(self.path, "r+", encoding="utf-8") as fh:
            data = fh.read()
            fh.seek(0)
            fh.truncate()
            fh.write(data[: -len(data.splitlines()[-1]) // 2])
        rows = index.rows(self.path)
        self.assertEqual([r.seq for r in rows], [1])
        self.assertEqual(index.watermark(self.path), 100 + 100)

    def test_tabs_and_newlines_in_arg_do_not_break_the_row_shape(self):
        index.append_rows(self.path, [mk(1, arg="a\tb\nc")])
        rows = index.rows(self.path)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("\t", rows[0].arg)
        self.assertNotIn("\n", rows[0].arg)

    def test_failed_events_round_trip_their_ok_flag(self):
        index.append_rows(self.path, [mk(1, ok=False)])
        self.assertFalse(index.rows(self.path)[0].ok)

    def test_arg_is_truncated_to_120(self):
        index.append_rows(self.path, [mk(1, arg="x" * 400)])
        self.assertLessEqual(len(index.rows(self.path)[0].arg), 120)

    def test_file_is_grep_able_plain_text(self):
        index.append_rows(self.path, [mk(1, arg="pytest tests/test_index.py")])
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn("pytest tests/test_index.py", fh.read())

    def test_human_events_keep_text_out_of_the_index(self):
        """색인은 포인터다. 본문을 담으면 아카이브가 원본 두 벌이 된다."""
        index.append_rows(self.path, [mk(1, author="human", verb="said",
                                         text="비밀 이야기", arg="")])
        with open(self.path, encoding="utf-8") as fh:
            self.assertNotIn("비밀 이야기", fh.read())


class TestAppendNew(unittest.TestCase):
    """커서는 seq 가 아니라 바이트 offset 이다(#23)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "s.idx")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_call_indexes_everything_with_the_parser_seq(self):
        self.assertEqual(index.append_new(self.path, [mk(1), mk(2), mk(3)]), 3)
        self.assertEqual([r.seq for r in index.rows(self.path)], [1, 2, 3])

    def test_second_call_only_adds_events_past_the_last_record(self):
        index.append_new(self.path, [mk(1), mk(2)])
        self.assertEqual(index.append_new(self.path, [mk(1), mk(2), mk(3)]), 1)
        rows = index.rows(self.path)
        self.assertEqual([r.seq for r in rows], [1, 2, 3])
        self.assertEqual([r.offset for r in rows], [100, 200, 300])

    def test_a_parser_that_drops_more_records_does_not_lose_new_events(self):
        """업그레이드 전 파서가 seq 1..5 를 매겼고, 새 파서는 그중 둘을 버려
        같은 세션의 이후 이벤트가 seq 4, 5 로 나온다. seq 커서였다면 둘 다
        `seq > 5` 에 걸려 빠졌다."""
        index.append_new(self.path, [mk(i) for i in range(1, 6)])
        reparsed = [mk(1), mk(3), mk(5),
                    mk(4, offset=600), mk(5, offset=700)]
        self.assertEqual(index.append_new(self.path, reparsed), 2)
        rows = index.rows(self.path)
        self.assertEqual([r.offset for r in rows][-2:], [600, 700])
        # 번호는 이 색인 안에서 이어진다 — 겹치면 `show <세션>#4` 가 모호해진다.
        self.assertEqual([r.seq for r in rows], [1, 2, 3, 4, 5, 6, 7])

    def test_a_parser_that_reads_more_records_does_not_duplicate(self):
        index.append_new(self.path, [mk(1), mk(3)])
        reparsed = [mk(1), mk(2, offset=150), mk(3), mk(4)]
        self.assertEqual(index.append_new(self.path, reparsed), 1)
        self.assertEqual([r.offset for r in index.rows(self.path)], [100, 300, 400])

    def test_events_sharing_one_record_are_not_split_across_calls(self):
        """Claude 의 assistant 레코드 하나가 tool_use 여러 개를 낳으면 이벤트들이
        같은 offset/length 를 공유한다."""
        same = [mk(1), mk(2, offset=100), mk(3, offset=100)]
        index.append_new(self.path, same)
        self.assertEqual(index.append_new(self.path, same + [mk(4, offset=200)]), 1)
        self.assertEqual(len(index.rows(self.path)), 4)

    def test_nothing_new_writes_nothing(self):
        index.append_new(self.path, [mk(1)])
        size = os.path.getsize(self.path)
        self.assertEqual(index.append_new(self.path, [mk(1)]), 0)
        self.assertEqual(os.path.getsize(self.path), size)


if __name__ == "__main__":
    unittest.main()
