"""#35 `omhc trace <path>` — finds events that touched a file across sessions
in the index (precedent: sessionwiki's `trace`)."""
from __future__ import annotations

import io
import json
import os
import unittest

from omhc import cli, due, index
from omhc.event import Event

from ._repo import TempRepo


def _write_idx(state: str, session_id: str, rows) -> None:
    """rows are (seq, verb, paths, arg) tuples. paths is a tuple of strings."""
    idx_dir = os.path.join(state, "index")
    os.makedirs(idx_dir, exist_ok=True)
    events = [
        Event(seq=seq, epoch=1700000000.0 + seq, author="agent", verb=verb,
              ok=True, text="", arg=arg, paths=paths, offset=seq * 10, length=5)
        for seq, verb, paths, arg in rows
    ]
    index.append_rows(os.path.join(idx_dir, session_id + ".idx"), events)


def _mark_delivered(state: str, session_id: str) -> None:
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, due.DELIVERED_NAME), "a", encoding="utf-8") as fh:
        fh.write("\t".join((session_id, "claude-code", "codex-cli", "1700000000")) + "\n")


class TestTrace(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        cwd = os.getcwd()
        os.chdir(self.t.root)
        self.addCleanup(os.chdir, cwd)

    def _mark(self, session_id: str, harness: str) -> None:
        from omhc import ledger

        ledger.append({"repo": self.t.key, "harness": harness, "session": session_id,
                       "event": "start", "epoch": 1700000000.0, "path": "x",
                       "cwd": self.t.root}, home=self.t.home)

    def _trace(self, path, extra=()):
        out = io.StringIO()
        code = cli.cmd_trace(
            cli.build_parser().parse_args(["trace", path] + list(extra)),
            home=self.t.home, out=out)
        return code, out.getvalue()

    def test_relative_target_matches_absolute_index_path(self):
        abs_path = os.path.join(self.t.root, "omhc", "cli.py")
        _write_idx(self.t.state, "aaaaaaaa1111",
                  [(1, "modified", (abs_path,), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        self.assertIn("aaaaaaaa", out)
        self.assertIn("codex-cli", out)
        self.assertIn("modified", out)

    def test_absolute_target_matches_repo_relative_index_path(self):
        _write_idx(self.t.state, "aaaaaaaa1111",
                  [(1, "modified", ("omhc/cli.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        abs_path = os.path.join(self.t.root, "omhc", "cli.py")
        code, out = self._trace(abs_path)
        self.assertEqual(code, 0)
        self.assertIn("aaaaaaaa", out)

    def test_multiple_sessions_ordered_by_delivery_newest_last(self):
        _write_idx(self.t.state, "aaaaaaaa1111",
                  [(1, "modified", ("omhc/cli.py",), "")])
        _write_idx(self.t.state, "bbbbbbbb2222",
                  [(1, "modified", ("omhc/cli.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark("bbbbbbbb2222", "claude-code")
        # Delivery order: a first, b later -> b is newest, so it must be the last line.
        self._mark_delivered_helper("aaaaaaaa1111")
        self._mark_delivered_helper("bbbbbbbb2222")

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("aaaaaaaa"))
        self.assertTrue(lines[1].startswith("bbbbbbbb"))

    def test_default_excludes_inspected_and_ran(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [
            (1, "modified", ("omhc/cli.py",), ""),
            (2, "inspected", ("omhc/cli.py",), ""),
            (3, "ran", ("omhc/cli.py",), "pytest"),
        ])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("modified", lines[0])

    def test_all_includes_inspected_and_ran(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [
            (1, "modified", ("omhc/cli.py",), ""),
            (2, "inspected", ("omhc/cli.py",), ""),
            (3, "ran", ("omhc/cli.py",), "pytest"),
        ])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("omhc/cli.py", ["--all"])
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)

    def test_last_limits_to_the_n_most_recent(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [
            (1, "modified", ("omhc/cli.py",), ""),
            (2, "modified", ("omhc/cli.py",), ""),
            (3, "modified", ("omhc/cli.py",), ""),
        ])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("omhc/cli.py", ["--last", "1"])
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("#3", lines[0])

    def test_json_output_is_a_list_of_dicts(self):
        _write_idx(self.t.state, "aaaaaaaa1111",
                  [(1, "modified", ("omhc/cli.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("omhc/cli.py", ["--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["harness"], "codex-cli")
        self.assertEqual(payload[0]["verb"], "modified")
        self.assertTrue(payload[0]["ref"].endswith("#1"))

    def test_no_match_gives_a_hint_about_indexing(self):
        _write_idx(self.t.state, "aaaaaaaa1111",
                  [(1, "modified", ("omhc/other.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        self.assertIn("no indexed events touched", out)
        self.assertIn("only delivered or watched", out)

    def test_ref_is_resolvable_by_show(self):
        pin_dir = os.path.join(self.t.state, "pinned", "aaaaaaaa1111")
        os.makedirs(pin_dir, exist_ok=True)
        with open(os.path.join(pin_dir, "source.jsonl"), "wb") as fh:
            fh.write(b"0" * 30 + b"hit!!" + b"0" * 5)
        events = [Event(seq=1, epoch=1700000001.0, author="agent", verb="modified",
                       ok=True, text="", arg="", paths=("omhc/cli.py",),
                       offset=30, length=5)]
        idx_dir = os.path.join(self.t.state, "index")
        os.makedirs(idx_dir, exist_ok=True)
        index.append_rows(os.path.join(idx_dir, "aaaaaaaa1111.idx"), events)
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        ref = out.strip().split()[0]

        show_out = io.StringIO()
        show_err = io.StringIO()
        show_code = cli.cmd_show(
            cli.build_parser().parse_args(["show", ref]),
            home=self.t.home, out=show_out, err=show_err)
        self.assertEqual(show_code, 0)
        self.assertEqual(show_out.getvalue(), "hit!!\n")

    def _mark_delivered_helper(self, session_id: str) -> None:
        _mark_delivered(self.t.state, session_id)

    def _pulls(self):
        from omhc import ledger

        return [r for r in ledger.read(repo_key=self.t.key, home=self.t.home)
                if r.get("event") == "pull"]

    # --- review (#35) 1: defect where the ambiguity guard is bypassed by a multi-path row ------------

    def test_ambiguous_suffix_match_across_a_multi_path_row_is_reported_not_guessed(self):
        """Reproduces (review): one s1 row carries two distinct suffix-matching paths,
        and s2 carries only one of them — stopping at the first match wrongly judges
        it as "only one bucket", erasing the fact that s1 doesn't actually know which
        file it points to."""
        _write_idx(self.t.state, "aaaaaaaa1111", [
            (1, "modified", ("omhc/adapters/__init__.py", "omhc/__init__.py"), ""),
        ])
        _write_idx(self.t.state, "bbbbbbbb2222", [
            (1, "modified", ("omhc/adapters/__init__.py",), ""),
        ])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark("bbbbbbbb2222", "claude-code")
        self._mark_delivered_helper("aaaaaaaa1111")
        self._mark_delivered_helper("bbbbbbbb2222")

        code, out = self._trace("__init__.py")
        self.assertEqual(code, 0)
        self.assertIn("ambiguous", out)
        self.assertIn("omhc/__init__.py", out)
        self.assertIn("omhc/adapters/__init__.py", out)
        # Must not guess-print the history of the wrong file (the adapters one).
        self.assertNotIn("bbbbbbbb", out)
        self.assertEqual(self._pulls(), [])

        # With --json, emit a machine-readable object instead of a hint sentence (review).
        code, out = self._trace("__init__.py", ["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out),
                         {"ambiguous": ["omhc/__init__.py", "omhc/adapters/__init__.py"]})

    def test_suffix_match_respects_path_segment_boundaries(self):
        """`a/b.py` can be the same file as `x/a/b.py`, but not `xa/b.py` — must
        compare by '/'-split segments, not string suffix."""
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "modified", ("sub/a/b.py",), "")])
        _write_idx(self.t.state, "bbbbbbbb2222", [(1, "modified", ("subxa/b.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark("bbbbbbbb2222", "claude-code")
        self._mark_delivered_helper("aaaaaaaa1111")
        self._mark_delivered_helper("bbbbbbbb2222")

        code, out = self._trace("a/b.py")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("aaaaaaaa"))

    def test_unique_basename_suffix_fallback_still_works(self):
        """If there's only one suffix candidate (unambiguous), it's still used as a match."""
        _write_idx(self.t.state, "aaaaaaaa1111",
                  [(1, "modified", ("omhc/adapters/codex_cli.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("codex_cli.py")
        self.assertEqual(code, 0)
        self.assertIn("aaaaaaaa", out)

    def test_symlinked_index_path_resolves_to_the_same_real_file(self):
        """Review: the macOS `/var` -> `/private/var` kind of case. Even if the
        index has a symlink-traversing path and the query has the real path (or
        vice versa), they must resolve to the same file — realpath normalization
        must hit exactly, with no suffix guessing."""
        real_dir = os.path.join(self.t.root, "realdir")
        link_dir = os.path.join(self.t.root, "linkdir")
        os.makedirs(real_dir)
        os.symlink(real_dir, link_dir)
        idx_path = os.path.join(link_dir, "f.py")
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "modified", (idx_path,), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        real_path = os.path.join(real_dir, "f.py")
        code, out = self._trace(real_path)
        self.assertEqual(code, 0)
        self.assertIn("aaaaaaaa", out)
        self.assertNotIn("ambiguous", out)

    # --- review (#35) 2: pull bookkeeping only for the match actually printed -----------------------

    def test_pull_recorded_only_for_the_session_actually_shown(self):
        """`zzzzzzzz9999` is the most recent delivery but touches an unrelated
        file — must not use #N's "most recent delivery" approximation, and must
        record `aaaaaaaa1111`, the one actually printed, as pulled."""
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "modified", ("omhc/cli.py",), "")])
        _write_idx(self.t.state, "zzzzzzzz9999", [(1, "modified", ("omhc/other.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark("zzzzzzzz9999", "claude-code")
        self._mark_delivered_helper("aaaaaaaa1111")
        self._mark_delivered_helper("zzzzzzzz9999")

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        pulls = self._pulls()
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["session"], "aaaaaaaa1111")

    def test_no_pull_recorded_when_the_matched_session_was_never_delivered(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "modified", ("omhc/cli.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        # Indexed only by watch, not in delivered.tsv.

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        self.assertIn("aaaaaaaa", out)
        self.assertEqual(self._pulls(), [])

    def test_no_pull_recorded_when_nothing_matches(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "modified", ("omhc/other.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        self.assertIn("no indexed events touched", out)
        self.assertEqual(self._pulls(), [])

    # --- review (#35) 3: harness for a session with no ledger start row (watch-only) -------

    def test_harness_is_a_question_mark_when_no_ledger_start_row_exists(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "modified", ("omhc/cli.py",), "")])
        self._mark_delivered_helper("aaaaaaaa1111")  # mark() was never called.

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        fields = out.strip().splitlines()[0].split()
        self.assertIn("?", fields)


class TestTraceRefusesAtSlashRoot(unittest.TestCase):
    """`/` is not a project root (#35 requirement: same refusal as log)."""

    def test_refuses_at_slash(self):
        out = io.StringIO()
        cwd = os.getcwd()
        try:
            os.chdir("/")
            code = cli.cmd_trace(
                cli.build_parser().parse_args(["trace", "x"]),
                home="/nonexistent-omhc-test-home", out=out)
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 1)
        self.assertIn("not a project root", out.getvalue())


if __name__ == "__main__":
    unittest.main()
