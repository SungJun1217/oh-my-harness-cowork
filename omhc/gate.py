from __future__ import annotations

import json
import os
from typing import Optional

from . import fsio


def _gate_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "gate")


def claim(state_dir: str, adapter_id: str, session_id: str) -> bool:
    """True exactly once per (adapter_id, session_id).

    Measured: the SessionStart hook fired **6 times** within one session
    (alongside PreToolUse 39, PostToolUse 39, Stop 18, UserPromptSubmit 11).
    Without a gate, the same handoff would land in the context window six
    times.

    O_CREAT|O_EXCL is atomic within a filesystem, so even if several hooks
    fire concurrently only one passes. The core owns this — it's not left to
    adapters.
    """
    if not session_id:
        return False
    path = os.path.join(_gate_dir(state_dir),
                        "{}.{}".format(adapter_id, session_id))
    return fsio.claim_exclusive(path)


def hook_payload(raw: str) -> dict:
    """Parses hook stdin exactly once. Any failure yields an empty dict.

    This try/except/isinstance dance was duplicated in three places, so the
    answer to "what happens on bad stdin" lived in three places too.
    """
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def session_id_from_payload(payload: dict) -> Optional[str]:
    """Best-effort extraction of the session id.

    The key differs per harness and may be absent. Falls back to the
    transcript_path's filename — Claude Code's filename is the session uuid.
    """
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


def session_id_from_hook_payload(raw: str) -> Optional[str]:
    """Session id straight from raw stdin. Convenience for one-shot callers."""
    return session_id_from_payload(hook_payload(raw))
