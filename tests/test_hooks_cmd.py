"""`omhc hooks install|uninstall` — the CLI level. How the file actually
changes is covered by `TestHookconfMergeStrip` in tests/test_hookconf.py.
This covers output wording, `--harness`, exit code, and parity with install.sh."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from omhc import cli, hookconf

from ._repo import REPO


def _make_bin(home: str) -> None:
    bin_path = os.path.join(home, ".local", "bin", "omhc")
    os.makedirs(os.path.dirname(bin_path), exist_ok=True)
    with open(bin_path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(bin_path, 0o755)


def _run(argv, home):
    out = io.StringIO()
    code = cli.main(argv, home=home, out=out)
    return code, out.getvalue()


class TestHooksInstallCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        _make_bin(self.home)
        patcher = mock.patch.object(cli.adapters, "present",
                                    return_value=["claude-code", "codex-cli"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_install_creates_file_and_status_row_becomes_pass(self):
        code, text = _run(["hooks", "install", "--harness", "claude-code"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("installed ->", text)
        self.assertIn("PASS", text)
        settings = os.path.join(self.home, ".claude", "settings.json")
        self.assertTrue(os.path.exists(settings))

        hc = cli.adapters.get("claude-code", home=self.home).hook_config()
        fragment = hookconf.load_fragment(hc.fragment_name)
        ok, detail = hookconf.inspect(hc.config_path, fragment, self.home)
        self.assertTrue(ok, detail)

    def test_install_also_writes_the_turn_userpromptsubmit_group(self):
        # v2 phase 2 (#42): one `omhc hooks install` call covers both
        # managed events in the shared fragment file.
        _run(["hooks", "install", "--harness", "claude-code"], self.home)
        settings = os.path.join(self.home, ".claude", "settings.json")
        with open(settings, encoding="utf-8") as fh:
            conf = json.load(fh)
        self.assertIn("UserPromptSubmit", conf["hooks"])
        self.assertIn("omhc turn --harness claude-code",
                      conf["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])

    def test_an_old_install_with_only_sessionstart_is_flagged_fail(self):
        # An install from before #42 (only mark/brief, no turn hook) must not
        # silently stay PASS forever — the human needs to be told to rerun
        # `omhc hooks install` to pick up the new UserPromptSubmit group.
        settings = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        fragment = hookconf.load_fragment("claude-settings.fragment.json")
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": fragment["SessionStart"]}}, fh)
        out = io.StringIO()
        cli.cmd_status(cli.build_parser().parse_args(["status"]), home=self.home, out=out)
        text = out.getvalue()
        self.assertIn("claude-code hooks", text)
        for line in text.splitlines():
            if "claude-code hooks" in line:
                self.assertIn("FAIL", line)
                break
        else:
            self.fail("no claude-code hooks row found")

        # Running install again brings it back to PASS.
        code, _text = _run(["hooks", "install", "--harness", "claude-code"], self.home)
        self.assertEqual(code, 0)
        out2 = io.StringIO()
        cli.cmd_status(cli.build_parser().parse_args(["status"]), home=self.home, out=out2)
        for line in out2.getvalue().splitlines():
            if "claude-code hooks" in line:
                self.assertIn("PASS", line)
                break
        else:
            self.fail("no claude-code hooks row found")

    def test_uninstall_also_removes_the_turn_group(self):
        _run(["hooks", "install", "--harness", "claude-code"], self.home)
        _run(["hooks", "uninstall", "--harness", "claude-code"], self.home)
        settings = os.path.join(self.home, ".claude", "settings.json")
        with open(settings, encoding="utf-8") as fh:
            conf = json.load(fh)
        self.assertNotIn("UserPromptSubmit", conf.get("hooks", {}))

    def test_harness_flag_limits_the_run_to_one_adapter(self):
        code, text = _run(["hooks", "install", "--harness", "claude-code"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("claude-code:", text)
        self.assertNotIn("codex-cli:", text)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".codex", "hooks.json")))

    def test_codex_post_write_note_is_printed(self):
        code, text = _run(["hooks", "install", "--harness", "codex-cli"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("untrusted", text)  # codex-cli hook_config()'s post_write_note

    def test_unknown_harness_exits_nonzero(self):
        code, text = _run(["hooks", "install", "--harness", "nope"], self.home)
        self.assertNotEqual(code, 0)

    def test_malformed_config_exits_1_and_touches_nothing(self):
        settings = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings))
        with open(settings, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        code, text = _run(["hooks", "install", "--harness", "claude-code"], self.home)
        self.assertEqual(code, 1)
        with open(settings, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")

    def test_uninstall_removes_only_omhc_hooks(self):
        _run(["hooks", "install", "--harness", "claude-code"], self.home)
        settings = os.path.join(self.home, ".claude", "settings.json")
        with open(settings, encoding="utf-8") as fh:
            conf = json.load(fh)
        conf["hooks"]["SessionStart"][0]["hooks"].insert(
            0, {"type": "command", "command": "echo hello"})
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump(conf, fh)

        code, text = _run(["hooks", "uninstall", "--harness", "claude-code"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("removed from", text)
        with open(settings, encoding="utf-8") as fh:
            conf = json.load(fh)
        self.assertEqual(conf["hooks"]["SessionStart"],
                         [{"hooks": [{"type": "command", "command": "echo hello"}]}])

    def test_uninstall_twice_says_nothing_to_remove(self):
        _run(["hooks", "install", "--harness", "claude-code"], self.home)
        _run(["hooks", "uninstall", "--harness", "claude-code"], self.home)
        code, text = _run(["hooks", "uninstall", "--harness", "claude-code"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", text)

    def test_exit_code_is_1_when_the_post_install_reinspect_still_fails(self):
        # A home deliberately without a binary — the file is written but the
        # re-inspection is FAIL, and that fact must be reflected in the exit code too (#7 review 2).
        home = os.path.join(self._tmp.name, "home-nobin")
        os.makedirs(home)
        code, text = _run(["hooks", "install", "--harness", "claude-code"], home)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", text)
        settings = os.path.join(home, ".claude", "settings.json")
        self.assertTrue(os.path.exists(settings))  # the file was still actually written


class TestTurnHookInlineCodexInstall(unittest.TestCase):
    """Review #1 finding 5: an inline `config.toml` SessionStart/brief
    install must not leave the turn (UserPromptSubmit) half stuck FAIL
    forever — omhc never auto-installs into config.toml at all, so there's
    no `omhc hooks install` fix for it to point at."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        _make_bin(self.home)
        directory = os.path.join(self.home, ".codex")
        os.makedirs(directory, exist_ok=True)
        bin_path = os.path.join(self.home, ".local", "bin", "omhc")
        with open(os.path.join(directory, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(
                '[[hooks.SessionStart]]\n\n'
                '[[hooks.SessionStart.hooks]]\n'
                'type = "command"\n'
                'command = "{bin} mark --harness codex-cli"\n\n'
                '[[hooks.SessionStart.hooks]]\n'
                'type = "command"\n'
                'command = "{bin} brief --harness codex-cli --wire claude"\n'
                .format(bin=bin_path))

    def test_judge_hooks_row_is_unjudged_not_fail(self):
        inst = cli.adapters.get("codex-cli", home=self.home)
        hc = inst.hook_config()
        fragment = hookconf.load_fragment(hc.fragment_name)
        custom = getattr(inst, "hooks_status", None)
        ok, detail = cli._judge_hooks_row(hc, fragment, inst, custom)
        self.assertIsNone(ok)
        self.assertIn("config.toml", detail)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".codex", "hooks.json")))

    def test_hooks_install_prints_the_hint_instead_of_writing_hooks_json(self):
        code, text = _run(["hooks", "install", "--harness", "codex-cli"], self.home)
        self.assertIn("config.toml by hand", text)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".codex", "hooks.json")))

    def test_working_inline_turn_hook_passes_no_hint_no_downgrade(self):
        # Review #1 finding 3 (round 3): if config.toml *also* has a working
        # UserPromptSubmit -> omhc turn call, that satisfies the turn half
        # entirely -- no hint, no unjudged downgrade.
        bin_path = os.path.join(self.home, ".local", "bin", "omhc")
        with open(os.path.join(self.home, ".codex", "config.toml"), "a", encoding="utf-8") as fh:
            fh.write(
                '\n[[hooks.UserPromptSubmit]]\n\n'
                '[[hooks.UserPromptSubmit.hooks]]\n'
                'type = "command"\n'
                'command = "{bin} turn --harness codex-cli"\n'
                .format(bin=bin_path))
        inst = cli.adapters.get("codex-cli", home=self.home)
        hc = inst.hook_config()
        fragment = hookconf.load_fragment(hc.fragment_name)
        custom = getattr(inst, "hooks_status", None)
        ok, detail = cli._judge_hooks_row(hc, fragment, inst, custom)
        self.assertNotEqual(ok, False)
        self.assertNotIn("config.toml by hand", detail)

        code, text = _run(["hooks", "install", "--harness", "codex-cli"], self.home)
        self.assertNotIn("config.toml by hand", text)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".codex", "hooks.json")))

    def test_broken_turn_call_in_hooks_json_stays_fail_even_with_inline_sessionstart(self):
        # Review #1 finding 3 (round 3): a turn call that genuinely exists in
        # hooks.json but fails the structural check (wrong --harness here)
        # must stay FAIL -- never downgraded just because SessionStart/brief
        # happens to be installed inline.
        hooks_json = os.path.join(self.home, ".codex", "hooks.json")
        with open(hooks_json, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"UserPromptSubmit": [{"hooks": [
                {"type": "command",
                 "command": "$HOME/.local/bin/omhc turn --harness wrong-harness"},
            ]}]}}, fh)
        inst = cli.adapters.get("codex-cli", home=self.home)
        hc = inst.hook_config()
        fragment = hookconf.load_fragment(hc.fragment_name)
        custom = getattr(inst, "hooks_status", None)
        ok, detail = cli._judge_hooks_row(hc, fragment, inst, custom)
        self.assertFalse(ok)
        self.assertIn("turn hook", detail)
        self.assertNotIn("config.toml by hand", detail)


class TestCodexPlainHooksJsonUpgradePath(unittest.TestCase):
    """Review #1 finding 1 (round 4): round 3's `_has_inline_layer` gate
    keyed the unjudged downgrade off codex-cli merely *having* a
    `toml_config_path()` method — which every codex-cli instance always has,
    regardless of whether config.toml is actually in play — so a plain
    hooks.json-only install missing just the turn group was wrongly waved
    through as unjudged instead of FAIL, and `omhc hooks install` then saw
    "already up to date" and never added it. Pins the full upgrade path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        _make_bin(self.home)
        directory = os.path.join(self.home, ".codex")
        os.makedirs(directory, exist_ok=True)
        bin_path = os.path.join(self.home, ".local", "bin", "omhc")
        with open(os.path.join(directory, "hooks.json"), "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": "{} mark --harness codex-cli".format(bin_path)},
                {"type": "command",
                 "command": "{} brief --harness codex-cli --wire claude".format(bin_path)},
            ]}]}}, fh)

    def test_status_row_fails_then_install_adds_turn_then_passes(self):
        inst = cli.adapters.get("codex-cli", home=self.home)
        hc = inst.hook_config()
        fragment = hookconf.load_fragment(hc.fragment_name)
        custom = getattr(inst, "hooks_status", None)

        ok, detail = cli._judge_hooks_row(hc, fragment, inst, custom)
        self.assertFalse(ok, detail)
        self.assertNotIn("config.toml by hand", detail)

        code, text = _run(["hooks", "install", "--harness", "codex-cli"], self.home)
        self.assertNotIn("already up to date", text)
        self.assertIn("installed ->", text)
        self.assertEqual(code, 0, text)

        with open(os.path.join(self.home, ".codex", "hooks.json"), encoding="utf-8") as fh:
            conf = json.load(fh)
        self.assertIn("UserPromptSubmit", conf["hooks"])

        inst2 = cli.adapters.get("codex-cli", home=self.home)
        ok2, detail2 = cli._judge_hooks_row(hc, fragment, inst2, getattr(inst2, "hooks_status", None))
        self.assertTrue(ok2, detail2)


class TestTurnHookInlineNotApplicable(unittest.TestCase):
    """Review #1 finding 3 (round 3 regression fix): an adapter with no
    inline config.toml concept at all (Claude Code) must never be downgraded
    to unjudged just because it also has no such layer -- a missing turn
    hook there is a plain FAIL, exactly like a missing SessionStart hook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        _make_bin(self.home)

    def test_claude_missing_turn_hook_is_fail_not_unjudged(self):
        settings = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        fragment = hookconf.load_fragment("claude-settings.fragment.json")
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": fragment["SessionStart"]}}, fh)
        inst = cli.adapters.get("claude-code", home=self.home)
        hc = inst.hook_config()
        custom = getattr(inst, "hooks_status", None)
        ok, detail = cli._judge_hooks_row(hc, fragment, inst, custom)
        self.assertFalse(ok)
        self.assertNotIn("config.toml", detail)


class TestHooksNoAction(unittest.TestCase):
    """#19: calling bare `omhc hooks` with no action goes to stderr with usage
    and exit 2, per argparse convention."""

    def test_no_action_prints_usage_to_stderr_and_exits_2(self):
        out = io.StringIO()
        err = io.StringIO()
        code = cli.cmd_hooks(cli.build_parser().parse_args(["hooks"]), out=out, err=err)
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("usage:", err.getvalue())


class TestHooksFreshUser(unittest.TestCase):
    """Right after a curl install, neither harness has run yet, so the session
    directories `detect()` looks for (`~/.claude/projects`, `~/.codex/sessions`)
    don't exist (#7 review 1). `adapters.present` is not pinned — the detection
    rule itself is exactly what this test is about."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        _make_bin(self.home)

    def test_default_install_targets_a_harness_whose_config_dir_exists_even_undetected(self):
        os.makedirs(os.path.join(self.home, ".claude"))  # projects/ doesn't exist yet
        inst = cli.adapters.get("claude-code", home=self.home)
        self.assertFalse(inst.detect().present)

        code, text = _run(["hooks", "install"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("claude-code:", text)
        settings = os.path.join(self.home, ".claude", "settings.json")
        self.assertTrue(os.path.exists(settings))

    def test_no_harness_found_at_all_prints_a_hint_and_exits_1(self):
        code, text = _run(["hooks", "install"], self.home)
        self.assertEqual(code, 1)
        self.assertIn("no harness found", text)
        self.assertIn("--harness", text)

    def test_status_hooks_row_shows_fail_for_a_fresh_undetected_harness(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        out = io.StringIO()
        cli.cmd_status(cli.build_parser().parse_args(["status"]), home=self.home, out=out)
        text = out.getvalue()
        self.assertIn("claude-code hooks", text)


# --- install.sh parity --------------------------------------------------


def _extract_install_sh_stripper() -> str:
    """The python heredoc body inside `strip_omhc_hooks() { ... }`. Doesn't
    rely on the first `<<'PY'` in the file — anchors on the function name so
    that adding another heredoc to install.sh doesn't extract the wrong thing (#7 review 6)."""
    with open(os.path.join(REPO, "install.sh"), encoding="utf-8") as fh:
        text = fh.read()
    fn = re.search(r"strip_omhc_hooks\(\)\s*\{.*?<<'PY'\n(.*?)\nPY\n", text, re.DOTALL)
    assert fn, "could not find strip_omhc_hooks()'s python heredoc in install.sh"
    return fn.group(1)


class TestInstallShParity(unittest.TestCase):
    """Checks that install.sh's SessionStart stripper (regex-based) and
    hookconf.strip (argv-structure-based) produce the same result *and the same
    "changed" signal* on the same input (install.sh's rc 0/3 vs. hookconf.strip()'s
    True/False).

    Cases that look alike as strings but differ structurally are where the two
    judgments deliberately diverge (see the comment atop omhc/hookconf.py — a
    module comment, not a docstring). All three observed divergences are
    excluded from parity here:
      - `cd ~ && omhc brief ...` — install.sh removes it but hookconf leaves it
        alone (the whole command's argv[0] is "cd").
      - `/usr/bin/env omhc mark ...` — likewise, only install.sh removes it
        (argv[0] is "env").
      - `'omhc' 'mark' --harness x` — hookconf recognizes it via shlex parsing,
        but install.sh's regex doesn't allow quotes before "mark"/"brief".
    """

    @classmethod
    def setUpClass(cls):
        cls.script = _extract_install_sh_stripper()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _run_install_sh(self, conf: dict):
        path = os.path.join(self._tmp.name, "a.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(conf, fh)
        script_path = os.path.join(self._tmp.name, "strip.py")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(self.script)
        rc = subprocess.run([sys.executable, script_path, path]).returncode
        self.assertIn(rc, (0, 3), "install.sh stripper failed with an unexpected code: {}".format(rc))
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), rc == 0

    def _run_hookconf_strip(self, conf: dict):
        path = os.path.join(self._tmp.name, "b.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(conf, fh)
        # strip_all, not bare strip() — install.sh's script always covers
        # both managed events (SessionStart, UserPromptSubmit) in one pass
        # (v2 phase 2, #42), so that's what it must be compared against for
        # true parity. Equivalent to bare strip() on every case above (none
        # of them declare a UserPromptSubmit key).
        changed = hookconf.strip_all(path)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), changed

    def _assert_parity(self, conf: dict) -> None:
        got_sh, changed_sh = self._run_install_sh(json.loads(json.dumps(conf)))
        got_py, changed_py = self._run_hookconf_strip(json.loads(json.dumps(conf)))
        self.assertEqual(got_sh, got_py)
        self.assertEqual(changed_sh, changed_py,
                         "changed signal diverged: install.sh={} hookconf={}".format(
                             changed_sh, changed_py))

    def test_mixed_group(self):
        self._assert_parity({"hooks": {"SessionStart": [
            {"hooks": [
                {"type": "command", "command": "echo hello"},
                {"type": "command", "command": "$HOME/.local/bin/omhc mark --harness claude-code"},
                {"type": "command", "command": "$HOME/.local/bin/omhc brief --harness claude-code --wire claude"},
            ]},
        ]}})

    def test_a_stop_hook_is_left_alone(self):
        self._assert_parity({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "omhc done"}]}],
            "SessionStart": [
                {"hooks": [
                    {"type": "command", "command": "$HOME/.local/bin/omhc mark --harness claude-code"},
                    {"type": "command", "command": "$HOME/.local/bin/omhc brief --harness claude-code --wire claude"},
                ]},
            ],
        }})

    def test_a_quoted_path(self):
        self._assert_parity({"hooks": {"SessionStart": [
            {"hooks": [
                {"type": "command", "command": '"$HOME/.local/bin/omhc" mark --harness claude-code'},
                {"type": "command", "command": '"$HOME/.local/bin/omhc" brief --harness claude-code --wire claude'},
            ]},
        ]}})

    def test_only_omhc_hooks(self):
        self._assert_parity({"hooks": {"SessionStart": [
            {"hooks": [
                {"type": "command", "command": "$HOME/.local/bin/omhc mark --harness claude-code"},
                {"type": "command", "command": "$HOME/.local/bin/omhc brief --harness claude-code --wire claude"},
            ]},
        ]}})

    def test_no_omhc_hooks(self):
        self._assert_parity({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "echo hello"}]},
        ]}})

    def test_preexisting_empty_session_start_array(self):
        self._assert_parity({"hooks": {"SessionStart": []}})

    def test_preexisting_empty_group_alongside_a_user_group(self):
        self._assert_parity({"hooks": {"SessionStart": [
            {"hooks": []},
            {"hooks": [{"type": "command", "command": "echo hello"}]},
        ]}})

    def test_user_prompt_submit_turn_hook_is_stripped_too(self):
        # v2 phase 2 (#42): both managed events in one config, only the
        # UserPromptSubmit (turn) group has anything to remove.
        self._assert_parity({"hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo hello"}]}],
            "UserPromptSubmit": [
                {"hooks": [
                    {"type": "command", "command": "$HOME/.local/bin/omhc turn --harness claude-code"},
                ]},
            ],
        }})

    def test_untouched_empty_event_array_survives_a_change_in_the_other_event(self):
        # Review #1 finding 8: SessionStart actually has an omhc hook to
        # remove; UserPromptSubmit is a pre-existing empty array that was
        # never installed at all — it must be left exactly as-is, not
        # reassigned or deleted just because SessionStart changed in the
        # same pass.
        conf = {"hooks": {
            "SessionStart": [
                {"hooks": [
                    {"type": "command", "command": "$HOME/.local/bin/omhc mark --harness claude-code"},
                    {"type": "command", "command": "$HOME/.local/bin/omhc brief --harness claude-code --wire claude"},
                ]},
            ],
            "UserPromptSubmit": [],
        }}
        got_sh, changed_sh = self._run_install_sh(json.loads(json.dumps(conf)))
        got_py, changed_py = self._run_hookconf_strip(json.loads(json.dumps(conf)))
        self.assertTrue(changed_sh)
        self.assertTrue(changed_py)
        self.assertIn("UserPromptSubmit", got_sh["hooks"])
        self.assertEqual(got_sh["hooks"]["UserPromptSubmit"], [])
        self._assert_parity(conf)

    def test_both_managed_events_present_and_removed_together(self):
        self._assert_parity({"hooks": {
            "SessionStart": [
                {"hooks": [
                    {"type": "command", "command": "$HOME/.local/bin/omhc mark --harness claude-code"},
                    {"type": "command", "command": "$HOME/.local/bin/omhc brief --harness claude-code --wire claude"},
                ]},
            ],
            "UserPromptSubmit": [
                {"hooks": [
                    {"type": "command", "command": "$HOME/.local/bin/omhc turn --harness claude-code"},
                ]},
            ],
        }})

    def test_only_omhc_hooks_drops_the_hooks_key_entirely(self):
        # #20: if SessionStart holds only omhc hooks, after removal hooks must not
        # remain as an empty object — the hooks key itself must disappear, and both
        # judgments agree on this.
        conf = {"hooks": {"SessionStart": [
            {"hooks": [
                {"type": "command", "command": "$HOME/.local/bin/omhc mark --harness claude-code"},
                {"type": "command", "command": "$HOME/.local/bin/omhc brief --harness claude-code --wire claude"},
            ]},
        ]}}
        got_sh, changed_sh = self._run_install_sh(json.loads(json.dumps(conf)))
        got_py, changed_py = self._run_hookconf_strip(json.loads(json.dumps(conf)))
        self.assertTrue(changed_sh)
        self.assertTrue(changed_py)
        self.assertEqual(got_sh, {})
        self.assertEqual(got_py, {})
        self._assert_parity(conf)


if __name__ == "__main__":
    unittest.main()
