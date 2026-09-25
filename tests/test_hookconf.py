"""omhc/hookconf.py — 훅 설치 판정의 공유 스키마. `omhc status` 의
`<adapter-id> hooks` 행과 다음 유닛의 `omhc hooks install` 이 여기 하나를
공유한다."""
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
            missing_mark["SessionStart"][0]["hooks"][1]]  # brief 만 남긴다
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
        """os.environ 이 아니라 인자로 준 home 을 써야 한다 — 실제 $HOME 과
        다른 홈을 흉내 낼 수 있어야 하기 때문이다(개발 머신이 자기 훅을 늘
        가지고 있는 것과 무관하게 테스트가 결정적이어야 한다)."""
        self._write(self.fragment)
        other_home = os.path.join(self._tmp.name, "elsewhere")
        os.makedirs(other_home)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, other_home)
        self.assertFalse(ok)
        self.assertIn("not executable", detail)

    # --- 리뷰 1라운드: 구조적 판정 --------------------------------------

    def _fragment_with(self, binary: str, *, swap_flags: bool = False,
                       wire: str = "claude", trailing_space: bool = False) -> dict:
        """같은 mark/brief 를 `binary` 표현식으로 다시 쓴 조각을 만든다.
        `install_omhc_commands 는 argv 로 비교하므로, 문자열 표현이 달라도
        같은 호출이면 여전히 PASS 여야 한다는 게 이 절의 요점이다."""
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
        # argparse 가 받는 --harness=… --wire=… 형태도 같은 훅이다.
        frag = json.loads(json.dumps(self.fragment))
        for group in frag["SessionStart"]:
            for hook in group["hooks"]:
                hook["command"] = re.sub(r"--(harness|wire) (\S+)", r"--\1=\2", hook["command"])
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok, detail)

    def test_backslash_in_home_is_literal_not_a_regex_template(self):
        # re.sub 의 치환 템플릿으로 읽히면 re.error(invalid group reference)로 터졌다.
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
        os.makedirs(self.bin_path)  # 같은 자리에 실행파일 대신 디렉터리를 둔다
        self._write(self.fragment)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("not executable", detail)

    def test_echo_user_hook_is_ignored_not_mistaken_for_omhc(self):
        """구조 판정: 첫 토큰이 omhc 가 아니면(echo) 문자열에 'omhc brief' 가
        들어 있어도 설치로 착각하지 않는다 — install.sh 의 삭제용 문자열
        정규식과는 다른 목적이라 결과가 갈라질 수 있다(모듈 docstring 참고)."""
        frag = json.loads(json.dumps(self.fragment))
        frag["SessionStart"][0]["hooks"] = [
            {"type": "command", "command": "echo 'run omhc brief later'"}]
        self._write(frag)
        ok, detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertFalse(ok)
        self.assertIn("not installed", detail)

    # --- #20: matcher/type 이 세션 시작에 안 걸리는 설치는 FAIL --------------

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

    # Claude Code 2.1.281 실측 매칭 의미론을 그대로 거울에 비춘다: "단순"
    # matcher(`^[a-zA-Z0-9_|]+$`)는 "|" 로 쪼개 정확히 일치해야 하고, 그 외는
    # 고정 없는 부분 검색(re.search)이다.
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
        """리뷰 결함: runnable 호출만 비교하면 안 도는 여분의 omhc 그룹(예:
        matcher "resume")이 있어도 PASS 로 보인다 — merge() 의 "PASS 면 중복
        omhc 호출도 없다" 는 전제가 깨진다."""
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
        # `--text` 뒤에 값이 없는데 곧장 `--harness codex-cli` 가 오면, 예전
        # 파서는 "--harness" 를 --text 의 값으로 삼켜 진짜 --harness 를 잃었다.
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
    """`hookconf.has_runnable_call` — codex-cli 어댑터의 `hook_is_installed`
    가 부분 문자열 대신 쓰는 구조적 판정(#20)."""

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
        # 문자열에 "omhc brief" 가 들어 있어도 argv[0] 이 omhc 가 아니면 아니다
        # — 옛 부분 문자열 판정이라면 여기서 오탐했다.
        self._write({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "echo 'run omhc brief --harness codex-cli later'"},
        ]}]}})
        self.assertFalse(hookconf.has_runnable_call(
            self.config_path, "brief", {"--harness": "codex-cli"}))

    def test_true_when_a_valueless_flag_precedes_harness(self):
        # `--text` 뒤에 값 없이 곧장 `--harness` 가 오면, 예전 파서는
        # "--harness" 를 --text 의 값으로 삼켜 has_runnable_call 이 오탐 False
        # 를 내고 install_handoff 가 Path B 로 새버렸다.
        self._write({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "$HOME/.local/bin/omhc brief --text --harness codex-cli"},
        ]}]}})
        self.assertTrue(hookconf.has_runnable_call(
            self.config_path, "brief", {"--harness": "codex-cli"}))


class TestHookconfMergeStrip(unittest.TestCase):
    """`hookconf.merge`/`strip` — `omhc hooks install|uninstall` 이 파일을
    실제로 쓰는 부분. CLI 레벨의 출력·`--harness`·exit code 는
    tests/test_hooks_cmd.py 가 맡는다."""

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

    # --- 리뷰 1라운드: merge 가 이미 PASS 하는 손 설치는 건드리지 않는다 -----

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
        # 대조군: PASS 가 아니면(낡은 --wire) 여전히 다시 쓴다.
        stale = json.loads(json.dumps(self.fragment))
        stale["SessionStart"][0]["hooks"][1]["command"] = (
            "$HOME/.local/bin/omhc brief --harness claude-code --wire sdk")
        self._write({"hooks": stale})
        changed = hookconf.merge(self.config_path, self.fragment, self.home)
        self.assertTrue(changed)
        ok, _detail = hookconf.inspect(self.config_path, self.fragment, self.home)
        self.assertTrue(ok)

    # --- 리뷰 1라운드: strip 이 손대지 않은 빈 그룹까지 지우면 안 된다 -----

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

    # --- 리뷰 1라운드: 읽기 전용 설정 파일 ---------------------------------

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
        # hooks 가 SessionStart 만 들고 있었다면 hooks 자체도 사라져야 한다 —
        # {"hooks": {}} 흔적을 남기지 않는다(#20).
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
    """config.toml 의 인라인 `[[hooks.<Event>]]` (#32). 공식 문서
    (developers.openai.com/codex/config-advanced, "Hooks" 절) 의 예시 그대로
    array-of-tables 구조다."""

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
        # 실측(이 머신의 ~/.codex/config.toml): [hooks.state...] 는 신뢰
        # bookkeeping 이지 인라인 [hooks] 가 아니다 — hooks.<Event> 패턴이
        # 아니므로 조용히 무시돼야 한다.
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
        # 리뷰 #2 재현: 그룹의 `hooks` 키(내부적으로 리스트로 초기화된다)를
        # 본문의 `hooks = "oops"` 로 덮어쓰면, 다음 [[hooks.<E>.hooks]] 가
        # 그 리스트에 append 하려다 문자열이라 죽는다.
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
        # inspect_toml 은 parse_toml_hooks 자체가 예상 밖으로 던지는 경우까지
        # 대비한다(리뷰 #2) — 그 경로를 이 테스트에서 강제로 재현한다.
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
