"""`omhc status` 의 세 라벨(PASS/FAIL/`----`) 계약. 결정 C1: `----` 는 SKIP 이
아니다 — 아직 판단할 근거가 없거나 정보성인 행을 실패로도 성공으로도 위장하지
않고 보여주는 라벨이며, exit code 에 영향을 주지 않는다(#8)."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from omhc import cli, due, index
from omhc.event import Event

from ._repo import TempRepo, plant_hook_install


def _find_row(text: str, label: str):
    """텍스트 출력에서 `label` 행을 찾아 (verdict_word, detail) 을 돌려준다."""
    for line in text.splitlines():
        rest = line[5:]
        if rest[: len(label)] == label and rest[len(label): len(label) + 1] in ("", " "):
            return line[:4], rest[len(label):].strip()
    raise AssertionError("no {!r} row in:\n{}".format(label, text))


class TestStatusRows(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        cwd = os.getcwd()
        os.chdir(self.t.root)
        self.addCleanup(os.chdir, cwd)
        # adapters 행이 이 유닛의 관심사가 아니므로 고정한다 — 실제 $HOME 을
        # 보는 adapters.present() 가 테스트 머신마다 다른 결과를 주면 안 된다.
        patcher = mock.patch.object(cli.adapters, "present", return_value=["claude-code"])
        patcher.start()
        self.addCleanup(patcher.stop)
        # 이 유닛의 관심사가 아닌 `claude-code hooks` 행을 PASS 로 고정한다 —
        # 안 그러면 이 파일의 모든 exit-code 단정이 hookconf 의 관심사와 섞인다.
        plant_hook_install(self.t.home, "claude-code")
        os.environ.pop(due.OFF_ENV, None)
        self.addCleanup(os.environ.pop, due.OFF_ENV, None)

    def run_status(self, extra_args=()):
        out = io.StringIO()
        code = cli.cmd_status(
            cli.build_parser().parse_args(["status"] + list(extra_args)),
            home=self.t.home, out=out)
        return code, out.getvalue()

    def run_status_json(self):
        out = io.StringIO()
        code = cli.cmd_status(
            cli.build_parser().parse_args(["status", "--json"]),
            home=self.t.home, out=out)
        return code, json.loads(out.getvalue())

    def test_fresh_repo_ledger_and_archive_are_uninformative_not_failed(self):
        code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "ledger")
        self.assertEqual(word, "----")
        self.assertIn("no sessions recorded", detail)
        word, detail = _find_row(text, "archive")
        self.assertEqual(word, "----")
        self.assertIn("nothing handed off", detail)

    def test_injections_without_pins_fail_the_archive_row(self):
        os.makedirs(self.t.state, exist_ok=True)
        with open(os.path.join(self.t.state, due.DELIVERED_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("s1\tclaude-code\tcodex-cli\t1700000000\n")

        code, text = self.run_status()
        self.assertEqual(code, 1)
        word, detail = _find_row(text, "archive")
        self.assertEqual(word, "FAIL")
        self.assertIn("1 injections but nothing pinned", detail)

        code_json, payload = self.run_status_json()
        self.assertEqual(code_json, code)
        row = next(r for r in payload["rows"] if r["label"] == "archive")
        self.assertEqual(row["verdict"], "fail")

    def _write_idx(self, session_id: str) -> None:
        idx_dir = os.path.join(self.t.state, "index")
        os.makedirs(idx_dir, exist_ok=True)
        ev = Event(seq=1, epoch=1700000000.0, author="human", verb="said",
                   ok=True, text="hi", arg="hi", paths=(), offset=0, length=10)
        index.append_rows(os.path.join(idx_dir, session_id + ".idx"), [ev])

    def test_an_indexed_but_never_pinned_session_fails_archive_even_with_zero_lag(self):
        """리뷰 결함: pinned/<sid>/source.jsonl 이 없어도 lag_bytes 는 size 0 -
        watermark 0 = 0 으로 나와 PASS 처럼 보였다. `pinned` 를 실제로 봐야 한다."""
        self._write_idx("s1")
        os.makedirs(self.t.state, exist_ok=True)
        with open(os.path.join(self.t.state, due.DELIVERED_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("s1\tclaude-code\tcodex-cli\t1700000000\n")

        code, text = self.run_status()
        self.assertEqual(code, 1)
        word, detail = _find_row(text, "archive")
        self.assertEqual(word, "FAIL")
        self.assertIn("1 injections but nothing pinned", detail)

    def test_archive_passes_only_once_a_session_is_actually_pinned(self):
        self._write_idx("s1")
        pin_dir = os.path.join(self.t.state, "pinned", "s1")
        os.makedirs(pin_dir, exist_ok=True)
        with open(os.path.join(pin_dir, "source.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("x" * 20)

        code, text = self.run_status()
        word, detail = _find_row(text, "archive")
        self.assertEqual(word, "PASS")
        self.assertIn("s1"[:8], detail)
        self.assertIn("tail=", detail)

        code_json, payload = self.run_status_json()
        self.assertEqual(code_json, code)
        row = next(r for r in payload["rows"] if r["label"] == "archive")
        self.assertEqual(row["verdict"], "pass")

    def test_omhc_off_env_is_informational_and_names_the_source(self):
        with mock.patch.dict(os.environ, {due.OFF_ENV: "1"}):
            code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "off switch")
        self.assertEqual(word, "----")
        self.assertIn("off (OMHC_OFF=1)", detail)

    def test_off_marker_file_is_informational_and_names_the_marker_path(self):
        os.makedirs(self.t.state, exist_ok=True)
        marker = os.path.join(self.t.state, due.OFF_MARKER)
        open(marker, "w").close()

        code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "off switch")
        self.assertEqual(word, "----")
        self.assertIn("off (marker {})".format(marker), detail)

    def test_json_exit_code_matches_text_exit_code_on_a_clean_repo(self):
        code, _text = self.run_status()
        code_json, payload = self.run_status_json()
        self.assertEqual(code_json, code)
        self.assertIn("rows", payload)
        labels = {r["label"]: r["verdict"] for r in payload["rows"]}
        self.assertIsNone(labels["ledger"])
        self.assertIsNone(labels["off switch"])
        self.assertEqual(labels["adapters"], "pass")

    def test_pull_rate_counts_distinct_delivered_sessions_not_pull_rows(self):
        os.makedirs(self.t.state, exist_ok=True)
        with open(os.path.join(self.t.state, due.DELIVERED_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("s1\tclaude-code\tcodex-cli\t1700000000\n")
            fh.write("s2\tclaude-code\tcodex-cli\t1700000001\n")
        from omhc import ledger

        # s1 을 세 번 show 해도 (via 는 show/log 무관) 한 번만 센다. s3 은
        # delivered.tsv 에 없는 세션이라 X 를 늘리면 안 된다.
        for _ in range(3):
            ledger.append({"repo": self.t.key, "event": "pull", "via": "show",
                           "session": "s1", "epoch": 1700000002},
                          home=self.t.home)
        ledger.append({"repo": self.t.key, "event": "pull", "via": "log",
                       "session": "s3", "epoch": 1700000003}, home=self.t.home)

        code, text = self.run_status()
        word, detail = _find_row(text, "pull rate")
        self.assertEqual(word, "----")
        self.assertIn("pulled 1 of 2 recent injections", detail)

        code_json, payload = self.run_status_json()
        self.assertEqual(payload["pulls"], 1)
        self.assertEqual(payload["injections"], 2)
        self.assertEqual(payload["recent_injections"], 2)
        self.assertEqual(payload["pull_rate_window"], cli.PULL_RATE_WINDOW)

    def test_a_reopen_line_does_not_count_as_an_injection(self):
        """resume 이 남긴 reopen 줄(#22)은 전달이 아니다 — injections/pull rate
        분모에 끼면 안 된다."""
        os.makedirs(self.t.state, exist_ok=True)
        with open(os.path.join(self.t.state, due.DELIVERED_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("s1\tclaude-code\tcodex-cli\t1700000000\n")
            fh.write("s1\treopen\tcodex-cli\t1700000005\n")

        code, text = self.run_status()
        word, detail = _find_row(text, "pull rate")
        self.assertEqual(word, "----")
        self.assertIn("pulled 0 of 1 recent injections", detail)

        code_json, payload = self.run_status_json()
        self.assertEqual(payload["injections"], 1)

    def test_pull_rate_windows_the_denominator_to_the_most_recent_injections(self):
        """#25: 분모를 delivered.tsv 전체로 두면 한 레포를 오래 쓸수록 옛
        전달이 영원히 분모에 남아 인출률이 서서히 낮아 보인다. 최근
        PULL_RATE_WINDOW 개 전달만 분모로 삼아야 한다."""
        os.makedirs(self.t.state, exist_ok=True)
        from omhc import ledger

        window = cli.PULL_RATE_WINDOW
        with open(os.path.join(self.t.state, due.DELIVERED_NAME), "w",
                  encoding="utf-8") as fh:
            # 창보다 오래된 전달 하나 — 실제로 인출됐지만 분모·분자 모두에서
            # 빠져야 한다.
            fh.write("old\tclaude-code\tcodex-cli\t1700000000\n")
            for i in range(window):
                fh.write("s{}\tclaude-code\tcodex-cli\t{}\n".format(i, 1700000001 + i))
        ledger.append({"repo": self.t.key, "event": "pull", "via": "show",
                       "session": "old", "epoch": 1700000002}, home=self.t.home)
        ledger.append({"repo": self.t.key, "event": "pull", "via": "show",
                       "session": "s0", "epoch": 1700000003}, home=self.t.home)

        code, text = self.run_status()
        word, detail = _find_row(text, "pull rate")
        self.assertEqual(word, "----")
        self.assertIn("pulled 1 of {} recent injections (window {})".format(
            window, window), detail)

        code_json, payload = self.run_status_json()
        self.assertEqual(payload["injections"], window + 1)
        self.assertEqual(payload["recent_injections"], window)
        self.assertEqual(payload["recent_pulls"], 1)
        # `pulls` 는 전체 기간 값 그대로다 — `pulls / injections` 가 뜻을 잃지 않게.
        self.assertEqual(payload["pulls"], 2)

    def test_status_reads_the_ledger_exactly_once(self):
        """#25: repo_key 로 한 번, health 용 limit=0 으로 한 번 — 총 두 번
        읽던 것을 한 번으로 합쳤다."""
        from omhc import ledger

        real_read = ledger.read
        calls = []

        def counting(*a, **kw):
            calls.append(kw)
            return real_read(*a, **kw)

        with mock.patch.object(cli.ledger, "read", side_effect=counting):
            code, _text = self.run_status()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0].get("limit"), 0)
        self.assertNotIn("repo_key", calls[0])

    def test_status_ledger_row_count_matches_reading_with_the_repo_filter_directly(self):
        """한 번 읽고 메모리에서 거른 결과가 `read(repo_key=key)` 를 직접
        부른 것과 같아야 한다(필터가 limit 보다 먼저 적용되는 규칙까지)."""
        from omhc import ledger

        for i in range(5):
            ledger.append({"repo": "other-repo", "harness": "claude",
                           "session": "o{}".format(i), "event": "start"},
                          home=self.t.home)
        for i in range(3):
            ledger.append({"repo": self.t.key, "harness": "claude",
                           "session": "m{}".format(i), "event": "start"},
                          home=self.t.home)
        with mock.patch.object(cli.ledger, "DEFAULT_LIMIT", 2):
            code, text = self.run_status()
            code_json, payload = self.run_status_json()
        expected = ledger.read(home=self.t.home, repo_key=self.t.key, limit=2)
        self.assertEqual(code, 0)
        self.assertEqual(code_json, code)
        word, detail = _find_row(text, "ledger")
        self.assertEqual(word, "----")
        self.assertIn("{} rows for this repo".format(len(expected)), detail)
        self.assertEqual(payload["ledger_rows"], len(expected))
        self.assertEqual([r["session"] for r in expected], ["m1", "m2"])

    def test_hooks_row_passes_when_the_shipped_fragment_is_installed(self):
        """setUp 이 이미 claude-code 훅을 심어 둔다 — 여기서는 그 행이 실제로
        나타나고 게이팅에 참여할 수 있다는 것만 확인한다."""
        code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "claude-code hooks")
        self.assertEqual(word, "PASS")
        self.assertEqual(detail, "installed")

        code_json, payload = self.run_status_json()
        row = next(r for r in payload["rows"] if r["label"] == "claude-code hooks")
        self.assertEqual(row["verdict"], "pass")

    def test_hooks_row_fails_and_gates_when_no_hook_is_installed(self):
        os.remove(os.path.join(self.t.home, ".claude", "settings.json"))

        code, text = self.run_status()
        self.assertEqual(code, 1)
        word, detail = _find_row(text, "claude-code hooks")
        self.assertEqual(word, "FAIL")
        self.assertIn("not installed", detail)
        self.assertIn("omhc hooks install", detail)

    def test_hooks_row_is_still_judged_when_health_raises(self):
        """리뷰 결함: health() 의 예외가 hooks 판정 자체를 건너뛰면 안 된다 —
        두 진단은 서로 독립이어야 한다."""
        from omhc.adapters import claude_code as CC

        with mock.patch.object(CC.ClaudeCodeAdapter, "health",
                               side_effect=RuntimeError("boom")):
            code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "claude-code hooks")
        self.assertEqual(word, "PASS")
        self.assertEqual(detail, "installed")

    def test_hooks_row_fails_loudly_when_judging_itself_raises(self):
        """조용히 버리지 않는다 — 판정 자체가 죽으면 그 사실이 FAIL 행으로
        보고돼야 한다(예: hooks/ 가 없어 load_fragment 가 실패하는 경우)."""
        with mock.patch.object(cli.hookconf, "load_fragment",
                               side_effect=OSError("no such file")):
            code, text = self.run_status()
        self.assertEqual(code, 1)
        word, detail = _find_row(text, "claude-code hooks")
        self.assertEqual(word, "FAIL")
        self.assertIn("cannot check hooks", detail)

    def test_status_at_slash_shows_fail_root_and_gates(self):
        cwd = os.getcwd()
        os.chdir("/")
        self.addCleanup(os.chdir, cwd)
        code, text = self.run_status()
        self.assertEqual(code, 1)
        word, detail = _find_row(text, "root")
        self.assertEqual(word, "FAIL")
        self.assertIn("is not a project root", detail)
        self.assertNotIn("orphaned state dir", text)

        code_json, payload = self.run_status_json()
        self.assertEqual(code_json, 1)
        row = next(r for r in payload["rows"] if r["label"] == "root")
        self.assertEqual(row["verdict"], "fail")
        self.assertIn("is not a project root", payload["refused"])
        self.assertIsNone(payload["orphaned_state"])
        # #19: 정상 경로와 같은 최상위 키 집합 — 값은 비어 있어도 소비자가
        # `/` 에서만 KeyError 로 죽지 않는다. 목록을 손으로 적지 않고 정상
        # 경로의 실제 출력과 비교한다(손 목록은 #25 의 새 키를 놓쳤다).
        os.chdir(self.t.root)
        _code, normal = self.run_status_json()
        self.assertEqual(set(normal) - set(payload), set())

    def test_status_at_slash_reports_an_orphaned_state_dir_in_text_and_json(self):
        from omhc import locate

        orphan_state = locate.state_dir(locate.repo_key("/"), home=self.t.home)
        os.makedirs(orphan_state, exist_ok=True)
        cwd = os.getcwd()
        os.chdir("/")
        self.addCleanup(os.chdir, cwd)

        code, text = self.run_status()
        self.assertEqual(code, 1)
        self.assertIn("orphaned state dir: {}".format(orphan_state), text)

        code_json, payload = self.run_status_json()
        self.assertEqual(code_json, 1)
        self.assertEqual(payload["orphaned_state"], orphan_state)

    def test_health_row_with_ok_none_is_uninformative_and_never_gates(self):
        fake = mock.Mock()
        fake.health.return_value = (("custom diag", None, "not judgeable yet"),)
        # hook_config 는 선택 메서드다 — 명시적으로 None 을 줘서 이 유닛의
        # 관심사가 아닌 hooks 행이 끼어들지 않게 한다(Mock 기본값은 MagicMock
        # 이라 hook_config()가 None 이 아닌 것처럼 보여 hooks 판정이 돈다).
        fake.hook_config.return_value = None
        with mock.patch.object(cli.adapters, "get", return_value=fake):
            code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "custom diag")
        self.assertEqual(word, "----")
        self.assertEqual(detail, "not judgeable yet")

        with mock.patch.object(cli.adapters, "get", return_value=fake):
            code_json, payload = self.run_status_json()
        self.assertEqual(code_json, code)
        row = next(r for r in payload["rows"] if r["label"] == "custom diag")
        self.assertIsNone(row["verdict"])


if __name__ == "__main__":
    unittest.main()
