from __future__ import annotations

import os
import tempfile
import unittest

from omhc import locate

from . import _repo
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

    def test_owning_repo_key_is_none_without_a_cwd(self):
        self.assertIsNone(locate.owning_repo_key(None))
        self.assertIsNone(locate.owning_repo_key(""))

    def test_owning_repo_key_matches_repo_key_of_the_resolved_root(self):
        sub = os.path.join(REPO, "omhc", "adapters")
        self.assertEqual(locate.owning_repo_key(sub), locate.repo_key(REPO))

    def test_owning_repo_key_finds_a_nested_git_root_before_a_non_git_parent(self):
        with tempfile.TemporaryDirectory() as parent:
            child = os.path.join(parent, "child")
            os.makedirs(child)
            _repo.git(child, "init", "-q")
            self.assertEqual(locate.owning_repo_key(child),
                             locate.repo_key(os.path.realpath(child)))
            self.assertNotEqual(locate.owning_repo_key(child),
                                locate.repo_key(os.path.realpath(parent)))

    def test_omhc_root_marker_in_a_parent_wins_over_the_cwd_fallback(self):
        with tempfile.TemporaryDirectory() as parent:
            open(os.path.join(parent, ".omhc-root"), "w").close()
            sub = os.path.join(parent, "a", "b")
            os.makedirs(sub)
            self.assertEqual(locate.resolve_repo_root(sub), os.path.realpath(parent))

    def test_omhc_root_marker_may_be_a_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            os.makedirs(os.path.join(parent, ".omhc-root"))
            self.assertEqual(locate.resolve_repo_root(parent), os.path.realpath(parent))

    def test_nested_git_beats_a_parent_omhc_root_marker(self):
        """가장 가까운 조상이 이긴다 — .omhc-root 가 상위에 있어도 자기 .git 을
        가진 자식이 우선."""
        with tempfile.TemporaryDirectory() as parent:
            open(os.path.join(parent, ".omhc-root"), "w").close()
            child = os.path.join(parent, "child")
            os.makedirs(child)
            _repo.git(child, "init", "-q")
            self.assertEqual(locate.resolve_repo_root(child), os.path.realpath(child))

    def test_agents_md_and_omhc_dir_in_a_parent_are_not_markers(self):
        with tempfile.TemporaryDirectory() as parent:
            open(os.path.join(parent, "AGENTS.md"), "w").close()
            os.makedirs(os.path.join(parent, ".omhc"))
            sub = os.path.join(parent, "sub")
            os.makedirs(sub)
            self.assertEqual(locate.resolve_repo_root(sub), os.path.realpath(sub))

    def test_refused_root_rejects_only_slash(self):
        self.assertIsNotNone(locate.refused_root("/"))
        self.assertIsNone(locate.refused_root(os.path.expanduser("~")))
        self.assertIsNone(locate.refused_root(REPO))


if __name__ == "__main__":
    unittest.main()
