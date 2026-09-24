"""`omhc hooks install|uninstall` — CLI 레벨. 파일을 실제로 어떻게 바꾸는지는
tests/test_hookconf.py 의 `TestHookconfMergeStrip` 이 맡는다. 여기는 출력
문구·`--harness`·exit code·install.sh 와의 패리티다."""
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

    def test_harness_flag_limits_the_run_to_one_adapter(self):
        code, text = _run(["hooks", "install", "--harness", "claude-code"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("claude-code:", text)
        self.assertNotIn("codex-cli:", text)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".codex", "hooks.json")))

    def test_codex_post_write_note_is_printed(self):
        code, text = _run(["hooks", "install", "--harness", "codex-cli"], self.home)
        self.assertEqual(code, 0)
        self.assertIn("신뢰", text)  # codex-cli hook_config() 의 post_write_note

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
        # 일부러 바이너리를 두지 않은 홈 — 파일은 쓰이지만 재검사는 FAIL 이고,
        # 그 사실이 exit code 에도 반영돼야 한다(#7 리뷰 2).
        home = os.path.join(self._tmp.name, "home-nobin")
        os.makedirs(home)
        code, text = _run(["hooks", "install", "--harness", "claude-code"], home)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", text)
        settings = os.path.join(home, ".claude", "settings.json")
        self.assertTrue(os.path.exists(settings))  # 그래도 파일은 실제로 쓰였다


class TestHooksFreshUser(unittest.TestCase):
    """curl 설치 직후, 어느 하네스도 아직 한 번도 안 돌아 `detect()` 가 보는
    세션 디렉터리(`~/.claude/projects`, `~/.codex/sessions`)가 없는 상태
    (#7 리뷰 1). `adapters.present` 를 고정하지 않는다 — 이 테스트의 요점이
    바로 그 감지 규칙 자체다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.makedirs(self.home)
        _make_bin(self.home)

    def test_default_install_targets_a_harness_whose_config_dir_exists_even_undetected(self):
        os.makedirs(os.path.join(self.home, ".claude"))  # projects/ 는 아직 없다
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


# --- install.sh 패리티 --------------------------------------------------


def _extract_install_sh_stripper() -> str:
    """`strip_omhc_hooks() { ... }` 안의 python 헤레독 본문. 파일에서 처음
    나오는 `<<'PY'` 에 기대지 않는다 — install.sh 에 다른 heredoc 이 추가돼도
    엉뚱한 걸 뽑지 않도록 함수 이름에 앵커를 건다(#7 리뷰 6)."""
    with open(os.path.join(REPO, "install.sh"), encoding="utf-8") as fh:
        text = fh.read()
    fn = re.search(r"strip_omhc_hooks\(\)\s*\{.*?<<'PY'\n(.*?)\nPY\n", text, re.DOTALL)
    assert fn, "install.sh 에서 strip_omhc_hooks() 의 python 헤레독을 못 찾았다"
    return fn.group(1)


class TestInstallShParity(unittest.TestCase):
    """install.sh 의 SessionStart 스트리퍼(정규식 기반)와 hookconf.strip
    (argv 구조 기반)이 같은 입력에서 같은 결과 *와 같은 "바뀌었다" 신호* 를
    내는지 본다(install.sh 의 rc 0/3 대 hookconf.strip() 의 True/False).

    문자열만 닮았을 뿐 구조가 다른 경우는 두 판정이 의도적으로 갈라진다
    (omhc/hookconf.py 상단 주석 — docstring 이 아니라 모듈 코멘트 — 참고).
    실측된 세 divergence 모두 여기 패리티 대상에서 제외한다:
      - `cd ~ && omhc brief …` — install.sh 는 지우지만 hookconf 는 손대지
        않는다(전체 명령의 argv[0] 는 "cd").
      - `/usr/bin/env omhc mark …` — 마찬가지로 install.sh 만 지운다
        (argv[0] 는 "env").
      - `'omhc' 'mark' --harness x` — hookconf 는 shlex 로 풀어 인식하지만
        install.sh 의 정규식은 "mark"/"brief" 앞의 따옴표를 허용하지 않는다.
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
        self.assertIn(rc, (0, 3), "install.sh 스트리퍼가 예상 못한 코드로 실패: {}".format(rc))
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), rc == 0

    def _run_hookconf_strip(self, conf: dict):
        path = os.path.join(self._tmp.name, "b.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(conf, fh)
        changed = hookconf.strip(path)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), changed

    def _assert_parity(self, conf: dict) -> None:
        got_sh, changed_sh = self._run_install_sh(json.loads(json.dumps(conf)))
        got_py, changed_py = self._run_hookconf_strip(json.loads(json.dumps(conf)))
        self.assertEqual(got_sh, got_py)
        self.assertEqual(changed_sh, changed_py,
                         "changed 신호가 갈렸다: install.sh={} hookconf={}".format(
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

    def test_only_omhc_hooks_drops_the_hooks_key_entirely(self):
        # #20: SessionStart 가 omhc 훅뿐이면, 지우고 나서 hooks 가 빈 객체로
        # 남는 게 아니라 hooks 키 자체가 사라져야 한다 — 두 판정이 같이 그런다.
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
