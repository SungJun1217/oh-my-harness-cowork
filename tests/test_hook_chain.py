from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OMHC = os.path.join(ROOT, "bin", "omhc")
FRAGMENTS = {
    "claude-code": os.path.join(ROOT, "hooks", "claude-settings.fragment.json"),
    "codex-cli": os.path.join(ROOT, "hooks", "codex-hooks.json"),
}


def shipped_commands(path: str):
    """배포된 훅 파일에서 SessionStart 명령을 **배포된 순서 그대로** 뽑는다."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for group in data["hooks"]["SessionStart"]:
        for hook in group["hooks"]:
            assert hook["type"] == "command", hook
            out.append(hook["command"])
    return out


class TestShippedHookFiles(unittest.TestCase):
    def test_both_fragments_are_valid_json(self):
        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                with open(path, encoding="utf-8") as fh:
                    json.load(fh)

    def test_mark_runs_before_brief(self):
        """순서가 바뀌면 자기 세션이 원장에 없는 상태로 brief 가 돈다."""
        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                commands = shipped_commands(path)
                self.assertEqual(len(commands), 2)
                self.assertIn(" mark ", commands[0])
                self.assertIn(" brief ", commands[1])

    def test_each_command_names_its_own_harness(self):
        for harness, path in FRAGMENTS.items():
            for command in shipped_commands(path):
                with self.subTest(harness=harness, command=command):
                    self.assertIn("--harness {}".format(harness), command)

    def test_brief_pins_exactly_one_wire_format(self):
        """세 형식을 동시에 내보내면 Claude Code 가 둘 다 읽어 두 번 주입된다."""
        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                brief_command = shipped_commands(path)[1]
                self.assertEqual(len(re.findall(r"--wire\s+\S+", brief_command)), 1)

    def test_fragments_document_the_install_step(self):
        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertIn("omhc", text)
                self.assertIn("_comment", text)


class TestHookChainExecution(unittest.TestCase):
    """배포된 명령을 실제로 실행한다. 인자 문자열의 오타까지 잡는다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True,
                       capture_output=True)
        self.env = dict(os.environ, HOME=self.home)
        self.env.pop("OMHC_OFF", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, command: str, stdin_text: str):
        # 배포된 명령의 $HOME/.local/bin/omhc 를 이 레포의 런처로 바꾼다.
        # 인자와 순서는 그대로 두는 것이 이 테스트의 핵심이다.
        argv = shlex.split(command.replace("$HOME/.local/bin/omhc", OMHC))
        return subprocess.run(argv, input=stdin_text, capture_output=True, text=True,
                              env=self.env, cwd=self.repo, timeout=60)

    def _payload(self, session_id: str) -> str:
        return json.dumps({"cwd": os.path.realpath(self.repo),
                           "session_id": session_id})

    def _plant_codex(self, session_id="cx1"):
        import time as _time

        stamp = _time.gmtime()
        directory = os.path.join(self.home, ".codex", "sessions",
                                 _time.strftime("%Y/%m/%d", stamp))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "rollout-{}.jsonl".format(session_id))
        rows = [
            {"timestamp": "2026-09-22T16:30:00.000Z", "ordinal": 0,
             "type": "session_meta",
             "payload": {"session_id": session_id,
                         "cwd": os.path.realpath(self.repo)}},
            {"timestamp": "2026-09-22T16:30:01.000Z", "ordinal": 1,
             "type": "response_item",
             "payload": {"type": "message", "role": "user", "id": "u1",
                         "content": [{"type": "input_text",
                                      "text": "리더를 붙여서 양방향으로 만들기"}]}},
        ]
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def test_claude_chain_with_nothing_to_send_is_silent(self):
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            got = self._run(command, self._payload("me1"))
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(got.stdout, "", command)

    def test_codex_then_claude_produces_valid_wire_json(self):
        self._plant_codex()
        # 1) Codex 세션이 시작됐다고 기록
        for command in shipped_commands(FRAGMENTS["codex-cli"]):
            got = self._run(command, self._payload("cx1"))
            self.assertEqual(got.returncode, 0, got.stderr)
        # 2) Claude 세션 시작 → 표식이 나와야 한다
        outputs = []
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            got = self._run(command, self._payload("me1"))
            self.assertEqual(got.returncode, 0, got.stderr)
            outputs.append(got.stdout)
        payload = json.loads(outputs[1])
        body = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[omhc]", body)
        self.assertLessEqual(len(body.encode("utf-8")), 900)

    def test_refiring_the_chain_in_the_same_session_is_silent(self):
        self._plant_codex()
        for command in shipped_commands(FRAGMENTS["codex-cli"]):
            self._run(command, self._payload("cx1"))
        first = [self._run(c, self._payload("me1")).stdout
                 for c in shipped_commands(FRAGMENTS["claude-code"])]
        second = [self._run(c, self._payload("me1")).stdout
                  for c in shipped_commands(FRAGMENTS["claude-code"])]
        self.assertTrue(first[1])
        self.assertEqual(second[1], "", "게이트가 재발동을 막지 못했다")

    def test_chain_survives_broken_stdin(self):
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            got = self._run(command, "{not json")
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(got.stdout, "")

    def test_chain_survives_empty_stdin(self):
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            got = self._run(command, "")
            self.assertEqual(got.returncode, 0, got.stderr)


if __name__ == "__main__":
    unittest.main()
