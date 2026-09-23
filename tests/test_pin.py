from __future__ import annotations

import os
import tempfile
import unittest

from omhc import pin

from . import _repo


def ref_for(path: str):
    # 사이드카 디렉터리 이름이 session_id 에서 나온다 — 테스트가 "sess1" 을 심는다.
    return _repo.ref_for("claude-code", path, session_id="sess1", cwd="/repo")


class TestPin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")
        self.src = os.path.join(self.tmp.name, "live.jsonl")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write('{"type":"user"}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_pinned_file_shares_an_inode_with_the_original(self):
        pinned = pin.pin_session(self.state, ref_for(self.src))
        self.assertEqual(os.stat(self.src).st_ino, os.stat(pinned).st_ino)

    def test_appends_to_the_original_are_visible_through_the_pin(self):
        pinned = pin.pin_session(self.state, ref_for(self.src))
        with open(self.src, "a", encoding="utf-8") as fh:
            fh.write('{"type":"assistant"}\n')
        with open(pinned, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 2)

    def test_bytes_survive_removal_of_the_original_dirent(self):
        pinned = pin.pin_session(self.state, ref_for(self.src))
        os.unlink(self.src)
        with open(pinned, encoding="utf-8") as fh:
            self.assertIn('"user"', fh.read())

    def test_pinning_costs_no_extra_disk(self):
        before = os.stat(self.src).st_nlink
        pin.pin_session(self.state, ref_for(self.src))
        self.assertEqual(os.stat(self.src).st_nlink, before + 1)

    def test_pin_is_idempotent(self):
        first = pin.pin_session(self.state, ref_for(self.src))
        second = pin.pin_session(self.state, ref_for(self.src))
        self.assertEqual(first, second)
        self.assertEqual(os.stat(self.src).st_ino, os.stat(second).st_ino)

    def test_sidecar_tool_results_are_pinned_too(self):
        """큰 tool_result 는 <persisted-output> 스텁으로 치환되고 내용이 외부화된다.

        사이드카를 같이 고정하지 않으면 스텁이 해소되지 않는다.
        """
        sidecar_dir = os.path.join(self.tmp.name, "sess1", "tool-results")
        os.makedirs(sidecar_dir)
        with open(os.path.join(sidecar_dir, "abc.txt"), "w", encoding="utf-8") as fh:
            fh.write("externalized output")
        pinned = pin.pin_session(self.state, ref_for(self.src))
        mirrored = os.path.join(os.path.dirname(pinned), "tool-results", "abc.txt")
        self.assertTrue(os.path.exists(mirrored))
        with open(mirrored, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "externalized output")

    def test_missing_source_does_not_raise(self):
        ref = ref_for(self.src)
        os.unlink(self.src)
        self.assertIsNone(pin.pin_session(self.state, ref))

    def test_result_reports_sidecar_count(self):
        sidecar_dir = os.path.join(self.tmp.name, "sess1", "tool-results")
        os.makedirs(sidecar_dir)
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(sidecar_dir, name), "w", encoding="utf-8") as fh:
                fh.write("x")
        result = pin.pin_session_result(self.state, ref_for(self.src))
        self.assertTrue(result.linked)
        self.assertEqual(result.sidecars, 2)
        self.assertEqual(result.error, "")

    def test_cross_device_failure_is_reported_not_silent(self):
        """실측 조건: 이 머신의 /tmp 는 tmpfs(dev=35), 홈은 dev=66305 다.

        조용히 None 을 돌려주면 아카이브가 없는 것을 아무도 모른다.
        """
        home_src = os.path.join(os.path.expanduser("~"), ".omhc-pin-probe.jsonl")
        with open(home_src, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        try:
            if os.stat(home_src).st_dev == os.stat("/tmp").st_dev:
                self.skipTest("이 머신에서는 /tmp 와 홈이 같은 장치다")
            result = pin.pin_session_result(self.state, ref_for(home_src))
            self.assertFalse(result.linked)
            self.assertIsNone(result.path)
            self.assertIn("cross-device", result.error)
            self.assertFalse(bool(result))
        finally:
            os.unlink(home_src)


if __name__ == "__main__":
    unittest.main()
