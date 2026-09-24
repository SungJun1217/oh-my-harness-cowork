from __future__ import annotations

import os
from typing import Iterable, List, NamedTuple, Optional, Tuple

from . import fsio
from .event import ARG_LIMIT

# 열 순서. 행당 60~90바이트를 목표로 한다.
COLUMNS = ("seq", "epoch", "author", "verb", "ok", "offset", "length", "paths", "arg")


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
    fsio.append_blob(path, "\n".join(lines) + "\n")
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
        parsed = _parse(line)
        if parsed is not None:
            out.append(parsed)
    return out


def _parse(line: str) -> Optional[Row]:
    if not line:
        return None
    parts = line.split("\t")
    if len(parts) != len(COLUMNS):
        return None
    try:
        return Row(
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
    except ValueError:
        return None


# 꼬리에서 읽을 바이트 수. 한 행이 115바이트쯤이므로 4KB면 마지막 온전한 행을
# 확실히 담는다.
_TAIL_BYTES = 4096


def last_row(path: str) -> Optional[Row]:
    """마지막 온전한 행. 파일 전체를 파싱하지 않는다.

    실측: 56KB 색인에서 rows() 는 473행을 파싱해 정수 하나를 돌려주느라 2.05ms 가
    걸렸고, status 와 watch.lag 은 그것을 세션마다 루프로 돌린다.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
            chunk = fh.read()
    except OSError:
        return None
    lines = chunk.split(b"\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    else:
        lines = lines[:-1]  # 잘린 마지막 행은 버린다
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]  # 앞쪽 잘린 행도 버린다
    for raw in reversed(lines):
        parsed = _parse(raw.decode("utf-8", "replace"))
        if parsed is not None:
            return parsed
    return None


def watermark(path: str) -> int:
    """다음 읽기를 시작할 소스 파일 내 바이트 위치."""
    last = last_row(path)
    return (last.offset + last.length) if last else 0


def last_seq(path: str) -> int:
    last = last_row(path)
    return last.seq if last else 0


def append_new(path: str, events: Iterable) -> int:
    """아직 색인되지 않은 이벤트만 덧붙인다. brief 와 watch.sweep 이 같은 규칙을
    쓴다 — 둘이 어긋나면 데몬이 돌 때 같은 이벤트가 두 번 색인된다.

    커서는 seq 가 아니라 소스 파일의 바이트 offset 이다(#23). seq 는 파서가 매기는
    서수라 파서가 레코드를 더 버리도록 바뀌면 업그레이드 전에 색인된 세션의 이후
    이벤트 seq 가 예전보다 작아지고, `seq > last_seq` 로 고르면 그만큼이 영영
    색인되지 않는다(반대로 더 많이 읽게 바뀌면 중복된다). 바이트 위치는 파서와
    무관하다. 한 레코드에서 나온 이벤트는 같은 offset 을 공유하고 항상 한 번에
    읽히므로 "마지막 행의 레코드 끝 이후" 로 고르면 빠짐도 겹침도 없다.

    덧붙이는 행의 seq 는 이 색인의 마지막 seq 에서 이어 다시 매긴다. 파서 seq 를
    그대로 쓰면 위 경우에 기존 행과 번호가 겹쳐 `show <세션>#N` 이 엉뚱한 행을
    연다. 파서가 그대로면 두 번호는 같다.
    """
    cursor = watermark(path)
    seq = last_seq(path)
    fresh = []
    for ev in events:
        if ev.offset < cursor:
            continue
        seq += 1
        fresh.append(ev._replace(seq=seq))
    return append_rows(path, fresh)


def find(path: str, seq: int) -> Optional[Row]:
    for row in rows(path):
        if row.seq == seq:
            return row
    return None


REFS_NAME = "refs.tsv"


def write_refs(state_dir: str, ref, tags) -> None:
    """태그 → (세션, 소스 경로, 오프셋, 길이). 900바이트 안에 세션 id 가 없어도
    `omhc show E1` 이 풀리는 근거다. 매 표식마다 다시 쓴다."""
    lines = [
        "\t".join((tag, ref.session_id, ref.source_path, str(ev.offset),
                   str(ev.length), str(ev.seq)))
        for tag, ev in tags
    ]
    fsio.write_atomic(os.path.join(state_dir, REFS_NAME),
                      "\n".join(lines) + "\n" if lines else "")


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
