from __future__ import annotations

import os
import time
from typing import Optional

from . import adapters, fsio
from .adapter import (
    AdapterUnavailable,
    Capability,
    HandoffBundle,
    InstallReceipt,
    NoInjectionChannel,
)

OUTBOX_DIR = os.path.join(".omhc", "outbox")


def _iso(epoch: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


def file_drop(bundle: HandoffBundle, why: str, *, now: float) -> InstallReceipt:
    """보편 바닥. 주입 경로가 전부 막혀도 사람이 읽을 파일은 남는다.

    **이 함수도 예외를 던지지 않는다.** 레포 루트가 읽기 전용(CI 체크아웃,
    root 소유 마운트, 디스크 꽉 찬 경우)이면 바닥마저 무너지는데, deliver() 의
    '절대 던지지 않는다' 계약은 정확히 그 지점에서 깨져선 안 된다. 쓸 수 없으면
    그 사실을 receipt 에 담아 돌려준다.
    """
    path = ""
    try:
        directory = os.path.join(bundle.repo_root, OUTBOX_DIR)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(
            directory, "{}-to-{}.md".format(_iso(now), bundle.to_adapter_id)
        )
        fsio.write_atomic(
            path, "<!-- omhc file drop: {} -->\n{}".format(why, bundle.body_md)
        )
    except OSError as exc:
        return InstallReceipt(
            channel="nowhere",
            paths_written=(),
            consumed_on_read=False,
            cleanup_hint="{}; file drop also failed: {}".format(why, exc),
        )
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
    allow_fallbacks: bool = True,
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

    # 채널 목록은 어댑터의 속성이다. 라우터에 벤더 문자열이 하나라도 있으면
    # "어댑터 추가는 파일 하나 + 픽스처 하나" 가 거짓이 된다.
    channels = [adapter.install_handoff]
    if allow_fallbacks:
        try:
            channels.extend(getattr(adapter, "fallback_channels", tuple)() or ())
        except Exception:
            pass

    reasons = []
    for channel in channels:
        try:
            return channel(bundle)
        except NoInjectionChannel as exc:
            reasons.append("no channel: {}".format(exc))
        except Exception as exc:  # 어댑터의 나쁜 하루가 세션 시작을 깨뜨리면 안 된다
            reasons.append("{}: {}".format(type(exc).__name__, exc))

    return file_drop(bundle, "; ".join(reasons) or "no channels declared", now=stamp)
