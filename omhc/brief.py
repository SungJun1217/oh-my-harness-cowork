from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Optional

from . import adapters, due, gate, index, locate, mint, pin
from .adapter import SessionRef

GUARD_LOG = "guard.log"
NOTES_NAME = "notes.txt"


def _log_failure(home: Optional[str], detail: str) -> None:
    """실패를 남기되 절대 던지지 않는다. 훅 경로에서 죽으면 세션 시작이 깨진다."""
    try:
        root = os.path.join(home or os.path.expanduser("~"), ".omhc")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, GUARD_LOG), "a", encoding="utf-8") as fh:
            fh.write("--- {}\n{}\n".format(time.strftime("%Y-%m-%dT%H:%M:%SZ"), detail))
    except OSError:
        pass


def _notes(state_dir: str, limit: int = 2) -> list:
    try:
        with open(os.path.join(state_dir, NOTES_NAME), encoding="utf-8",
                  errors="replace") as fh:
            lines = [line.strip() for line in fh if line.strip()]
    except OSError:
        return []
    return lines[-limit:]


def hook_wire(text: str) -> str:
    """Claude Code / Codex 가 세션 컨텍스트로 받아들이는 모양."""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        },
        ensure_ascii=False,
    )


def compute(
    *,
    my_harness: str,
    my_session_id: str,
    repo_root: str,
    home: Optional[str] = None,
    now: Optional[float] = None,
    budget: int = mint.BUDGET,
    force: bool = False,
) -> str:
    """전달할 표식 본문. 보낼 것이 없으면 빈 문자열.

    이 함수는 예외를 던질 수 있다 — 호출자(run)가 감싼다. 테스트는 여기를 직접
    불러 실패를 볼 수 있어야 한다.
    """
    stamp = time.time() if now is None else now
    key = locate.repo_key(repo_root)
    state = locate.state_dir(key, home=home)

    watermark = due.due(key, my_harness, my_session_id, stamp, home=home)
    if watermark is None:
        return ""

    if not force and not gate.claim(state, my_harness, my_session_id):
        # 실측: SessionStart 훅이 한 세션에서 6회 발동했다.
        return ""

    adapter = adapters.get(watermark.harness, home=home)
    refs = [r for r in adapter.list_sessions(repo_root)
            if r.session_id == watermark.session_id]
    if not refs and watermark.path and os.path.exists(watermark.path):
        refs = [SessionRef(
            adapter_id=watermark.harness, session_id=watermark.session_id,
            source_path=watermark.path, cwd=repo_root, epoch=watermark.epoch,
            size=os.path.getsize(watermark.path),
        )]
    if not refs:
        return ""

    ref = refs[0]
    read = adapter.read_session(ref)
    body = mint.mint(read, to_adapter_id=my_harness, budget=budget, now=stamp,
                     notes=_notes(state))
    if not body:
        return ""

    # 아카이브는 표식을 만든 뒤에 만든다 — 실패해도 표식은 나가야 한다.
    try:
        pin.pin_session(state, ref)
        index.append_rows(os.path.join(state, "index", ref.session_id + ".idx"),
                          read.events)
    except OSError as exc:
        _log_failure(home, "archive failed: {}".format(exc))

    due.mark_delivered(state, watermark, to_harness=my_harness, epoch=stamp)
    return body


def run(argv, stdin_text: str = "", *, home: Optional[str] = None,
        now: Optional[float] = None, out=None) -> int:
    """훅 진입점. **절대 예외를 던지지 않고, 실패하면 빈 stdout + exit 0.**

    세션 시작을 깨뜨리는 것이 이 도구의 최악 결과다. 아무것도 주입하지 못하는 것은
    그에 비해 아무 일도 아니다.
    """
    harness = ""
    budget = mint.BUDGET
    force = False
    as_text = False
    args = list(argv)
    while args:
        token = args.pop(0)
        if token == "--harness" and args:
            harness = args.pop(0)
        elif token == "--budget" and args:
            try:
                budget = int(args.pop(0))
            except ValueError:
                budget = mint.BUDGET
        elif token == "--force":
            force = True
        elif token in ("--text", "--dry-run"):
            as_text = True
    if not harness:
        return 0

    stream = sys.stdout if out is None else out
    try:
        session_id = gate.session_id_from_hook_payload(stdin_text) or ""
        payload_cwd = ""
        if stdin_text:
            try:
                payload = json.loads(stdin_text)
                if isinstance(payload, dict):
                    payload_cwd = str(payload.get("cwd") or "")
            except ValueError:
                payload_cwd = ""
        repo_root = locate.resolve_repo_root(payload_cwd or None)
        body = compute(
            my_harness=harness,
            my_session_id=session_id,
            repo_root=repo_root,
            home=home,
            now=now,
            budget=budget,
            force=force,
        )
        if not body:
            return 0
        if len(body.encode("utf-8")) > budget:
            # 출력 직전 재검사. 버그가 과대 페이로드를 주입하지 못하게 한다.
            _log_failure(home, "body exceeded budget at print time; suppressed")
            return 0
        stream.write(body if as_text else hook_wire(body) + "\n")
        return 0
    except Exception:
        _log_failure(home, traceback.format_exc())
        return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdin_text = ""
    if not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read()
        except Exception:
            stdin_text = ""
    return run(argv, stdin_text)
