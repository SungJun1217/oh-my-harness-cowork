"""테스트가 공유하는 경로·픽스처·빌더의 단일 정의.

절대 경로를 하드코딩하면 다른 체크아웃·다른 머신에서 `list_sessions()` 가 0건을
돌려주고, 세션을 순회하는 불변식들이 **단정을 하나도 실행하지 않은 채 PASS** 가
된다 — "외래 물질이 새지 않는다" 는 보장이 검증됐다고 보고되면서 실제로는 아무것도
검사되지 않는, 가장 위험한 종류의 통과다.

같은 이유로 세션 픽스처 빌더도 여기 하나만 둔다. 세 테스트 모듈이 각자 Codex
rollout 을 만들고 있었고 이미 갈라져 있었다(한쪽은 원장까지 쓰고, 한쪽은 턴 수를
받고, 한쪽은 둘 다 없었다). Codex 의 날짜 디렉터리 구조나 session_meta 모양이
바뀌면 세 곳을 찾아야 하고, 놓친 사본은 어댑터가 더 이상 읽지 않는 파일을 심으면서
계속 통과한다.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

# tests/_repo.py → 레포 루트
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIXTURES = os.path.join(REPO, "tests", "fixtures")
EXPECTED = os.path.join(FIXTURES, "expected.json")
CLAUDE_LIVE = os.path.join(FIXTURES, "claude", "live.jsonl")
CLAUDE_SUB = os.path.join(FIXTURES, "claude", "subagent.jsonl")
CODEX_EXEC = os.path.join(FIXTURES, "codex", "exec.jsonl")
CODEX_TOOLS = os.path.join(FIXTURES, "codex", "tools.jsonl")
CODEX_EDIT = os.path.join(FIXTURES, "codex", "edit.jsonl")

MISSING = "픽스처가 없다. `python3 tests/harvest.py` 를 먼저 실행하라."


def have_fixtures(*paths) -> bool:
    return all(os.path.exists(p) for p in (paths or (EXPECTED, CLAUDE_LIVE, CODEX_EXEC)))


def load_expected() -> dict:
    with open(EXPECTED, encoding="utf-8") as fh:
        return json.load(fh)


def iter_json(path: str):
    """(index, row) 를 yield 한다. 깨진 줄은 건너뛴다."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            try:
                yield i, json.loads(line)
            except ValueError:
                continue


def ref_for(adapter_id: str, path: str, *, session_id: str = "", cwd: str = REPO):
    """SessionRef 하나. 세 모듈이 각자 만들면서 session_id 규약이 셋으로 갈렸다."""
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
    """임시 JSONL 을 만들고 경로를 돌려준다. 호출자가 unlink 한다."""
    fh = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    with fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return fh.name


def git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True,
                   text=True)


# --- Codex rollout 픽스처 ------------------------------------------------------


def codex_meta_row(session_id: str, cwd: str) -> dict:
    return {
        "timestamp": "2026-09-22T16:30:00.000Z", "ordinal": 0,
        "type": "session_meta",
        "payload": {"session_id": session_id, "cwd": cwd},
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
    """실측 모양(codex-cli 0.156.1). 한 번의 셸 실행은 레코드 두 개를 남긴다.

    response_item/custom_tool_call(name="exec") 은 모델이 쓴 JS 래퍼라 명령도
    종료 코드도 없다. 사실은 event_msg/item_completed 의 CommandExecution 에 있다.
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
) -> str:
    """임시 홈에 Codex rollout 하나를 심는다. 경로를 돌려준다.

    ledger_home 을 주면 원장에 start 행도 남긴다 — brief/due 경로를 태우는 테스트가
    필요로 한다.
    """
    stamp = time.gmtime(when or time.time())
    directory = os.path.join(home, ".codex", "sessions",
                             time.strftime("%Y/%m/%d", stamp))
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "rollout-{}.jsonl".format(session_id))

    rows = [codex_meta_row(session_id, cwd), codex_user_row(human)]
    for i in range(shell_turns):
        fails = failing_shell and i == 0
        # 실패 턴은 의미 있는 명령을 쓴다 — FAIL 슬롯 단정이 그 문자열을 본다.
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


class TempRepo:
    """임시 홈 + git 레포 한 쌍. setUp 네 곳에 복제돼 있던 것.

    사용:
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
