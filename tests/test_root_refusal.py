"""#12: `.omhc-root` 마커와 `/` 루트 거부. `mark`/`note`/`watch` 가 각자 소유하는
거부 동작 — `resolve_repo_root` 자체는 절대 거부하지 않는다(mint.relativize 등은
어떤 루트에서도 계속 동작해야 한다), `locate.refused_root` 가 별도 술어다."""
from __future__ import annotations

import io
import json
import os
import unittest

from omhc import cli

from ._repo import TempRepo


class TestMarkRefusesSlash(unittest.TestCase):
    def test_mark_at_slash_writes_nothing_and_prints_nothing(self):
        t = TempRepo()
        self.addCleanup(t.close)
        stdin = json.dumps({"cwd": "/", "session_id": "s1"})
        args = cli.build_parser().parse_args(
            ["mark", "--harness", "claude-code", "--stdin", stdin])
        out = io.StringIO()
        code = cli.cmd_mark(args, home=t.home, out=out)
        self.assertEqual((code, out.getvalue()), (0, ""))
        ledger_path = os.path.join(t.home, ".omhc", "ledger.jsonl")
        self.assertFalse(os.path.exists(ledger_path))


class TestNoteRefusesSlash(unittest.TestCase):
    def test_note_at_slash_exits_2_and_creates_no_state(self):
        t = TempRepo()
        self.addCleanup(t.close)
        cwd = os.getcwd()
        os.chdir("/")
        self.addCleanup(os.chdir, cwd)
        args = cli.build_parser().parse_args(["note", "hello"])
        out, err = io.StringIO(), io.StringIO()
        code = cli.cmd_note(args, home=t.home, out=out, err=err)
        self.assertEqual(code, 2)
        self.assertTrue(err.getvalue())
        self.assertFalse(os.path.isdir(os.path.join(t.home, ".omhc")))


class TestWatchRefusesSlash(unittest.TestCase):
    def test_watch_at_slash_refuses(self):
        t = TempRepo()
        self.addCleanup(t.close)
        cwd = os.getcwd()
        os.chdir("/")
        self.addCleanup(os.chdir, cwd)
        args = cli.build_parser().parse_args(["watch", "--once"])
        out = io.StringIO()
        code = cli.cmd_watch(args, home=t.home, out=out)
        self.assertEqual(code, 1)


class TestLogRefusesSlash(unittest.TestCase):
    """#19: log/clear 도 note/status/watch 와 같은 거부를 한다 — 읽기·정리용이라
    무해하지만, `/` 에서 전체 원장을 뒤지는 것도 오사용이긴 마찬가지다."""

    def test_log_at_slash_refuses(self):
        t = TempRepo()
        self.addCleanup(t.close)
        cwd = os.getcwd()
        os.chdir("/")
        self.addCleanup(os.chdir, cwd)
        args = cli.build_parser().parse_args(["log"])
        out = io.StringIO()
        code = cli.cmd_log(args, home=t.home, out=out)
        self.assertEqual(code, 1)


class TestClearRefusesSlash(unittest.TestCase):
    def test_clear_at_slash_refuses(self):
        t = TempRepo()
        self.addCleanup(t.close)
        cwd = os.getcwd()
        os.chdir("/")
        self.addCleanup(os.chdir, cwd)
        args = cli.build_parser().parse_args(["clear"])
        out = io.StringIO()
        code = cli.cmd_clear(args, home=t.home, out=out)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
