from __future__ import annotations

import os
import time
from typing import Optional

from . import adapters, agents_md
from .adapter import (
    AdapterUnavailable,
    Capability,
    HandoffBundle,
    InstallReceipt,
    NoInjectionChannel,
)

OUTBOX_DIR = os.path.join(".omhc", "outbox")


def resume_instead(source_adapter_id: str, to_adapter_id: str) -> bool:
    """같은 벤더면 파이프라인을 단축한다.

    `claude --resume` 은 무손실이고 thinking 블록까지 보존한다. 우리 요약은 그보다
    **열등하다**. 같은 벤더 안에서 이 도구를 쓰는 것은 손해다.
    """
    return source_adapter_id == to_adapter_id


def _iso(epoch: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


def file_drop(bundle: HandoffBundle, why: str, *, now: float) -> InstallReceipt:
    """보편 바닥. 주입 경로가 전부 막혀도 사람이 읽을 파일은 남는다.

    모든 경로가 receipt 로 끝나며 조용히 사라지지 않는다.
    """
    directory = os.path.join(bundle.repo_root, OUTBOX_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(
        directory, "{}-to-{}.md".format(_iso(now), bundle.to_adapter_id)
    )
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("<!-- omhc file drop: {} -->\n".format(why))
        fh.write(bundle.body_md)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return InstallReceipt(
        channel="file-drop",
        paths_written=(path,),
        consumed_on_read=False,
        cleanup_hint="rm {}".format(path),
    )


def deliver(
    bundle: HandoffBundle,
    *,
    home: Optional[str] = None,
    now: Optional[float] = None,
    allow_agents_md: bool = True,
) -> InstallReceipt:
    """핸드오프를 라우팅한다. **절대 예외를 던지지 않는다.**

    순서: 어댑터의 install_handoff → (Codex 면) AGENTS.md Path B → 보편 바닥.
    """
    stamp = time.time() if now is None else now
    try:
        adapter = adapters.get(bundle.to_adapter_id, home=home)
    except AdapterUnavailable as exc:
        return file_drop(bundle, "unknown adapter: {}".format(exc), now=stamp)

    if Capability.WRITE not in getattr(adapter, "capabilities", frozenset()):
        return file_drop(bundle, "adapter is read-only", now=stamp)

    try:
        return adapter.install_handoff(bundle)
    except NoInjectionChannel as exc:
        reason = "no injection channel: {}".format(exc)
    except Exception as exc:  # 어댑터의 나쁜 하루가 세션 시작을 깨뜨리면 안 된다
        reason = "adapter raised {}: {}".format(type(exc).__name__, exc)

    if allow_agents_md and bundle.to_adapter_id == "codex-cli":
        try:
            return agents_md.install(bundle, now=stamp)
        except Exception as exc:
            reason += "; agents-md failed: {}".format(exc)

    return file_drop(bundle, reason, now=stamp)
