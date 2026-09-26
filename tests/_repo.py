"""Single source of truth for the paths, fixtures, and builders tests share.

Hardcoding absolute paths makes `list_sessions()` return 0 results on a
different checkout or machine, and invariants that iterate over sessions
would then **PASS without running a single assertion** — the most dangerous
kind of pass, since the "no foreign material leaks" guarantee gets reported
as verified while nothing was actually checked.

For the same reason, the session fixture builders live only here too. Three
test modules used to build their own Codex rollouts and had already
diverged (one also wrote the ledger, one took a turn count, one had
neither). If Codex's date-directory layout or session_meta shape changes,
three places would need updating, and a missed copy keeps passing while
planting a file the adapter no longer reads.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

# tests/_repo.py → repo root
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIXTURES = os.path.join(REPO, "tests", "fixtures")
EXPECTED = os.path.join(FIXTURES, "expected.json")
CLAUDE_LIVE = os.path.join(FIXTURES, "claude", "live.jsonl")
CLAUDE_SUB = os.path.join(FIXTURES, "claude", "subagent.jsonl")
CODEX_EXEC = os.path.join(FIXTURES, "codex", "exec.jsonl")
CODEX_TOOLS = os.path.join(FIXTURES, "codex", "tools.jsonl")
CODEX_EDIT = os.path.join(FIXTURES, "codex", "edit.jsonl")

MISSING = "no fixtures. Run `python3 tests/harvest.py` first."


def have_fixtures(*paths) -> bool:
    return all(os.path.exists(p) for p in (paths or (EXPECTED, CLAUDE_LIVE, CODEX_EXEC)))


def load_expected() -> dict:
    with open(EXPECTED, encoding="utf-8") as fh:
        return json.load(fh)


def iter_json(path: str):
    """Yield (index, row). Skip malformed lines."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            try:
                yield i, json.loads(line)
            except ValueError:
                continue


def ref_for(adapter_id: str, path: str, *, session_id: str = "", cwd: str = REPO):
    """One SessionRef. Three modules each built their own, splitting the session_id convention three ways."""
    from omhc import adapter as A

    return A.SessionRef(
        adapter_id=adapter_id,
        session_id=session_id or os.path.basename(path).split(".")[0],
        source_path=path,
        cwd=cwd,
        epoch=0.0,
        size=os.path.getsize(path) if os.path.exists(path) else 0,
    )


def write_jsonl(rows, *, suffix: str = ".jsonl") -> str:
    """Create a temp JSONL and return its path. Caller unlinks it."""
    fh = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    with fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return fh.name


def git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True,
                   text=True)


# --- Codex rollout fixtures ------------------------------------------------------


def codex_meta_row(session_id: str, cwd: str, extra: dict = None) -> dict:
    payload = {"session_id": session_id, "cwd": cwd}
    if extra:
        payload.update(extra)
    return {
        "timestamp": "2026-09-22T16:30:00.000Z", "ordinal": 0,
        "type": "session_meta",
        "payload": payload,
    }


def codex_user_row(text: str, ordinal: int = 1) -> dict:
    return {
        "timestamp": "2026-09-22T16:30:01.000Z", "ordinal": ordinal,
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "id": "u{}".format(ordinal),
                    "content": [{"type": "input_text", "text": text}]},
    }


def codex_item_row(item: dict, ordinal: int) -> dict:
    return {
        "timestamp": "2026-09-22T16:30:03.000Z", "ordinal": ordinal,
        "type": "event_msg",
        "payload": {"type": "item_completed", "thread_id": "t", "turn_id": "u",
                    "item": item},
    }


def codex_shell_rows(command, ordinal: int = 2, failed: bool = False, cwd: str = REPO):
    """Observed shape (codex-cli 0.150.1-0.155.1, era B). One shell run leaves two records.

    response_item/custom_tool_call(name="exec") is a JS wrapper the model wrote, so it has
    neither the command nor the exit code. Those actually live in event_msg/item_completed's
    CommandExecution.
    """
    call_id = "c{}".format(ordinal)
    line = " ".join(command)
    return [
        {"timestamp": "2026-09-22T16:30:02.000Z", "ordinal": ordinal,
         "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec", "call_id": call_id,
                     "status": "completed",
                     "input": "text((await tools.exec_command({{cmd:{}}})).output);"
                              .format(json.dumps(line))}},
        codex_item_row({
            "type": "CommandExecution", "id": "exec-" + call_id,
            "command": ["/bin/bash", "-lc", line], "cwd": "file://" + cwd,
            "parsed_cmd": [{"type": "unknown", "cmd": line}],
            "status": "failed" if failed else "completed",
            "exit_code": 1 if failed else 0,
            "stdout": "3 failed" if failed else "", "stderr": "",
        }, ordinal + 1),
    ]


# --- era A (codex-cli 0.141-0.142) fixtures --------------------------------
# Shell is a function_call, output is plain text (not JSON) — a different
# envelope from codex_shell_rows(era B) above.


def _exec_output_text(status_line: str, output_body: str = "") -> str:
    return ("Chunk ID: c-abc\nWall time: 0.5 seconds\n{}\n"
            "Original token count: 10\nOutput:\n{}".format(status_line, output_body))


def codex_exec_command_rows(cmd, code=None, running_sid=None, workdir=None,
                            ordinal: int = 2):
    """A function_call name=exec_command + plain-text function_call_output pair.

    code and running_sid both None means abort (no output) —
    observed: one exec_command had no output at all (abort).
    """
    call_id = "ec{}".format(ordinal)
    line = " ".join(cmd) if isinstance(cmd, list) else cmd
    args = {"cmd": line}
    if workdir:
        args["workdir"] = workdir
    rows = [{"timestamp": "2026-09-22T16:30:02.000Z", "ordinal": ordinal,
             "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command",
                        "call_id": call_id, "arguments": json.dumps(args)}}]
    if code is not None:
        status_line = "Process exited with code {}".format(code)
    elif running_sid is not None:
        status_line = "Process running with session ID {}".format(running_sid)
    else:
        return rows  # abort: no output record.
    rows.append({"timestamp": "2026-09-22T16:30:03.000Z", "ordinal": ordinal + 1,
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": call_id,
                           "output": _exec_output_text(status_line)}})
    return rows


def codex_write_stdin_rows(sid, code, ordinal: int = 10):
    """A pair: send input to a backgrounded exec_command (session ID sid)
    and receive its result (exit code) back."""
    call_id = "ws{}".format(ordinal)
    return [
        {"timestamp": "2026-09-22T16:30:10.000Z", "ordinal": ordinal,
         "type": "response_item",
         "payload": {"type": "function_call", "name": "write_stdin",
                    "call_id": call_id,
                    "arguments": json.dumps({"session_id": sid, "chars": "\n"})}},
        {"timestamp": "2026-09-22T16:30:11.000Z", "ordinal": ordinal + 1,
         "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": call_id,
                    "output": _exec_output_text(
                        "Process exited with code {}".format(code))}},
    ]


def codex_apply_patch_rows(headers, with_filechange: bool = True, workdir=None,
                           ordinal: int = 20):
    """A custom_tool_call name=apply_patch (top-level input=raw patch text) +
    an optional FileChange item_completed with the same id."""
    call_id = "ap{}".format(ordinal)
    payload = {"type": "custom_tool_call", "name": "apply_patch", "call_id": call_id,
              "input": "\n".join(headers)}
    if workdir:
        payload["workdir"] = workdir
    rows = [{"timestamp": "2026-09-22T16:30:20.000Z", "ordinal": ordinal,
             "type": "response_item", "payload": payload}]
    if with_filechange:
        changes = {h.split(": ", 1)[1]: {"type": "update", "unified_diff": ""}
                  for h in headers if ": " in h}
        rows.append(codex_item_row({
            "type": "FileChange", "id": call_id, "status": "completed",
            "changes": changes,
        }, ordinal + 1))
    return rows


def codex_spawn_agent_row(task_name: str, message: str, ordinal: int = 30) -> dict:
    return {"timestamp": "2026-09-22T16:30:30.000Z", "ordinal": ordinal,
            "type": "response_item",
            "payload": {"type": "function_call", "name": "spawn_agent",
                       "call_id": "sa{}".format(ordinal),
                       "arguments": json.dumps({
                           "task_name": task_name, "agent_type": "worker",
                           "message": message, "fork_turns": []})}}


def plant_codex(
    home: str,
    cwd: str,
    *,
    session_id: str = "cx1",
    human: str = "필드 경로부터 다시 확인해줘",
    shell_turns: int = 0,
    failing_shell: bool = False,
    ledger_home: str = "",
    when: float = 0.0,
    meta_extra: dict = None,
) -> str:
    """Plant one Codex rollout in a temp home. Return its path.

    Passing ledger_home also writes a start row to the ledger — needed by tests
    that exercise the brief/due path. meta_extra is merged as-is into
    session_meta.payload (e.g. tests planting a subagent marker).
    """
    stamp = time.gmtime(when or time.time())
    directory = os.path.join(home, ".codex", "sessions",
                             time.strftime("%Y/%m/%d", stamp))
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "rollout-{}.jsonl".format(session_id))

    rows = [codex_meta_row(session_id, cwd, meta_extra), codex_user_row(human)]
    for i in range(shell_turns):
        fails = failing_shell and i == 0
        # The failing turn uses a meaningful command — the FAIL slot assertion checks that string.
        command = ["pytest", "-q"] if fails else ["ls", str(i)]
        rows.extend(codex_shell_rows(command, ordinal=2 + i * 2, failed=fails))
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    if ledger_home:
        from omhc import ledger, locate

        ledger.append({"repo": locate.repo_key(cwd), "harness": "codex-cli",
                       "session": session_id, "event": "start",
                       "epoch": (when or time.time()) - 600,
                       "path": path, "cwd": cwd}, home=ledger_home)
    return path


def append_codex_turn(path: str, ordinal: int = 90) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for row in codex_shell_rows(["echo", str(ordinal)], ordinal=ordinal):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_codex_user_turn(path: str, text: str, ordinal: int = 90) -> None:
    """A single user message row shaped like what `codex exec resume <id> "<text>"`
    appends to the same rollout (#27). `text=""` reproduces an empty-prompt resume."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(codex_user_row(text, ordinal=ordinal), ensure_ascii=False) + "\n")


def plant_hook_install(home: str, adapter_id: str) -> None:
    """Plant `adapter_id`'s SessionStart hook in a temp home, verbatim from the real install fragment.

    Pins the `<adapter-id> hooks` row that `omhc status` now judges via hookconf
    to PASS in tests that aren't concerned with this unit — also plants a dummy
    executable at `$HOME/.local/bin/omhc` so the binary-executable check passes too.
    """
    from omhc import hookconf

    fragment_name = {
        "claude-code": "claude-settings.fragment.json",
        "codex-cli": "codex-hooks.json",
    }[adapter_id]
    config_path = {
        "claude-code": os.path.join(home, ".claude", "settings.json"),
        "codex-cli": os.path.join(home, ".codex", "hooks.json"),
    }[adapter_id]
    fragment = hookconf.load_fragment(fragment_name)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump({"hooks": fragment}, fh)

    bin_path = os.path.join(home, ".local", "bin", "omhc")
    os.makedirs(os.path.dirname(bin_path), exist_ok=True)
    with open(bin_path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(bin_path, 0o755)


class TempRepo:
    """A temp home + git repo pair. Was duplicated across four setUp methods.

    Usage:
        self.t = TempRepo(); self.addCleanup(self.t.close)
        self.t.home / self.t.root / self.t.state / self.t.env
    """

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self._tmp.name, "home")
        self.repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        self.root = os.path.realpath(self.repo)
        self.env = dict(os.environ, HOME=self.home)
        self.env.pop("OMHC_OFF", None)

    @property
    def key(self) -> str:
        from omhc import locate

        return locate.repo_key(self.root)

    @property
    def state(self) -> str:
        from omhc import locate

        return locate.state_dir(self.key, home=self.home)

    def plant_codex(self, **kw) -> str:
        kw.setdefault("cwd", self.root)
        return plant_codex(self.home, **kw)

    def close(self) -> None:
        self._tmp.cleanup()
