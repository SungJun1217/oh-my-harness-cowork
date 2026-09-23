from __future__ import annotations

import os
import re
from typing import Optional

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


def splice(path: str, body: str, *, captured_at: float, file_header: str = "") -> None:
    """마커 구간을 멱등하게 교체한다. 원자적으로 쓴다.

    tmp + fsync + os.replace 를 쓴다 — 사람이 편집 중인 파일을 반쯤 쓴 상태로
    남기면 안 된다.
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

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".omhc.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(updated)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def strip(path: str) -> bool:
    """마커 구간을 제거한다. 그것만 있던 파일이면 파일을 지운다."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    except OSError:
        return False
    if not _BLOCK.search(existing):
        return False
    remainder = _BLOCK.sub("", existing, count=1)
    if not remainder.strip():
        try:
            os.unlink(path)
        except OSError:
            return False
        return True
    # splice 가 끼워 넣은 빈 줄 하나를 되돌린다.
    if remainder.endswith("\n\n"):
        remainder = remainder[:-1]
    tmp = path + ".omhc.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(remainder)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
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
