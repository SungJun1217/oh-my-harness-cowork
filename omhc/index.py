from __future__ import annotations

import os
from typing import Iterable, List, NamedTuple, Optional, Tuple

from . import fsio
from .event import ARG_LIMIT

# Column order. Targets 60-90 bytes per row.
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
    """Strips characters that would break the TSV row shape.

    The index is a pointer, so fidelity doesn't need to be kept here — the
    original text is read straight from the source via offset/length.
    """
    return text.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def append_rows(path: str, events: Iterable) -> int:
    """Appends Events to the index. Never rewrites, so it survives an
    interrupted session too."""
    lines = []
    for ev in events:
        # Body text isn't stored here. Storing it would give the archive two
        # copies of the original.
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
    """Reads the index. Skips a truncated last row — the normal result of an
    interrupted write."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return []
    out: List[Row] = []
    lines = raw.split("\n")
    # The last element should be the empty string after the final newline.
    # If it isn't, the last row was truncated.
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


# Bytes to read from the tail. A row is roughly 115 bytes, so 4KB reliably
# captures the last complete row.
_TAIL_BYTES = 4096


def last_row(path: str) -> Optional[Row]:
    """Last complete row. Doesn't parse the whole file.

    Measured: on a 56KB index, rows() took 2.05ms parsing 473 rows just to
    return a single integer, and status/watch.lag loop this per session.
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
        lines = lines[:-1]  # drop the truncated last row
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]  # drop the truncated leading row too
    for raw in reversed(lines):
        parsed = _parse(raw.decode("utf-8", "replace"))
        if parsed is not None:
            return parsed
    return None


def watermark(path: str) -> int:
    """The byte position in the source file where the next read should start."""
    last = last_row(path)
    return (last.offset + last.length) if last else 0


def last_seq(path: str) -> int:
    last = last_row(path)
    return last.seq if last else 0


def append_new(path: str, events: Iterable) -> int:
    """Appends only events not yet indexed. `brief` and `watch.sweep` share this
    rule — if they diverged, the daemon running would double-index the same
    event.

    The cursor is the source file's byte offset, not seq (#23). seq is an
    ordinal the parser assigns; if the parser later changes to drop more
    records, seq for later events in an already-indexed session shifts down,
    and picking by `seq > last_seq` would leave that many events unindexed
    forever (conversely, parsing more would duplicate them). A byte position
    is parser-independent. Events from one record share an offset and are
    always read together, so picking "after the last row's record end" misses
    nothing and double-counts nothing.

    Appended rows get seq renumbered continuing from this index's last seq.
    Using the parser's seq as-is would collide with existing rows in the case
    above and make `show <session>#N` open the wrong row. If the parser is
    unchanged the two numbers are the same anyway.
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
    """Tag -> (session, source path, offset, length). This is what lets
    `omhc show E1` resolve even though the 900-byte body has no session id.
    Rewritten on every handoff."""
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
