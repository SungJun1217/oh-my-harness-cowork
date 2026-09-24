"""`omhc mark` 의 원장 백필. 신뢰되지 않은 Codex 훅 때문에 그 세션이 원장에
전혀 없어도, Claude 쪽 mark 가 discover() 로 찾아 채워 넣어 due() 가 여전히
Codex→Claude 를 볼 수 있게 한다. due() 자체는 손대지 않는다.
"""
from __future__ import annotations

import io
import json
import os
import time
import unittest
from unittest import mock

from omhc import adapters, cli, due, ledger, locate

from . import _repo


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


class Harness:
    def __init__(self):
        self.t = _repo.TempRepo()
        self.home = self.t.home
        self.root = self.t.root
        self.key = self.t.key
        self.state = self.t.state

    def close(self):
        self.t.close()

    def plant(self, session_id, epoch, human="필드 경로부터 다시 확인해줘",
             meta_extra=None, ledger_home=""):
        extra = {"timestamp": _iso(epoch)}
        if meta_extra:
            extra.update(meta_extra)
        return self.t.plant_codex(session_id=session_id, human=human, when=epoch,
                                  meta_extra=extra, ledger_home=ledger_home)

    def mark(self, harness="claude-code", session_id="me1", env=None):
        stdin = json.dumps({"cwd": self.root, "session_id": session_id})
        args = cli.build_parser().parse_args(
            ["mark", "--harness", harness, "--stdin", stdin])
        out = io.StringIO()
        patched = mock.patch.dict(os.environ, env or {})
        with patched:
            code = cli.cmd_mark(args, home=self.home, out=out)
        return code, out.getvalue()

    def scan_rows(self, harness="codex-cli"):
        rows = ledger.read(repo_key=self.key, home=self.home)
        return [r for r in rows if r.get("harness") == harness and r.get("via") == "scan"]


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_claude_mark_backfills_an_unledgered_codex_session(self):
        """Codex 훅이 신뢹되지 않아 원장에 없는 세션도 discover() 로 채워진다."""
        now = time.time()
        self.h.plant("cx1", now - 600)
        code, _ = self.h.mark()
        self.assertEqual(code, 0)
        rows = self.h.scan_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "cx1")

    def test_due_then_sees_the_backfilled_session(self):
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark()
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.harness, "codex-cli")
        self.assertEqual(got.session_id, "cx1")

    def test_brief_produces_a_handoff_from_the_backfilled_session(self):
        from omhc import brief

        now = time.time()
        self.h.plant("cx1", now - 600, human="리더를 붙여서 양방향으로 만들기")
        self.h.mark()
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.root, home=self.h.home, now=now)
        self.assertIn("[omhc]", body)
        self.assertIn("리더를 붙여서", body)

    def test_older_unledgered_session_never_overrides_a_newer_ledgered_one(self):
        """due() 는 원장 append 순서로 "가장 최근"을 고른다. 오래된 세션이
        나중에 붙으면 최신으로 오인된다 — 그래서 더 오래된 것은 걸러야 한다."""
        now = time.time()
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "cx-new",
                      "event": "start", "epoch": now - 100, "path": "/nope",
                      "cwd": self.h.root}, home=self.h.home)
        self.h.plant("cx-old", now - 500)  # 원장의 cx-new 보다 오래됐다
        self.h.mark()
        self.assertEqual(self.h.scan_rows(), [])
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-new")

    def test_a_newer_unledgered_session_is_appended_after_the_ledgered_one(self):
        now = time.time()
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "cx-old",
                      "event": "start", "epoch": now - 500, "path": "/nope",
                      "cwd": self.h.root}, home=self.h.home)
        self.h.plant("cx-new", now - 100)
        self.h.mark()
        rows = self.h.scan_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "cx-new")
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-new")

    def test_six_session_starts_append_the_same_session_only_once(self):
        """SessionStart 가 한 세션에서 6번 발동한 실측을 흉내낸다."""
        now = time.time()
        self.h.plant("cx1", now - 600)
        for _ in range(6):
            self.h.mark()
        self.assertEqual(len(self.h.scan_rows()), 1)

    def test_omhc_off_disables_the_scan(self):
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark(env={"OMHC_OFF": "1"})
        self.assertEqual(self.h.scan_rows(), [])

    def test_subagent_rollout_is_never_backfilled(self):
        now = time.time()
        self.h.plant("cx-sub", now - 600,
                     meta_extra={"thread_source": "subagent"})
        self.h.mark()
        self.assertEqual(self.h.scan_rows(), [])

    def test_exec_rollout_is_not_backfilled_by_default(self):
        now = time.time()
        self.h.plant("cx-exec", now - 600,
                     meta_extra={"source": "exec", "originator": "codex_exec"})
        self.h.mark()
        self.assertEqual(self.h.scan_rows(), [])

    def test_exec_rollout_is_backfilled_under_the_headless_override(self):
        now = time.time()
        self.h.plant("cx-exec", now - 600,
                     meta_extra={"source": "exec", "originator": "codex_exec"})
        self.h.mark(env={"OMHC_ALLOW_HEADLESS": "1"})
        self.assertEqual(len(self.h.scan_rows()), 1)

    def test_a_garbage_rollout_leaves_mark_exiting_zero_with_empty_stdout(self):
        directory = os.path.join(self.h.home, ".codex", "sessions",
                                 time.strftime("%Y/%m/%d", time.gmtime()))
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "rollout-garbage.jsonl"), "wb") as fh:
            fh.write(b"\x00\xff{not json\n\n\x80\x81")
        code, out = self.h.mark()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_the_per_mark_cap_holds(self):
        now = time.time()
        for i in range(cli.BACKFILL_CAP + 3):
            self.h.plant("cx{}".format(i), now - 1000 + i)
        self.h.mark()
        self.assertEqual(len(self.h.scan_rows()), cli.BACKFILL_CAP)

    def test_cap_exceeded_still_delivers_the_newest_session(self):
        """리뷰 결함: 오름차순으로 걷다 cap 에서 끊으면 가장 오래된 5개가
        남는다. due() 는 원장의 마지막 것을 고르므로, 8개 중 5개만 담을 때
        cx7(최신) 대신 cx4 가 나오면 안 된다."""
        now = time.time()
        total = cli.BACKFILL_CAP + 3
        for i in range(total):
            self.h.plant("cx{}".format(i), now - 1000 + i)
        self.h.mark()
        newest = "cx{}".format(total - 1)
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, newest)
        rows = self.h.scan_rows()
        self.assertIn(newest, {r["session"] for r in rows})
        oldest = "cx0"
        self.assertNotIn(oldest, {r["session"] for r in rows})

    def test_sessions_from_a_nested_child_repo_are_not_backfilled(self):
        """Claude 가 `.git` 없는 부모 디렉터리에서 열리면, discover() 의
        equal-or-descendant 판정이 그 아래 **자기 `.git`을 가진 자식** 레포의
        세션까지 통과시킨다(중첩 워크트리·서브모듈과 같은 모양). 그 세션을
        부모의 repo 키로 원장에 적으면 다른 레포의 GOAL 이 새어든다 — repo 키가
        다르면 걸러야 한다."""
        now = time.time()
        outer = os.path.dirname(self.h.t.repo)  # repo 의 부모. .git 없음
        self.h.plant("cx-child", now - 600)  # cwd 는 기본값(child repo, self.h.root)

        stdin = json.dumps({"cwd": outer, "session_id": "me1"})
        args = cli.build_parser().parse_args(
            ["mark", "--harness", "claude-code", "--stdin", stdin])
        out = io.StringIO()
        code = cli.cmd_mark(args, home=self.h.home, out=out)
        self.assertEqual(code, 0)

        rows = ledger.read(home=self.h.home)
        leaked = [r for r in rows if r.get("session") == "cx-child"
                 and r.get("via") == "scan"]
        self.assertEqual(leaked, [], rows)

    def test_already_delivered_session_is_not_redelivered_after_a_backfill(self):
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark()
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        # 다시 mark 가 돌아도(재발동) 이미 전달된 것은 다시 나오지 않는다.
        self.h.mark()
        self.assertIsNone(due.due(self.h.key, "claude-code", "me1", now, home=self.h.home))

    def test_status_codex_hook_still_fails_with_only_scan_rows(self):
        """백필이 만든 via:scan 행이 '훅이 실제로 돌았다' 는 증거로 둔갑하면
        신뢰 안 된 훅을 가려버린다 — health() 는 이미 scan 을 무시한다."""
        now = time.time()
        hooks_dir = os.path.join(self.h.home, ".codex")
        os.makedirs(hooks_dir, exist_ok=True)
        hooks_path = os.path.join(hooks_dir, "hooks.json")
        with open(hooks_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "omhc brief"}]}]}}, fh)
        install_epoch = now - 3600
        os.utime(hooks_path, (install_epoch, install_epoch))
        self.h.plant("cx1", now - 600)  # install 이후 시작
        self.h.mark()
        self.assertEqual(len(self.h.scan_rows()), 1)

        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.h.root)
        try:
            with mock.patch.object(cli.adapters, "present", return_value=["codex-cli"]):
                cli.cmd_status(cli.build_parser().parse_args(["status"]),
                               home=self.h.home, out=out)
        finally:
            os.chdir(cwd)
        line = next(l for l in out.getvalue().splitlines() if "codex hook" in l)
        self.assertTrue(line.startswith("FAIL"), out.getvalue())


if __name__ == "__main__":
    unittest.main()
