from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest

from . import _repo

ROOT = _repo.REPO
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

    def test_shipped_wire_matches_the_adapters_declared_wire(self):
        """조각 파일의 --wire 와 어댑터의 wire 속성이 따로 논다는 것 자체가 이번

        버그가 숨었던 방식이다(어댑터는 sdk, 조각은 sdk, 실제 codex-cli 는 둘 다
        거부 — 둘이 일치해도 틀릴 수 있다는 것이 아니라, 어댑터를 고치고 조각을
        안 고치면 다시 벌어지는 종류의 불일치라 자동으로 잡아 둔다).
        """
        from omhc import adapters

        for harness, path in FRAGMENTS.items():
            with self.subTest(harness=harness):
                brief_command = shipped_commands(path)[1]
                match = re.search(r"--wire\s+(\S+)", brief_command)
                self.assertIsNotNone(match, brief_command)
                shipped_wire = match.group(1)
                declared_wire = getattr(adapters.get(harness), "wire", None)
                self.assertEqual(shipped_wire, declared_wire, harness)

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
        self.t = _repo.TempRepo()
        self.addCleanup(self.t.close)
        self.home = self.t.home
        self.repo = self.t.repo
        self.env = self.t.env

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
        return self.t.plant_codex(session_id=session_id,
                                  human="리더를 붙여서 양방향으로 만들기")

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


class TestF7ZeroCase(unittest.TestCase):
    """스펙 §16-7: 갈아타지 않는 세션 시작에는 주입이 0회여야 한다.

    F7(프로토타입의 15KB 무상한 주입)이 재설계의 이유였으므로, 0 케이스는
    주장이 아니라 관측으로 확인한다.
    """

    def setUp(self):
        self.t = _repo.TempRepo()
        self.addCleanup(self.t.close)
        self.home = self.t.home
        self.repo = self.t.repo
        self.env = self.t.env

    def _chain(self, session_id: str):
        outputs = []
        payload = json.dumps({"cwd": os.path.realpath(self.repo),
                              "session_id": session_id})
        for command in shipped_commands(FRAGMENTS["claude-code"]):
            argv = shlex.split(command.replace("$HOME/.local/bin/omhc", OMHC))
            got = subprocess.run(argv, input=payload, capture_output=True, text=True,
                                 env=self.env, cwd=self.repo, timeout=60)
            self.assertEqual(got.returncode, 0, got.stderr)
            outputs.append(got.stdout)
        return outputs

    def _ledger_lines(self):
        path = os.path.join(self.home, ".omhc", "ledger.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [line for line in fh if line.strip()]

    def test_ten_claude_only_session_starts_inject_nothing(self):
        """주입은 0회, **그러나 원장은 10줄이어야 한다.**

        부재만 단정하면 바이너리가 아무 일도 안 했을 때도 통과한다 — 잘못된 HOME
        해석이나 조기 종료로 무력화된 경우와 구분이 안 된다. 원장 10줄이
        '체인이 실제로 돌았다' 는 적극적 증거다.
        """
        for i in range(10):
            outputs = self._chain("claude-session-{}".format(i))
            self.assertEqual(outputs[1], "", "{}번째 세션에서 주입이 일어났다".format(i))
        self.assertEqual(len(self._ledger_lines()), 10,
                         "체인이 실제로 돌지 않았다 — 부재 단정이 무의미해진다")

    def test_ten_session_starts_leave_no_delivered_rows(self):
        """주입이 없으면 delivered.tsv 도 없어야 한다.

        실측에서는 레포별 상태 디렉터리 자체가 만들어지지 않는다 — 원장 한 줄
        (약 220바이트)만 ~/.omhc/ledger.jsonl 에 쌓인다. 그것이 F7 의 0 케이스다.
        """
        for i in range(10):
            self._chain("claude-session-{}".format(i))
        found = []
        for dirpath, _dirs, names in os.walk(os.path.join(self.home, ".omhc")):
            found += [os.path.join(dirpath, n) for n in names if n == "delivered.tsv"]
        self.assertEqual(found, [], "주입이 없었는데 delivered 기록이 생겼다")

    def test_ten_session_starts_cost_only_the_ledger_lines(self):
        for i in range(10):
            self._chain("claude-session-{}".format(i))
        total = 0
        for dirpath, _dirs, names in os.walk(os.path.join(self.home, ".omhc")):
            for name in names:
                total += os.path.getsize(os.path.join(dirpath, name))
        # 원장 10줄 + 게이트 마커. 킬로바이트 단위가 아니라 수백 바이트여야 한다.
        self.assertLess(total, 8 * 1024,
                        "갈아타지 않는 세션 10회가 {}B 를 남겼다".format(total))

    def test_ten_session_starts_write_no_artifact(self):
        for i in range(10):
            self._chain("claude-session-{}".format(i))
        found = []
        for dirpath, _dirs, names in os.walk(os.path.join(self.home, ".omhc")):
            found += [n for n in names if n == "omhc.txt"]
        self.assertEqual(found, [], "주입이 없었는데 산출물 파일이 생겼다")


if __name__ == "__main__":
    unittest.main()
