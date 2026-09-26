"""omhc/turn.py — the UserPromptSubmit hook entry point (v2 phase 2, #42).
Unit-level behavior lives in tests/test_stale.py; this covers the entry
point itself: JSON wire shape, invariant 2 (never raises), and the "does not
import omhc.cli" contract bin/omhc's dedicated branch depends on."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
import unittest.mock

from omhc import turn

from . import _repo
from ._repo import REPO, TempRepo

BIN = os.path.join(REPO, "bin", "omhc")


class TestMainNeverExitsNonzero(unittest.TestCase):
    """Review #1 finding 3: argparse errors on UserPromptSubmit must never
    become exit 2 — that would block the user's own prompt (invariant 2)."""

    def setUp(self):
        # main() without --stdin reads the real stdin to EOF, as the hook
        # does. A test runner's stdin can be an open pipe or socket that never
        # reaches EOF (a backgrounded run hung for 84 minutes), so give every
        # call here an empty, closed stdin.
        patcher = unittest.mock.patch.object(turn.sys, "stdin", io.StringIO(""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_harness_is_silent(self):
        self.assertEqual(turn.main([]), 0)

    def test_unknown_flag_is_silent(self):
        self.assertEqual(turn.main(["--bogus", "x"]), 0)

    def test_harness_with_no_value_is_silent(self):
        self.assertEqual(turn.main(["--harness"]), 0)

    def test_totally_malformed_args_never_raise_or_exit(self):
        for argv in (["--harness=", "--stdin"], ["-h"], ["not", "even", "flags"]):
            with self.subTest(argv=argv):
                try:
                    rc = turn.main(argv)
                except SystemExit as exc:  # pragma: no cover - the bug this pins
                    self.fail("main() exited via SystemExit({})".format(exc.code))
                self.assertEqual(rc, 0)

    def test_bin_omhc_turn_with_bad_args_exits_0_with_empty_stdout(self):
        result = subprocess.run([BIN, "turn", "--bogus"], input="{}",
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_bin_omhc_turn_missing_harness_exits_0_with_empty_stdout(self):
        result = subprocess.run([BIN, "turn"], input="{}",
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class TestEmitWireBudget(unittest.TestCase):
    def test_note_past_wire_budget_is_suppressed_not_written(self):
        out = io.StringIO()
        huge = "x" * (turn.WIRE_BUDGET + 100)
        with unittest.mock.patch.object(turn.stale, "check", return_value=huge):
            rc = turn.emit(harness="claude-code", stdin_text="{}", out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")

    def test_broken_pipe_on_write_never_raises(self):
        class _Boom:
            def write(self, _text):
                raise BrokenPipeError()

        with unittest.mock.patch.object(turn.stale, "check", return_value="[omhc] x\nFILE  a\n"):
            rc = turn.emit(harness="claude-code", stdin_text="{}", out=_Boom())
        self.assertEqual(rc, 0)


class TestEmit(unittest.TestCase):
    def test_no_note_is_silent(self):
        out = io.StringIO()
        rc = turn.emit(harness="claude-code", stdin_text="", out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")

    def test_a_note_is_wrapped_as_userpromptsubmit_hookspecificoutput(self):
        out = io.StringIO()
        with unittest.mock.patch.object(turn.stale, "check", return_value="[omhc] x\nFILE  a\n"):
            rc = turn.emit(harness="claude-code", stdin_text="{}", out=out)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("FILE", payload["hookSpecificOutput"]["additionalContext"])

    def test_stale_check_raising_never_breaks_emit(self):
        out = io.StringIO()
        with unittest.mock.patch.object(turn.stale, "check", side_effect=RuntimeError("boom")):
            rc = turn.emit(harness="claude-code", stdin_text="{}", out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")

    def test_no_harness_is_silent(self):
        out = io.StringIO()
        rc = turn.emit(harness="", stdin_text="{}", out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")


class TestBinDoesNotImportCli(unittest.TestCase):
    """bin/omhc's `turn` branch must route straight to omhc.turn.main without
    ever importing omhc.cli (module docstring, and CLAUDE.md task #42 phase 2:
    cli.py pulls in agents_md/deliver/gate/hookconf/index/managed_block/pin/
    watch — none of which the per-human-turn hot path needs)."""

    def test_turn_subcommand_never_imports_cli(self):
        code = (
            "import sys; sys.path.insert(0, {!r})\n"
            "from omhc.turn import main\n"
            "main(['--harness', 'claude-code'])\n"
            "assert 'omhc.cli' not in sys.modules, sorted(sys.modules)\n"
            "print('ok')\n"
        ).format(REPO)
        result = subprocess.run(
            [sys.executable, "-c", code], input="{}", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_bin_omhc_turn_routes_without_cli(self):
        result = subprocess.run(
            [BIN, "turn", "--harness", "claude-code"],
            input="{}", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class TestLatency(unittest.TestCase):
    """Measures (not asserts a hard SLA on shared CI hardware) `bin/omhc
    turn`'s end-to-end wall time on both the no-growth fast path and the
    growth path, and reports the numbers (docs/v2-concurrency.md: "150ms p95
    per human turn, fast path under 80ms")."""

    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)

    def _payload(self, own_path, session_id="claude1"):
        return json.dumps({
            "session_id": session_id, "transcript_path": own_path,
            "cwd": self.t.root, "hook_event_name": "UserPromptSubmit",
            "prompt": "next",
        })

    def _run(self, payload) -> float:
        start = time.monotonic()
        result = subprocess.run(
            [BIN, "turn", "--harness", "claude-code"],
            input=payload, capture_output=True, text=True,
            env=dict(os.environ, HOME=self.t.home))
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 0, result.stderr)
        return elapsed

    def test_fast_path_and_growth_path_latency(self):
        own_path = os.path.join(self.t.home, "own.jsonl")
        with open(own_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "cwd": self.t.root,
                                 "timestamp": "2026-09-25T00:00:00.000Z",
                                 "message": {"content": "레이턴시 측정"}}) + "\n")
        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        payload = self._payload(own_path)

        # First call: own read + one foreign first-observation. Second call:
        # the fast path this test's docstring cites (unchanged foreign size).
        self._run(payload)
        fast = min(self._run(payload) for _ in range(3))

        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(os.path.join(self.t.root, "x.py"))],
                    ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        grown = self._run(payload)

        print("\n[latency] omhc turn fast path: {:.1f}ms, growth path: {:.1f}ms".format(
            fast * 1000, grown * 1000))
        # Not asserted against the doc's 80/150ms target — shared CI/dev
        # hardware varies too much for a hard SLA here; the point of this
        # test is the printed measurement itself.
        self.assertLess(fast, 5.0)  # sanity: didn't hang
        self.assertLess(grown, 5.0)


if __name__ == "__main__":
    unittest.main()
