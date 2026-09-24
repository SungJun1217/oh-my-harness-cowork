from __future__ import annotations

import os
import tempfile
import unittest

from omhc import locate

from ._repo import REPO



class TestLocate(unittest.TestCase):
    def test_repo_key_is_basename_plus_sha1_prefix(self):
        """공식을 단정한다. 리터럴을 박으면 다른 체크아웃에서 깨진다."""
        import hashlib

        expected = "{}-{}".format(
            os.path.basename(REPO),
            hashlib.sha1(REPO.encode("utf-8")).hexdigest()[:8],
        )
        self.assertEqual(locate.repo_key(REPO), expected)

    def test_known_value_for_this_machine(self):
        """이 머신의 실측 값. 다른 경로에서는 건너뛴다."""
        if REPO != "/home/ec2-user/capstone/oh-my-harness-cowork":
            self.skipTest("다른 체크아웃 경로: {}".format(REPO))
        self.assertEqual(locate.repo_key(REPO), "oh-my-harness-cowork-25358bbb")

    def test_repo_key_is_stable_and_8_hex_chars(self):
        key = locate.repo_key("/tmp/whatever")
        name, _, suffix = key.rpartition("-")
        self.assertEqual(name, "whatever")
        self.assertEqual(len(suffix), 8)
        self.assertTrue(all(c in "0123456789abcdef" for c in suffix))

    def test_resolve_repo_root_from_subdirectory_returns_toplevel(self):
        sub = os.path.join(REPO, "omhc", "adapters")
        self.assertEqual(locate.resolve_repo_root(sub), REPO)

    def test_resolve_repo_root_outside_git_returns_realpath_of_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(locate.resolve_repo_root(d), os.path.realpath(d))

    def test_is_within_is_equal_or_descendant(self):
        self.assertTrue(locate.is_within(REPO, REPO))
        self.assertTrue(locate.is_within(REPO, os.path.join(REPO, "docs")))
        self.assertFalse(locate.is_within(REPO, "/home/ec2-user"))
        self.assertFalse(locate.is_within(REPO, REPO + "-other"))

    def test_relativize_returns_posix_or_none(self):
        self.assertEqual(
            locate.relativize(REPO, os.path.join(REPO, "omhc/locate.py")),
            "omhc/locate.py",
        )
        self.assertIsNone(locate.relativize(REPO, "/etc/hosts"))

    def test_state_dir_is_under_home_dot_omhc(self):
        self.assertEqual(locate.state_dir("k", home="/h"), "/h/.omhc/k")


if __name__ == "__main__":
    unittest.main()
