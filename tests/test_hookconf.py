"""omhc/hookconf.py — the shared schema for judging hook installs. `omhc status`'s
`<adapter-id> hooks` row and `omhc hooks install`'s next unit both share this
one source."""
from __future__ import annotations

import json
import re
import os
import stat
import tempfile
import unittest
from unittest import mock

from omhc import hookconf


class TestHookconf(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        self.config_path = os.path.join(self.home, "settings.json")
        self.fragment = hookconf.load_fragment("claude-settings.fragment.json")

        self.bin_path = os.path.join(self.home, ".local", "bin", "omhc")
        os.makedirs(os.path.dirname(self.bin_path), exist_ok=True)
        with open(self.bin_path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.bin_path, 0o755)

    def _write(self, hooks_by_event: dict) -> None:
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": hooks_by_event}, fh)

    def test_fragments_dir_resolves_to_the_shipped_hooks_directory(self):
        self.assertTrue(os.path.isdir(hookconf.fragments_dir()))
        self.assertTrue(os.path.exists(
            os.path.join(hookconf.fragments_dir(), "claude-settings.fragment.json")))

    def test_missing_file_fails_with_install_hint(self):
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("not installed", detail)
        self.assertIn("omhc hooks install", detail)

    def test_unparseable_file_fails(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("cannot parse", detail)

    def test_non_utf8_file_fails_as_cannot_parse(self):
        with open(self.config_path, "wb") as fh:
            fh.write(b"\xff\xfe\x00{not utf-8")
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("cannot parse", detail)

    def test_installed_exactly_per_the_fragment_passes(self):
        self._write(self.fragment)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok)
        self.assertEqual(detail, "installed")

    def test_no_omhc_commands_under_session_start_fails_as_not_installed(self):
        self._write({"SessionStart": [
            {"hooks": [{"type": "command", "command": "echo hi"}]}]})
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("not installed", detail)

    def test_stale_wire_flag_differs_from_shipped_fragment(self):
        stale = json.loads(json.dumps(self.fragment))
        stale["SessionStart"][0]["hooks"][1]["command"] = (
            "$HOME/.local/bin/omhc brief --harness claude-code --wire sdk")
        self._write(stale)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("differs from shipped fragment", detail)
        self.assertIn("omhc hooks install", detail)

    def test_missing_mark_differs_from_shipped_fragment(self):
        missing_mark = json.loads(json.dumps(self.fragment))
        missing_mark["SessionStart"][0]["hooks"] = [
            missing_mark["SessionStart"][0]["hooks"][1]]  # keep only brief
        self._write(missing_mark)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("differs from shipped fragment", detail)

    def test_brief_before_mark_differs_from_shipped_fragment(self):
        reordered = json.loads(json.dumps(self.fragment))
        hooks = reordered["SessionStart"][0]["hooks"]
        reordered["SessionStart"][0]["hooks"] = [hooks[1], hooks[0]]
        self._write(reordered)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("differs from shipped fragment", detail)

    def test_binary_not_executable_fails(self):
        os.chmod(self.bin_path, 0o644)
        self._write(self.fragment)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("not executable", detail)

    def test_a_user_hook_in_the_same_group_is_fine(self):
        with_user_hook = json.loads(json.dumps(self.fragment))
        with_user_hook["SessionStart"][0]["hooks"].insert(
            0, {"type": "command", "command": "echo hello"})
        self._write(with_user_hook)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok)

    def test_other_events_are_ignored(self):
        with_other_event = json.loads(json.dumps(self.fragment))
        with_other_event["Stop"] = [
            {"hooks": [{"type": "command", "command": "omhc done"}]}]
        self._write(with_other_event)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok)

    def test_home_expansion_uses_the_given_home_not_the_real_environ(self):
        """Must use the home passed as an argument, not os.environ — so a home
        different from the real $HOME can be simulated (the test must be
        deterministic regardless of whether the dev machine's own hooks are installed)."""
        self._write(self.fragment)
        other_home = os.path.join(self._tmp.name, "elsewhere")
        os.makedirs(other_home)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, other_home)
        self.assertFalse(ok)
        self.assertIn("not executable", detail)

    # --- review round 1: structural judging --------------------------------------

    def _fragment_with(self, binary: str, *, swap_flags: bool = False,
                       wire: str = "claude", trailing_space: bool = False) -> dict:
        """Build a fragment where the same mark/brief is rewritten with a `binary` expression.
        `install_omhc_commands` compares by argv, so the point of this section is that
        differing string representations of the same call must still PASS."""
        frag = json.loads(json.dumps(self.fragment))
        hooks = frag["SessionStart"][0]["hooks"]
        hooks[0]["command"] = "{} mark --harness claude-code".format(binary)
        if swap_flags:
            hooks[1]["command"] = "{} brief --wire {} --harness claude-code".format(
                binary, wire)
        else:
            hooks[1]["command"] = "{} brief --harness claude-code --wire {}".format(
                binary, wire)
        if trailing_space:
            hooks[1]["command"] += " "
        return frag

    def test_absolute_path_passes(self):
        self._write(self._fragment_with(self.bin_path))
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    def test_tilde_path_passes(self):
        self._write(self._fragment_with("~/.local/bin/omhc"))
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    def test_dollar_brace_home_passes(self):
        self._write(self._fragment_with("${HOME}/.local/bin/omhc"))
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    def test_quoted_command_passes(self):
        frag = self._fragment_with('"$HOME/.local/bin/omhc"')
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    def test_bare_name_found_on_path_passes(self):
        path_dir = os.path.join(self._tmp.name, "pathdir")
        os.makedirs(path_dir)
        shim = os.path.join(path_dir, "omhc")
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(shim, 0o755)
        self._write(self._fragment_with("omhc"))
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = path_dir + os.pathsep + old_path
        try:
            ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        finally:
            os.environ["PATH"] = old_path
        self.assertTrue(ok, detail)

    def test_repo_checkout_path_passes(self):
        repo_bin = os.path.join(self._tmp.name, "repo", "bin", "omhc")
        os.makedirs(os.path.dirname(repo_bin))
        with open(repo_bin, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(repo_bin, 0o755)
        self._write(self._fragment_with(repo_bin))
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    def test_swapped_flag_order_passes(self):
        self._write(self._fragment_with(self.bin_path, swap_flags=True))
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    def test_flag_equals_value_form_passes(self):
        # The --harness=... --wire=... form argparse accepts is the same hook too.
        frag = json.loads(json.dumps(self.fragment))
        for group in frag["SessionStart"]:
            for hook in group["hooks"]:
                hook["command"] = re.sub(r"--(harness|wire) (\S+)", r"--\1=\2", hook["command"])
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    def test_backslash_in_home_is_literal_not_a_regex_template(self):
        # Used to blow up with re.error(invalid group reference) if read as a re.sub replacement template.
        hookconf._resolve_binary("$HOME/x", "/tmp/h\\1x")

    def test_trailing_space_passes(self):
        self._write(self._fragment_with(self.bin_path, trailing_space=True))
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    def test_stale_wire_fails_naming_wire(self):
        self._write(self._fragment_with(self.bin_path, wire="sdk"))
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("differs from shipped fragment", detail)
        self.assertIn("--wire", detail)

    def test_directory_in_place_of_binary_fails(self):
        os.unlink(self.bin_path)
        os.makedirs(self.bin_path)  # put a directory in the same spot instead of an executable
        self._write(self.fragment)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("not executable", detail)

    def test_echo_user_hook_is_ignored_not_mistaken_for_omhc(self):
        """Structural judging: if the first token isn't omhc (echo), the string
        containing 'omhc brief' is not mistaken for an install — this serves a
        different purpose than install.sh's removal string regex, so results can
        diverge (see module docstring)."""
        frag = json.loads(json.dumps(self.fragment))
        frag["SessionStart"][0]["hooks"] = [
            {"type": "command", "command": "echo 'run omhc brief later'"}]
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("not installed", detail)

    # --- #20: an install whose matcher/type never fires at session start is FAIL --------------

    def test_matcher_that_excludes_startup_fails_naming_the_matcher(self):
        frag = json.loads(json.dumps(self.fragment))
        frag["SessionStart"][0]["matcher"] = "compact"
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("never run at session start", detail)
        self.assertIn("matcher 'compact'", detail)
        self.assertIn("omhc hooks install", detail)

    def test_matcher_that_includes_startup_passes(self):
        frag = json.loads(json.dumps(self.fragment))
        frag["SessionStart"][0]["matcher"] = "startup|resume|clear|compact"
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    # Mirrors Claude Code 2.1.281's observed matching semantics exactly: a "simple"
    # matcher (`^[a-zA-Z0-9_|]+$`) must split on "|" and match exactly, otherwise
    # it's an unanchored substring search (re.search).
    def test_simple_matcher_start_alone_does_not_run_at_startup(self):
        self.assertFalse(hookconf._matcher_runs_at_startup("start"))

    def test_regex_matcher_caret_start_runs_at_startup(self):
        self.assertTrue(hookconf._matcher_runs_at_startup("^start"))

    def test_regex_matcher_with_wildcard_runs_at_startup(self):
        self.assertTrue(hookconf._matcher_runs_at_startup("sta.t"))

    def test_invalid_regex_matcher_does_not_run_at_startup(self):
        self.assertFalse(hookconf._matcher_runs_at_startup("startup("))

    def test_hook_type_prompt_fails_naming_the_type(self):
        frag = json.loads(json.dumps(self.fragment))
        frag["SessionStart"][0]["hooks"][0]["type"] = "prompt"
        frag["SessionStart"][0]["hooks"][1]["type"] = "prompt"
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("never run at session start", detail)
        self.assertIn("type 'prompt'", detail)

    def test_duplicate_omhc_groups_reported_as_duplicate_not_wrong_order(self):
        frag = json.loads(json.dumps(self.fragment))
        frag["SessionStart"] = frag["SessionStart"] * 2  # mark, brief, mark, brief
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("duplicate omhc hooks (found mark, brief, mark, brief)", detail)

    def test_extra_non_runnable_omhc_group_fails_as_duplicate(self):
        """Review defect: comparing only runnable calls makes an extra omhc group
        that never runs (e.g. matcher "resume") look like a PASS — this breaks
        merge()'s assumption that "PASS implies no duplicate omhc calls"."""
        frag = json.loads(json.dumps(self.fragment))
        extra = json.loads(json.dumps(self.fragment["SessionStart"][0]))
        extra["matcher"] = "resume"
        frag["SessionStart"].append(extra)
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("differs from shipped fragment", detail)
        self.assertIn("duplicate omhc hooks", detail)

    def test_flag_value_that_looks_like_a_flag_does_not_eat_the_next_real_flag(self):
        # If `--text` has no value and `--harness codex-cli` follows right after,
        # the old parser swallowed "--harness" as --text's value and lost the real --harness.
        frag = json.loads(json.dumps(self.fragment))
        frag["SessionStart"][0]["hooks"][1]["command"] = (
            "$HOME/.local/bin/omhc brief --text --harness claude-code --wire claude")
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, frag, self.home)
        self.assertTrue(ok, detail)

    def test_bare_name_not_found_on_path_fails(self):
        self._write(self._fragment_with("omhc"))
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.path.join(self._tmp.name, "empty-path-dir")
        os.makedirs(os.environ["PATH"], exist_ok=True)
        try:
            ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        finally:
            os.environ["PATH"] = old_path
        self.assertFalse(ok)
        self.assertIn("cannot verify", detail)
        self.assertIn("PATH", detail)


class TestHasRunnableCall(unittest.TestCase):
    """`hookconf.has_runnable_call` — the structural judging the codex-cli
    adapter's `hook_is_installed` uses instead of substring matching (#20)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = os.path.join(self._tmp.name, "hooks.json")

    def _write(self, conf) -> None:
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(conf, fh)

    def test_missing_file_is_false_not_raise(self):
        self.assertFalse(hookconf.has_runnable_call(self.config_path, "brief"))

    def test_malformed_json_is_false_not_raise(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertFalse(hookconf.has_runnable_call(self.config_path, "brief"))

    def test_true_for_a_matching_runnable_brief_call(self):
        self._write({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "$HOME/.local/bin/omhc brief --harness codex-cli --wire claude"},
        ]}]}})
        self.assertTrue(hookconf.has_runnable_call(
            self.config_path, "brief", {"--harness": "codex-cli"}))

    def test_false_when_harness_flag_differs(self):
        self._write({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "$HOME/.local/bin/omhc brief --harness claude-code --wire claude"},
        ]}]}})
        self.assertFalse(hookconf.has_runnable_call(
            self.config_path, "brief", {"--harness": "codex-cli"}))

    def test_false_when_not_runnable_at_startup(self):
        self._write({"hooks": {"SessionStart": [{"matcher": "compact", "hooks": [
            {"type": "command", "command": "$HOME/.local/bin/omhc brief --harness codex-cli"},
        ]}]}})
        self.assertFalse(hookconf.has_runnable_call(
            self.config_path, "brief", {"--harness": "codex-cli"}))

    def test_false_for_a_lookalike_user_hook(self):
        # Not a match even if the string contains "omhc brief", if argv[0] isn't omhc
        # — the old substring-based judging would have false-positived here.
        self._write({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "echo 'run omhc brief --harness codex-cli later'"},
        ]}]}})
        self.assertFalse(hookconf.has_runnable_call(
            self.config_path, "brief", {"--harness": "codex-cli"}))

    def test_true_when_a_valueless_flag_precedes_harness(self):
        # If `--harness` follows `--text` with no value in between, the old parser
        # swallowed "--harness" as --text's value, has_runnable_call false-negatived,
        # and install_handoff leaked onto Path B.
        self._write({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "$HOME/.local/bin/omhc brief --text --harness codex-cli"},
        ]}]}})
        self.assertTrue(hookconf.has_runnable_call(
            self.config_path, "brief", {"--harness": "codex-cli"}))


class TestHookconfMergeStrip(unittest.TestCase):
    """`hookconf.merge`/`strip` — the part of the file `omhc hooks install|uninstall`
    actually writes. CLI-level output, `--harness`, and exit code are covered by
    tests/test_hooks_cmd.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = os.path.join(self._tmp.name, "settings.json")
        self.fragment = hookconf.load_fragment("claude-settings.fragment.json")

        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        self.bin_path = os.path.join(self.home, ".local", "bin", "omhc")
        os.makedirs(os.path.dirname(self.bin_path), exist_ok=True)
        with open(self.bin_path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.bin_path, 0o755)

    def _read(self):
        with open(self.config_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_merge_into_missing_file_creates_it(self):
        self.assertFalse(os.path.exists(self.config_path))
        changed = hookconf.merge(self.config_path, self.fragment, self.home)
        self.assertTrue(changed)
        self.assertEqual(self._read(), {"hooks": self.fragment})
        self.assertFalse(os.path.exists(self.config_path + ".omhc-bak"))

    def test_merge_twice_is_idempotent_no_backup_mtime_unchanged(self):
        hookconf.merge(self.config_path, self.fragment, self.home)
        before = os.stat(self.config_path).st_mtime_ns
        changed = hookconf.merge(self.config_path, self.fragment, self.home)
        self.assertFalse(changed)
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, before)
        self.assertFalse(os.path.exists(self.config_path + ".omhc-bak"))

    def test_merge_preserves_user_hooks_other_events_and_other_keys(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({
                "other_top_level_key": "keep me",
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "omhc done"}]}],
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo hello"}]},
                    ],
                },
            }, fh)
        changed = hookconf.merge(self.config_path, self.fragment, self.home)
        self.assertTrue(changed)
        conf = self._read()
        self.assertEqual(conf["other_top_level_key"], "keep me")
        self.assertEqual(conf["hooks"]["Stop"],
                         [{"hooks": [{"type": "command", "command": "omhc done"}]}])
        session_start = conf["hooks"]["SessionStart"]
        self.assertEqual(session_start[0],
                         {"hooks": [{"type": "command", "command": "echo hello"}]})
        self.assertEqual(session_start[1], self.fragment["SessionStart"][0])

    def test_backup_matches_original_bytes_and_mode(self):
        original = json.dumps({"hooks": {}}, indent=2)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write(original)
        os.chmod(self.config_path, 0o640)
        hookconf.merge(self.config_path, self.fragment, self.home)
        backup = self.config_path + ".omhc-bak"
        with open(backup, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.config_path).st_mode), 0o640)

    def test_symlinked_config_stays_a_symlink(self):
        real = os.path.join(self._tmp.name, "real-settings.json")
        with open(real, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {}}, fh)
        link = os.path.join(self._tmp.name, "settings.json")
        os.symlink(real, link)
        hookconf.merge(link, self.fragment, self.home)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), real)
        with open(real, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"hooks": self.fragment})

    def test_malformed_json_raises_and_touches_nothing(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(hookconf.HookConfigError):
            hookconf.merge(self.config_path, self.fragment, self.home)
        with open(self.config_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")
        self.assertFalse(os.path.exists(self.config_path + ".omhc-bak"))

    # --- review round 1: merge must not touch a manual install that already PASSes -----

    def _write(self, conf) -> None:
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(conf, fh)

    def test_merge_leaves_an_already_passing_install_untouched_omhc_group_first(self):
        conf = {"hooks": {"SessionStart": [
            self.fragment["SessionStart"][0],
            {"hooks": [{"type": "command", "command": "echo user"}]},
        ]}}
        self._write(conf)
        before = os.stat(self.config_path).st_mtime_ns
        changed = hookconf.merge(self.config_path, self.fragment, self.home)
        self.assertFalse(changed)
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, before)
        self.assertFalse(os.path.exists(self.config_path + ".omhc-bak"))
        self.assertEqual(self._read(), conf)

    def test_merge_leaves_a_user_added_timeout_field_untouched(self):
        omhc_group = json.loads(json.dumps(self.fragment["SessionStart"][0]))
        for h in omhc_group["hooks"]:
            h["timeout"] = 30
        conf = {"hooks": {"SessionStart": [omhc_group]}}
        self._write(conf)
        before = os.stat(self.config_path).st_mtime_ns
        changed = hookconf.merge(self.config_path, self.fragment, self.home)
        self.assertFalse(changed)
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, before)
        self.assertEqual(self._read(), conf)

    def test_merge_leaves_a_matcher_group_untouched(self):
        omhc_group = json.loads(json.dumps(self.fragment["SessionStart"][0]))
        omhc_group["matcher"] = "*"
        conf = {"hooks": {"SessionStart": [omhc_group]}}
        self._write(conf)
        before = os.stat(self.config_path).st_mtime_ns
        changed = hookconf.merge(self.config_path, self.fragment, self.home)
        self.assertFalse(changed)
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, before)
        self.assertEqual(self._read(), conf)

    def test_merge_still_rewrites_a_failing_install(self):
        # Control: if it's not PASS (stale --wire), it's still rewritten.
        stale = json.loads(json.dumps(self.fragment))
        stale["SessionStart"][0]["hooks"][1]["command"] = (
            "$HOME/.local/bin/omhc brief --harness claude-code --wire sdk")
        self._write({"hooks": stale})
        changed = hookconf.merge(self.config_path, self.fragment, self.home)
        self.assertTrue(changed)
        ok, _detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok)

    # --- review round 1: strip must not remove an empty group it didn't touch -----

    def test_strip_leaves_a_preexisting_empty_session_start_array_untouched(self):
        self._write({"hooks": {"SessionStart": []}})
        before = os.stat(self.config_path).st_mtime_ns
        changed = hookconf.strip(self.config_path)
        self.assertFalse(changed)
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, before)
        self.assertEqual(self._read(), {"hooks": {"SessionStart": []}})

    def test_strip_leaves_a_preexisting_empty_group_untouched(self):
        conf = {"hooks": {"SessionStart": [
            {"hooks": []},
            {"hooks": [{"type": "command", "command": "echo user"}]},
        ]}}
        self._write(conf)
        before = os.stat(self.config_path).st_mtime_ns
        changed = hookconf.strip(self.config_path)
        self.assertFalse(changed)
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, before)
        self.assertEqual(self._read(), conf)

    # --- review round 1: a read-only config file ---------------------------------

    def test_merge_refuses_a_read_only_config(self):
        self._write({"hooks": {}})
        os.chmod(self.config_path, 0o444)
        try:
            with self.assertRaises(hookconf.HookConfigError):
                hookconf.merge(self.config_path, self.fragment, self.home)
            with open(self.config_path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"hooks": {}})
        finally:
            os.chmod(self.config_path, 0o644)

    def test_stale_read_only_backup_does_not_block_a_later_change(self):
        self._write({"hooks": {}})
        backup = self.config_path + ".omhc-bak"
        with open(backup, "w", encoding="utf-8") as fh:
            fh.write("stale")
        os.chmod(backup, 0o444)
        changed = hookconf.merge(self.config_path, self.fragment, self.home)
        self.assertTrue(changed)
        with open(backup, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"hooks": {}})

    def test_non_utf8_raises_and_touches_nothing(self):
        with open(self.config_path, "wb") as fh:
            fh.write(b"\xff\xfe\x00{not utf-8")
        with self.assertRaises(hookconf.HookConfigError):
            hookconf.strip(self.config_path)
        with open(self.config_path, "rb") as fh:
            self.assertEqual(fh.read(), b"\xff\xfe\x00{not utf-8")

    def test_malformed_shape_raises_and_touches_nothing(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"SessionStart": "not-a-list"}}, fh)
        with self.assertRaises(hookconf.HookConfigError):
            hookconf.strip(self.config_path)

    def test_strip_removes_only_omhc_hooks(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "omhc done"}]}],
                    "SessionStart": [
                        {"hooks": [
                            {"type": "command", "command": "echo hello"},
                            self.fragment["SessionStart"][0]["hooks"][0],
                            self.fragment["SessionStart"][0]["hooks"][1],
                        ]},
                    ],
                },
            }, fh)
        changed = hookconf.strip(self.config_path)
        self.assertTrue(changed)
        conf = self._read()
        self.assertEqual(conf["hooks"]["SessionStart"],
                         [{"hooks": [{"type": "command", "command": "echo hello"}]}])
        self.assertEqual(conf["hooks"]["Stop"],
                         [{"hooks": [{"type": "command", "command": "omhc done"}]}])

    def test_strip_drops_empty_group_and_key(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": self.fragment}, fh)
        changed = hookconf.strip(self.config_path)
        self.assertTrue(changed)
        conf = self._read()
        # If hooks held only SessionStart, hooks itself must disappear too —
        # no {"hooks": {}} residue left behind (#20).
        self.assertNotIn("hooks", conf)

    def test_strip_drops_empty_session_start_key_but_keeps_other_hook_events(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": dict(self.fragment, Stop=[
                {"hooks": [{"type": "command", "command": "omhc done"}]}])}, fh)
        changed = hookconf.strip(self.config_path)
        self.assertTrue(changed)
        conf = self._read()
        self.assertNotIn("SessionStart", conf["hooks"])
        self.assertEqual(conf["hooks"]["Stop"],
                         [{"hooks": [{"type": "command", "command": "omhc done"}]}])

    def test_strip_twice_second_time_nothing_to_remove(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": self.fragment}, fh)
        hookconf.strip(self.config_path)
        before = os.stat(self.config_path).st_mtime_ns
        changed = hookconf.strip(self.config_path)
        self.assertFalse(changed)
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, before)

    def test_strip_on_missing_file_is_a_noop(self):
        self.assertFalse(os.path.exists(self.config_path))
        changed = hookconf.strip(self.config_path)
        self.assertFalse(changed)
        self.assertFalse(os.path.exists(self.config_path))


class TestTomlInlineHooks(unittest.TestCase):
    """Inline `[[hooks.<Event>]]` in config.toml (#32). The array-of-tables
    structure exactly as shown in the official docs
    (developers.openai.com/codex/config-advanced, "Hooks" section)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.toml_path = os.path.join(self._tmp.name, "config.toml")

    def _write(self, text: str) -> None:
        with open(self.toml_path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_parses_the_documented_shape(self):
        self._write(
            '[[hooks.PreToolUse]]\n'
            'matcher = "^Bash$"\n'
            '\n'
            '[[hooks.PreToolUse.hooks]]\n'
            'type = "command"\n'
            'command = \'/usr/bin/python3 "policy.py"\'\n'
            'timeout = 30\n'
            'statusMessage = "Checking Bash command"\n'
        )
        with open(self.toml_path, encoding="utf-8") as fh:
            hooks = hookconf.parse_toml_hooks(fh.read())
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "^Bash$")
        self.assertEqual(hooks["PreToolUse"][0]["hooks"][0]["command"],
                         '/usr/bin/python3 "policy.py"')

    def test_has_runnable_call_toml_true_for_a_matching_session_start_hook(self):
        self._write(
            '[[hooks.SessionStart]]\n'
            '\n'
            '[[hooks.SessionStart.hooks]]\n'
            'type = "command"\n'
            'command = "omhc brief --harness codex-cli"\n'
        )
        self.assertTrue(hookconf.has_runnable_call_toml(
            self.toml_path, "brief", {"--harness": "codex-cli"}))

    def test_has_runnable_call_toml_false_when_matcher_excludes_startup(self):
        self._write(
            '[[hooks.SessionStart]]\n'
            'matcher = "compact"\n'
            '\n'
            '[[hooks.SessionStart.hooks]]\n'
            'type = "command"\n'
            'command = "omhc brief --harness codex-cli"\n'
        )
        self.assertFalse(hookconf.has_runnable_call_toml(
            self.toml_path, "brief", {"--harness": "codex-cli"}))

    def test_ignores_unrelated_tables(self):
        # Observed (this machine's ~/.codex/config.toml): [hooks.state...] is trust
        # bookkeeping, not inline [hooks] — it doesn't match the hooks.<Event> pattern,
        # so it should be silently ignored.
        self._write(
            '[hooks.state]\n'
            '\n'
            '[hooks.state."/x/hooks.json:session_start:0:0"]\n'
            'sha256 = "deadbeef"\n'
            '\n'
            '[projects."/a/b"]\n'
            'trust_level = "trusted"\n'
        )
        with open(self.toml_path, encoding="utf-8") as fh:
            hooks = hookconf.parse_toml_hooks(fh.read())
        self.assertEqual(hooks, {})

    def test_missing_file_is_false_not_raise(self):
        self.assertFalse(hookconf.has_runnable_call_toml(self.toml_path, "brief"))

    def test_garbage_file_is_false_not_raise(self):
        self._write("not { valid toml at all !!!\n[[[broken\n")
        self.assertFalse(hookconf.has_runnable_call_toml(self.toml_path, "brief"))

    def test_a_hooks_key_that_shadows_the_hooks_list_does_not_raise(self):
        # Reproduces review #2: overwriting the group's `hooks` key (internally
        # initialized as a list) with a body `hooks = "oops"` makes the next
        # [[hooks.<E>.hooks]] blow up trying to append to a string.
        self._write(
            '[[hooks.SessionStart]]\n'
            'hooks = "oops"\n'
            '\n'
            '[[hooks.SessionStart.hooks]]\n'
            'type = "command"\n'
            'command = "omhc brief --harness codex-cli"\n'
        )
        with open(self.toml_path, encoding="utf-8") as fh:
            hooks = hookconf.parse_toml_hooks(fh.read())
        self.assertEqual(hooks["SessionStart"][0]["hooks"][0]["command"],
                         "omhc brief --harness codex-cli")
        self.assertTrue(hookconf.has_runnable_call_toml(
            self.toml_path, "brief", {"--harness": "codex-cli"}))

    def test_inspect_toml_wraps_a_parse_failure_as_cannot_parse(self):
        # inspect_toml also guards even against parse_toml_hooks itself raising
        # unexpectedly (review #2) — this test forces that path.
        self._write('[[hooks.SessionStart]]\n')
        with mock.patch.object(hookconf, "parse_toml_hooks", side_effect=RuntimeError("boom")):
            ok, detail = hookconf.inspect_toml(self.toml_path, {}, os.path.dirname(self.toml_path))
        self.assertFalse(ok)
        self.assertIn("cannot parse", detail)

    def test_inspect_toml_pass_when_matching_shipped_fragment(self):
        fragment = {"SessionStart": [{"hooks": [
            {"type": "command", "command": "omhc brief --harness codex-cli"},
        ]}]}
        home = os.path.dirname(self.toml_path)
        os.makedirs(os.path.join(home, ".local", "bin"), exist_ok=True)
        bin_path = os.path.join(home, ".local", "bin", "omhc")
        with open(bin_path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(bin_path, 0o755)
        self._write(self._toml_with_bin(home))
        ok, detail = hookconf.inspect_toml(self.toml_path, fragment, home)
        self.assertTrue(ok, detail)

    def _toml_with_bin(self, home: str) -> str:
        return (
            '[[hooks.SessionStart]]\n'
            '\n'
            '[[hooks.SessionStart.hooks]]\n'
            'type = "command"\n'
            'command = "{}/.local/bin/omhc brief --harness codex-cli"\n'
        ).format(home)


if __name__ == "__main__":
    unittest.main()
