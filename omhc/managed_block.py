from __future__ import annotations

import os
import re
from typing import Optional

from . import fsio

MARKER_ID = "omhc"
BEGIN_PREFIX = "<!-- {}:begin".format(MARKER_ID)
END = "<!-- {}:end -->".format(MARKER_ID)

# 24시간. 지나면 붕괴시킨다 — 어제의 표식이 오늘의 지시처럼 읽히면 안 된다.
STALE_AFTER_SECONDS = 24 * 3600

_BLOCK = re.compile(
    re.escape(BEGIN_PREFIX) + r'\s+captured="(?P<captured>[0-9.]+)"\s*-->'
    r".*?" + re.escape(END) + r"\n?",
    re.S,
)


def _begin(captured_at: float) -> str:
    return '{} captured="{:.0f}" -->'.format(BEGIN_PREFIX, captured_at)


def _neutralize(body: str) -> str:
    """본문이 마커를 담으면 구조가 깨진다. 무해하게 바꿔 둔다."""
    return body.replace(END, END.replace("<!--", "<!_-")).replace(
        BEGIN_PREFIX, BEGIN_PREFIX.replace("<!--", "<!_-")
    )


def _write_target(path: str) -> str:
    """실제로 덮어쓸 경로. `path` 가 심볼릭 링크면 그 대상을 돌려준다.

    os.replace 는 목적지가 심볼릭 링크여도 디렉터리 엔트리(링크 자체)를 바꿔치기
    한다 — 링크가 가리키던 실제 파일은 그대로 두고 링크만 평범한 파일로
    대체돼 버린다. AGENTS.md 가 CLAUDE.md 와 공유하려고 일부러 둔 심링크라면
    이 함수가 없으면 collapse() 가 공유 배선을 끊는다.
    """
    try:
        if os.path.islink(path):
            return os.path.realpath(path)
    except OSError:
        pass
    return path


def _has_multiple_links(path: str) -> bool:
    try:
        return os.stat(path).st_nlink > 1
    except OSError:
        return False


def _write_shared(target: str, content: str) -> None:
    """하드링크가 걸린 파일을 그 자리에서 덮어쓴다 (inode 를 바꾸지 않는다).

    fsio.write_atomic 은 tmp + os.replace 라 디렉터리 엔트리만 새 inode 로
    바꿔치기한다 — CLAUDE.md 와 AGENTS.md 가 하드링크로 같은 inode 를 공유할 때
    이걸 쓰면 이 이름만 새 inode(수정된 내용)를 보고 다른 이름은 옛 inode(블록이
    남은 원본)를 계속 본다. r+ 로 열어 같은 inode 에 직접 써야 두 이름이
    갈라지지 않는다.
    """
    with open(target, "r+", encoding="utf-8") as fh:
        fh.write(content)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())


def _write(target: str, content: str) -> None:
    if _has_multiple_links(target):
        _write_shared(target, content)
    else:
        fsio.write_atomic(target, content)


def splice(path: str, body: str, *, captured_at: float, file_header: str = "") -> None:
    """마커 구간을 멱등하게 교체한다. 원자적으로 쓴다.

    tmp + fsync + os.replace 를 쓴다 — 사람이 편집 중인 파일을 반쯤 쓴 상태로
    남기면 안 된다. 다만 대상이 하드링크로 공유된 파일이면 `_write` 가 그 자리
    수정으로 대신한다 (`_write_shared` 참고).
    """
    block = "{}\n{}\n{}\n".format(_begin(captured_at), _neutralize(body).rstrip("\n"), END)

    existing = ""
    created = True
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
        created = False
    except OSError:
        existing = ""

    if _BLOCK.search(existing):
        updated = _BLOCK.sub(block, existing, count=1)
    else:
        prefix = file_header if created and file_header else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        updated = "{}{}{}{}".format(
            prefix, existing, "\n" if existing else "", block
        )

    _write(_write_target(path), updated)


def strip(path: str) -> bool:
    """마커 구간을 제거한다. 그것만 있던 파일이면 파일을 지운다.

    `path` 가 심볼릭 링크거나 하드링크로 공유된 파일이면 이름/inode 를 살려두고
    내용만 비운다 — 링크를 지우거나 바꿔치기하면 공유 배선(예: CLAUDE.md ->
    AGENTS.md)이 끊긴다. 이 파일이 omhc 만의 것이면 비어 있는 실제 파일이
    남는 편이, 공유 파일을 없애거나 갈라 버리는 것보다 덜 놀랍다.
    """
    try:
        is_link = os.path.islink(path)
    except OSError:
        is_link = False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    except OSError:
        return False
    if not _BLOCK.search(existing):
        return False
    remainder = _BLOCK.sub("", existing, count=1)
    target = _write_target(path)
    shared = is_link or _has_multiple_links(target)
    if not remainder.strip():
        if shared:
            try:
                _write(target, "")
            except OSError:
                return False
            return True
        try:
            os.unlink(path)
        except OSError:
            return False
        return True
    # splice 가 끼워 넣은 빈 줄 하나를 되돌린다.
    if remainder.endswith("\n\n"):
        remainder = remainder[:-1]
    _write(target, remainder)
    return True


def installed_captured_at(path: str) -> Optional[float]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    except OSError:
        return None
    m = _BLOCK.search(existing)
    if not m:
        return None
    try:
        return float(m.group("captured"))
    except (TypeError, ValueError):
        return None


def is_stale(path: str, *, now: float, ttl: float = STALE_AFTER_SECONDS) -> bool:
    captured = installed_captured_at(path)
    if captured is None:
        return False
    return (now - captured) > ttl
