"""omhc/stale.py — the v2 phase 2 (#42) per-human-turn overlap check.
`omhc/turn.py` is the thin hook entry; this exercises the comparison logic
directly (see its own module docstring)."""
from __future__ import annotations

import io
import json
import os
import time
import unittest
import unittest.mock

from omhc import cli, ledger, locate, stale
from omhc.adapters import codex_cli as CX

from . import _repo
from ._repo import REPO, TempRepo


def _mark(home: str, harness: str, session_id: str, transcript_path: str, cwd: str,
         source=None) -> None:
    """A real `cli.cmd_mark` call — exercises the actual code path that
    records `start_size`/`source` on a genuine hook row (review #1
    finding 1, round 5), rather than hand-building the row and risking
    testing an assumption about its shape instead of the real one."""
    payload = {"cwd": cwd, "session_id": session_id, "transcript_path": transcript_path}
    if source is not None:
        payload["source"] = source
    args = cli.build_parser().parse_args(
        ["mark", "--harness", harness, "--stdin", json.dumps(payload)])
    cli.cmd_mark(args, home=home, out=io.StringIO())


_TS = "2026-09-25T00:00:00.000Z"


def _write_claude(path: str, human: str, edited_paths) -> None:
    """A minimal Claude Code transcript: one human turn, one assistant Edit
    tool_use per path in `edited_paths`."""
    rows = [
        {"type": "user", "cwd": REPO, "timestamp": _TS,
         "message": {"content": human}},
    ]
    for i, p in enumerate(edited_paths):
        rows.append({
            "type": "assistant", "cwd": REPO, "timestamp": _TS,
            "message": {"content": [
                {"type": "tool_use", "id": "t{}".format(i), "name": "Edit",
                 "input": {"file_path": p}},
            ]},
        })
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _claude_payload(session_id: str, path: str, cwd: str) -> str:
    return json.dumps({
        "session_id": session_id, "transcript_path": path, "cwd": cwd,
        "hook_event_name": "UserPromptSubmit", "prompt": "next",
    })


class TestNoGrowth(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)

    def test_silent_when_the_foreign_session_never_grows(self):
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "내 세션의 목표", [])
        foreign_path = self.t.plant_codex(session_id="cx1", shell_turns=0,
                                          ledger_home=self.t.home)
        payload = _claude_payload("claude1", own_path, self.t.root)

        note1 = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(note1, "")  # first observation of the foreign session
        note2 = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(note2, "")  # unchanged size, nothing to say

    def test_no_foreign_sessions_at_all_is_silent(self):
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "혼자 쓰는 중", [])
        payload = _claude_payload("claude1", own_path, self.t.root)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")


class TestOverlap(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        self.shared_abs = os.path.join(self.t.root, "omhc", "brief.py")
        os.makedirs(os.path.dirname(self.shared_abs), exist_ok=True)
        with open(self.shared_abs, "w", encoding="utf-8") as fh:
            fh.write("# placeholder\n")

    def _touch_own(self):
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "브리프 고쳐줘", [self.shared_abs])
        payload = _claude_payload("claude1", own_path, self.t.root)
        # First call: records this session's own touched paths, and the
        # foreign session's baseline (first observation, silent).
        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(note, "")
        return own_path, payload, foreign_path

    def test_note_when_foreign_modified_intersects_touched(self):
        own_path, payload, foreign_path = self._touch_own()
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(self.shared_abs)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("codex-cli", note)
        self.assertIn("omhc/brief.py", note)
        self.assertIn("FILE", note)
        # No PULL line (review #1 finding 7) — `omhc trace` only searches
        # the index, which an undelivered running session doesn't have.
        self.assertNotIn("PULL", note)
        self.assertLessEqual(len(note.encode("utf-8")), stale.NOTE_BUDGET)

    def test_silent_when_foreign_modified_is_disjoint(self):
        own_path, payload, foreign_path = self._touch_own()
        other_abs = os.path.join(self.t.root, "omhc", "cli.py")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(other_abs)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(note, "")

    def test_two_foreign_sessions_each_overlapping_a_different_file_are_both_reported(self):
        """Review #1 finding 2: only the first overlapping session used to be
        reported while every candidate's baseline still advanced — silently
        losing every other real overlap."""
        a = os.path.join(self.t.root, "a.py")
        b = os.path.join(self.t.root, "b.py")
        for p in (a, b):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("x\n")
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a, b])
        payload = _claude_payload("claude1", own_path, self.t.root)
        f1 = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        f2 = self.t.plant_codex(session_id="cx2", ledger_home=self.t.home)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        for f, p in ((f1, a), (f2, b)):
            with open(f, "a", encoding="utf-8") as fh:
                for row in _repo.codex_apply_patch_rows(
                        ["*** Update File: {}".format(p)], ordinal=50):
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("cx1", note)
        self.assertIn("a.py", note)
        self.assertIn("cx2", note)
        self.assertIn("b.py", note)
        # Both baselines advanced — nothing repeats on the next turn.
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_no_repeat_after_baseline_advances(self):
        own_path, payload, foreign_path = self._touch_own()
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(self.shared_abs)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        first = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertNotEqual(first, "")
        second = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(second, "")  # same growth, already advanced past it


class TestOffAndGarbage(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)

    def test_omhc_off_is_silent(self):
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "끄고 테스트", [])
        payload = _claude_payload("claude1", own_path, self.t.root)
        os.makedirs(self.t.state, exist_ok=True)
        with open(os.path.join(self.t.state, "off"), "w", encoding="utf-8") as fh:
            fh.write("")
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_garbage_stdin_never_raises(self):
        self.assertEqual(stale.check(harness="claude-code", stdin_text="{not json",
                                     home=self.t.home), "")

    def test_empty_stdin_never_raises(self):
        self.assertEqual(stale.check(harness="claude-code", stdin_text="",
                                     home=self.t.home), "")

    def test_no_session_id_is_silent(self):
        payload = json.dumps({"cwd": self.t.root, "transcript_path": "/x.jsonl"})
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_root_refusal_is_silent(self):
        payload = json.dumps({"session_id": "s1", "cwd": "/", "transcript_path": "/x.jsonl"})
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_no_harness_is_silent(self):
        payload = json.dumps({"session_id": "s1", "cwd": self.t.root})
        self.assertEqual(stale.check(harness="", stdin_text=payload, home=self.t.home), "")


class TestBaselineByAppendOrder(unittest.TestCase):
    """Review #1 finding 1 (round 3 — round 2's rule was wrong): the
    first-observation baseline for a foreign session is chosen by ledger
    append order (invariant 6), but only a *genuine* start row (no `via`, no
    `grew` — an adapter's own hook actually firing) counts as evidence that a
    session started after mine. `via:"scan"` bookkeeping (backfill/reactivate)
    only reflects when this machine *noticed* a session, not when it
    started, so it must never be read as "started after me" on its own."""

    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)

    def _own_row(self, session_id="claude1", path=""):
        ledger.append({"repo": self.t.key, "harness": "claude-code",
                       "session": session_id, "event": "start",
                       "epoch": time.time(), "path": path, "cwd": self.t.root},
                      home=self.t.home)

    def test_foreign_genuine_row_after_mine_is_new_and_reported_the_same_turn(self):
        """repro2 shape, corrected: this session's own (genuine) ledger row
        exists first — the realistic case, since `omhc mark` always records
        it before `omhc turn` ever runs. Codex then starts for real (its own
        hook writes a plain `start` row, no `via`) *after* that, and already
        has the edit by the time this hook ever looks. Review #1 finding 1:
        classified as genuinely new (baseline 0). Finding 2: read in the
        *same* turn as the first observation, not the next one — the old
        "wait one more turn" behavior was needlessly slow, not just wrong."""
        own_path = os.path.join(self.t.home, "own.jsonl")
        shared = os.path.join(self.t.root, "a.py")
        with open(shared, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        _write_claude(own_path, "고쳐줘", [shared])
        self._own_row(path=own_path)
        payload = _claude_payload("claude1", own_path, self.t.root)

        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        # cx1's own hook start row lands *after* mine, and it already has
        # the edit by the time we ever look at it.
        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(shared)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        # First observation of cx1 -- reported on THIS SAME turn (finding 2),
        # not deferred to the turn after.
        note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("a.py", note)

    def test_foreign_started_before_my_session_keeps_pre_existing_content_silent(self):
        """The opposite order: the foreign session (and its edit) already
        existed before this session's own ledger row landed — presumed
        already covered by the SessionStart handoff — so first observation
        still snaps to the current size, and that pre-existing edit is never
        surfaced on its own (there is nothing *new* to report)."""
        shared = os.path.join(self.t.root, "a.py")
        with open(shared, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(shared)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        # This session's own start row lands *after* the foreign one.
        self._own_row()

        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [shared])
        payload = _claude_payload("claude1", own_path, self.t.root)

        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")  # first observation, silent
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")  # nothing NEW since

    def test_my_own_row_missing_but_known_foreign_snapshot_still_catches_it(self):
        """repro2 exactly as given (no own ledger row at all — e.g. a
        hand-run check before `mark` ever wrote one): `my_pos` is None on
        every turn, but review #1 finding 3 (round 4) means this is no
        longer fatal — this session's *first-ever* turn establishes
        `known_foreign` from whatever's visible in the ledger tail at that
        moment (cx1 doesn't exist yet here), so once cx1 appears afterward
        with a genuine row, it's recognized as "not part of the original
        snapshot" and reported as new regardless of `my_pos`."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        payload = _claude_payload("claude1", own_path, self.t.root)

        # Establishing turn: no foreign sessions exist yet.
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        # First observation of cx1 -- not in the established snapshot, has a
        # genuine row now -- reported the same turn (findings 2+3 together).
        note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("a.py", note)

    def test_my_own_row_missing_and_foreign_already_present_at_establishment_stays_conservative(self):
        """The narrower case finding 1's "never 0" rule still guards: on the
        very first (establishing) turn itself, with no own ledger row to
        judge order from, a foreign session that *already exists* (and
        already has the edit) at that exact moment is folded into the
        snapshot as "already there" — never reported out of thin air with no
        way to confirm it actually started after this session."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        payload = _claude_payload("claude1", own_path, self.t.root)

        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        # Establishing turn: cx1 already exists (and already has the edit),
        # my_pos is None (no own row) -- conservative, silent.
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")
        # And it's part of the established snapshot now, so it stays silent.
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_resumed_old_session_start_size_excludes_its_old_history_repro5(self):
        """repro5 (review #1 finding 1, round 5): a week-old Codex session
        whose *original* genuine start row has long scrolled out of the
        bounded ledger tail (simulated here by heavy other-repo ledger
        traffic, same shape as the real repro5.py) is later resumed. Its
        resume leaves *another* genuine row (`cli.cmd_mark`, real call, not
        hand-built) — indistinguishable from a brand-new session's row by
        shape alone (no `via`, no `grew`). Without the recorded `start_size`
        this session's days-old edit reads as "just happened"; with it (the
        resume's own transcript size, which already includes that edit),
        the baseline correctly excludes it."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        foreign_path = self.t.plant_codex(session_id="cxweek")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        # cxweek's original genuine start row, a week ago.
        ledger.append({"repo": self.t.key, "harness": "codex-cli",
                       "session": "cxweek", "event": "start",
                       "epoch": time.time() - 7 * 86400, "path": foreign_path,
                       "cwd": self.t.root}, home=self.t.home)
        # Heavy unrelated ledger traffic (other repos) pushes that original
        # row out of the bounded tail entirely (same shape as repro5.py).
        for i in range(1200):
            ledger.append({"repo": "other-repo-key-xxxxxxxxxxxx", "harness": "claude-code",
                           "session": "s%05d-%s" % (i, "x" * 80), "event": "start",
                           "epoch": time.time(), "path": "/o/%d.jsonl" % i, "cwd": "/o"},
                          home=self.t.home)

        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        self._own_row(path=own_path)
        payload = _claude_payload("claude1", own_path, self.t.root)

        # Establishing turn -- cxweek's original row is already out of the
        # tail, so it's neither a candidate nor part of the snapshot.
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        # `codex resume cxweek` -- a real cmd_mark call, appending a genuine
        # start row with no new edits yet.
        _mark(self.t.home, "codex-cli", "cxweek", foreign_path, self.t.root, source="resume")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("안녕")) + "\n")

        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_freshly_marked_session_start_size_still_catches_early_edits(self):
        """The other half of finding 1 (round 5): a genuinely brand-new
        foreign session's own real `cmd_mark` call records a `start_size`
        near 0 (its transcript is empty/minimal at that point) -- an edit
        made shortly after must still be caught, exactly like before this
        change (now via the recorded size instead of position inference)."""
        own_path = os.path.join(self.t.home, "own.jsonl")
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        _write_claude(own_path, "고쳐줘", [a])
        self._own_row(path=own_path)
        payload = _claude_payload("claude1", own_path, self.t.root)

        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        foreign_path = self.t.plant_codex(session_id="cx1")
        _mark(self.t.home, "codex-cli", "cx1", foreign_path, self.t.root, source="startup")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("a.py", note)

    def test_old_session_classified_before_start_size_is_ever_consulted_repro6(self):
        """repro6 `old` mode (review #1 finding 1, round 6 — round 5's rule
        was wrong): a Codex session's real, everyday `startup` mark (small
        `start_size`) runs and edits a file *before* this session's own real
        mark even happens. Round 5 consulted the recorded `start_size`
        before classifying old vs. new at all, so this ordinary, already-old
        session had its whole history read on this session's very first turn
        and reported as if it just happened -- worse than the bug
        `start_size` was added to fix (repro5), since it hits every
        already-running Codex session post-upgrade, not just resumes.
        Classification must happen first: a genuine row before mine always
        means old, `start_size` never even consulted."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        foreign_path = self.t.plant_codex(session_id="cx1")
        # cx1 started (real mark) BEFORE my session, edited a.py, then idle.
        _mark(self.t.home, "codex-cli", "cx1", foreign_path, self.t.root, source="startup")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        _mark(self.t.home, "claude-code", "claude1", own_path, self.t.root, source="startup")
        payload = _claude_payload("claude1", own_path, self.t.root)

        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_old_session_outside_top_candidates_is_still_classified_old_repro7(self):
        """repro7 (review #1 finding, round 7): four real Codex sessions
        (`cx0`..`cx3`) all start (genuine `startup` marks) before this
        session's own mark; the *oldest* of them, `cx0`, edited a file long
        ago and then went idle. `FOREIGN_CANDIDATES` (3) means `cx0` -- the
        oldest -- sits outside the top-3 newest candidates on this session's
        establishing turn, so round 6's "only what the candidate loop
        actually reached" `known_foreign` never classified it at all. When
        `cx0` is later resumed (a genuine row, no new edits) and becomes a
        top-3 candidate for the first time, it must still be recognized as
        old (round 7 part (a): classify every visible foreign session id at
        establishment, not just the ones reached that turn) — never treated
        as new just because this is the first time it was actually looked at."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        paths = {}
        for i in range(4):
            sid = "cx{}".format(i)
            paths[sid] = self.t.plant_codex(session_id=sid)
            _mark(self.t.home, "codex-cli", sid, paths[sid], self.t.root, source="startup")
            if i == 0:
                with open(paths[sid], "a", encoding="utf-8") as fh:
                    for row in _repo.codex_apply_patch_rows(
                            ["*** Update File: {}".format(a)], ordinal=50):
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        _mark(self.t.home, "claude-code", "claude1", own_path, self.t.root, source="startup")
        payload = _claude_payload("claude1", own_path, self.t.root)

        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        # cx0 is resumed -- no new edits -- and only now becomes a top-3
        # candidate (cx1/cx2/cx3 were the newest 3 before this).
        _mark(self.t.home, "codex-cli", "cx0", paths["cx0"], self.t.root, source="resume")
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_resumed_week_old_session_start_size_still_excludes_history_repro6(self):
        """repro6 `resume-week` mode -- same shape as
        `test_resumed_old_session_start_size_excludes_its_old_history_repro5`
        but built with the exact `mark()` helper/sequence the reviewer's
        repro6.py uses (a week-old session never ledgered at all until it's
        resumed): still silent both turns."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        foreign_path = self.t.plant_codex(session_id="cx1")

        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        _mark(self.t.home, "claude-code", "claude1", own_path, self.t.root, source="startup")

        # The week-old session grows before it's ever resumed/ledgered.
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        payload = _claude_payload("claude1", own_path, self.t.root)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        _mark(self.t.home, "codex-cli", "cx1", foreign_path, self.t.root, source="resume")
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_genuinely_new_session_edited_after_my_start_is_caught_repro6(self):
        """repro6 `new` mode: a Codex session that genuinely starts (real
        `startup` mark) *after* my own session, then edits a file -- still
        caught, same turn."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        _mark(self.t.home, "claude-code", "claude1", own_path, self.t.root, source="startup")
        payload = _claude_payload("claude1", own_path, self.t.root)

        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        foreign_path = self.t.plant_codex(session_id="cx1")
        _mark(self.t.home, "codex-cli", "cx1", foreign_path, self.t.root, source="startup")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("a.py", note)

    def test_stale_recorded_size_is_never_used_repro4(self):
        """repro4 (review #1 finding 2, round 4): an *earlier* claude session's
        mark backfilled cx1 at size S0 (`via:"scan"`, before this session even
        started). cx1 kept growing after that -- all still before this
        session's own genuine start row -- and by the time this session's own
        SessionStart handoff ran, it had already disclosed everything up to
        the file's current size, not S0. Using the backfill row's recorded
        `size` (round 3's shortcut) would re-report that already-handed-off
        growth as new; the fix always snaps an "old" baseline to the current
        size instead."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        foreign_path = self.t.plant_codex(session_id="cx1")
        # An earlier claude session's mark backfilled cx1 at size S0.
        ledger.append({"repo": self.t.key, "harness": "codex-cli",
                       "session": "cx1", "event": "start", "via": "scan",
                       "epoch": time.time() - 7300, "path": foreign_path,
                       "cwd": self.t.root, "size": os.path.getsize(foreign_path)},
                      home=self.t.home)
        # cx1 keeps working -- edits a.py -- all BEFORE this session starts.
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        # Now this session starts for real (genuine mark) -- its own
        # SessionStart handoff has already delivered cx1's DID a.py.
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        self._own_row(path=own_path)
        payload = _claude_payload("claude1", own_path, self.t.root)

        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")  # cx1 idle since before my start
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")  # still idle

    def test_own_row_scrolled_out_of_the_tail_on_a_later_turn_still_catches_a_new_session(self):
        """Review #1 finding 3 (round 4): this session's own start row is
        only in the ledger tail on the *establishing* turn -- by a later
        turn, enough other traffic (other repos, other sessions) has pushed
        it out of the 200KB bounded tail entirely (simulated here directly
        by monkeypatching the tail size rather than writing hundreds of
        padding rows). A brand-new foreign session that only appears after
        that point must still be recognized as new via the persisted
        `known_foreign` snapshot, not conservatively ignored just because
        `my_pos` can no longer be resolved."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        self._own_row(path=own_path)
        payload = _claude_payload("claude1", own_path, self.t.root)

        # Establishing turn -- my_pos resolves fine, no foreign sessions yet.
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        # Simulate this session's own row having scrolled out of the bounded
        # tail by the time of the next turn: read_tail's own bound is what
        # would eventually do this on a real, heavily-trafficked ledger;
        # here the *effect* (my_pos unresolvable) is reproduced directly by
        # patching read_tail to drop rows at or before my own row's position.
        real_read_tail = ledger.read_tail

        def _tail_without_my_row(*a_, **kw):
            rows = real_read_tail(*a_, **kw)
            return [r for r in rows if not (r.get("harness") == "claude-code"
                                            and r.get("session") == "claude1")]

        with unittest.mock.patch.object(ledger, "read_tail", side_effect=_tail_without_my_row):
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("a.py", note)

    def test_backfilled_dead_session_never_warns_repro3(self):
        """repro3: a dead Codex session that never grew, backfilled
        (`via:"scan"`) at this session's own SessionStart *after* this
        session's own row landed. Its position alone would look like
        "started after me", but it's not a genuine row — must be treated as
        old, using its recorded `size` as the baseline, and never reported
        even once this session later touches the same file."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        # yesterday's dead codex session that already edited a.py, never
        # recorded in the ledger by its own (untrusted) hook.
        foreign_path = self.t.plant_codex(session_id="cxold")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [])
        self._own_row(path=own_path)
        # Backfilled *after* my own row, via cli._backfill_foreign_sessions'
        # shape: via:"scan", carrying the size it observed at scan time.
        ledger.append({"repo": self.t.key, "harness": "codex-cli",
                       "session": "cxold", "event": "start",
                       "epoch": time.time() - 86400, "path": foreign_path,
                       "cwd": self.t.root, "via": "scan",
                       "size": os.path.getsize(foreign_path)}, home=self.t.home)

        payload = _claude_payload("claude1", own_path, self.t.root)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

        # This session now touches a.py too -- cxold still never grew, so
        # there must still be nothing to report.
        _write_claude(own_path, "고쳐줘", [a])
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")

    def test_an_earlier_row_before_mine_makes_it_old_even_with_a_later_genuine_row(self):
        """A session that started long ago (an earlier genuine row before
        mine), then resumed/grew again after mine started (another row,
        after) -- the *earlier* row must still win: this is an old session,
        not a new one, regardless of what comes after."""
        a = os.path.join(self.t.root, "a.py")
        with open(a, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        foreign_path = self.t.plant_codex(session_id="cx1")
        # cx1's own original (genuine) start row, well before mine.
        ledger.append({"repo": self.t.key, "harness": "codex-cli",
                       "session": "cx1", "event": "start",
                       "epoch": time.time() - 3600, "path": foreign_path,
                       "cwd": self.t.root}, home=self.t.home)

        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [a])
        self._own_row(path=own_path)

        # cx1 grows again later (e.g. reactivated after a resume) -- another
        # row lands *after* mine, but it doesn't erase the earlier one.
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(a)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        ledger.append({"repo": self.t.key, "harness": "codex-cli",
                       "session": "cx1", "event": "start", "via": "scan", "grew": 1,
                       "size": os.path.getsize(foreign_path),
                       "epoch": time.time(), "path": foreign_path,
                       "cwd": self.t.root}, home=self.t.home)

        payload = _claude_payload("claude1", own_path, self.t.root)
        # Old (the earlier row before mine wins) -- first observation snaps
        # to the recorded size, silent, and stays silent.
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")


class TestTouchedCap(unittest.TestCase):
    """Review #1 finding 6: insertion order, oldest dropped first, cap 500."""

    def test_oldest_dropped_first_and_insertion_order_kept(self):
        t = stale._Touched([])
        for i in range(510):
            t.add("f{}.py".format(i))
        lst = t.to_list()
        self.assertEqual(stale.TOUCHED_CAP, 500)
        self.assertEqual(len(lst), 500)
        self.assertNotIn("f0.py", lst)
        self.assertNotIn("f9.py", lst)
        self.assertIn("f10.py", lst)
        self.assertEqual(lst[0], "f10.py")
        self.assertEqual(lst[-1], "f509.py")

    def test_re_adding_an_existing_path_does_not_move_it_or_grow(self):
        t = stale._Touched(["a.py", "b.py"])
        t.add("a.py")
        self.assertEqual(t.to_list(), ["a.py", "b.py"])


class TestByteCap(unittest.TestCase):
    def test_render_never_exceeds_budget_with_long_korean_paths(self):
        paths = ["긴/한국어/경로/{}/파일이름아주아주아주길게만들어봅니다.py".format(i)
                 for i in range(30)]
        out = stale._render([("codex-cli", "01a0d2e1deadbeef", paths)])
        self.assertLessEqual(len(out.encode("utf-8")), stale.NOTE_BUDGET)
        self.assertIn("FILE", out)

    def test_still_discloses_when_the_only_path_does_not_fit(self):
        # Review #1 finding 4 (round 3): an overlap must never be silently
        # lost -- even when literally nothing fits, the header and a MORE
        # disclosure still go out (this used to return "" entirely).
        out = stale._render([("codex-cli", "s", ["x" * 1000])])
        self.assertNotEqual(out, "")
        self.assertIn("codex-cli", out)
        self.assertIn("MORE", out)
        self.assertIn("+1 file", out)
        self.assertNotIn("FILE", out)  # nothing actually fit
        self.assertLessEqual(len(out.encode("utf-8")), stale.NOTE_BUDGET)

    def test_an_over_long_path_in_the_first_session_does_not_wipe_out_the_second(self):
        # Review #1 finding 4 (round 3): dropping from the tail unconditionally
        # meant one over-long path in an EARLY session could wipe out a later
        # session that would have fit easily. Must skip just the bad entry.
        out = stale._render([
            ("codex-cli", "s1", ["가" * 200]),
            ("codex-cli", "s2", ["b.py"]),
        ])
        self.assertIn("s2", out)
        self.assertIn("b.py", out)
        self.assertNotIn("s1", out)
        self.assertIn("MORE", out)
        self.assertIn("+1 file", out)
        self.assertIn("+1 session", out)
        self.assertLessEqual(len(out.encode("utf-8")), stale.NOTE_BUDGET)

    def test_multiple_overlapping_sessions_are_all_shown(self):
        out = stale._render([
            ("codex-cli", "cx1", ["a.py"]),
            ("codex-cli", "cx2", ["b.py"]),
        ])
        self.assertIn("cx1", out)
        self.assertIn("a.py", out)
        self.assertIn("cx2", out)
        self.assertIn("b.py", out)
        self.assertLessEqual(len(out.encode("utf-8")), stale.NOTE_BUDGET)

    def test_truncated_multi_session_case_discloses_more(self):
        # Many sessions, each with a long path — must not silently drop any
        # of them (review #1 finding 2): whatever doesn't fit shows as MORE.
        overlaps = [("codex-cli", "sess{}".format(i), ["매우/긴/경로/이름/{}.py".format(i) * 3])
                    for i in range(10)]
        out = stale._render(overlaps)
        self.assertLessEqual(len(out.encode("utf-8")), stale.NOTE_BUDGET)
        self.assertIn("MORE", out)
        self.assertIn("+", out)

    def test_empty_overlaps_is_silent(self):
        self.assertEqual(stale._render([]), "")


class TestTimeBudget(unittest.TestCase):
    def test_deadline_already_passed_stops_before_reading_foreign_sessions(self):
        t = TempRepo()
        self.addCleanup(t.close)
        own_path = os.path.join(t.home, "own.jsonl")
        _write_claude(own_path, "시간 예산 테스트", [])
        t.plant_codex(session_id="cx1", ledger_home=t.home)
        payload = _claude_payload("claude1", own_path, t.root)
        note = stale.check(harness="claude-code", stdin_text=payload, home=t.home,
                           deadline=time.monotonic() - 1)
        self.assertEqual(note, "")
        # Nothing observed yet, since the loop never entered — no foreign
        # baseline was recorded either (still "unread", not "unchanged").
        state_path = stale._state_path(t.state, "claude-code", "claude1")
        with open(state_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved.get("foreign"), {})


def _codex_assistant_row(text: str, ordinal: int = 1) -> dict:
    """The agent's own text -- output_text, not input_text (codex_cli.py's
    _TEXT_BLOCKS: user/developer use input_text, assistant uses output_text).
    Used only to prove phase 3 never surfaces this (invariant 3)."""
    return {
        "timestamp": "2026-09-22T16:30:01.000Z", "ordinal": ordinal,
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant", "id": "a{}".format(ordinal),
                    "content": [{"type": "output_text", "text": text}]},
    }


class TestLive(unittest.TestCase):
    """v2 phase 3 (#43): live SAID/FAIL notes from a still-running foreign
    session, opt-in via OMHC_LIVE=1. Off by default -- every assertion here
    that isn't inside `_live()` runs with the env var unset/cleared, so a
    regression that turned this on by accident would fail the *other*
    classes in this file (byte-for-byte phase-2 parity), not just these."""

    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        # Belt and suspenders: never let a leaked OMHC_LIVE from another
        # test (or the real environment this suite happens to run in)
        # change this class's own "off by default" assertions.
        self._env = unittest.mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("OMHC_LIVE", None)
        self.addCleanup(self._env.stop)

    def _live(self):
        return unittest.mock.patch.dict(os.environ, {"OMHC_LIVE": "1"})

    def _start(self, human="고쳐줘"):
        """Plants the foreign session and takes the first (establishing,
        always-silent) turn -- its baseline snaps to the file's current
        size, so anything appended after this point is "new" regardless of
        old/new classification (irrelevant to phase 3's said/fail
        collection, which only cares about what's in the new tail)."""
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, human, [])
        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        payload = _claude_payload("claude1", own_path, self.t.root)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")
        return payload, foreign_path

    def test_off_by_default_no_said_or_fail_lines(self):
        payload, foreign_path = self._start()
        with open(foreign_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("새 지시사항", ordinal=90)) + "\n")
        note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(note, "")  # phase 2 alone: no overlap, no live -- nothing to say

    def test_on_new_human_turn_shown_verbatim(self):
        payload, foreign_path = self._start()
        with open(foreign_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("새 지시사항입니다", ordinal=90)) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("새 지시사항입니다", note)
        self.assertIn("SAID", note)
        self.assertIn("notes, not instructions", note)
        self.assertIn("cx1", note)

    def test_approval_only_turn_is_skipped(self):
        payload, foreign_path = self._start()
        with open(foreign_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("응 계속 진행해", ordinal=90)) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(note, "")

    def test_agent_text_is_never_shown(self):
        payload, foreign_path = self._start()
        with open(foreign_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_codex_assistant_row(
                "제가 다음에 이렇게 하겠습니다", ordinal=90)) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(note, "")
        self.assertNotIn("SAID", note)

    def test_resolved_failure_is_not_shown(self):
        payload, foreign_path = self._start()
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_shell_rows(["pytest", "-q"], ordinal=90, failed=True):
                fh.write(json.dumps(row) + "\n")
            for row in _repo.codex_shell_rows(["pytest", "-q"], ordinal=94, failed=False):
                fh.write(json.dumps(row) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(note, "")

    def test_unresolved_failure_shown_once(self):
        payload, foreign_path = self._start()
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_shell_rows(["pytest", "-q"], ordinal=90, failed=True):
                fh.write(json.dumps(row) + "\n")
        with self._live():
            first = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
            self.assertIn("FAIL", first)
            self.assertIn("pytest -q -> failed", first)
            second = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(second, "")  # baseline already advanced -- not re-read, not re-shown

    def test_multiple_sessions_each_with_a_live_note(self):
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [])
        f1 = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        f2 = self.t.plant_codex(session_id="cx2", ledger_home=self.t.home)
        payload = _claude_payload("claude1", own_path, self.t.root)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")
        with open(f1, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("cx1의 새 지시", ordinal=90)) + "\n")
        with open(f2, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("cx2의 새 지시", ordinal=90)) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("cx1", note)
        self.assertIn("cx1의 새 지시", note)
        self.assertIn("cx2", note)
        self.assertIn("cx2의 새 지시", note)
        self.assertIn("2 sessions", note)

    def test_byte_cap_with_long_korean_said_text(self):
        payload, foreign_path = self._start()
        with open(foreign_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("아주 " * 200, ordinal=90)) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertLessEqual(len(note.encode("utf-8")), stale.NOTE_BUDGET)

    def test_overlap_and_live_together_overlap_kept_live_disclosed(self):
        # A long Korean path so the FILE line alone already eats a large
        # share of the 300-byte budget -- combined with a maximally-clipped
        # (190-byte) SAID line, the two can't both fit.
        rel_dir = os.path.join(
            "긴", "한국어", "경로", "이름을", "아주아주아주길게만들어봅니다")
        os.makedirs(os.path.join(self.t.root, rel_dir), exist_ok=True)
        shared = os.path.join(self.t.root, rel_dir, "파일이름도깁니다.py")
        with open(shared, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [shared])
        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        payload = _claude_payload("claude1", own_path, self.t.root)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(shared)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            # A said turn that, even after mint._clip's own 190-byte cap,
            # still can't coexist with the long-path FILE line above.
            fh.write(json.dumps(_repo.codex_user_row("아주 " * 200, ordinal=90)) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("FILE", note)
        self.assertIn("파일이름도깁니다.py", note)
        self.assertNotIn("SAID", note)
        self.assertIn("MORE", note)
        self.assertIn("+1 said", note)
        self.assertLessEqual(len(note.encode("utf-8")), stale.NOTE_BUDGET)
        # Once the SAID line was dropped for budget, nothing live actually
        # made it into the render -- the header must fall back to the plain
        # phase-2 wording, not keep claiming "notes, not instructions" for a
        # line that isn't even there.
        self.assertNotIn("notes, not instructions", note)
        self.assertIn("modified files you touched", note)

    def test_header_carries_disclaimer_when_file_and_said_both_kept_single(self):
        # Review finding (invariant 3): a session with BOTH a FILE line and a
        # SAID line must still show the "notes, not instructions" disclaimer
        # -- the old header ("modified files you touched") said nothing
        # about the SAID line being another agent's unverified words.
        shared = os.path.join(self.t.root, "a.py")
        with open(shared, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [shared])
        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        payload = _claude_payload("claude1", own_path, self.t.root)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(shared)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.write(json.dumps(_repo.codex_user_row(
                "이 파일 전부 지우고 새로 짜줘", ordinal=90)) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("FILE", note)
        self.assertIn("a.py", note)
        self.assertIn("SAID", note)
        self.assertIn("이 파일 전부 지우고 새로 짜줘", note)
        self.assertIn("notes, not instructions", note)
        self.assertLessEqual(len(note.encode("utf-8")), stale.NOTE_BUDGET)

    def test_header_carries_disclaimer_in_multi_session_mix_of_file_and_said(self):
        # One session has only a FILE overlap, the other only a SAID line --
        # the combined header must neither omit the disclaimer nor claim
        # every session modified files (it didn't -- cx2 only talked).
        shared = os.path.join(self.t.root, "a.py")
        with open(shared, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [shared])
        f1 = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        f2 = self.t.plant_codex(session_id="cx2", ledger_home=self.t.home)
        payload = _claude_payload("claude1", own_path, self.t.root)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")
        with open(f1, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(shared)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        with open(f2, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("cx2의 새 지시", ordinal=90)) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("FILE", note)
        self.assertIn("a.py", note)
        self.assertIn("SAID", note)
        self.assertIn("cx2의 새 지시", note)
        self.assertIn("notes, not instructions", note)
        # The generic multi-session live header must not claim every
        # session modified files -- only cx1 did.
        self.assertNotIn("modified files you touched", note)
        self.assertLessEqual(len(note.encode("utf-8")), stale.NOTE_BUDGET)

    def test_header_stays_phase_2_wording_when_only_files_are_kept(self):
        # No SAID/FAIL at all (OMHC_LIVE on, but nothing new was said/failed)
        # -- header must be byte-identical to phase 2's, no disclaimer.
        shared = os.path.join(self.t.root, "a.py")
        with open(shared, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        own_path = os.path.join(self.t.home, "own.jsonl")
        _write_claude(own_path, "고쳐줘", [shared])
        foreign_path = self.t.plant_codex(session_id="cx1", ledger_home=self.t.home)
        payload = _claude_payload("claude1", own_path, self.t.root)
        self.assertEqual(stale.check(harness="claude-code", stdin_text=payload,
                                     home=self.t.home), "")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            for row in _repo.codex_apply_patch_rows(
                    ["*** Update File: {}".format(shared)], ordinal=50):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertIn("FILE", note)
        self.assertIn("modified files you touched", note)
        self.assertNotIn("notes, not instructions", note)

    def test_omhc_off_wins_over_live(self):
        payload, foreign_path = self._start()
        os.makedirs(self.t.state, exist_ok=True)
        with open(os.path.join(self.t.state, "off"), "w", encoding="utf-8") as fh:
            fh.write("")
        with open(foreign_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("새 지시사항", ordinal=90)) + "\n")
        with self._live():
            note = stale.check(harness="claude-code", stdin_text=payload, home=self.t.home)
        self.assertEqual(note, "")

    def test_garbage_payload_still_returns_empty_not_raise(self):
        with self._live():
            note = stale.check(harness="claude-code", stdin_text="{not json",
                               home=self.t.home)
        self.assertEqual(note, "")


if __name__ == "__main__":
    unittest.main()
