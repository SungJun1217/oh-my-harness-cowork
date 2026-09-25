from __future__ import annotations

import os
import time
from typing import List, Optional

from . import adapters, fsio
from .adapter import (
    AdapterUnavailable,
    Capability,
    HandoffBundle,
    InstallReceipt,
    NoInjectionChannel,
)

OMHC_DIR = ".omhc"
OUTBOX_DIR = os.path.join(OMHC_DIR, "outbox")
# 오래된 outbox 파일이 무한히 쌓이지 않도록 cmd_mark 가 이 나이를 넘긴 것만
# 지운다(#36). AGENTS.md 구간의 STALE_AFTER_SECONDS 와 값은 같지만 의미가
# 다르다 — 저건 "지시로 안 읽히게"이고 이건 "디스크에 안 쌓이게"다.
OUTBOX_TTL_SECONDS = 24 * 3600
# omhc 가 만든 outbox 파일만 지운다 — 이 헤더로 자기 것을 알아본다.
FILE_DROP_HEADER_PREFIX = "<!-- omhc file drop"


def _iso(epoch: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


def _iso_readable(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _safe_header_text(text: str) -> str:
    """헤더 HTML 주석 안에 안전하게 넣을 수 있는 한 줄로 만든다.

    `why` 는 예외 메시지·경로를 그대로 담을 수 있어 임의 텍스트다 — `-->` 가
    섞이면 주석이 거기서 끝나고 그 뒤의 `captured=`/`captured_utc=` 가
    본문으로 새 버린다(리뷰 #3). 개행도 접어서 헤더가 항상 한 줄(따라서 항상
    첫 줄)이도록 한다 — `_is_own_outbox_file` 이 첫 줄만 본다."""
    return text.replace("-->", "--&gt;").replace("\n", " ").replace("\r", " ")


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
        # git status 에 `.omhc/` 가 새지 않도록 등재를 시도한다(#36 리뷰 #1:
        # `.omhc/` 를 방금 만들었을 때만 시도하면, 이미 outbox 가 있던
        # 기존 사용자는 영영 등재되지 않는다 — 매 file drop 마다 시도하되,
        # register_outbox_exclude 자신이 이미 등재됐으면 파일 한 번 읽는
        # 것만으로 끝낸다(invariant 2: 실패해도 file drop 자체는 계속된다)).
        try:
            from . import agents_md

            agents_md.register_outbox_exclude(bundle.repo_root)
        except Exception:
            pass
        path = os.path.join(
            directory, "{}-to-{}.md".format(_iso(now), bundle.to_adapter_id)
        )
        header = '{}: {} captured="{:.0f}" captured_utc="{}" -->'.format(
            FILE_DROP_HEADER_PREFIX, _safe_header_text(why), now, _iso_readable(now))
        fsio.write_atomic(path, "{}\n{}".format(header, bundle.body_md))
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


def _is_own_outbox_file(path: str) -> bool:
    """`file_drop` 이 만든 파일인가. 이름 규칙과 헤더를 **둘 다** 본다 —
    이름만 보면(예: `*-to-*.md`) 사람이 우연히 같은 이름으로 둔 파일도
    지울 수 있다."""
    name = os.path.basename(path)
    if not (name.endswith(".md") and "-to-" in name):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return False
    return first.startswith(FILE_DROP_HEADER_PREFIX)


def prune_outbox(repo_root: str, *, now: float, ttl: Optional[float] = OUTBOX_TTL_SECONDS,
                 force: bool = False) -> List[str]:
    """omhc 가 쓴 outbox 파일을 지운다. `cmd_mark`(훅 경로, ttl 초과분만) 와
    `omhc clear`(force=True, 전부) 가 공유한다. 지운 경로들을 돌려준다.

    `.omhc/outbox/` 가 아예 없으면(아직 file drop 을 한 번도 안 했다) 조용히
    아무것도 하지 않는다 — 훅 경로에서 매번 없는 디렉터리를 만들 이유가 없다.
    """
    directory = os.path.join(repo_root, OUTBOX_DIR)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    removed = []
    for name in names:
        path = os.path.join(directory, name)
        if not _is_own_outbox_file(path):
            continue
        if not force and ttl is not None:
            try:
                age = now - os.stat(path).st_mtime
            except OSError:
                continue
            if age <= ttl:
                continue
        try:
            os.unlink(path)
        except OSError:
            continue
        removed.append(path)
    return removed
