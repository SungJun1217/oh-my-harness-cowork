"""#10 `omhc show '#N'` 이 다른 세션의 이벤트를 여는 결함, #15a/#15c 의 log/show
사용성 항목. 세 항목 모두 여기서 다룬다(구버전 누적 항목은 9495f87 로 이미 고쳐짐)."""
from __future__ import annotations

import io
import os
import unittest

from omhc import cli, due, index
from omhc.event import Event

from ._repo import TempRepo


def _write_idx(state: str, session_id: str, rows) -> None:
    """rows 는 (seq, verb, arg) 튜플. author/ok/offset/length 는 테스트에
    중요하지 않은 값으로 고정한다."""
    idx_dir = os.path.join(state, "index")
    os.makedirs(idx_dir, exist_ok=True)
    events = [
        Event(seq=seq, epoch=1700000000.0 + seq, author="human", verb=verb,
              ok=True, text=arg, arg=arg, paths=(), offset=seq * 10, length=5)
        for seq, verb, arg in rows
    ]
    index.append_rows(os.path.join(idx_dir, session_id + ".idx"), events)


def _mark_delivered(state: str, session_id: str) -> None:
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, due.DELIVERED_NAME), "a", encoding="utf-8") as fh:
        fh.write("\t".join((session_id, "claude-code", "codex-cli", "1700000000")) + "\n")


class TestShowSeqRef(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        cwd = os.getcwd()
        os.chdir(self.t.root)
        self.addCleanup(os.chdir, cwd)

    def _show(self, target, **kw):
        out = io.StringIO()
        err = io.StringIO()
        code = cli.cmd_show(
            cli.build_parser().parse_args(["show", target] + (["--full"] if kw.get("full") else [])),
            home=self.t.home, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_bare_seq_ref_resolves_to_the_log_default_session(self):
        """두 세션이 모두 #3 을 갖는다 — 가장 최근 전달된 세션 것이 열려야 한다."""
        _write_idx(self.t.state, "aaaaaaaa1111", [(3, "said", "old-session-text")])
        _write_idx(self.t.state, "bbbbbbbb2222", [(3, "said", "new-session-text")])
        _mark_delivered(self.t.state, "aaaaaaaa1111")
        _mark_delivered(self.t.state, "bbbbbbbb2222")

        # 원본 바이트는 offset/length 로 찾는다 — pinned 원본이 없으면 실패하므로
        # source.jsonl 을 상태 디렉터리 아래 심고 fallback 경로로 쓰게 한다.
        pin_dir = os.path.join(self.t.state, "pinned", "bbbbbbbb2222")
        os.makedirs(pin_dir, exist_ok=True)
        with open(os.path.join(pin_dir, "source.jsonl"), "wb") as fh:
            fh.write(b"0" * 30 + b"new!!" + b"0" * 30)

        code, out, err = self._show("#3")
        self.assertEqual(code, 0)
        # stdout 은 원본 바이트 그대로다 — `show '#3' | jq .` 같은 파이프가
        # 깨지면 안 된다(리뷰 결함). 어떤 세션을 골랐는지는 stderr 로만 간다.
        self.assertEqual(out, "new!!\n")
        self.assertIn("bbbbbbbb", err)
        self.assertNotIn("aaaaaaaa", err)

        from omhc import ledger
        pulls = [r for r in ledger.read(repo_key=self.t.key, home=self.t.home)
                 if r.get("event") == "pull"]
        self.assertEqual(pulls[-1]["session"], "bbbbbbbb2222")

    def test_explicit_prefix_selects_that_session(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(3, "said", "old")])
        _write_idx(self.t.state, "bbbbbbbb2222", [(3, "said", "new")])
        _mark_delivered(self.t.state, "bbbbbbbb2222")

        pin_dir = os.path.join(self.t.state, "pinned", "aaaaaaaa1111")
        os.makedirs(pin_dir, exist_ok=True)
        with open(os.path.join(pin_dir, "source.jsonl"), "wb") as fh:
            fh.write(b"0" * 30 + b"old!!" + b"0" * 30)

        code, out, err = self._show("aaaaaaaa#3")
        self.assertEqual(code, 0)
        # 명시적 프리픽스라 헤더에 선택 이유를 달 필요는 없지만, 잘못된(최근 전달)
        # 세션이 아니라 지정한 세션에서 읽혔는지는 pull 회계로 확인한다.
        from omhc import ledger
        pulls = [r for r in ledger.read(repo_key=self.t.key, home=self.t.home)
                 if r.get("event") == "pull"]
        self.assertEqual(pulls[-1]["session"], "aaaaaaaa1111")

    def test_ambiguous_prefix_errors_with_candidates(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "said", "x")])
        _write_idx(self.t.state, "aaaaaaaa2222", [(1, "said", "y")])
        code, out, err = self._show("aaaaaaaa#1")
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", out)
        # 후보는 축약하지 않고 전체 id 로 보여준다 — 접두사로 줄이면 그 자체가
        # 다시 모호해질 수 있다(리뷰 결함).
        self.assertIn("aaaaaaaa1111", out)
        self.assertIn("aaaaaaaa2222", out)

    def test_ambiguous_prefix_lists_full_ids_even_when_they_share_13_chars(self):
        """13자는 표시상 선호일 뿐이다 — 그 안에서 안 갈리는 두 id 를 후보로 줄여
        보여주면 후보 목록 자체가 서로 구분 안 되는 결함이 생긴다(리뷰 결함)."""
        long_a = "aaaaaaaaaaaaa1111"  # 앞 13자 "aaaaaaaaaaaaa" 동일
        long_b = "aaaaaaaaaaaaa2222"
        _write_idx(self.t.state, long_a, [(1, "said", "x")])
        _write_idx(self.t.state, long_b, [(1, "said", "y")])
        code, out, err = self._show("aaaaaaaaaaaaa#1")
        self.assertEqual(code, 1)
        self.assertIn(long_a, out)
        self.assertIn(long_b, out)

    def test_no_default_session_gives_a_hint_not_a_crash(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "said", "x")])
        code, out, err = self._show("#1")
        self.assertEqual(code, 1)
        self.assertIn("no default session", out)

    def test_default_session_delivered_but_not_indexed_gives_a_distinct_hint(self):
        """#N 힌트가 "아무것도 전달된 적 없다" 와 "전달은 됐는데 색인이 아직
        없다" 를 뭉뚱그리면 안 된다(리뷰 결함)."""
        _mark_delivered(self.t.state, "not-indexed-yet")
        code, out, err = self._show("#1")
        self.assertEqual(code, 1)
        self.assertIn("not-indexed-yet", out)
        self.assertNotIn("no default session", out)


class TestLogRefsAndSaidPreview(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        cwd = os.getcwd()
        os.chdir(self.t.root)
        self.addCleanup(os.chdir, cwd)

    def _log(self, extra=()):
        out = io.StringIO()
        code = cli.cmd_log(
            cli.build_parser().parse_args(["log"] + list(extra)), home=self.t.home, out=out)
        return code, out.getvalue()

    def test_log_refs_are_directly_passable_to_show(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "ran", "pytest")])
        code, out = self._log()
        self.assertEqual(code, 0)
        line = out.strip().splitlines()[0]
        ref = line.split()[0]
        self.assertRegex(ref, r"^[0-9a-f]+#1$")

    def test_said_row_without_text_shows_a_hint(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "said", "")])
        code, out = self._log()
        self.assertEqual(code, 0)
        self.assertIn("omhc show", out)

    def test_unique_prefix_grows_past_13_chars_when_still_colliding(self):
        """13자는 표시상 선호일 뿐이다 — 그걸로도 안 갈리면 더 늘려야 한다. 안 그러면
        log 가 찍은 ref 를 show 가 모호하다고 거부한다(리뷰 결함)."""
        _write_idx(self.t.state, "aaaaaaaaaaaaa1111", [(1, "ran", "x")])
        _write_idx(self.t.state, "aaaaaaaaaaaaa2222", [(1, "ran", "y")])
        code, out = self._log()
        self.assertEqual(code, 0)
        refs = [line.split()[0] for line in out.strip().splitlines()]
        sessions = [r.split("#")[0] for r in refs]
        self.assertEqual(len(sessions), len(set(sessions)))

    def test_unique_prefix_grows_when_ids_share_the_first_8_chars(self):
        """Codex UUIDv7 은 앞 8자가 시간대로 겹친다(#15c) — log 는 그래도 서로
        다른 ref 를 찍어야 한다."""
        _write_idx(self.t.state, "0199aaaa1111", [(1, "ran", "x")])
        _write_idx(self.t.state, "0199aaaa2222", [(1, "ran", "y")])
        code, out = self._log()
        self.assertEqual(code, 0)
        refs = [line.split()[0] for line in out.strip().splitlines()]
        sessions = [r.split("#")[0] for r in refs]
        self.assertEqual(len(sessions), len(set(sessions)))


if __name__ == "__main__":
    unittest.main()
