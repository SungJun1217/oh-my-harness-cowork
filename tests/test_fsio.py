from __future__ import annotations

import json
import os
import tempfile
import unittest

from omhc import fsio


class TestWriteAtomic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "deep", "nested", "f.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_parent_directories(self):
        fsio.write_atomic(self.path, "hello")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello")

    def test_leaves_no_tmp_file_behind(self):
        fsio.write_atomic(self.path, "hello")
        directory = os.path.dirname(self.path)
        self.assertEqual([n for n in os.listdir(directory) if n.endswith(".tmp")], [])

    def test_replaces_existing_content_wholesale(self):
        fsio.write_atomic(self.path, "first")
        fsio.write_atomic(self.path, "second")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "second")

    def test_fsync_can_be_skipped_but_defaults_on(self):
        fsio.write_atomic(self.path, "x", fsync=False)
        self.assertTrue(os.path.exists(self.path))

    def test_unwritable_target_raises_oserror_for_the_caller_to_handle(self):
        """훅 경로의 호출자가 try/except OSError 로 감싸는 계약을 유지한다."""
        with self.assertRaises(OSError):
            fsio.write_atomic("/proc/omhc-nonexistent/f.txt", "x")


class TestAppend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "log.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_line_adds_exactly_one_newline(self):
        fsio.append_line(self.path, "a")
        fsio.append_line(self.path, "b\n")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "a\nb\n")

    def test_append_line_is_atomic_under_concurrency(self):
        pids = []
        for _ in range(8):
            pid = os.fork()
            if pid == 0:
                try:
                    for i in range(200):
                        fsio.append_line(self.path, json.dumps({"n": i}))
                finally:
                    os._exit(0)
            pids.append(pid)
        for pid in pids:
            os.waitpid(pid, 0)
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1600)
        for line in lines:
            json.loads(line)

    def test_append_line_creates_the_file_with_owner_only_mode(self):
        fsio.append_line(self.path, "a")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_append_blob_writes_many_lines_at_once(self):
        fsio.append_blob(self.path, "a\nb\nc\n")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines(), ["a", "b", "c"])


class TestReadHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_text_returns_default_when_missing(self):
        self.assertEqual(fsio.read_text("/nope/missing", "fallback"), "fallback")

    def test_size_of_returns_default_when_missing(self):
        self.assertEqual(fsio.size_of("/nope/missing", -1), -1)

    def test_unlink_quiet_reports_whether_it_removed_anything(self):
        path = os.path.join(self.tmp.name, "f")
        open(path, "w").close()
        self.assertTrue(fsio.unlink_quiet(path))
        self.assertFalse(fsio.unlink_quiet(path))

    def test_listdir_suffix_is_sorted_and_tolerates_a_missing_dir(self):
        for name in ("b.idx", "a.idx", "c.txt"):
            open(os.path.join(self.tmp.name, name), "w").close()
        got = fsio.listdir_suffix(self.tmp.name, ".idx")
        self.assertEqual([os.path.basename(p) for p in got], ["a.idx", "b.idx"])
        self.assertEqual(fsio.listdir_suffix("/nope/missing", ".idx"), [])

    def test_same_inode_returns_none_when_a_path_is_missing(self):
        path = os.path.join(self.tmp.name, "f")
        open(path, "w").close()
        self.assertTrue(fsio.same_inode(path, path))
        self.assertIsNone(fsio.same_inode(path, "/nope/missing"))


class TestClaimExclusive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "claim")

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_succeeds_once_then_fails(self):
        self.assertTrue(fsio.claim_exclusive(self.path, "pid"))
        self.assertFalse(fsio.claim_exclusive(self.path, "pid"))

    def test_claim_writes_the_contents(self):
        fsio.claim_exclusive(self.path, "12345")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "12345")

    def test_claim_on_an_unwritable_parent_returns_false(self):
        self.assertFalse(fsio.claim_exclusive("/proc/omhc-nonexistent/x"))

    def test_only_one_process_wins_the_claim(self):
        read_fd, write_fd = os.pipe()
        pids = []
        for _ in range(8):
            pid = os.fork()
            if pid == 0:
                try:
                    won = fsio.claim_exclusive(self.path, str(os.getpid()))
                    os.write(write_fd, b"1" if won else b"0")
                finally:
                    os._exit(0)
            pids.append(pid)
        for pid in pids:
            os.waitpid(pid, 0)
        os.close(write_fd)
        data = os.read(read_fd, 64)
        os.close(read_fd)
        self.assertEqual(data.count(b"1"), 1)


if __name__ == "__main__":
    unittest.main()
