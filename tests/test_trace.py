"""#35 `omhc trace <path>` — 색인에서 그 파일을 건드린 이벤트를 세션을 넘나들며
찾는다(sessionwiki `trace` 선례)."""
from __future__ import annotations

import io
import json
import os
import unittest

from omhc import cli, due, index
from omhc.event import Event

from ._repo import TempRepo


def _write_idx(state: str, session_id: str, rows) -> None:
    """rows 는 (seq, verb, paths, arg) 튜플. paths 는 문자열 튜플."""
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
        # 전달 순서: a 먼저, b 나중 -> b 가 최신이라 마지막 줄이어야 한다.
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

    # --- 리뷰(#35) 1: 모호성 가드가 다중 경로 행 앞에서 뚫리는 결함 ------------

    def test_ambiguous_suffix_match_across_a_multi_path_row_is_reported_not_guessed(self):
        """재현(리뷰): s1 행 하나가 두 개의 서로 다른 접미사-일치 경로를 담고,
        s2 는 그중 하나만 담는다 — 첫 매치에서 멈추면 "버킷이 하나뿐"으로
        잘못 판정해 s1 이 실은 어느 파일을 가리키는지 모른다는 사실이 사라진다."""
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
        # 잘못된 파일(adapters 쪽)의 이력을 짐작해서 찍으면 안 된다.
        self.assertNotIn("bbbbbbbb", out)
        self.assertEqual(self._pulls(), [])

        # --json 이면 안내 문장 대신 기계가 읽는 객체를 낸다(리뷰).
        code, out = self._trace("__init__.py", ["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out),
                         {"ambiguous": ["omhc/__init__.py", "omhc/adapters/__init__.py"]})

    def test_suffix_match_respects_path_segment_boundaries(self):
        """`a/b.py` 는 `x/a/b.py` 와는 같은 파일일 수 있어도 `xa/b.py` 와는
        아니다 — 문자열 접미사가 아니라 '/' 로 쪼갠 조각 단위로 비교해야 한다."""
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
        """접미사 후보가 하나뿐이면(모호하지 않으면) 여전히 매치로 쓴다."""
        _write_idx(self.t.state, "aaaaaaaa1111",
                  [(1, "modified", ("omhc/adapters/codex_cli.py",), "")])
        self._mark("aaaaaaaa1111", "codex-cli")
        self._mark_delivered_helper("aaaaaaaa1111")

        code, out = self._trace("codex_cli.py")
        self.assertEqual(code, 0)
        self.assertIn("aaaaaaaa", out)

    def test_symlinked_index_path_resolves_to_the_same_real_file(self):
        """리뷰: macOS 의 `/var` -> `/private/var` 류. 색인엔 심볼릭 링크를 통한
        경로가, 질의엔 실제 경로가(혹은 그 반대) 들어와도 같은 파일이어야
        한다 — realpath 정규화가 접미사 추측 없이 바로 정확히 맞혀야 한다."""
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

    # --- 리뷰(#35) 2: pull 회계는 실제로 찍힌 매치에만 -----------------------

    def test_pull_recorded_only_for_the_session_actually_shown(self):
        """`zzzzzzzz9999` 가 가장 최근 전달이지만 무관한 파일이다 — `#N` 의
        "가장 최근 전달" 근사를 쓰면 안 되고, 실제로 찍힌 `aaaaaaaa1111` 이
        인출됐다고 적어야 한다."""
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
        # watch 로만 색인됐고 delivered.tsv 엔 없다.

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

    # --- 리뷰(#35) 3: 원장에 start 행이 없는(watch 전용) 세션의 하네스 -------

    def test_harness_is_a_question_mark_when_no_ledger_start_row_exists(self):
        _write_idx(self.t.state, "aaaaaaaa1111", [(1, "modified", ("omhc/cli.py",), "")])
        self._mark_delivered_helper("aaaaaaaa1111")  # mark() 는 안 불렀다.

        code, out = self._trace("omhc/cli.py")
        self.assertEqual(code, 0)
        fields = out.strip().splitlines()[0].split()
        self.assertIn("?", fields)


class TestTraceRefusesAtSlashRoot(unittest.TestCase):
    """`/` 는 프로젝트 루트가 아니다(#35 요구: log 와 같은 거부)."""

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
