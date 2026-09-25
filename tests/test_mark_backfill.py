"""`omhc mark` 의 원장 백필. 신뢰되지 않은 Codex 훅 때문에 그 세션이 원장에
전혀 없어도, Claude 쪽 mark 가 discover() 로 찾아 채워 넣어 due() 가 여전히
Codex→Claude 를 볼 수 있게 한다. due() 자체는 손대지 않는다.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
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

    def mark(self, harness="claude-code", session_id="me1", env=None, source=None):
        payload = {"cwd": self.root, "session_id": session_id}
        if source is not None:
            payload["source"] = source
        stdin = json.dumps(payload)
        args = cli.build_parser().parse_args(
            ["mark", "--harness", harness, "--stdin", stdin])
        out = io.StringIO()
        patched = mock.patch.dict(os.environ, env or {})
        with patched:
            code = cli.cmd_mark(args, home=self.home, out=out)
        return code, out.getvalue()

    def scan_rows(self, harness="codex-cli"):
        # #28: rebase 마커도 via=="scan" 이지만 session 이 없다 — 이 헬퍼는
        # 항상 "특정 세션에 대한" 스캔 행을 의미했으므로 마커는 제외한다.
        rows = ledger.read(repo_key=self.key, home=self.home)
        return [r for r in rows if r.get("harness") == harness and r.get("via") == "scan"
               and r.get("session")]

    def marker_rows(self, harness="codex-cli"):
        rows = ledger.read(repo_key=self.key, home=self.home)
        return [r for r in rows if r.get("harness") == harness
               and r.get("event") == cli.REBASE_EVENT]


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

    def test_mark_still_exits_0_with_empty_stdout_when_its_own_row_is_refused(self):
        """#22: transcript_path 가 상한을 못 맞추는 극단(예: PATH_MAX 급 경로)
        에서도 훅 경로(mark)는 절대 던지지 않고 빈 stdout·exit 0 이어야 한다
        (invariant 2). 거부는 눈에 보이는 곳(ledger.rejected)에만 남는다."""
        from omhc import ledger

        payload = {"cwd": self.h.root, "session_id": "me1",
                   "transcript_path": "/p" * 500}
        args = cli.build_parser().parse_args(
            ["mark", "--harness", "claude-code", "--stdin", json.dumps(payload)])
        out = io.StringIO()
        code = cli.cmd_mark(args, home=self.h.home, out=out)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        self.assertEqual(rows, [])
        rejected = ledger.read_rejected(home=self.h.home, repo_key=self.h.key)
        self.assertEqual(len(rejected), 1)

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

    def test_two_sessions_starting_in_the_same_whole_second_both_land(self):
        """#22: session_meta.timestamp 는 초 단위다. 첫 mark 가 세션 하나를
        채워 그 epoch 가 newest_start 가 된 뒤, 같은 초에 시작한 **다른**
        세션이 두 번째 mark 에서 `<=` 비교에 걸려 사라지면 안 된다 — id 가
        다르면 이미 아는 세션이 아니다."""
        now = time.time()
        same_second = now - 500
        self.h.plant("cx-a", same_second)
        self.h.mark()
        self.assertEqual([r["session"] for r in self.h.scan_rows()], ["cx-a"])

        self.h.plant("cx-b", same_second + 0.4)  # 같은 초 → 같은 ISO 문자열
        self.h.mark()
        sessions = {r["session"] for r in self.h.scan_rows()}
        self.assertEqual(sessions, {"cx-a", "cx-b"})

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

    def test_backfill_accepts_a_subdirectory_session_under_an_omhc_root_parent(self):
        """#12: git 이 아닌 프로젝트도 `.omhc-root` 마커로 서브디렉터리 세션을
        같은 레포로 백필한다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            sub = os.path.join(root, "sub")
            os.makedirs(sub)
            open(os.path.join(root, ".omhc-root"), "w").close()
            now = time.time()
            _repo.plant_codex(self.h.home, cwd=sub, session_id="cx-sub",
                              when=now - 600,
                              meta_extra={"timestamp": _iso(now - 600)})

            stdin = json.dumps({"cwd": root, "session_id": "me1"})
            args = cli.build_parser().parse_args(
                ["mark", "--harness", "claude-code", "--stdin", stdin])
            out = io.StringIO()
            code = cli.cmd_mark(args, home=self.h.home, out=out)
            self.assertEqual(code, 0)

            key = locate.repo_key(os.path.realpath(root))
            rows = [r for r in ledger.read(repo_key=key, home=self.h.home)
                   if r.get("harness") == "codex-cli" and r.get("via") == "scan"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session"], "cx-sub")

    def test_backfill_still_rejects_a_nested_git_child_under_an_omhc_root_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            child = os.path.join(root, "child")
            os.makedirs(child)
            open(os.path.join(root, ".omhc-root"), "w").close()
            _repo.git(child, "init", "-q")
            now = time.time()
            _repo.plant_codex(self.h.home, cwd=child, session_id="cx-child",
                              when=now - 600,
                              meta_extra={"timestamp": _iso(now - 600)})

            stdin = json.dumps({"cwd": root, "session_id": "me1"})
            args = cli.build_parser().parse_args(
                ["mark", "--harness", "claude-code", "--stdin", stdin])
            out = io.StringIO()
            code = cli.cmd_mark(args, home=self.h.home, out=out)
            self.assertEqual(code, 0)

            key = locate.repo_key(os.path.realpath(root))
            rows = [r for r in ledger.read(repo_key=key, home=self.h.home)
                   if r.get("session") == "cx-child"]
            self.assertEqual(rows, [])

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
                {"hooks": [{"type": "command", "command": "omhc brief --harness codex-cli"}]}]}}, fh)
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


class TestResumeReopensDelivery(unittest.TestCase):
    """`source:"resume"` 가 SessionStart payload 로 오면(#22, Claude Code 와
    Codex 둘 다 실측), 이미 전달됐던 세션도 due() 가 다시 집어야 한다 —
    `codex exec resume` 은 같은 rollout 에 이어붙고 새 rollout(session_meta)을
    만들지 않는다."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def _deliver(self, now, human):
        from omhc import brief

        path = self.h.plant("cx1", now - 600, human=human)
        self.h.mark()
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        self.assertIsNone(
            due.due(self.h.key, "claude-code", "me1", now, home=self.h.home))
        return brief, path

    def _resume_with_new_turn(self, path, text):
        # `codex exec resume` 은 새 rollout 을 만들지 않고 같은 파일에
        # 이어붙인다(실측) — session_meta 는 다시 안 쓴다.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row(text, ordinal=50),
                                ensure_ascii=False) + "\n")

    def test_resume_after_delivery_makes_it_due_again(self):
        now = time.time()
        _, path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._resume_with_new_turn(path, "이제 두 번째 턴도 반영해줘")
        self.h.mark(harness="codex-cli", session_id="cx1", source="resume")
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")

    def test_next_brief_of_the_other_harness_contains_the_resumed_turn(self):
        now = time.time()
        brief, path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._resume_with_new_turn(path, "이제 두 번째 턴도 반영해줘")
        self.h.mark(harness="codex-cli", session_id="cx1", source="resume")
        body = brief.compute(my_harness="claude-code", my_session_id="me2",
                             repo_root=self.h.root, home=self.h.home, now=now)
        self.assertIn("이제 두 번째 턴도 반영해줘", body)

    def test_compact_after_delivery_does_not_reopen(self):
        now = time.time()
        self._deliver(now, "필드 경로부터 다시 확인해줘")
        self.h.mark(harness="codex-cli", session_id="cx1", source="compact")
        self.assertIsNone(
            due.due(self.h.key, "claude-code", "me2", now, home=self.h.home))

    def test_fork_after_delivery_does_not_reopen(self):
        """source:"fork" 는 새 session_id 로 오므로 cmd_mark 는 그저 평범한
        start 행을 남긴다(#34) — resume 처럼 이미 전달된 세션을 reopen 하지
        않는다. Claude 쪽 fork 적격성은 어댑터의 classify() 가 판정한다."""
        now = time.time()
        self._deliver(now, "필드 경로부터 다시 확인해줘")
        code, out = self.h.mark(harness="codex-cli", session_id="cx1", source="fork")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIsNone(
            due.due(self.h.key, "claude-code", "me2", now, home=self.h.home))

    def test_claude_receiving_side_with_source_fork_is_unaffected(self):
        """cmd_mark 의 source 분기는 harness 를 가리지 않는다 — Claude 자신의
        포크가 SessionStart 를 source:"fork" 로 낼 때도 그저 새 start 행
        하나일 뿐, resume 취급으로 새지 않는다."""
        now = time.time()
        code, out = self.h.mark(harness="claude-code", session_id="fork1", source="fork")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        starts = [r for r in rows if r.get("harness") == "claude-code"
                 and r.get("session") == "fork1" and r.get("event") == "start"]
        self.assertEqual(len(starts), 1)

    def test_resume_of_a_never_delivered_session_is_unchanged(self):
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark(harness="codex-cli", session_id="cx1", source="resume")
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")


class TestReactivateGrownSessions(unittest.TestCase):
    """#22 마지막 구멍: 신뢰 안 된 Codex 훅에서 `codex exec resume` 은 같은
    rollout 파일에 이어 쓰고 `session_meta` 를 다시 안 쓴다 — 훅이 안 도는
    경우 discover()/첫 줄 시작 시각으로는 이 재개를 절대 못 본다. 파일 크기
    성장 + offset 부터만 읽는 read_session_since 로 잡는다."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def _append_human_turn(self, path, text, ordinal=90):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row(text, ordinal=ordinal),
                                ensure_ascii=False) + "\n")

    def _deliver(self, now, human):
        path = self.h.plant("cx1", now - 600, human=human)
        self.h.mark()  # backfill 이 baseline size 를 원장에 남긴다
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        self.assertIsNone(
            due.due(self.h.key, "claude-code", "me1", now, home=self.h.home))
        return path

    def _grew_rows(self, session="cx1"):
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        return [r for r in rows if r.get("harness") == "codex-cli"
               and r.get("session") == session and r.get("grew")]

    def test_growth_with_a_human_turn_is_reactivated_without_any_hook(self):
        """훅이 한 번도 안 돌아도(source:"resume" 없이 그냥 mark 만 반복해도)
        새 사람 턴이 생기면 due() 가 다시 집는다."""
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._append_human_turn(path, "이제 두 번째 턴도 반영해줘")
        self.h.mark()  # source 없는 평범한 mark — 훅이 신뢰 안 되는 상황을 흉내
        self.assertEqual(len(self._grew_rows()), 1)
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")

    def test_next_brief_contains_the_reactivated_turn_even_after_delivery(self):
        from omhc import brief

        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._append_human_turn(path, "이제 두 번째 턴도 반영해줘")
        self.h.mark()
        body = brief.compute(my_harness="claude-code", my_session_id="me2",
                             repo_root=self.h.root, home=self.h.home, now=now)
        self.assertIn("이제 두 번째 턴도 반영해줘", body)

    def test_a_first_baseline_taken_mid_long_record_does_not_redeliver_old_content(self):
        """첫 관측 순간 64KB 넘는 레코드를 쓰는 중이어도 baseline 을 0 으로
        두지 않는다 — 0 이면 다음 판정이 처음부터 읽어 **원래의** 사람 턴으로
        거짓 재활성화하고 옛 내용을 다시 넘긴다(리뷰 t7 재현)."""
        now = time.time()
        path = self.h.plant("cx1", now - 600, human="필드 경로부터 다시 확인해줘")
        big = json.dumps({"timestamp": "2026-09-24T00:00:00Z", "type": "response_item",
                          "payload": {"type": "function_call_output", "call_id": "c9",
                                      "output": "x" * 80000}}) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(big[:70000])  # 64KB 넘게 쓰는 중 — 아직 개행 없음
        self.h.mark()
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(big[70000:])  # 레코드가 끝났다 — 새 사람 턴은 없다
        self.h.mark()
        self.assertEqual(self._grew_rows(), [])
        self.assertIsNone(
            due.due(self.h.key, "claude-code", "me2", now, home=self.h.home))

    def test_growth_without_a_human_turn_is_not_reactivated(self):
        """에이전트 혼잣말(셸 실행)만 붙은 성장은 재개로 보지 않는다 — 기준
        크기만 갱신한다(seen 행)."""
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        _repo.append_codex_turn(path, ordinal=90)  # 순수 에이전트 셸 실행
        self.h.mark()
        self.assertEqual(self._grew_rows(), [])
        self.assertIsNone(
            due.due(self.h.key, "claude-code", "me2", now, home=self.h.home))
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        seen = [r for r in rows if r.get("harness") == "codex-cli"
               and r.get("session") == "cx1" and r.get("event") == "seen"]
        self.assertEqual(len(seen), 1, rows)

    def test_a_torn_last_line_does_not_lose_the_completed_human_turn(self):
        """리뷰(2차) #2: os.stat 이 레코드를 쓰는 도중을 잡으면(마지막 줄이
        개행 없이 끝난다) baseline 에 그 부분 바이트까지 포함하면 안 된다 —
        나중에 그 레코드가 마저 쓰인 뒤 읽으면 skip-to-newline 로직이 완성된
        레코드 전체를 건너뛰어 사람 턴을 영영 잃는다."""
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        # 완전한 에이전트 턴 한 줄(개행으로 끝난다) — 이것만으로는 재개가 아니다.
        agent_row = {"timestamp": _iso(now), "ordinal": 90, "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "id": "a90",
                                "content": [{"type": "output_text",
                                            "text": "에이전트 혼잣말"}]}}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(agent_row, ensure_ascii=False) + "\n")

        human_row = _repo.codex_user_row("사람의 완성된 재개 턴", ordinal=91)
        full_line = json.dumps(human_row, ensure_ascii=False).encode("utf-8")
        torn = full_line[:30]  # 개행 없이 레코드 중간에서 끊는다 — 쓰는 도중
        with open(path, "ab") as fh:
            fh.write(torn)

        self.h.mark()  # baseline 이 이 미완성 줄을 포함하면 안 된다
        self.assertEqual(self._grew_rows(), [])

        with open(path, "ab") as fh:
            fh.write(full_line[30:] + b"\n")  # 나머지를 마저 쓴다

        self.h.mark()  # 이제 완성됐다 — 재개로 잡혀야 한다
        self.assertEqual(len(self._grew_rows()), 1)
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")

    def test_the_existing_dedupe_test_still_holds(self):
        """size 기록이 더해져도 #22 의 기존 dedupe 보장은 그대로다."""
        now = time.time()
        self.h.plant("cx1", now - 600)
        self.h.mark()
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)
        self.h.mark()
        self.assertIsNone(due.due(self.h.key, "claude-code", "me1", now, home=self.h.home))
        self.assertEqual(self._grew_rows(), [])

    def test_a_newer_session_in_the_same_interval_wins_over_a_grown_older_one(self):
        """조건 (b): 같은 mark 호출이 방금 더 최신 세션(B)을 채웠다면, 그 안에서
        낡은 재개(A)를 얹어 due() 가 B 대신 A 를 고르게 하면 안 된다."""
        now = time.time()
        path_a = self._deliver(now, "A 세션 첫 턴")
        self._append_human_turn(path_a, "A 세션 재개 턴")
        self.h.plant("cx-b", now - 100, human="B 세션 첫 턴")

        self.h.mark()  # 한 호출 안에서 backfill(B) + reactivate(A) 후보가 겹친다
        self.assertEqual(self._grew_rows(), [], "B 가 채워진 호출에서는 A 를 재개하지 않는다")
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx-b")

        # 다음 호출에서도 A 는 B 에 밀린 것으로 남는다(조건 a: baseline 뒤에
        # 다른 세션의 start 행이 있다).
        self.h.mark()
        self.assertEqual(self._grew_rows(), [])
        got = due.due(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-b")

    def test_a_grows_again_after_b_is_delivered_and_is_reactivated(self):
        """리뷰 #1: `superseded` 로 건너뛴 세션이 영원히 막히면 안 된다 — B
        이후 A 가 **다시** 자라면(새 사람 턴) 다시 잡혀야 한다. B 가 막 들어온
        순간(`_rebaseline_after_fresh_start`) A 의 baseline 이 이미 B 뒤로
        옮겨지므로, 그 뒤의 성장은 곧바로 새 판정을 받는다."""
        now = time.time()
        path_a = self._deliver(now, "A 세션 첫 턴")
        self._append_human_turn(path_a, "B 이전의 재개 턴")
        self.h.plant("cx-b", now - 100, human="B 세션 첫 턴")

        self.h.mark()  # backfill(B) 이 이 순간 A 의 baseline 도 B 뒤로 옮긴다
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-b")
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)

        self.h.mark()  # 재기준점 이후로는 더 자라지 않았다 — 재개 아님
        self.assertEqual(self._grew_rows(), [])

        self._append_human_turn(path_a, "B 이후의 진짜 재개 턴")
        self.h.mark()  # baseline 이 B 뒤에 있으니 이 성장은 곧바로 새 판정이다
        self.assertEqual(len(self._grew_rows()), 1)
        got2 = due.due(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertIsNotNone(got2)
        self.assertEqual(got2.session_id, "cx1")

    def test_a_resume_that_starts_only_after_b_is_delivered_is_reactivated(self):
        """리뷰(2차) #1 정확한 재현: A 는 B 이전에 전혀 안 자란다 — 딱 한 번,
        B 가 이미 전달된 **뒤**에만 재개된다. 라운드1 픽스(성장 시점에만
        superseded 를 흡수)는 이 경우 baseline 이 계속 B 앞에 남아 있어 이
        유일한 재개까지 통째로 흡수해 버렸다 — mark 를 몇 번을 더 불러도
        due() 가 영원히 None 이었다."""
        now = time.time()
        path_a = self._deliver(now, "A 세션 첫 턴")
        self.h.plant("cx-b", now - 100, human="B 세션 첫 턴")

        self.h.mark()  # backfill(B) — 이 순간 A 는 아직 안 자랐다
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertEqual(got.session_id, "cx-b")
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)

        self._append_human_turn(path_a, "B 전달 후 A 의 유일한 재개 턴")
        for _ in range(3):
            self.h.mark()
        self.assertEqual(len(self._grew_rows()), 1)
        got2 = due.due(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertIsNotNone(got2)
        self.assertEqual(got2.session_id, "cx1")

    def test_t5_b_started_via_its_own_trusted_hook_still_rebaselines_a(self):
        """리뷰(3차) #1, 재현 t5: B 가 백필이 아니라 **자기 자신의 신뢰된
        Codex 훅**으로 직접 start 행을 남기면 `_backfill_foreign_sessions`
        는 그 세션을 "이미 안다"고 보고 다시 안 채우므로(`fresh` 가 비어
        있다), 백필 시점 재기준점(`_rebaseline_after_fresh_start`)이 전혀
        안 돈다. 그래도 다음 Claude mark 의 지연(lazy) 재기준점(원장
        `_reactivate_grown_sessions`)이 A 를 구해야 한다 — 성장 여부와
        무관하게 낡은 baseline 을 B 뒤로 옮긴다."""
        now = time.time()
        path_a = self._deliver(now, "A 세션 첫 턴")

        # B 자신의 신뢰된 훅이 직접 start 행을 남긴다 — 백필이 아니다.
        self.h.mark(harness="codex-cli", session_id="cx-b")

        self.h.mark()  # Claude mark — B 를 알게 되고, A 의 baseline 을 지연 이동한다
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx-b")
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)

        # 훅 없이(SessionStart 발동 없이) A 를 계속 타이핑한다.
        self._append_human_turn(path_a, "훅 없이 A 를 계속 타이핑")
        for _ in range(3):
            self.h.mark()
        self.assertEqual(len(self._grew_rows()), 1)
        got2 = due.due(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertIsNotNone(got2)
        self.assertEqual(got2.session_id, "cx1")

    def test_an_adapter_that_cannot_tell_never_gets_a_growth_row(self):
        """리뷰 #2: `read_session_since` 가 (계약대로) 항상 None 을 돌려주는
        어댑터(Claude, 아직 미구현)는 성장이 있어도 seen/grew 행을 하나도
        안 남긴다 — 계약은 "이 어댑터는 구분할 수 없다"이지, "매번 재확인해
        모르는 채로 seen 행만 쌓는다"가 아니다. 실측(고침 전): mark 4번 →
        seen 4번."""
        now = time.time()
        fake_claude_path = os.path.join(self.h.home, "fake-claude-session.jsonl")
        with open(fake_claude_path, "w", encoding="utf-8") as fh:
            fh.write("x" * 100 + "\n")
        ledger.append({
            "repo": self.h.key, "harness": "claude-code", "session": "cl1",
            "event": "start", "epoch": now - 600, "path": fake_claude_path,
            "cwd": self.h.root, "via": "scan", "size": os.path.getsize(fake_claude_path),
        }, home=self.h.home)

        for _ in range(4):
            with open(fake_claude_path, "a", encoding="utf-8") as fh:
                fh.write("y" * 100 + "\n")
            code, _ = self.h.mark(harness="codex-cli", session_id="cx1")
            self.assertEqual(code, 0)

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        claude_side = [r for r in rows if r.get("harness") == "claude-code"
                      and r.get("session") == "cl1"]
        self.assertEqual(len(claude_side), 1, claude_side)  # 처음 심은 한 줄뿐

    def test_a_baseline_older_than_the_discover_window_is_still_reactivated(self):
        """discover() 는 14일 창만 스캔하지만(SCAN_DAYS), 재개 감지는 원장에
        이미 적힌 path 로만 stat 하므로 그보다 오래된 baseline 도 잡는다."""
        for days in (10, 20):
            with self.subTest(days=days):
                h = Harness()
                self.addCleanup(h.close)
                now = time.time()
                when = now - days * 86400
                sid = "cx-old{}".format(days)
                path = h.plant(sid, when)
                ledger.append({
                    "repo": h.key, "harness": "codex-cli", "session": sid,
                    "event": "start", "epoch": when, "path": path,
                    "cwd": h.root, "via": "scan", "size": os.path.getsize(path),
                }, home=h.home)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(
                        _repo.codex_user_row("오래된 세션의 재개 턴", ordinal=90),
                        ensure_ascii=False) + "\n")

                code, _ = h.mark()
                self.assertEqual(code, 0)
                got = due.due(h.key, "claude-code", "me-old", now, home=h.home)
                self.assertIsNotNone(got)
                self.assertEqual(got.session_id, sid)

    def test_six_repeated_marks_reactivate_exactly_once(self):
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._append_human_turn(path, "이제 두 번째 턴도 반영해줘")
        for _ in range(6):
            self.h.mark()
        self.assertEqual(len(self._grew_rows()), 1)

    def test_deadline_exceeded_writes_no_rows(self):
        now = time.time()
        path = self._deliver(now, "필드 경로부터 다시 확인해줘")
        self._append_human_turn(path, "이제 두 번째 턴도 반영해줘")
        with mock.patch.object(cli, "BACKFILL_TIME_BUDGET", -1000.0):
            self.h.mark()
        self.assertEqual(self._grew_rows(), [])
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        seen = [r for r in rows if r.get("harness") == "codex-cli"
               and r.get("event") == "seen"]
        self.assertEqual(seen, [])
        self.assertIsNone(
            due.due(self.h.key, "claude-code", "me2", now, home=self.h.home))

    def test_a_garbage_size_field_does_not_break_mark(self):
        now = time.time()
        path = self.h.plant("cx1", now - 600)
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "cx1",
                      "event": "start", "epoch": now - 600, "path": path,
                      "cwd": self.h.root, "via": "scan", "size": "not-a-number"},
                     home=self.h.home)
        code, out = self.h.mark()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


@contextlib.contextmanager
def _deadline_trips_after_first_stat(target_name):
    """#28 리뷰 재현: `cli.<target_name>` 안에서 `os.stat` 이 처음 불린
    **뒤부터** `time.time()` 이 deadline 을 훌쩍 넘긴 값을 돌려주게 만든다 —
    그 함수를 호출하는 동안만(armed 는 그 함수 진입 전엔 절대 안 켜진다).
    첫 세션의 stat 은 정상적으로 끝나고, 그다음 세션 처리 직전의 deadline
    검사에서 걸려 `break` 하게 된다."""
    orig_fn = getattr(cli, target_name)
    orig_stat = os.stat
    orig_time = time.time
    state = {"armed": False}

    def fake_stat(path, *a, **kw):
        result = orig_stat(path, *a, **kw)
        state["armed"] = True
        return result

    def fake_time():
        return (orig_time() + 10 ** 6) if state["armed"] else orig_time()

    def wrapped(*args, **kwargs):
        with mock.patch("os.stat", side_effect=fake_stat), \
             mock.patch.object(time, "time", side_effect=fake_time):
            return orig_fn(*args, **kwargs)

    with mock.patch.object(cli, target_name, side_effect=wrapped):
        yield


class TestRebaseMarker(unittest.TestCase):
    """#28: backfill/지연 재기준 둘 다에서, 안 자란 다른 세션들은 개별 seen
    행 대신 (repo, harness) 당 하나의 `rebase` 마커로 흡수한다."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        # 마커 개수는 한 번의 판정을 끝까지 마쳤을 때만 센다. 훅 예산(80ms)을
        # 실제 시계로 재면 부하가 큰 머신(load 7)에서 세션 60개 판정이 예산에
        # 걸려 마커를 안 쓰고, 테스트가 3번 중 2번 실패했다. 예산 초과는
        # _deadline_trips_after_first_stat 이 시계를 건너뛰어 따로 확인한다.
        patcher = mock.patch.object(cli, "BACKFILL_TIME_BUDGET", 60.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_known_unchanged(self, n, base):
        """이미 알려진, 이번 판정에서 안 자랄 세션 n 개를 원장에 직접 심는다
        (실제 backfill 을 거치지 않고 "알려진 세션" 상태만 재현한다)."""
        sids = []
        for i in range(n):
            sid = "old{}".format(i)
            path = self.h.plant(sid, base - 10000 + i)
            ledger.append({
                "repo": self.h.key, "harness": "codex-cli", "session": sid,
                "event": "start", "epoch": base - 10000 + i, "path": path,
                "cwd": self.h.root, "via": "scan", "size": os.path.getsize(path),
            }, home=self.h.home)
            sids.append(sid)
        return sids

    def test_twenty_unchanged_sessions_yield_markers_not_per_session_seen_rows(self):
        """20 개(딱 REACTIVATE_SCAN_CAP) + 4 개가 새로 들어와도 여전히 라운드당
        마커 하나, 개별 seen 행은 없다(#28 2차 리뷰: complete 는 cap 과
        무관하다 — 아래 test_beyond_the_cap_* 이 cap 을 넘는 규모를 다룬다)."""
        now = time.time()
        self._seed_known_unchanged(20, now)

        for i in range(4):
            self.h.plant("new{}".format(i), now - 1000 + i * 10)
            self.h.mark()

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        markers = [r for r in codex_rows if r.get("event") == cli.REBASE_EVENT]
        old_seen = [r for r in codex_rows if r.get("event") == "seen"
                   and str(r.get("session", "")).startswith("old")]
        self.assertLessEqual(len(markers), 4, codex_rows)
        self.assertEqual(old_seen, [])
        # 마커는 session/path 가 없다 — known_sessions·newest_start·log 랭크가
        # 세션으로 착각하면 안 된다(#28).
        for m in markers:
            self.assertNotIn("session", m)
            self.assertNotIn("path", m)

    def test_a_session_that_grew_before_b_still_gets_its_own_seen_row(self):
        """애매한 사전 성장(B 전, agent 전용)은 여전히 자기 seen 행을 받는다
        — 마커가 그 흡수를 대신 삼켜 위치를 잘못 앞당기면 안 된다."""
        now = time.time()
        sids = self._seed_known_unchanged(3, now)
        grown_sid = sids[0]
        grown_path = None
        for r in ledger.read(repo_key=self.h.key, home=self.h.home):
            if r.get("session") == grown_sid:
                grown_path = r.get("path")
        _repo.append_codex_turn(grown_path, ordinal=90)  # 순수 에이전트 셸 실행

        self.h.plant("new-b", now - 500)
        self.h.mark()  # backfill(B) 이 이 순간 old0 은 이미 자란 상태다

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        grown_seen = [r for r in codex_rows if r.get("session") == grown_sid
                     and r.get("event") == "seen"]
        self.assertEqual(len(grown_seen), 1, codex_rows)
        markers = [r for r in codex_rows if r.get("event") == cli.REBASE_EVENT]
        self.assertEqual(len(markers), 1, codex_rows)

    def test_lazy_path_writes_one_marker_under_a_trusted_hook_b(self):
        """B 가 자기 자신의 신뢰된 훅으로 직접 들어오면(백필이 아니다) 지연
        재기준(`_reactivate_grown_sessions`)이 돈다 — 안 자란 여러 A 를 한
        번의 마커로 흡수하고, A 의 나중 human 턴은 여전히 재활성화된다."""
        now = time.time()
        sids = self._seed_known_unchanged(5, now)

        self.h.mark(harness="codex-cli", session_id="new-b")  # B 자신의 신뢰된 훅

        self.h.mark()  # Claude mark — 지연 재기준이 5개의 A 를 한 마커로 흡수
        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        markers = [r for r in codex_rows if r.get("event") == cli.REBASE_EVENT]
        old_seen = [r for r in codex_rows if r.get("event") == "seen"
                   and str(r.get("session", "")).startswith("old")]
        self.assertEqual(len(markers), 1, codex_rows)
        self.assertEqual(old_seen, [])

        # B(new-b) 를 전달한 뒤, A(=sids[0]) 의 사람 턴이 계속되면 여전히
        # 재활성화된다.
        got_b = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got_b)
        self.assertEqual(got_b.session_id, "new-b")
        due.mark_delivered(self.h.state, got_b, to_harness="claude-code", epoch=now)

        reactivated_sid = sids[0]
        reactivated_path = None
        for r in rows:
            if r.get("session") == reactivated_sid:
                reactivated_path = r.get("path")
        with open(reactivated_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("B 이후 A 의 재개 턴", ordinal=90),
                                ensure_ascii=False) + "\n")
        self.h.mark()
        got = due.due(self.h.key, "claude-code", "me3", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, reactivated_sid)

    def test_beyond_the_cap_backfill_path_still_yields_one_marker_per_round(self):
        """[리뷰 2차] `complete` 는 cap(`REACTIVATE_SCAN_CAP`) 초과와 무관하다
        — 알려진 세션이 cap 을 훨씬 넘어도(top-N 만 후보이고 나머지는 애초에
        손대지 않으므로) 라운드당 마커 하나, 확인된 세션엔 개별 seen 행이
        없다. cap 을 `complete` 에 얹었던 1차 실수를 되돌리면 n=25/60 에서
        HEAD 와 같은 행 폭증(측정: 84행/80 seen)이 재현된다."""
        for n in (cli.REACTIVATE_SCAN_CAP + 5, cli.REACTIVATE_SCAN_CAP + 40):
            with self.subTest(n=n):
                h = Harness()
                self.addCleanup(h.close)
                now = time.time()
                for i in range(n):
                    sid = "old{}".format(i)
                    path = h.plant(sid, now - 100000 + i)
                    ledger.append({
                        "repo": h.key, "harness": "codex-cli", "session": sid,
                        "event": "start", "epoch": now - 100000 + i, "path": path,
                        "cwd": h.root, "via": "scan", "size": os.path.getsize(path),
                    }, home=h.home)

                for r in range(4):
                    h.plant("new-b{}".format(r), now - 1000 + r * 10)
                    h.mark()

                rows = ledger.read(repo_key=h.key, home=h.home)
                codex_rows = [row for row in rows if row.get("harness") == "codex-cli"]
                markers = [row for row in codex_rows if row.get("event") == cli.REBASE_EVENT]
                old_seen = [row for row in codex_rows if row.get("event") == "seen"
                           and str(row.get("session", "")).startswith("old")]
                self.assertEqual(len(markers), 4, codex_rows)
                self.assertEqual(old_seen, [], codex_rows)

    def test_beyond_the_cap_lazy_path_still_yields_one_marker_per_round(self):
        """위와 같지만 B 가 매번 backfill 이 아니라 자기 자신의 신뢰된
        훅으로 직접 들어오는 경우(지연 재기준 경로)."""
        for n in (cli.REACTIVATE_SCAN_CAP + 5, cli.REACTIVATE_SCAN_CAP + 40):
            with self.subTest(n=n):
                h = Harness()
                self.addCleanup(h.close)
                now = time.time()
                for i in range(n):
                    sid = "old{}".format(i)
                    path = h.plant(sid, now - 100000 + i)
                    ledger.append({
                        "repo": h.key, "harness": "codex-cli", "session": sid,
                        "event": "start", "epoch": now - 100000 + i, "path": path,
                        "cwd": h.root, "via": "scan", "size": os.path.getsize(path),
                    }, home=h.home)

                for r in range(4):
                    h.mark(harness="codex-cli", session_id="new-b{}".format(r))
                    h.mark()  # Claude mark — 지연 재기준

                rows = ledger.read(repo_key=h.key, home=h.home)
                codex_rows = [row for row in rows if row.get("harness") == "codex-cli"]
                markers = [row for row in codex_rows if row.get("event") == cli.REBASE_EVENT]
                old_seen = [row for row in codex_rows if row.get("event") == "seen"
                           and str(row.get("session", "")).startswith("old")]
                self.assertEqual(len(markers), 4, codex_rows)
                self.assertEqual(old_seen, [], codex_rows)

    def test_x_far_reentering_via_its_own_hook_is_not_falsely_reactivated(self):
        """[리뷰 2차, "x-far" 재현] cap 밖에 있던 세션이 B 전에 이미 자란
        채(사람 턴 포함) 마커에 확인된 적 없는데, 나중에 자기 신뢰된
        훅으로(path 는 있지만 size 는 없는 새 start 행) 재진입해 이번
        라운드의 top-N 안으로 들어와도, 마커가 그 애매한 사전 성장을 명확한
        재개로 둔갑시키면 안 된다 — `marker_covers` 는 마커를 **쓸 당시**의
        top-N 만 기억해야 한다(마커 이전 구간으로 슬라이스해 재구성)."""
        now = time.time()
        n = cli.REACTIVATE_SCAN_CAP + 5
        xfar_sid = "old0"  # 가장 먼저 심어 가장 오래된 것 — top-N(cap) 밖으로 밀린다
        xfar_path = None
        for i in range(n):
            sid = "old{}".format(i)
            path = self.h.plant(sid, now - 100000 + i)
            if sid == xfar_sid:
                xfar_path = path
            ledger.append({
                "repo": self.h.key, "harness": "codex-cli", "session": sid,
                "event": "start", "epoch": now - 100000 + i, "path": path,
                "cwd": self.h.root, "via": "scan", "size": os.path.getsize(path),
            }, home=self.h.home)
        # x-far 는 B 가 들어오기 전에 이미 사람 턴으로 자란다 — cap 밖이라
        # 이번 라운드엔 확인되지 않는다.
        with open(xfar_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("B 전, cap 밖에서의 사전 성장",
                                                       ordinal=90),
                                ensure_ascii=False) + "\n")

        self.h.plant("new-b", now - 500, human="B 세션 첫 턴")
        self.h.mark()  # backfill(B) — top-N(cap) 안만 확인되고 마커가 찍힌다

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        self.assertEqual(
            len([r for r in codex_rows if r.get("event") == cli.REBASE_EVENT]), 1,
            codex_rows)
        # x-far 는 이번 라운드에 확인되지 않았다 — 아무 행도 안 남는다.
        self.assertEqual(
            [r for r in codex_rows if r.get("session") == xfar_sid
             and r.get("via") == "scan" and r is not codex_rows[0]], [])

        # x-far 가 자기 신뢰된 훅으로 재진입한다 — path 는 진짜지만(같은
        # rollout) size 는 없다(#28 2차 리뷰가 재현하는 정확한 모양).
        stdin = json.dumps({"cwd": self.h.root, "session_id": xfar_sid,
                            "transcript_path": xfar_path})
        args = cli.build_parser().parse_args(
            ["mark", "--harness", "codex-cli", "--stdin", stdin])
        out = io.StringIO()
        code = cli.cmd_mark(args, home=self.h.home, out=out)
        self.assertEqual(code, 0)

        self.h.mark()  # Claude mark — 지연 재기준이 이제 top-N 에 들어온 x-far 를 본다

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        xfar_grew = [r for r in codex_rows if r.get("session") == xfar_sid and r.get("grew")]
        self.assertEqual(xfar_grew, [], codex_rows)

        # 진짜 더 최신 세션(C)이 들어오면 due() 는 여전히 그걸 돌려준다 —
        # x-far 가 아니다. x-far 재진입 행의 epoch 는 그 mark 호출 시점의
        # **실제** 시각(cmd_mark 가 round(time.time(),0) 을 쓴다)이라, C 도
        # 그 뒤의 실제 시각으로 심어야 newest_start 필터에 안 걸린다.
        after_reentry = time.time()
        self.h.plant("new-c", after_reentry + 10, human="C 세션 첫 턴")
        self.h.mark()
        got = due.due(self.h.key, "claude-code", "me-final", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "new-c")

    def _seed_two_known(self, now, grown_sid_human_turn):
        """a-stale(B 전에 이미 사람 턴으로 자람) 와 a-unch(안 자람) 를
        원장에 심는다 — 아래 세 리뷰 재현이 공유하는 준비 단계."""
        path_stale = self.h.plant("a-stale", now - 5000, human="원래 턴")
        ledger.append({
            "repo": self.h.key, "harness": "codex-cli", "session": "a-stale",
            "event": "start", "epoch": now - 5000, "path": path_stale,
            "cwd": self.h.root, "via": "scan", "size": os.path.getsize(path_stale),
        }, home=self.h.home)
        path_unch = self.h.plant("a-unch", now - 4000)
        ledger.append({
            "repo": self.h.key, "harness": "codex-cli", "session": "a-unch",
            "event": "start", "epoch": now - 4000, "path": path_unch,
            "cwd": self.h.root, "via": "scan", "size": os.path.getsize(path_unch),
        }, home=self.h.home)
        if grown_sid_human_turn:
            with open(path_stale, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(_repo.codex_user_row("B 전의 사람 턴", ordinal=90),
                                    ensure_ascii=False) + "\n")
        return path_stale, path_unch

    def test_deadline_trip_mid_backfill_rebaseline_does_not_mark_unverified_session(self):
        """[high, 리뷰 재현] 마커는 하네스 전체("이 하네스의 알려진 세션
        전부가 안 자란 채 확인됐다")에 적용된다 — deadline 이 a-unch 만
        확인한 채 a-stale(B 전에 이미 자람) 을 못 보고 끊으면, a-unch 만
        가지고 마커를 찍으면 안 된다. 안 그러면 다음 라운드에 a-stale 이
        마커 뒤로 잘못 밀려 애매한 사전 성장이 명확한 재개로 오판되고, due()
        가 B 대신 a-stale 을 돌려준다."""
        now = time.time()
        self._seed_two_known(now, grown_sid_human_turn=True)
        self.h.plant("new-b", now - 100, human="B 세션 첫 턴")

        with _deadline_trips_after_first_stat("_rebaseline_after_fresh_start"):
            self.h.mark()  # backfill(B) — a-unch 만 확인되고 a-stale 은 못 본다

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        self.assertEqual([r for r in codex_rows if r.get("event") == cli.REBASE_EVENT],
                         [], "전수 확인이 아니었으니 마커를 찍으면 안 된다")

        self.h.mark()  # 정상 라운드 — a-stale 의 사전 성장은 흡수돼야지 재개가 아니다
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "new-b")

    def test_deadline_trip_mid_lazy_reactivate_does_not_mark_unverified_session(self):
        """[high, 리뷰 재현] 위와 같은 재현을 지연(lazy) 경로에서 — B 가
        백필이 아니라 자기 자신의 신뢰된 훅으로 들어온 경우."""
        now = time.time()
        self._seed_two_known(now, grown_sid_human_turn=True)
        self.h.mark(harness="codex-cli", session_id="new-b")  # B 자신의 신뢰된 훅

        with _deadline_trips_after_first_stat("_reactivate_grown_sessions"):
            self.h.mark()  # Claude mark — 지연 재기준, a-unch 만 확인되고 끊긴다

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        self.assertEqual([r for r in codex_rows if r.get("event") == cli.REBASE_EVENT],
                         [], "전수 확인이 아니었으니 마커를 찍으면 안 된다")

        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "new-b")

    def test_transient_stat_error_on_one_session_does_not_mark_the_others(self):
        """[medium, 리뷰 재현] a-stale 의 stat 이 일시적으로 실패해도(예:
        PermissionError) a-unch 가 안 자란 것으로 확인됐다는 이유만으로
        마커를 찍으면 안 된다 — 확인 못 한 a-stale 에도 그 마커가 똑같이
        적용되기 때문이다."""
        now = time.time()
        path_stale, _ = self._seed_two_known(now, grown_sid_human_turn=True)
        self.h.plant("new-b", now - 100, human="B 세션 첫 턴")

        orig_stat = os.stat

        def flaky_stat(path, *a, **kw):
            if path == path_stale:
                raise PermissionError("transient")
            return orig_stat(path, *a, **kw)

        with mock.patch("os.stat", side_effect=flaky_stat):
            self.h.mark()

        rows = ledger.read(repo_key=self.h.key, home=self.h.home)
        codex_rows = [r for r in rows if r.get("harness") == "codex-cli"]
        self.assertEqual([r for r in codex_rows if r.get("event") == cli.REBASE_EVENT],
                         [], "a-stale 을 확인 못 했으니 마커를 찍으면 안 된다")

        self.h.mark()  # stat 이 다시 정상 — a-stale 의 사전 성장은 흡수된다
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "new-b")


class TestLiveContinueWithoutASessionStart(unittest.TestCase):
    """#22 새로 발견된 두 번째 구멍: A 를 핸드오프한 뒤 사람이 **살아 있는**
    Codex 세션 A 에 계속 타이핑하다가 새 Claude 세션으로 옮기면, 그 Claude
    세션의 SessionStart 는 있지만(mark 는 돈다) A 쪽 SessionStart 는 전혀
    없다 — 그래도 성장 감지 + read_session_since 가 잡아야 한다(그 mark 가
    스캔 대상으로 codex-cli 세션을 다시 stat 하므로 Codex 훅과 무관하다)."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_live_continue_is_delivered_to_a_new_claude_session(self):
        now = time.time()
        path = self.h.plant("cx1", now - 600, human="첫 턴")
        self.h.mark(session_id="me1")
        got = due.due(self.h.key, "claude-code", "me1", now, home=self.h.home)
        self.assertIsNotNone(got)
        due.mark_delivered(self.h.state, got, to_harness="claude-code", epoch=now)

        # 사람이 살아 있는 Codex 세션 A 에 계속 타이핑한다 — Codex 쪽
        # SessionStart 는 전혀 없다.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_repo.codex_user_row("A 에서 계속 타이핑",
                                                      ordinal=90),
                                ensure_ascii=False) + "\n")

        # 새 Claude 세션의 SessionStart 만 발동한다.
        code, _ = self.h.mark(session_id="me2")
        self.assertEqual(code, 0)
        got = due.due(self.h.key, "claude-code", "me2", now, home=self.h.home)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "cx1")


class TestCompactSessionStart(unittest.TestCase):
    """#30: 자동 압축 뒤 같은 세션에서 SessionStart(source=compact) 가 다시
    발화한다. 이미 원장에 있는 세션이면 start 행을 또 남기지 않는다."""

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def _starts(self, harness, session):
        return [r for r in ledger.read(repo_key=self.h.key, home=self.h.home)
                if r.get("event") == "start" and r.get("harness") == harness
                and r.get("session") == session]

    def test_compact_after_startup_adds_no_start_row(self):
        self.h.mark(harness="codex-cli", session_id="cx1", source="startup")
        self.h.mark(harness="codex-cli", session_id="cx1", source="compact")
        self.h.mark(harness="codex-cli", session_id="cx1", source="compact")
        self.assertEqual(len(self._starts("codex-cli", "cx1")), 1)

    def test_compact_for_an_unknown_session_is_still_recorded(self):
        """startup 을 놓친 세션(도중에 설치)이면 compact 가 첫 기록이다."""
        self.h.mark(harness="codex-cli", session_id="cx9", source="compact")
        self.assertEqual(len(self._starts("codex-cli", "cx9")), 1)

    def test_compact_refreshes_age_for_a_long_lived_session(self):
        """due() 는 가장 최근 start 행으로 나이를 잰다. 며칠째 쓰는 세션이
        compact 만 반복해도 MAX_AGE 를 넘겨 빠지면 안 된다(#30 리뷰)."""
        old = time.time() - 8 * 86400
        ledger.append({"repo": self.h.key, "harness": "claude-code",
                       "session": "cc1", "event": "start", "epoch": old,
                       "path": "", "cwd": self.h.root}, home=self.h.home)
        self.h.mark(harness="claude-code", session_id="cc1", source="compact")
        self.assertEqual(len(self._starts("claude-code", "cc1")), 2)

    def test_a_scan_row_alone_does_not_suppress_the_hook_row(self):
        """backfill 이 대신 적은 행만 있으면 훅이 돈 증거를 남긴다."""
        ledger.append({"repo": self.h.key, "harness": "codex-cli",
                       "session": "cx2", "event": "start", "epoch": time.time(),
                       "path": "", "cwd": self.h.root, "via": "scan"},
                      home=self.h.home)
        self.h.mark(harness="codex-cli", session_id="cx2", source="compact")
        rows = self._starts("codex-cli", "cx2")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[-1].get("via"), "scan")

    def test_resume_still_adds_a_start_row(self):
        self.h.mark(harness="codex-cli", session_id="cx1", source="startup")
        self.h.mark(harness="codex-cli", session_id="cx1", source="resume")
        self.assertEqual(len(self._starts("codex-cli", "cx1")), 2)


if __name__ == "__main__":
    unittest.main()
