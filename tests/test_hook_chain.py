from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest

from . import _repo

ROOT = _repo.REPO
OMHC = os.path.join(ROOT, "bin", "omhc")
FRAGMENTS = {
    "claude-code": os.path.join(ROOT, "hooks", "claude-settings.fragment.json"),
    "codex-cli": os.path.join(ROOT, "hooks", "codex-hooks.json"),
}


def shipped_commands(path: str):
    """Pull SessionStart commands from a shipped hook file, **in shipped order**."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for group in data["hooks"]["SessionStart"]:
        for hook in group["hooks"]:
            assert hook["type"] == "command", hook
            out.append(hook["command"])
    return out


def shipped_turn_commands(path: str):
    """v2 phase 2 (#42): the UserPromptSubmit group's commands, same shape."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for group in data["hooks"]["UserPromptSubmit"]:
        for hook in group["hooks"]:
            assert hook["type"] == "command", hook
            out.append(hook["command"])
    return out


class TestShippedHookFiles(unittest.TestCase):
    def test_both_fragments_are_valid_json(self):
        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                with open(path, encoding="utf-8") as fh:
                    json.load(fh)

    def test_mark_runs_before_brief(self):
        """If the order flips, brief runs while its own session isn't in the ledger yet."""
        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                commands = shipped_commands(path)
                self.assertEqual(len(commands), 2)
                self.assertIn(" mark ", commands[0])
                self.assertIn(" brief ", commands[1])

    def test_each_command_names_its_own_harness(self):
        for harness, path in FRAGMENTS.items():
            for command in shipped_commands(path):
                with self.subTest(harness=harness, command=command):
                    self.assertIn("--harness {}".format(harness), command)

    def test_brief_pins_exactly_one_wire_format(self):
        """Emitting two formats at once means Claude Code reads both and injects twice."""
        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                brief_command = shipped_commands(path)[1]
                self.assertEqual(len(re.findall(r"--wire\s+\S+", brief_command)), 1)

    def test_shipped_wire_matches_the_adapters_declared_wire(self):
        """The fragment file's --wire drifting from the adapter's wire attribute is
        exactly how a past bug hid (the adapter said sdk, the fragment said sdk, and
        real codex-cli rejected both — not that agreement can still be wrong, but
        that this is the kind of mismatch that recurs if the adapter is fixed and
        the fragment isn't, so it's caught automatically).
        """
        from omhc import adapters

        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                brief_command = shipped_commands(path)[1]
                match = re.search(r"--wire\s+(\S+)", brief_command)
                self.assertIsNotNone(match, brief_command)
                shipped_wire = match.group(1)
                declared_wire = getattr(adapters.get(harness), "wire", None)
                self.assertEqual(shipped_wire, declared_wire, harness)

    def test_fragments_document_the_install_step(self):
        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertIn("omhc", text)
                self.assertIn("_comment", text)

    def test_turn_group_names_its_own_harness_and_takes_no_wire_flag(self):
        """v2 phase 2 (#42). `omhc turn` always wraps its note in the nested
        hookSpecificOutput/UserPromptSubmit shape itself (turn.py) — unlike
        brief, it has no --wire choice to pin."""
        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                commands = shipped_turn_commands(path)
                self.assertEqual(len(commands), 1)
                self.assertIn(" turn ", commands[0])
                self.assertIn("--harness {}".format(harness), commands[0])
                self.assertNotIn("--wire", commands[0])


class TestHookChainExecution(unittest.TestCase):
    """Actually runs the shipped commands. Catches even a typo in the argument string."""

    def setUp(self):
        self.t = _repo.TempRepo()
        self.addCleanup(self.t.close)
        self.home = self.t.home
        self.repo = self.t.repo
        self.env = self.t.env

    def _run(self, command: str, stdin_text: str):
        # Replace the shipped command's $HOME/.local/bin/omhc with this repo's launcher.
        # Leaving the arguments and order untouched is the whole point of this test.
        argv = shlex.split(command.replace("$HOME/.local/bin/omhc", OMHC))
        return subprocess.run(argv, input=stdin_text, capture_output=True, text=True,
                              env=self.env, cwd=self.repo, timeout=60)

    def _payload(self, session_id: str) -> str:
        return json.dumps({"cwd": os.path.realpath(self.repo),
                           "session_id": session_id})

    def _plant_codex(self, session_id="cx1"):
        return self.t.plant_codex(session_id=session_id,
                                  human="리더를 붙여서 양방향으로 만들기")

    def test_claude_chain_with_nothing_to_send_is_silent(self):
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            got = self._run(command, self._payload("me1"))
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(got.stdout, "", command)

    def test_codex_then_claude_produces_valid_wire_json(self):
        self._plant_codex()
        # 1) record that the Codex session started
        for command in shipped_commands(FRAGMENTS["codex-cli"]):
            got = self._run(command, self._payload("cx1"))
            self.assertEqual(got.returncode, 0, got.stderr)
        # 2) Claude session start -> a mark should be emitted
        outputs = []
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            got = self._run(command, self._payload("me1"))
            self.assertEqual(got.returncode, 0, got.stderr)
            outputs.append(got.stdout)
        payload = json.loads(outputs[1])
        body = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[omhc]", body)
        self.assertLessEqual(len(body.encode("utf-8")), 900)

    def test_refiring_the_chain_in_the_same_session_is_silent(self):
        self._plant_codex()
        for command in shipped_commands(FRAGMENTS["codex-cli"]):
            self._run(command, self._payload("cx1"))
        first = [self._run(c, self._payload("me1")).stdout
                 for c in shipped_commands(FRAGMENTS["claude-code"])]
        second = [self._run(c, self._payload("me1")).stdout
                  for c in shipped_commands(FRAGMENTS["claude-code"])]
        self.assertTrue(first[1])
        self.assertEqual(second[1], "", "the gate failed to prevent re-firing")

    def test_chain_survives_broken_stdin(self):
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            got = self._run(command, "{not json")
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(got.stdout, "")

    def test_chain_survives_empty_stdin(self):
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            got = self._run(command, "")
            self.assertEqual(got.returncode, 0, got.stderr)


class TestF7ZeroCase(unittest.TestCase):
    """Spec section 16-7: a session start with no harness switch must inject zero times.

    F7 (the prototype's uncapped 15KB injection) was the reason for the
    redesign, so the zero case is confirmed by observation, not by assertion alone.
    """

    def setUp(self):
        self.t = _repo.TempRepo()
        self.addCleanup(self.t.close)
        self.home = self.t.home
        self.repo = self.t.repo
        self.env = self.t.env

    def _chain(self, session_id: str):
        outputs = []
        payload = json.dumps({"cwd": os.path.realpath(self.repo),
                              "session_id": session_id})
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            argv = shlex.split(command.replace("$HOME/.local/bin/omhc", OMHC))
            got = subprocess.run(argv, input=payload, capture_output=True, text=True,
                                 env=self.env, cwd=self.repo, timeout=60)
            self.assertEqual(got.returncode, 0, got.stderr)
            outputs.append(got.stdout)
        return outputs

    def _ledger_lines(self):
        path = os.path.join(self.home, ".omhc", "ledger.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [line for line in fh if line.strip()]

    def test_ten_claude_only_session_starts_inject_nothing(self):
        """Zero injections, **but the ledger must have 10 lines.**

        Asserting absence alone would also pass if the binary did nothing at all —
        indistinguishable from being neutralized by a bad HOME resolution or an
        early exit. 10 ledger lines is positive evidence that the chain actually ran.
        """
        for i in range(10):
            outputs = self._chain("claude-session-{}".format(i))
            self.assertEqual(outputs[1], "", "session {} injected something".format(i))
        self.assertEqual(len(self._ledger_lines()), 10,
                         "the chain never actually ran — the absence assertion is meaningless")

    def test_ten_session_starts_leave_no_delivered_rows(self):
        """With no injections, delivered.tsv must not exist either.

        Observed behavior: the per-repo state directory itself never gets
        created — only a single ledger line (~220 bytes) accumulates in
        ~/.omhc/ledger.jsonl. That's F7's zero case.
        """
        for i in range(10):
            self._chain("claude-session-{}".format(i))
        found = []
        for dirpath, _dirs, names in os.walk(os.path.join(self.home, ".omhc")):
            found += [os.path.join(dirpath, n) for n in names if n == "delivered.tsv"]
        self.assertEqual(found, [], "no injections happened, but a delivered record was created")

    def test_ten_session_starts_cost_only_the_ledger_lines(self):
        for i in range(10):
            self._chain("claude-session-{}".format(i))
        total = 0
        for dirpath, _dirs, names in os.walk(os.path.join(self.home, ".omhc")):
            for name in names:
                total += os.path.getsize(os.path.join(dirpath, name))
        # 10 ledger lines + gate markers. Should be hundreds of bytes, not kilobytes.
        self.assertLess(total, 8 * 1024,
                        "10 non-switching sessions left {}B behind".format(total))

    def test_ten_session_starts_write_no_artifact(self):
        for i in range(10):
            self._chain("claude-session-{}".format(i))
        found = []
        for dirpath, _dirs, names in os.walk(os.path.join(self.home, ".omhc")):
            found += [n for n in names if n == "omhc.txt"]
        self.assertEqual(found, [], "no injections happened, but an artifact file was created")


if __name__ == "__main__":
    unittest.main()
