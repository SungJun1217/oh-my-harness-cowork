from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Optional

from . import adapters, deliver, due, fsio, gate, index, locate, mint, pin
from .adapter import HandoffBundle

GUARD_LOG = "guard.log"
NOTES_NAME = "notes.txt"
ARTIFACT_NAME = "omhc.txt"


def _log_failure(home: Optional[str], detail: str) -> None:
    """실패를 남기되 절대 던지지 않는다. 훅 경로에서 죽으면 세션 시작이 깨진다."""
    try:
        root = locate.omhc_root(home)
        os.makedirs(root, exist_ok=True)
        # 경로에 UTF-8 이 아닌 파일명이 섞이면(서로게이트) 쓰기가 UnicodeEncodeError
        # (ValueError)로 터진다. 로그 한 줄 때문에 전달이 끊기면 안 된다.
        with open(os.path.join(root, GUARD_LOG), "a", encoding="utf-8",
                  errors="backslashreplace") as fh:
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


# 세 형식을 동시에 내보내면 안 된다. Claude Code 는 additional_context 와
# hookSpecificOutput 을 중복 제거 없이 둘 다 읽으므로(설치된 superpowers 훅의
# 주석에서 확인) 핸드오프가 두 번 주입된다 — 게이트로 막은 중복을 와이어 레벨에서
# 되살리는 셈이다. 어느 형식을 쓰는지는 **어댑터가 선언한다**(adapter.wire).
DEFAULT_WIRE = "sdk"


def _ref_for(adapter, watermark, repo_root: str):
    """핸드오프할 세션 하나를 고른다. 원장이 기록한 경로를 **먼저** 쓴다.

    실측: list_sessions 는 이 머신에서 130개 파일 34.3MB 를 읽어 1건을 남겼고,
    그것만 249ms — 훅 예산 150ms 의 1.7배다. 원장 행에 그 파일의 경로가 이미
    적혀 있으므로 스캔 없이 바로 열면 된다.

    적격성 판정은 **어댑터가 소유한다**. 코어가 Claude 의 파서로 Codex rollout 을
    판정하면 찾는 필드가 없어 필터가 조용히 no-op 가 된다.
    """
    if watermark.path:
        ref = adapter.ref_for_path(watermark.path, watermark.session_id, repo_root)
        if ref is not None:
            return [ref]
    # 원장에 경로가 없거나 그 파일이 사라졌을 때만 전체 스캔으로 떨어진다.
    return [r for r in adapter.list_sessions(repo_root)
            if r.session_id == watermark.session_id]


def _wire_for(harness: str, home: Optional[str]) -> str:
    """이 하네스의 주입 형식. 어댑터가 선언한 것을 그대로 쓴다."""
    try:
        return getattr(adapters.get(harness, home=home), "wire", DEFAULT_WIRE)
    except Exception:
        return DEFAULT_WIRE


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
    refs = _ref_for(adapter, watermark, repo_root)
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
        pin_result = pin.pin_session_result(state, ref)
        if not pin_result.linked:
            # 조용히 넘기면 `omhc status` 의 archive 행이 핀 없이도 PASS 를
            # 낸다(리뷰 결함) — 훅 경로의 유일한 실패 로그에 남겨야 사람이
            # 원인을 알 수 있다. 여기서 던지면 안 되므로(invariant 2) 로그만.
            _log_failure(home, "pin failed: {}".format(pin_result.error))
        # watch.sweep 과 같은 증분 규칙을 쓴다. 전부 다시 덧붙이면 데몬이 돌고
        # 있을 때 같은 이벤트가 두 번 색인되어 `omhc log` 가 중복을 보이고
        # `omhc show #N` 이 낡은 행을 가리킬 수 있다.
        idx = os.path.join(state, "index", ref.session_id + ".idx")
        seen = index.last_seq(idx)
        index.append_rows(idx, [e for e in read.events if e.seq > seen])
        index.write_refs(state, ref, mint.failure_tags(read))
    except OSError as exc:
        _log_failure(home, "archive failed: {}".format(exc))

    # 전달은 deliver 가 라우팅한다. 여기서 파일을 직접 쓰면 채널 추상이 프로덕션
    # 경로를 우회해, receipt·Path B·보편 바닥이 단위 테스트에서만 동작한다.
    # 본문을 파일로도 남기는 것은 그 첫 채널의 일이다 —
    # `cat ~/.omhc/<key>/omhc.txt` 로 무엇이 들어갔는지 확인할 수 있어야 한다.
    try:
        receipt = deliver.deliver(
            HandoffBundle(body_md=body, repo_root=repo_root, to_adapter_id=my_harness),
            home=home, now=stamp,
        )
        if receipt.channel == "nowhere":
            _log_failure(home, "delivery found no channel: " + receipt.cleanup_hint)
    except Exception as exc:  # deliver 는 던지지 않아야 하지만 훅을 깨뜨릴 수는 없다
        _log_failure(home, "delivery failed: {}".format(exc))

    due.mark_delivered(state, watermark, to_harness=my_harness, epoch=stamp)
    return body


def emit(
    *,
    harness: str,
    stdin_text: str = "",
    budget: int = mint.BUDGET,
    wire: str = "",
    force: bool = False,
    as_text: bool = False,
    home: Optional[str] = None,
    now: Optional[float] = None,
    out=None,
) -> int:
    """훅 진입점. **절대 예외를 던지지 않고, 실패하면 빈 stdout + exit 0.**

    세션 시작을 깨뜨리는 것이 이 도구의 최악 결과다. 아무것도 주입하지 못하는 것은
    그에 비해 아무 일도 아니다.

    argv 를 다시 파싱하지 않는다. 이전에는 cli 가 argparse 결과를 문자열 목록으로
    되직렬화하고 여기서 손으로 만든 파서가 다시 읽었다 — 명령 표면이 두 깊이에
    정의돼 두 파서의 기본값을 손으로 맞춰야 했고, 손 파서는 모르는 토큰을 조용히
    무시했다.
    """
    if not harness:
        return 0
    stream = sys.stdout if out is None else out
    try:
        payload = gate.hook_payload(stdin_text)
        session_id = gate.session_id_from_payload(payload) or ""
        repo_root = locate.resolve_repo_root(str(payload.get("cwd") or "") or None)
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
        chosen = wire or _wire_for(harness, home)
        stream.write(body if as_text else hook_wire(body, chosen) + "\n")
        return 0
    except Exception:
        _log_failure(home, traceback.format_exc())
        return 0
