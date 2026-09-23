from __future__ import annotations

import os
from typing import Iterable, List, NamedTuple, Optional, Tuple

# 열 순서. 행당 60~90바이트를 목표로 한다.
COLUMNS = ("seq", "epoch", "author", "verb", "ok", "offset", "length", "paths", "arg")

ARG_LIMIT = 120


class Row(NamedTuple):
    seq: int
    epoch: float
    author: str
    verb: str
    ok: bool
    offset: int
    length: int
    paths: Tuple[str, ...]
    arg: str


def _clean(text: str) -> str:
    """TSV 행 모양을 깨뜨릴 문자를 없앤다.

    색인은 포인터이므로 충실도를 여기서 지킬 필요가 없다 — 원문은 offset/length
    로 원본에서 그대로 읽는다.
    """
    return text.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def append_rows(path: str, events: Iterable) -> int:
    """Event 를 색인에 덧붙인다. 되쓰지 않으므로 중단된 세션에서도 이어진다."""
    lines = []
    for ev in events:
        # 본문(text)은 담지 않는다. 담으면 아카이브가 원본 두 벌이 된다.
        lines.append(
            "\t".join(
                (
                    str(ev.seq),
                    "{:.0f}".format(ev.epoch),
                    ev.author,
                    ev.verb,
                    "1" if ev.ok else "0",
                    str(ev.offset),
                    str(ev.length),
                    ",".join(_clean(p) for p in ev.paths),
                    _clean(ev.arg)[:ARG_LIMIT],
                )
            )
        )
    if not lines:
        return 0
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    blob = ("\n".join(lines) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    return len(lines)


def rows(path: str) -> List[Row]:
    """색인을 읽는다. 잘린 마지막 행은 건너뛴다 — 중단된 쓰기의 정상적 결과다."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return []
    out: List[Row] = []
    lines = raw.split("\n")
    # 마지막 원소는 개행 뒤의 빈 문자열이어야 한다. 아니면 잘린 행이다.
    if lines and lines[-1] != "":
        lines = lines[:-1]
    for line in lines:
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != len(COLUMNS):
            continue
        try:
            out.append(
                Row(
                    seq=int(parts[0]),
                    epoch=float(parts[1]),
                    author=parts[2],
                    verb=parts[3],
                    ok=parts[4] == "1",
                    offset=int(parts[5]),
                    length=int(parts[6]),
                    paths=tuple(p for p in parts[7].split(",") if p),
                    arg=parts[8],
                )
            )
        except ValueError:
            continue
    return out


def watermark(path: str) -> int:
    """다음 읽기를 시작할 소스 파일 내 바이트 위치."""
    parsed = rows(path)
    if not parsed:
        return 0
    last = parsed[-1]
    return last.offset + last.length


def last_seq(path: str) -> int:
    parsed = rows(path)
    return parsed[-1].seq if parsed else 0


def find(path: str, seq: int) -> Optional[Row]:
    for row in rows(path):
        if row.seq == seq:
            return row
    return None
