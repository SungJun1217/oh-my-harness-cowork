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


if __name__ == "__main__":
    unittest.main()
