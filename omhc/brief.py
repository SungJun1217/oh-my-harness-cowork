from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Optional

from . import adapters, due, gate, index, locate, mint, pin
from .adapters import claude_code
from .adapter import SessionRef

GUARD_LOG = "guard.log"
NOTES_NAME = "notes.txt"
ARTIFACT_NAME = "omhc.txt"


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


REFS_NAME = "refs.tsv"


def _write_refs(state_dir: str, ref, tags) -> None:
    """태그 → (세션, 소스 경로, 오프셋, 길이). 900바이트 안에 세션 id 가 없어도
    `omhc show E1` 이 풀리는 근거다. 매 표식마다 다시 쓴다."""
    os.makedirs(state_dir, exist_ok=True)
    lines = [
        "\t".join((tag, ref.session_id, ref.source_path, str(ev.offset),
                   str(ev.length), str(ev.seq)))
        for tag, ev in tags
    ]
    path = os.path.join(state_dir, REFS_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        if lines:
            fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def read_refs(state_dir: str) -> dict:
    out = {}
    try:
        with open(os.path.join(state_dir, REFS_NAME), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 5:
                    out[parts[0]] = {
                        "session_id": parts[1], "source_path": parts[2],
                        "offset": int(parts[3]), "length": int(parts[4]),
                        "seq": int(parts[5]) if len(parts) > 5 else 0,
                    }
    except (OSError, ValueError):
        return out
    return out


# 와이어 형식은 하네스마다 다르다. 실측된 세 가지:
#   claude : {"hookSpecificOutput": {"hookEventName": "SessionStart",
#                                    "additionalContext": …}}
#   cursor : {"additional_context": …}            (snake_case)
#   sdk    : {"additionalContext": …}             (최상위, SDK 표준 / Copilot CLI)
#
# **세 형식을 동시에 내보내면 안 된다.** Claude Code 는 additional_context 와
# hookSpecificOutput 을 **중복 제거 없이 둘 다 읽으므로**(설치된 superpowers 훅의
# 주석에서 확인) 핸드오프가 두 번 주입된다 — 게이트로 막은 중복을 와이어 레벨에서
# 되살리는 셈이다.
#
# 환경변수로 플랫폼을 추측하지도 않는다. 우리가 하네스별 훅 설정을 직접 쓰므로
# 대상을 이미 알고 있고, 추측은 틀릴 수 있다(우리는 플러그인이 아니라 설정 훅이라
# CLAUDE_PLUGIN_ROOT 가 설정되지 않는다).
WIRE_BY_HARNESS = {
    "claude-code": "claude",
    "codex-cli": "sdk",
    "cursor-ide": "cursor",
}
DEFAULT_WIRE = "sdk"


def _fallback_ref(watermark, repo_root: str):
    """list_sessions 가 그 세션을 못 찾았을 때의 최후 수단.

    원장 경로를 그냥 신뢰하지 않고 트랜스크립트 머리를 다시 읽어 비대화형·
    서브체인 세션을 걸러낸다 — 필터를 우회하는 경로를 만들면 필터가 무의미해진다.
    """
    path = watermark.path
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    head = claude_code.head_of(path)
    if str(head.get("entrypoint") or "") in due.NON_INTERACTIVE:
        return []
    if head.get("sidechain") or head.get("agentId"):
        return []
    return [SessionRef(
        adapter_id=watermark.harness, session_id=watermark.session_id,
        source_path=path, cwd=repo_root, epoch=watermark.epoch,
        size=os.path.getsize(path),
    )]


def hook_wire(text: str, wire: str = "claude") -> str:
    """지정된 하나의 형식으로만 내보낸다."""
    if wire == "cursor":
        payload = {"additional_context": text}
    elif wire == "sdk":
        payload = {"additionalContext": text}
    else:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }
    return json.dumps(payload, ensure_ascii=False)


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

    adapter = adapters.get(watermark.harness, home=home)
    refs = [r for r in adapter.list_sessions(repo_root)
            if r.session_id == watermark.session_id]
    if not refs:
        # **폴백에 필터를 다시 적용해야 한다.** list_sessions 는 비대화형·서브체인
        # 세션을 걸러내는데, 원장 경로로 곧장 SessionRef 를 만들면 그 필터를
        # 우회해 남의 도구가 남긴 자동 세션을 사람의 작업으로 주입한다.
        refs = _fallback_ref(watermark, repo_root)
    if not refs:
        return ""

    ref = refs[0]
    read = adapter.read_session(ref)
    body = mint.mint(read, to_adapter_id=my_harness, budget=budget, now=stamp,
                     notes=_notes(state))
    if not body:
        # 보낼 것이 없으면 게이트를 쓰지 않는다. 첫 발동이 빈손으로 슬롯을
        # 태우면 밀리초 뒤에 데이터가 도착해도 그 세션은 영구히 못 받는다.
        return ""

    if not force and not gate.claim(state, my_harness, my_session_id):
        # 실측: SessionStart 훅이 한 세션에서 6회 발동했다.
        return ""

    # 아카이브는 표식을 만든 뒤에 만든다 — 실패해도 표식은 나가야 한다.
    try:
        pin.pin_session(state, ref)
        # watch.sweep 과 같은 증분 규칙을 쓴다. 전부 다시 덧붙이면 데몬이 돌고
        # 있을 때 같은 이벤트가 두 번 색인되어 `omhc log` 가 중복을 보이고
        # `omhc show #N` 이 낡은 행을 가리킬 수 있다.
        idx = os.path.join(state, "index", ref.session_id + ".idx")
        seen = index.last_seq(idx)
        index.append_rows(idx, [e for e in read.events if e.seq > seen])
        _write_refs(state, ref, mint.failure_tags(read))
    except OSError as exc:
        _log_failure(home, "archive failed: {}".format(exc))

    # 주입한 본문을 파일로도 남긴다. `cat ~/.omhc/<key>/omhc.txt` 로 무엇이
    # 들어갔는지 사람이 직접 확인하고 편집기로 고칠 수 있어야 한다.
    try:
        artifact = os.path.join(state, ARTIFACT_NAME)
        tmp = artifact + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, artifact)
    except OSError as exc:
        _log_failure(home, "artifact write failed: {}".format(exc))

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
    wire = ""
    args = list(argv)
    while args:
        token = args.pop(0)
        if token == "--harness" and args:
            harness = args.pop(0)
        elif token == "--wire" and args:
            wire = args.pop(0)
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
        chosen = wire or WIRE_BY_HARNESS.get(harness, DEFAULT_WIRE)
        stream.write(body if as_text else hook_wire(body, chosen) + "\n")
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
