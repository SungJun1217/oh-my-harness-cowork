"""§9 인출률 회계: `omhc show`/`log` 가 성공적으로 산출물을 인출하면 원장에
`{"event":"pull"}` 을 남긴다. 이 행은 due()/backfill/codex health 어느 것도
건드리지 않는다 — `harness` 키가 없기 때문이며, 그 계약을 여기서 고정한다.
"""
from __future__ import annotations

import io
import os
import time
import unittest
from unittest import mock

from omhc import cli, due, index, ledger

from ._repo import TempRepo

REPO_KEY = "pull-repo-key"


def _plant_show_target(t: TempRepo) -> str:
    """refs.tsv 항목 하나 + 원본 바이트를 심고 태그(E1)를 돌려준다."""
    os.makedirs(t.state, exist_ok=True)
    source = os.path.join(t.state, "source.jsonl")
    body = b'{"hello":"world"}\n'
    with open(source, "wb") as fh:
        fh.write(body)
    with open(os.path.join(t.state, index.REFS_NAME), "w", encoding="utf-8") as fh:
        fh.write("\t".join(("E1", "s-delivered", source, "0", str(len(body)), "1")) + "\n")
    return "E1"


def _mark_delivered(t: TempRepo, session_id: str) -> None:
    os.makedirs(t.state, exist_ok=True)
    with open(os.path.join(t.state, due.DELIVERED_NAME), "a", encoding="utf-8") as fh:
        fh.write("\t".join((session_id, "claude-code", "codex-cli", "1700000000")) + "\n")


class TestShowLogWritePullRows(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        cwd = os.getcwd()
        os.chdir(self.t.root)
        self.addCleanup(os.chdir, cwd)

    def _pull_rows(self):
        return [r for r in ledger.read(repo_key=self.t.key, home=self.t.home)
                if r.get("event") == "pull"]

    def test_show_writes_a_pull_row_pointing_at_last_delivered_session(self):
        _mark_delivered(self.t, "s-delivered")
        tag = _plant_show_target(self.t)
        out = io.StringIO()
        code = cli.cmd_show(
            cli.build_parser().parse_args(["show", tag]), home=self.t.home, out=out)
        self.assertEqual(code, 0)
        rows = self._pull_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["via"], "show")
        self.assertEqual(rows[0]["session"], "s-delivered")
        self.assertNotIn("harness", rows[0])

    def test_log_writes_a_pull_row_pointing_at_last_delivered_session(self):
        _mark_delivered(self.t, "s-delivered")
        out = io.StringIO()
        code = cli.cmd_log(cli.build_parser().parse_args(["log"]), home=self.t.home, out=out)
        self.assertEqual(code, 0)
        rows = self._pull_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["via"], "log")
        self.assertEqual(rows[0]["session"], "s-delivered")
        self.assertNotIn("harness", rows[0])

    def test_last_delivered_uses_append_order_not_the_more_recent_write(self):
        _mark_delivered(self.t, "s-old")
        _mark_delivered(self.t, "s-new")
        out = io.StringIO()
        cli.cmd_log(cli.build_parser().parse_args(["log"]), home=self.t.home, out=out)
        rows = self._pull_rows()
        self.assertEqual(rows[0]["session"], "s-new")

    def test_failing_show_writes_nothing(self):
        out = io.StringIO()
        code = cli.cmd_show(
            cli.build_parser().parse_args(["show", "E404"]), home=self.t.home, out=out)
        self.assertEqual(code, 1)
        self.assertEqual(self._pull_rows(), [])

    def test_no_delivery_yet_writes_nothing(self):
        out = io.StringIO()
        code = cli.cmd_log(cli.build_parser().parse_args(["log"]), home=self.t.home, out=out)
        self.assertEqual(code, 0)
        self.assertEqual(self._pull_rows(), [])

    def test_unwritable_ledger_does_not_change_show_output_or_exit_code(self):
        _mark_delivered(self.t, "s-delivered")
        tag = _plant_show_target(self.t)
        out_ok = io.StringIO()
        cli.cmd_show(cli.build_parser().parse_args(["show", tag]), home=self.t.home,
                     out=out_ok)

        # 두 번째 방출을 새 임시 레포에서 비교하기보다, 같은 상태에서 ledger.append
        # 만 실패하게 만들어 출력이 그대로인지 본다.
        with mock.patch.object(ledger, "append", side_effect=OSError("disk full")):
            out_fail = io.StringIO()
            code = cli.cmd_show(
                cli.build_parser().parse_args(["show", tag]), home=self.t.home,
                out=out_fail)
        self.assertEqual(code, 0)
        self.assertEqual(out_ok.getvalue(), out_fail.getvalue())

    def test_unwritable_ledger_does_not_change_log_output_or_exit_code(self):
        _mark_delivered(self.t, "s-delivered")
        out_ok = io.StringIO()
        cli.cmd_log(cli.build_parser().parse_args(["log"]), home=self.t.home, out=out_ok)

        with mock.patch.object(ledger, "append", side_effect=OSError("disk full")):
            out_fail = io.StringIO()
            code = cli.cmd_log(
                cli.build_parser().parse_args(["log"]), home=self.t.home, out=out_fail)
        self.assertEqual(code, 0)
        self.assertEqual(out_ok.getvalue(), out_fail.getvalue())


class TestPullRowsAreInvisibleToTheThreeConsumers(unittest.TestCase):
    """pull 행은 `harness` 키가 없다 — due()/backfill/codex health 세 곳 모두
    event=="start" 나 harness 필드에 기대므로, pull 행이 섞여도 결과가 같아야
    한다."""

    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)

    def _write_pull_row(self, session="cx1"):
        ledger.append({"repo": self.t.key, "event": "pull", "via": "show",
                       "session": session, "epoch": round(time.time(), 0)},
                      home=self.t.home)

    def test_pull_row_alone_does_not_make_a_session_due(self):
        self._write_pull_row("cx1")
        self.assertIsNone(
            due.due(self.t.key, "claude-code", "me1", time.time(), home=self.t.home))

    def test_pull_rows_interleaved_do_not_change_due(self):
        ledger.append({"repo": self.t.key, "harness": "codex-cli", "session": "cx1",
                       "event": "start", "epoch": time.time() - 10, "path": "/p/cx1",
                       "cwd": self.t.root}, home=self.t.home)
        self._write_pull_row("cx1")
        got = due.due(self.t.key, "claude-code", "me1", time.time(), home=self.t.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")

    def test_pull_rows_are_not_counted_as_codex_hook_having_run(self):
        from omhc.adapters.codex_cli import CodexCliAdapter

        adapter = CodexCliAdapter(home=self.t.home)
        self._write_pull_row("cx1")
        rows = ledger.read(home=self.t.home)
        with mock.patch.object(adapter, "hook_is_installed", return_value=True), \
             mock.patch.object(adapter, "hooks_path", return_value=__file__):
            result = adapter.health(self.t.root, rows)
        # health() 는 설치 이후 시작한 세션이 하나도 없으면 (있어도) 미판정
        # 또는 no-session 취지의 결과를 낸다 — 어느 쪽이든 pull-only 행이
        # "훅이 돌았다"는 PASS 증거로 오인되면 안 된다.
        for _label, ok, _detail in result:
            self.assertIsNot(ok, True)


if __name__ == "__main__":
    unittest.main()



class TestShowCreditsTheSessionItRead(unittest.TestCase):
    """show 는 실제로 읽은 세션에 인출을 돌린다 — 가장 최근 전달이 아니라."""

    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        cwd = os.getcwd()
        os.chdir(self.t.root)
        self.addCleanup(os.chdir, cwd)

    def test_show_records_the_resolved_session_and_tag(self):
        _mark_delivered(self.t, "s-old")
        tag = _plant_show_target(self.t)  # E1 → s-delivered
        _mark_delivered(self.t, "s-new")
        code = cli.cmd_show(cli.build_parser().parse_args(["show", tag]),
                            home=self.t.home, out=io.StringIO())
        self.assertEqual(code, 0)
        pulls = [r for r in ledger.read(repo_key=self.t.key, home=self.t.home)
                 if r.get("event") == "pull"]
        self.assertEqual(pulls[-1]["session"], "s-delivered")
        self.assertEqual(pulls[-1]["tag"], "E1")
        self.assertNotIn("harness", pulls[-1])
