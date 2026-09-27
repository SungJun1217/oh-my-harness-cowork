"""#51: a Claude session whose model call fails (401 / "Not logged in" /
rate limit) still runs mark/brief first and claims the handoff in
delivered.tsv — the next Claude session's due() walk then hits that
already-delivered source and stops, losing the handoff entirely. cmd_mark
bounces such a group back to "not delivered" once the *next* session of that
harness starts and the adapter confirms the model never really replied.
"""
from __future__ import annotations

import io
import json
import os
import time
import unittest
from unittest import mock

from omhc import brief, cli, due, ledger

from . import _repo

NOW = 1758500000.0


def _claude_row(kind, **extra):
    row = {"type": kind, "timestamp": "2026-09-25T00:00:00.000Z"}
    row.update(extra)
    return row


def _write_claude_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


NOT_LOGGED_IN = [
    _claude_row("assistant", isApiErrorMessage=True,
               message={"model": "<synthetic>", "content": [], "usage": {}}),
]

REAL_REPLY = [
    _claude_row("user", cwd="/x", message={"content": "hi"}),
    _claude_row("assistant",
               message={"model": "claude-x", "content": [{"type": "text", "text": "ok"}]}),
]


class Harness:
    def __init__(self):
        self.t = _repo.TempRepo()
        self.home = self.t.home
        self.root = self.t.root
        self.key = self.t.key
        self.state = self.t.state

    def close(self):
        self.t.close()

    def mark(self, harness, session_id, transcript_path=None):
        payload = {"cwd": self.root, "session_id": session_id}
        if transcript_path is not None:
            payload["transcript_path"] = transcript_path
        args = cli.build_parser().parse_args(
            ["mark", "--harness", harness, "--stdin", json.dumps(payload)])
        out = io.StringIO()
        code = cli.cmd_mark(args, home=self.home, out=out)
        return code, out.getvalue()

    def plant_codex(self, session_id, human="필드 경로부터 다시 확인해줘", when=NOW):
        return self.t.plant_codex(session_id=session_id, human=human,
                                  ledger_home=self.home, when=when)

    def claude_path(self, session_id):
        return os.path.join(self.home, ".claude", "projects", "p", session_id + ".jsonl")


class TestBounceIntegration(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def _deliver_to(self, session_id):
        return brief.compute(my_harness="claude-code", my_session_id=session_id,
                             repo_root=self.h.root, home=self.h.home, now=NOW)

    def test_failed_recipient_gets_bounced_and_source_is_redelivered(self):
        self.h.plant_codex("X1")
        c1 = self.h.claude_path("C1")
        self.h.mark("claude-code", "C1", transcript_path=c1)
        body = self._deliver_to("C1")
        self.assertIn("필드 경로", body)

        # C1's model call then fails — only the synthetic error record exists.
        _write_claude_jsonl(c1, NOT_LOGGED_IN)

        c2 = self.h.claude_path("C2")
        code, out = self.h.mark("claude-code", "C2", transcript_path=c2)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

        # The #27 guard must not have anything to suppress on: X1 is due again.
        got = due.due_one(self.h.key, "claude-code", "C2", NOW, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "X1")

        delivered = os.path.join(self.h.state, due.DELIVERED_NAME)
        with open(delivered, encoding="utf-8") as fh:
            lines = [ln.rstrip("\n").split("\t") for ln in fh if ln.strip()]
        bounce_lines = [p for p in lines if len(p) >= 2 and p[1] == due.BOUNCE_MARKER]
        self.assertEqual(len(bounce_lines), 1)
        self.assertEqual(bounce_lines[0][0], "X1")
        self.assertEqual(bounce_lines[0][2], "claude-code")

    def test_a_recipient_that_never_wrote_a_transcript_is_bounced(self):
        """#55: `claude -p` exited before the model call and wrote no file,
        but its SessionStart hooks had already claimed the handoff."""
        self.h.plant_codex("X1")
        c1 = self.h.claude_path("C1")  # recorded, but never created
        self.h.mark("claude-code", "C1", transcript_path=c1)
        self.assertIn("필드 경로", self._deliver_to("C1"))
        self.assertFalse(os.path.exists(c1))

        self.h.mark("claude-code", "C2", transcript_path=self.h.claude_path("C2"))
        got = due.due_one(self.h.key, "claude-code", "C2", NOW, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "X1")

    def test_successful_recipient_is_not_bounced(self):
        self.h.plant_codex("X1")
        c1 = self.h.claude_path("C1")
        self.h.mark("claude-code", "C1", transcript_path=c1)
        self._deliver_to("C1")
        _write_claude_jsonl(c1, REAL_REPLY)

        c2 = self.h.claude_path("C2")
        self.h.mark("claude-code", "C2", transcript_path=c2)

        got = due.due_one(self.h.key, "claude-code", "C2", NOW, home=self.h.home)
        self.assertIsNone(got)

    def test_recipient_equal_to_current_session_never_bounces(self):
        """The SessionStart hook fires several times per session — mark
        running again for the same session must never bounce its own delivery."""
        self.h.plant_codex("X1")
        c1 = self.h.claude_path("C1")
        self.h.mark("claude-code", "C1", transcript_path=c1)
        self._deliver_to("C1")
        _write_claude_jsonl(c1, NOT_LOGGED_IN)

        self.h.mark("claude-code", "C1", transcript_path=c1)
        got = due.due_one(self.h.key, "claude-code", "C1", NOW, home=self.h.home)
        self.assertIsNone(got)

    def test_second_mark_does_not_bounce_the_same_group_twice(self):
        self.h.plant_codex("X1")
        c1 = self.h.claude_path("C1")
        self.h.mark("claude-code", "C1", transcript_path=c1)
        self._deliver_to("C1")
        _write_claude_jsonl(c1, NOT_LOGGED_IN)

        c2 = self.h.claude_path("C2")
        self.h.mark("claude-code", "C2", transcript_path=c2)
        self.h.mark("claude-code", "C2", transcript_path=c2)  # hook fires several times

        delivered = os.path.join(self.h.state, due.DELIVERED_NAME)
        with open(delivered, encoding="utf-8") as fh:
            lines = [ln.rstrip("\n").split("\t") for ln in fh if ln.strip()]
        bounce_lines = [p for p in lines if len(p) >= 2 and p[1] == due.BOUNCE_MARKER]
        self.assertEqual(len(bounce_lines), 1)

    def test_also_sources_are_bounced_alongside_the_head(self):
        self.h.plant_codex("X1", when=NOW - 200)
        self.h.plant_codex("X2", when=NOW - 100)
        c1 = self.h.claude_path("C1")
        self.h.mark("claude-code", "C1", transcript_path=c1)
        body = self._deliver_to("C1")
        self.assertTrue(body)
        _write_claude_jsonl(c1, NOT_LOGGED_IN)

        c2 = self.h.claude_path("C2")
        self.h.mark("claude-code", "C2", transcript_path=c2)

        marks = due.due(self.h.key, "claude-code", "C2", NOW, home=self.h.home)
        self.assertEqual({m.session_id for m in marks}, {"X1", "X2"})

    def test_a_reopen_of_the_head_before_the_next_mark_does_not_suppress_the_bounce(self):
        """#51 review finding 2 repro: X1 (ALSO) + X2 (head) delivered to
        C1, C1 fails, the human resumes X2 in Codex (a reopen line, no
        bounce yet) before C2 ever starts. That reopen alone must not look
        like "already bounced" — X1 must not be lost, and X2's #27 baseline
        must fall back past the failed delivery instead of redelivering
        against its stale offset."""
        self.h.plant_codex("X1", when=NOW - 200)
        self.h.plant_codex("X2", when=NOW - 100)
        c1 = self.h.claude_path("C1")
        self.h.mark("claude-code", "C1", transcript_path=c1)
        self._deliver_to("C1")
        _write_claude_jsonl(c1, NOT_LOGGED_IN)

        due.mark_reopened(self.h.state, "X2", "codex-cli", NOW - 50)

        c2 = self.h.claude_path("C2")
        self.h.mark("claude-code", "C2", transcript_path=c2)

        marks = due.due(self.h.key, "claude-code", "C2", NOW, home=self.h.home)
        self.assertEqual({m.session_id for m in marks}, {"X1", "X2"})
        self.assertIsNone(due.last_delivery_offset(self.h.state, "X2", "claude-code"))

    def test_old_format_delivered_line_never_bounces(self):
        wm = due.Watermark(repo_key=self.h.key, harness="codex-cli", session_id="X1",
                           path="/p", event="start", epoch=NOW - 600)
        due.mark_delivered(self.h.state, wm, to_harness="claude-code", epoch=NOW - 500)
        c2 = self.h.claude_path("C2")
        code, out = self.h.mark("claude-code", "C2", transcript_path=c2)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        got = due.due_one(self.h.key, "claude-code", "C2", NOW, home=self.h.home)
        self.assertIsNone(got)

    def test_recipient_transcript_read_raising_still_exits_0_with_empty_stdout(self):
        self.h.plant_codex("X1")
        c1 = self.h.claude_path("C1")
        self.h.mark("claude-code", "C1", transcript_path=c1)
        self._deliver_to("C1")
        _write_claude_jsonl(c1, NOT_LOGGED_IN)

        c2 = self.h.claude_path("C2")
        with mock.patch("omhc.adapters.claude_code.ClaudeCodeAdapter.delivery_reached_model",
                        side_effect=RuntimeError("boom")):
            code, out = self.h.mark("claude-code", "C2", transcript_path=c2)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
