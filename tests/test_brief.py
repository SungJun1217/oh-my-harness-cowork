from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest

from omhc import brief, deliver, ledger, locate
from omhc.adapter import Capability, HandoffBundle

NOW = 1758500000.0


def git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True)


class Harness:
    """임시 홈 + 임시 레포에 Codex 세션 하나를 심어 두는 픽스처."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        self.repo_root = os.path.realpath(self.repo)
        self.key = locate.repo_key(self.repo_root)
        self.state = locate.state_dir(self.key, home=self.home)

    def close(self):
        self.tmp.cleanup()

    def plant_codex_session(self, session_id="cx1", human="필드 경로부터 다시 확인해줘"):
        import time as _time

        stamp = _time.gmtime(NOW)
        directory = os.path.join(
            self.home, ".codex", "sessions", _time.strftime("%Y/%m/%d", stamp)
        )
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "rollout-{}.jsonl".format(session_id))
        rows = [
            {"timestamp": "2026-09-22T16:30:00.000Z", "ordinal": 0,
             "type": "session_meta",
             "payload": {"session_id": session_id, "cwd": self.repo_root}},
            {"timestamp": "2026-09-22T16:30:01.000Z", "ordinal": 1,
             "type": "response_item",
             "payload": {"type": "message", "role": "user", "id": "u1",
                         "content": [{"type": "input_text", "text": human}]}},
            {"timestamp": "2026-09-22T16:30:02.000Z", "ordinal": 2,
             "type": "response_item",
             "payload": {"type": "function_call", "name": "shell", "call_id": "c1",
                         "arguments": json.dumps({"command": ["pytest", "-q"]})}},
            {"timestamp": "2026-09-22T16:30:03.000Z", "ordinal": 3,
             "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "c1",
                         "output": json.dumps({"exit_code": 1, "output": "3 failed"})}},
        ]
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        ledger.append({"repo": self.key, "harness": "codex-cli",
                       "session": session_id, "event": "start", "epoch": NOW - 600,
                       "path": path, "cwd": self.repo_root}, home=self.home)
        return path


class TestCompute(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_handoff_body_is_produced_for_a_foreign_session(self):
        self.h.plant_codex_session()
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertIn("[omhc]", body)
        self.assertIn("필드 경로", body)
        self.assertLessEqual(len(body.encode("utf-8")), 900)

    def test_failed_command_appears_as_fail(self):
        self.h.plant_codex_session()
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertIn("FAIL", body)
        self.assertIn("pytest", body)

    def test_nothing_to_send_yields_empty(self):
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(body, "")

    def test_second_call_in_the_same_session_yields_empty(self):
        """SessionStart 훅은 한 세션에서 여러 번 발동한다."""
        self.h.plant_codex_session()
        first = brief.compute(my_harness="claude-code", my_session_id="me1",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        second = brief.compute(my_harness="claude-code", my_session_id="me1",
                               repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertTrue(first)
        self.assertEqual(second, "")

    def test_delivered_session_is_not_resent_to_a_new_session(self):
        self.h.plant_codex_session()
        brief.compute(my_harness="claude-code", my_session_id="me1",
                      repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        again = brief.compute(my_harness="claude-code", my_session_id="me2",
                              repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(again, "")

    def test_archive_is_built_as_a_side_effect(self):
        path = self.h.plant_codex_session()
        brief.compute(my_harness="claude-code", my_session_id="me1",
                      repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        pinned = os.path.join(self.h.state, "pinned", "cx1", "source.jsonl")
        idx = os.path.join(self.h.state, "index", "cx1.idx")
        self.assertTrue(os.path.exists(pinned), "하드링크가 없다")
        self.assertEqual(os.stat(path).st_ino, os.stat(pinned).st_ino)
        self.assertTrue(os.path.exists(idx), "색인이 없다")

    def test_notes_are_included(self):
        self.h.plant_codex_session()
        os.makedirs(self.h.state, exist_ok=True)
        with open(os.path.join(self.h.state, "notes.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("rollout 이 source of truth\n")
        body = brief.compute(my_harness="claude-code", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertIn("source of truth", body)

    def test_same_vendor_yields_empty(self):
        self.h.plant_codex_session()
        body = brief.compute(my_harness="codex-cli", my_session_id="me1",
                             repo_root=self.h.repo_root, home=self.h.home, now=NOW)
        self.assertEqual(body, "")


class TestRunHostileInputs(unittest.TestCase):
    """스펙 §16-5: 적대적 입력 5종에서 빈 stdout + exit 0."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def _run(self, stdin_text, argv=("--harness", "claude-code")):
        out = io.StringIO()
        code = brief.run(list(argv), stdin_text, home=self.h.home, now=NOW, out=out)
        return code, out.getvalue()

    def test_missing_ledger(self):
        code, text = self._run(json.dumps({"cwd": self.h.repo_root,
                                           "session_id": "me1"}))
        self.assertEqual((code, text), (0, ""))

    def test_ledger_with_a_corrupt_half_line(self):
        self.h.plant_codex_session()
        path = os.path.join(self.h.home, ".omhc", ledger.LEDGER_NAME)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"repo":"x","harn\n')
        code, text = self._run(json.dumps({"cwd": self.h.repo_root,
                                           "session_id": "me1"}))
        self.assertEqual(code, 0)
        self.assertIn("[omhc]", text)

    def test_nonexistent_transcript_path(self):
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "gone",
                       "event": "start", "epoch": NOW - 10,
                       "path": "/nope/missing.jsonl", "cwd": self.h.repo_root},
                      home=self.h.home)
        code, text = self._run(json.dumps({"cwd": self.h.repo_root,
                                           "session_id": "me1"}))
        self.assertEqual((code, text), (0, ""))

    def test_dev_null_transcript_path(self):
        ledger.append({"repo": self.h.key, "harness": "codex-cli", "session": "null",
                       "event": "start", "epoch": NOW - 10, "path": "/dev/null",
                       "cwd": self.h.repo_root}, home=self.h.home)
        code, text = self._run(json.dumps({"cwd": self.h.repo_root,
                                           "session_id": "me1"}))
        self.assertEqual((code, text), (0, ""))

    def test_broken_stdin_json(self):
        self.h.plant_codex_session()
        code, text = self._run("{not json at all")
        self.assertEqual(code, 0)

    def test_empty_stdin(self):
        self.h.plant_codex_session()
        code, _text = self._run("")
        self.assertEqual(code, 0)

    def test_missing_harness_argument_is_a_noop(self):
        self.h.plant_codex_session()
        code, text = self._run(json.dumps({"cwd": self.h.repo_root}), argv=())
        self.assertEqual((code, text), (0, ""))

    def test_unwritable_home_does_not_raise(self):
        self.h.plant_codex_session()
        out = io.StringIO()
        code = brief.run(["--harness", "claude-code"],
                         json.dumps({"cwd": self.h.repo_root, "session_id": "me1"}),
                         home="/proc/omhc-nonexistent", now=NOW, out=out)
        self.assertEqual(code, 0)


class TestRunWireShape(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_stdout_is_the_hook_wire_json(self):
        self.h.plant_codex_session()
        out = io.StringIO()
        code = brief.run(["--harness", "claude-code"],
                         json.dumps({"cwd": self.h.repo_root, "session_id": "me1"}),
                         home=self.h.home, now=NOW, out=out)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("[omhc]", payload["hookSpecificOutput"]["additionalContext"])

    def test_text_mode_prints_the_body_only(self):
        self.h.plant_codex_session()
        out = io.StringIO()
        brief.run(["--harness", "claude-code", "--text"],
                  json.dumps({"cwd": self.h.repo_root, "session_id": "me1"}),
                  home=self.h.home, now=NOW, out=out)
        self.assertTrue(out.getvalue().startswith("[omhc]"))

    def test_budget_flag_is_respected(self):
        self.h.plant_codex_session()
        out = io.StringIO()
        brief.run(["--harness", "claude-code", "--text", "--budget", "400"],
                  json.dumps({"cwd": self.h.repo_root, "session_id": "me1"}),
                  home=self.h.home, now=NOW, out=out)
        self.assertLessEqual(len(out.getvalue().encode("utf-8")), 400)

    def test_second_run_in_the_same_session_prints_nothing(self):
        self.h.plant_codex_session()
        payload = json.dumps({"cwd": self.h.repo_root, "session_id": "me1"})
        first, second = io.StringIO(), io.StringIO()
        brief.run(["--harness", "claude-code"], payload, home=self.h.home,
                  now=NOW, out=first)
        brief.run(["--harness", "claude-code"], payload, home=self.h.home,
                  now=NOW, out=second)
        self.assertTrue(first.getvalue())
        self.assertEqual(second.getvalue(), "")


class TestDeliver(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def bundle(self, to="claude-code"):
        return HandoffBundle(body_md="[omhc] hi\n", repo_root=self.h.repo_root,
                             to_adapter_id=to)

    def test_same_vendor_is_short_circuited(self):
        self.assertTrue(deliver.resume_instead("claude-code", "claude-code"))
        self.assertFalse(deliver.resume_instead("codex-cli", "claude-code"))

    def test_claude_delivery_writes_the_state_artifact(self):
        receipt = deliver.deliver(self.bundle(), home=self.h.home, now=NOW)
        self.assertEqual(receipt.channel, "sessionstart-hook")
        self.assertTrue(os.path.exists(receipt.paths_written[0]))

    def test_unknown_adapter_falls_through_to_the_file_drop(self):
        receipt = deliver.deliver(self.bundle(to="nope-cli"), home=self.h.home,
                                  now=NOW)
        self.assertEqual(receipt.channel, "file-drop")
        self.assertTrue(os.path.exists(receipt.paths_written[0]))

    def test_file_drop_path_shape(self):
        receipt = deliver.file_drop(self.bundle(), "because", now=NOW)
        path = receipt.paths_written[0]
        self.assertIn(os.path.join(".omhc", "outbox"), path)
        self.assertTrue(path.endswith("-to-claude-code.md"))
        with open(path, encoding="utf-8") as fh:
            self.assertIn("because", fh.read())

    def test_read_only_adapter_falls_through_to_the_file_drop(self):
        from omhc import adapters

        class ReadOnly:
            adapter_id = "ro-cli"
            capabilities = frozenset({Capability.READ})

            def __init__(self, *, home=None, now=None):
                pass

        adapters.REGISTRY["ro-cli"] = ReadOnly
        try:
            receipt = deliver.deliver(self.bundle(to="ro-cli"), home=self.h.home,
                                      now=NOW)
            self.assertEqual(receipt.channel, "file-drop")
            with open(receipt.paths_written[0], encoding="utf-8") as fh:
                self.assertIn("read-only", fh.read())
        finally:
            del adapters.REGISTRY["ro-cli"]

    def test_every_path_ends_in_a_receipt(self):
        for to in ("claude-code", "codex-cli", "nope-cli"):
            receipt = deliver.deliver(self.bundle(to=to), home=self.h.home, now=NOW)
            self.assertTrue(receipt.channel)
            self.assertTrue(receipt.paths_written)


if __name__ == "__main__":
    unittest.main()
