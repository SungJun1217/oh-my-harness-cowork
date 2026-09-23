from __future__ import annotations

import json
import os
from typing import Optional

from . import fsio


def _gate_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "gate")


def claim(state_dir: str, adapter_id: str, session_id: str) -> bool:
    """(adapter_id, session_id) 당 정확히 한 번만 True.

    실측: SessionStart 훅이 한 세션 안에서 **6회** 발동했다(PreToolUse 39,
    PostToolUse 39, Stop 18, UserPromptSubmit 11 과 함께). 게이트가 없으면 같은
    핸드오프가 한 컨텍스트 창에 여섯 번 들어간다.

    O_CREAT|O_EXCL 은 같은 파일시스템 안에서 원자적이므로 훅이 동시에 여러 개
    떠도 하나만 통과한다. 코어가 소유하며 어댑터에 맡기지 않는다.
    """
    if not session_id:
        return False
    path = os.path.join(_gate_dir(state_dir),
                        "{}.{}".format(adapter_id, session_id))
    return fsio.claim_exclusive(path)


def session_id_from_hook_payload(raw: str) -> Optional[str]:
    """훅 stdin JSON 에서 세션 id 를 최선으로 꺼낸다.

    하네스마다 키가 다르고 없을 수도 있다. 없으면 transcript_path 의 파일명에서
    끌어낸다 — Claude Code 는 파일명이 세션 uuid 다.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("session_id", "sessionId", "thread_id", "threadId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    path = payload.get("transcript_path") or payload.get("transcriptPath")
    if isinstance(path, str) and path:
        name = os.path.basename(path)
        if name.endswith(".jsonl"):
            name = name[: -len(".jsonl")]
        return name or None
    return None
