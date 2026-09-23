from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

from omhc import adapter as A
from omhc import agents_md, managed_block


from ._repo import git  # noqa: E402  (공유 정의)


class TestAgentsMd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        git(self.repo, "init", "-q")
        self.path = os.path.join(self.repo, "AGENTS.md")

    def tearDown(self):
        self.tmp.cleanup()

    def bundle(self, body="[omhc] handoff\nGOAL  x\n"):
        return A.HandoffBundle(body_md=body, repo_root=self.repo,
                               to_adapter_id="codex-cli")

    def exclude_text(self) -> str:
        path = os.path.join(self.repo, ".git", "info", "exclude")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_install_writes_a_managed_block(self):
        receipt = agents_md.install(self.bundle(), now=1000.0)
        self.assertEqual(receipt.channel, "agents-md")
        self.assertIn(self.path, receipt.paths_written)
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("GOAL  x", text)
        self.assertIn(managed_block.END, text)

    def test_install_registers_the_file_in_git_info_exclude(self):
        """사용자 결정: 허용하되 git status 에 보이지 않고 클론 밖으로 나가지 않는다."""
        agents_md.install(self.bundle(), now=1000.0)
        self.assertIn("AGENTS.md", self.exclude_text())

    def test_exclude_registration_is_idempotent(self):
        agents_md.install(self.bundle(), now=1000.0)
        agents_md.install(self.bundle(), now=2000.0)
        self.assertEqual(self.exclude_text().count("AGENTS.md"), 1)

    def test_agents_md_does_not_show_up_in_git_status(self):
        agents_md.install(self.bundle(), now=1000.0)
        out = subprocess.run(["git", "-C", self.repo, "status", "--porcelain"],
                             capture_output=True, text=True, check=True)
        self.assertNotIn("AGENTS.md", out.stdout)

    def test_already_tracked_agents_md_is_never_excluded(self):
        """추적 중인 파일을 exclude 에 넣어도 무효이고, 사용자 파일을 건드리면 안 된다."""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("# 사람이 쓴 지침\n")
        git(self.repo, "add", "AGENTS.md")
        git(self.repo, "-c", "user.email=t@e", "-c", "user.name=t",
            "commit", "-qm", "add agents")
        receipt = agents_md.install(self.bundle(), now=1000.0)
        self.assertNotIn("AGENTS.md", self.exclude_text())
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# 사람이 쓴 지침", text)
        self.assertIn("tracked", receipt.cleanup_hint)

    def test_second_install_replaces_the_block(self):
        agents_md.install(self.bundle("first"), now=1000.0)
        agents_md.install(self.bundle("second"), now=2000.0)
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("second", text)
        self.assertNotIn("first", text)
        self.assertEqual(text.count(managed_block.END), 1)

    def test_collapse_removes_a_stale_block(self):
        agents_md.install(self.bundle(), now=1000.0)
        collapsed = agents_md.collapse(
            self.repo, now=1000.0 + managed_block.STALE_AFTER_SECONDS + 1
        )
        self.assertTrue(collapsed)
        self.assertFalse(os.path.exists(self.path))

    def test_collapse_keeps_a_fresh_block(self):
        agents_md.install(self.bundle(), now=1000.0)
        self.assertFalse(agents_md.collapse(self.repo, now=1000.0 + 60))
        self.assertTrue(os.path.exists(self.path))

    def test_collapse_forced_removes_even_a_fresh_block(self):
        agents_md.install(self.bundle(), now=1000.0)
        self.assertTrue(agents_md.collapse(self.repo, now=1000.0 + 60, force=True))

    def test_collapse_without_a_block_is_a_noop(self):
        self.assertFalse(agents_md.collapse(self.repo, now=1000.0))

    def test_install_outside_a_git_repo_still_writes_the_block(self):
        with tempfile.TemporaryDirectory() as plain:
            bundle = A.HandoffBundle(body_md="x", repo_root=plain,
                                     to_adapter_id="codex-cli")
            receipt = agents_md.install(bundle, now=1000.0)
            self.assertTrue(os.path.exists(os.path.join(plain, "AGENTS.md")))
            self.assertTrue(receipt.paths_written)

    def test_receipt_is_not_consumed_on_read(self):
        """AGENTS.md 는 세션마다 다시 읽히므로 한 번 읽고 사라지지 않는다."""
        receipt = agents_md.install(self.bundle(), now=1000.0)
        self.assertFalse(receipt.consumed_on_read)
        self.assertIn("omhc", receipt.cleanup_hint)


if __name__ == "__main__":
    unittest.main()
