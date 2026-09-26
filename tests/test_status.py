"""The contract of `omhc status`'s three labels (PASS/FAIL/`----`). Decision C1:
`----` is not SKIP — it's the label for a row with no basis for judgment yet,
or one that's merely informational, shown without disguising it as either
failure or success, and it never affects the exit code (#8)."""
from __future__ import annotations

import io
import json
import os
import time
import unittest
from unittest import mock

from omhc import cli, due, index
from omhc.event import Event

from ._repo import TempRepo, plant_hook_install


def _find_row(text: str, label: str):
    """Find the `label` row in the text output and return (verdict_word, detail)."""
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
        # Pin the adapters row since it's not this unit's concern — adapters.present(),
        # which looks at the real $HOME, must not give different results per test machine.
        patcher = mock.patch.object(cli.adapters, "present", return_value=["claude-code"])
        patcher.start()
        self.addCleanup(patcher.stop)
        # Pin the `claude-code hooks` row (not this unit's concern) to PASS —
        # otherwise every exit-code assertion in this file gets mixed up with hookconf's concerns.
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

    def _write_last_read(self, **fields):
        summary = {"harness": "codex-cli", "session": "01a0c9f4-06aa", "events": 117,
                   "unparsed": 0, "skipped": 402, "skipped_types": 12,
                   "epoch": 1758500000}
        summary.update(fields)
        os.makedirs(self.t.state, exist_ok=True)
        with open(os.path.join(self.t.state, "last_read.json"), "w") as fh:
            json.dump(summary, fh)

    def test_last_read_row_says_nothing_read_yet_on_a_fresh_repo(self):
        code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "last read")
        self.assertEqual(word, "----")
        self.assertIn("nothing read yet", detail)

    def test_last_read_row_shows_the_counts_and_never_gates(self):
        self._write_last_read()
        code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "last read")
        self.assertEqual(word, "----")
        self.assertIn("codex-cli 01a0c9f4: 117 events, 0 unparsed lines", detail)
        self.assertIn("402 records of 12 types skipped", detail)
        self.assertNotIn("format may have changed", detail)

    def test_zero_events_from_a_non_empty_session_points_at_a_format_change(self):
        self._write_last_read(events=0)
        code, text = self.run_status()
        self.assertEqual(code, 0, "an empty session is legitimate, so this row must not gate")
        _word, detail = _find_row(text, "last read")
        self.assertIn("format may have changed", detail)

    def test_last_read_is_in_the_json_output(self):
        self._write_last_read()
        _code, data = self.run_status_json()
        self.assertEqual(data["last_read"]["events"], 117)
        rows = {r["label"]: r for r in data["rows"]}
        self.assertIsNone(rows["last read"]["verdict"])

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
        """Review defect: even without pinned/<sid>/source.jsonl, lag_bytes came
        out as size 0 - watermark 0 = 0, which looked like a PASS. `pinned` must
        actually be checked."""
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

        # Even showing s1 three times (via is show/log-agnostic) counts as one.
        # s3 isn't in delivered.tsv, so it must not increment X.
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
        """A reopen line left by resume (#22) is not a delivery — it must not
        enter the injections/pull rate denominator."""
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
        """#25: keeping the denominator as the whole of delivered.tsv means the
        longer a repo is used, the more old deliveries linger in the denominator
        forever, making the pull rate look gradually lower. Only the most recent
        PULL_RATE_WINDOW deliveries should be the denominator."""
        os.makedirs(self.t.state, exist_ok=True)
        from omhc import ledger

        window = cli.PULL_RATE_WINDOW
        with open(os.path.join(self.t.state, due.DELIVERED_NAME), "w",
                  encoding="utf-8") as fh:
            # One delivery older than the window — actually pulled, but must be
            # excluded from both the denominator and numerator.
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
        # `pulls` stays the all-time value — so `pulls / injections` doesn't lose its meaning.
        self.assertEqual(payload["pulls"], 2)

    def test_status_reads_the_ledger_exactly_once(self):
        """#25: used to read twice — once with repo_key, once with limit=0 for
        health — now merged into a single read."""
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
        """Reading once and filtering in memory must give the same result as
        calling `read(repo_key=key)` directly (including the rule that the filter
        applies before the limit)."""
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

    def test_ledger_rejects_row_is_uninformative_with_no_refusals(self):
        code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "ledger rejects")
        self.assertEqual(word, "----")

        code_json, payload = self.run_status_json()
        self.assertEqual(payload["ledger_rejects"], 0)

    def test_ledger_rejects_row_fails_and_gates_after_a_refusal(self):
        """#22: even if the caller discards the False that append() returns,
        the refusal itself must show up in status."""
        from omhc import ledger

        ok = ledger.append(
            {"repo": self.t.key, "harness": "codex-cli", "session": "s" * 36,
             "event": "start", "path": "/p" * 400},
            home=self.t.home,
        )
        self.assertFalse(ok)

        code, text = self.run_status()
        self.assertEqual(code, 1)
        word, detail = _find_row(text, "ledger rejects")
        self.assertEqual(word, "FAIL")
        self.assertIn("1 session(s) dropped", detail)
        self.assertIn("omhc clear", detail)

        code_json, payload = self.run_status_json()
        self.assertEqual(payload["ledger_rejects"], 1)
        row = next(r for r in payload["rows"] if r["label"] == "ledger rejects")
        self.assertEqual(row["verdict"], "fail")

    def test_ledger_rejects_row_counts_distinct_sessions_not_raw_rows(self):
        """Even with legacy duplicate lines (accumulated before dedup existed),
        a single session must not be inflated as if it were dropped multiple times."""
        from omhc import ledger, fsio

        path = ledger._rejected_path(self.t.home)
        for _ in range(3):
            fsio.append_line(path, ledger._encode(
                {"repo": self.t.key, "harness": "codex-cli", "session": "s" * 36,
                 "event": "start", "epoch": time.time(), "bytes": 900}))

        code, text = self.run_status()
        self.assertEqual(code, 1)
        word, detail = _find_row(text, "ledger rejects")
        self.assertEqual(word, "FAIL")
        self.assertIn("1 session(s) dropped", detail)

        code_json, payload = self.run_status_json()
        self.assertEqual(payload["ledger_rejects"], 1)

    def test_ledger_rejects_row_ignores_rows_that_now_fit_under_a_raised_cap(self):
        """#22 review: even after raising MAX_LINE to fix things, an old
        rejection record must not stay FAIL forever — if `bytes` is now under the
        current cap, it's treated as already fixed."""
        from omhc import ledger, fsio

        path = ledger._rejected_path(self.t.home)
        fsio.append_line(path, ledger._encode(
            {"repo": self.t.key, "harness": "codex-cli", "session": "s" * 36,
             "event": "start", "epoch": time.time(), "bytes": ledger.MAX_LINE - 1}))

        code, text = self.run_status()
        self.assertEqual(code, 0)
        word, _detail = _find_row(text, "ledger rejects")
        self.assertEqual(word, "----")

        code_json, payload = self.run_status_json()
        self.assertEqual(payload["ledger_rejects"], 0)

    def test_clear_removes_only_this_repos_rejects(self):
        from omhc import ledger

        ledger.append({"repo": self.t.key, "harness": "codex-cli", "session": "s" * 36,
                       "event": "start", "path": "/p" * 400}, home=self.t.home)
        ledger.append({"repo": "some-other-repo", "harness": "codex-cli",
                       "session": "t" * 36, "event": "start", "path": "/p" * 400},
                      home=self.t.home)

        out = io.StringIO()
        code = cli.cmd_clear(cli.build_parser().parse_args(["clear"]),
                             home=self.t.home, out=out)
        self.assertEqual(code, 0)
        self.assertIn("ledger reject row(s)", out.getvalue())
        self.assertEqual(ledger.read_rejected(home=self.t.home, repo_key=self.t.key), [])
        self.assertEqual(
            len(ledger.read_rejected(home=self.t.home, repo_key="some-other-repo")), 1)

        code, text = self.run_status()
        word, _detail = _find_row(text, "ledger rejects")
        self.assertEqual(word, "----")

    def test_ledger_rejects_row_ignores_other_repos(self):
        from omhc import ledger

        ledger.append(
            {"repo": "some-other-repo", "harness": "codex-cli", "session": "s" * 36,
             "event": "start", "path": "/p" * 400},
            home=self.t.home,
        )
        code, text = self.run_status()
        self.assertEqual(code, 0)
        word, _detail = _find_row(text, "ledger rejects")
        self.assertEqual(word, "----")

    def test_hooks_row_passes_when_the_shipped_fragment_is_installed(self):
        """setUp already plants the claude-code hook — this only confirms the
        row actually shows up and can participate in gating."""
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
        """Review defect: an exception from health() must not skip the hooks
        judgment itself — the two diagnostics must be independent."""
        from omhc.adapters import claude_code as CC

        with mock.patch.object(CC.ClaudeCodeAdapter, "health",
                               side_effect=RuntimeError("boom")):
            code, text = self.run_status()
        self.assertEqual(code, 0)
        word, detail = _find_row(text, "claude-code hooks")
        self.assertEqual(word, "PASS")
        self.assertEqual(detail, "installed")

    def test_hooks_row_fails_loudly_when_judging_itself_raises(self):
        """Doesn't fail silently — if the judgment itself dies, that fact must
        be reported as a FAIL row (e.g. when hooks/ is missing and load_fragment fails)."""
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
        # #19: same set of top-level keys as the normal path — even with empty
        # values, a consumer won't die with a KeyError just because it's `/`.
        # Compare against the normal path's actual output instead of a hand-written
        # list (a hand-written list missed #25's new key).
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
        # hook_config is an optional method — give it None explicitly so the
        # hooks row, which isn't this unit's concern, doesn't butt in (Mock's
        # default is a MagicMock, which makes hook_config() look non-None and runs
        # the hooks judgment).
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
