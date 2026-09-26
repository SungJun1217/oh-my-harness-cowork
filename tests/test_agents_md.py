from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from omhc import adapter as A
from omhc import agents_md, cli, managed_block

from ._repo import TempRepo, git, plant_hook_install


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
        """User decision: allowed, but stays invisible to git status and never leaves the clone."""
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
        """Adding a tracked file to exclude is a no-op, and the user's file must not be touched."""
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

    def test_collapse_of_a_symlinked_agents_md_edits_the_target_and_keeps_the_link(self):
        """Review defect 1: if strip() replaces the link itself with a plain file, the shared wiring breaks."""
        target = os.path.join(self.repo, "docs.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        os.symlink(target, self.path)
        managed_block.splice(self.path, "[omhc] leaked\n", captured_at=1000.0)
        self.assertTrue(managed_block.strip(self.path))
        self.assertTrue(os.path.islink(self.path), "collapse must not replace the symlink")
        self.assertEqual(os.path.realpath(self.path), os.path.realpath(target))
        self.assertIsNone(managed_block.installed_captured_at(self.path))
        with open(target, encoding="utf-8") as fh:
            self.assertNotIn(managed_block.END, fh.read())

    def test_agents_md_collapse_wrapper_also_preserves_the_symlink(self):
        target = os.path.join(self.repo, "docs.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.symlink(target, self.path)
        managed_block.splice(self.path, "[omhc] leaked\n", captured_at=1000.0)
        self.assertTrue(agents_md.collapse(self.repo, now=1000.0, force=True))
        self.assertTrue(os.path.islink(self.path))
        self.assertIsNone(managed_block.installed_captured_at(self.path))

    def test_collapse_of_a_hardlinked_agents_md_edits_the_shared_inode(self):
        """Round 2 review defect: os.replace splits a hard link — this name gets a new
        inode while the other name (e.g. CLAUDE.md) keeps looking at the old inode with the block still in it."""
        other_name = os.path.join(self.repo, "CLAUDE.md")
        managed_block.splice(self.path, "[omhc] leaked\n", captured_at=1000.0)
        os.link(self.path, other_name)
        self.assertTrue(managed_block.strip(self.path))
        self.assertEqual(os.stat(self.path).st_ino, os.stat(other_name).st_ino,
                         "strip must not split the hard link")
        self.assertIsNone(managed_block.installed_captured_at(self.path))
        with open(other_name, encoding="utf-8") as fh:
            self.assertNotIn(managed_block.END, fh.read())

    def test_install_outside_a_git_repo_still_writes_the_block(self):
        with tempfile.TemporaryDirectory() as plain:
            bundle = A.HandoffBundle(body_md="x", repo_root=plain,
                                     to_adapter_id="codex-cli")
            receipt = agents_md.install(bundle, now=1000.0)
            self.assertTrue(os.path.exists(os.path.join(plain, "AGENTS.md")))
            self.assertTrue(receipt.paths_written)

    def test_receipt_is_not_consumed_on_read(self):
        """AGENTS.md is re-read every session, so it doesn't disappear after one read."""
        receipt = agents_md.install(self.bundle(), now=1000.0)
        self.assertFalse(receipt.consumed_on_read)
        self.assertIn("omhc", receipt.cleanup_hint)


class TestSharedWithClaude(unittest.TestCase):
    """If AGENTS.md's wiring also leaks into Claude Code, Path B must never be used."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.agents = os.path.join(self.repo, "AGENTS.md")
        self.claude = os.path.join(self.repo, "CLAUDE.md")

    def tearDown(self):
        self.tmp.cleanup()

    def test_agents_md_symlink_is_shared(self):
        target = os.path.join(self.repo, "docs.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.symlink(target, self.agents)
        self.assertIsNotNone(agents_md.shared_with_claude(self.repo))

    def test_claude_md_symlink_to_agents_md_is_shared(self):
        with open(self.agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        os.symlink(self.agents, self.claude)
        reason = agents_md.shared_with_claude(self.repo)
        self.assertIsNotNone(reason)
        self.assertIn("symlink", reason)

    def test_claude_md_importing_agents_md_is_shared(self):
        with open(self.agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        with open(self.claude, "w", encoding="utf-8") as fh:
            fh.write("# CLAUDE.md\n\n@AGENTS.md\n\nClaude 전용 내용\n")
        self.assertIsNotNone(agents_md.shared_with_claude(self.repo))

    def test_claude_md_merely_mentioning_agents_md_is_not_shared(self):
        with open(self.agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        with open(self.claude, "w", encoding="utf-8") as fh:
            fh.write("# CLAUDE.md\n\nSee AGENTS.md for the shared source, but not imported.\n")
        self.assertIsNone(agents_md.shared_with_claude(self.repo))

    def test_no_files_is_not_shared(self):
        self.assertIsNone(agents_md.shared_with_claude(self.repo))

    def test_claude_md_hardlinked_to_agents_md_is_shared(self):
        with open(self.agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        os.link(self.agents, self.claude)
        reason = agents_md.shared_with_claude(self.repo)
        self.assertIsNotNone(reason)
        self.assertIn("hard-linked", reason)

    def test_dot_claude_claude_md_importing_with_relative_path_is_shared(self):
        with open(self.agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        nested_dir = os.path.join(self.repo, ".claude")
        os.makedirs(nested_dir)
        with open(os.path.join(nested_dir, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write("Claude 전용 내용\n@../AGENTS.md\n")
        reason = agents_md.shared_with_claude(self.repo)
        self.assertIsNotNone(reason)
        self.assertIn(".claude", reason)

    def test_claude_local_md_importing_agents_md_is_shared(self):
        with open(self.agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        with open(os.path.join(self.repo, "CLAUDE.local.md"), "w", encoding="utf-8") as fh:
            fh.write("@AGENTS.md\n")
        self.assertIsNotNone(agents_md.shared_with_claude(self.repo))

    def test_import_with_trailing_punctuation_is_shared(self):
        with open(self.agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        with open(self.claude, "w", encoding="utf-8") as fh:
            fh.write("Read @AGENTS.md. It is the source of truth.\n")
        self.assertIsNotNone(agents_md.shared_with_claude(self.repo))

    def test_install_refuses_to_write_when_shared(self):
        with open(self.agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        os.symlink(self.agents, self.claude)
        bundle = A.HandoffBundle(body_md="[omhc] handoff\n", repo_root=self.repo,
                                 to_adapter_id="codex-cli")
        with self.assertRaises(A.NoInjectionChannel):
            agents_md.install(bundle)
        with open(self.agents, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "neutral instructions")

    def test_deliver_falls_to_file_drop_when_shared(self):
        from omhc import deliver

        with open(self.agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        os.symlink(self.agents, self.claude)
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        # detect() isn't needed to judge the codex-cli adapter as write-capable —
        # deliver only looks at capabilities; install_handoff must fail from the
        # missing hook and fall to Path B (agents_md.install) to exercise this test's target path.
        bundle = A.HandoffBundle(body_md="[omhc] handoff\nGOAL x\n",
                                 repo_root=self.repo, to_adapter_id="codex-cli")
        receipt = deliver.deliver(bundle, home=home, now=1000.0)
        self.assertEqual(receipt.channel, "file-drop")
        with open(self.agents, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "neutral instructions")


class TestStatusInstructionFiles(unittest.TestCase):
    """omhc status's instruction files row — distinguishes shared wiring from a stale leak."""

    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        # cmd_status finds the repo via cwd — this very repo under development
        # shares AGENTS.md with CLAUDE.md (ac95989), so without moving away it
        # would end up testing that wiring.
        cwd = os.getcwd()
        os.chdir(self.t.root)
        self.addCleanup(os.chdir, cwd)
        # The adapters row is not this class's concern — adapters.present(),
        # which looks at the real $HOME, would give a different result on CI
        # (no ~/.claude in home) versus a dev machine, making the exit-code
        # assertions below flaky depending on environment (review defect).
        patcher = mock.patch.object(cli.adapters, "present", return_value=["claude-code"])
        patcher.start()
        self.addCleanup(patcher.stop)
        # The `claude-code hooks` row isn't this class's concern either — pin it
        # so only the instruction files row can vary.
        plant_hook_install(self.t.home, "claude-code")

    def run_status(self):
        out = io.StringIO()
        code = cli.cmd_status(cli.build_parser().parse_args(["status"]),
                              home=self.t.home, out=out)
        return code, out.getvalue()

    def test_pass_when_not_shared(self):
        code, text = self.run_status()
        line = next(l for l in text.splitlines() if "instruction files" in l)
        self.assertTrue(line.startswith("PASS"))
        self.assertIn("no AGENTS.md", line)

    def test_pass_when_shared_but_no_stale_block(self):
        agents = os.path.join(self.t.root, "AGENTS.md")
        claude = os.path.join(self.t.root, "CLAUDE.md")
        with open(agents, "w", encoding="utf-8") as fh:
            fh.write("neutral instructions")
        os.symlink(agents, claude)
        code, text = self.run_status()
        line = next(l for l in text.splitlines() if "instruction files" in l)
        self.assertTrue(line.startswith("PASS"))
        self.assertIn("outbox", line)

    def test_fail_when_shared_with_a_stale_block(self):
        agents = os.path.join(self.t.root, "AGENTS.md")
        claude = os.path.join(self.t.root, "CLAUDE.md")
        os.symlink(agents, claude)
        bundle = A.HandoffBundle(body_md="[omhc] leaked\n", repo_root=self.t.root,
                                 to_adapter_id="codex-cli")
        managed_block.splice(agents, bundle.body_md, captured_at=1000.0)
        code, text = self.run_status()
        line = next(l for l in text.splitlines() if "instruction files" in l)
        self.assertTrue(line.startswith("FAIL"))
        self.assertIn("omhc clear", line)
        self.assertEqual(code, 1)

    def test_status_recovers_to_pass_after_clear_on_a_symlinked_agents_md(self):
        """Review defect 1: clear's prescribed fix must actually fix the problem it points to."""
        agents = os.path.join(self.t.root, "AGENTS.md")
        claude = os.path.join(self.t.root, "CLAUDE.md")
        target = os.path.join(self.t.root, "docs.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.symlink(target, agents)
        os.symlink(agents, claude)
        managed_block.splice(agents, "[omhc] leaked\n", captured_at=1000.0)

        code, text = self.run_status()
        self.assertEqual(code, 1)

        self.assertTrue(agents_md.collapse(self.t.root, now=1000.0, force=True))
        self.assertTrue(os.path.islink(agents), "clear must not break the shared symlink")

        code, text = self.run_status()
        line = next(l for l in text.splitlines() if "instruction files" in l)
        self.assertTrue(line.startswith("PASS"))
        # C1: empty ledger/archive are now `----`, which doesn't gate — exit is 0
        # if the rest of the rows all pass (used to be 1 here, since these two were FAIL).
        self.assertEqual(code, 0)

    def test_status_recovers_to_pass_after_clear_on_a_hardlinked_agents_md(self):
        """Round 2 review defect: splitting the hard link makes status falsely PASS as 'not shared'."""
        agents = os.path.join(self.t.root, "AGENTS.md")
        claude = os.path.join(self.t.root, "CLAUDE.md")
        managed_block.splice(agents, "[omhc] leaked\n", captured_at=1000.0)
        os.link(agents, claude)

        code, text = self.run_status()
        self.assertEqual(code, 1)

        self.assertTrue(agents_md.collapse(self.t.root, now=1000.0, force=True))
        self.assertEqual(os.stat(agents).st_ino, os.stat(claude).st_ino,
                         "clear must not split the hard link")

        code, text = self.run_status()
        line = next(l for l in text.splitlines() if "instruction files" in l)
        self.assertTrue(line.startswith("PASS"))
        self.assertIn("hard-linked", line)

    def test_json_reports_shared_and_stale_block_separately(self):
        agents = os.path.join(self.t.root, "AGENTS.md")
        claude = os.path.join(self.t.root, "CLAUDE.md")
        os.symlink(agents, claude)

        out = io.StringIO()
        cli.cmd_status(cli.build_parser().parse_args(["status", "--json"]),
                       home=self.t.home, out=out)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["instruction_files"]["stale_block"], False)
        self.assertIsNotNone(payload["instruction_files"]["shared"])

        managed_block.splice(agents, "[omhc] leaked\n", captured_at=1000.0)
        out = io.StringIO()
        cli.cmd_status(cli.build_parser().parse_args(["status", "--json"]),
                       home=self.t.home, out=out)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["instruction_files"]["stale_block"], True)
        self.assertIsNotNone(payload["instruction_files"]["shared"])


if __name__ == "__main__":
    unittest.main()
