from __future__ import annotations

import os
import re
import time
from typing import Optional

from . import fsio

MARKER_ID = "omhc"
BEGIN_PREFIX = "<!-- {}:begin".format(MARKER_ID)
END = "<!-- {}:end -->".format(MARKER_ID)

# 24 hours. Collapse past this — yesterday's marker must not read like today's instruction.
STALE_AFTER_SECONDS = 24 * 3600

_BLOCK = re.compile(
    re.escape(BEGIN_PREFIX) + r'\s+captured="(?P<captured>[0-9.]+)"'
    r'(?:\s+captured_utc="[^"]*")?\s*-->'
    r".*?" + re.escape(END) + r"\n?",
    re.S,
)


def _iso_readable(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _begin(captured_at: float) -> str:
    # captured_utc is a human-readable absolute time (#36) — the body's
    # relative age ("3m ago") is frozen at mint time, so this lets a later
    # reader tell how stale that relative value has become. captured(epoch)
    # is still read as-is by is_stale/installed_captured_at — this doesn't
    # change the marker format.
    return '{} captured="{:.0f}" captured_utc="{}" -->'.format(
        BEGIN_PREFIX, captured_at, _iso_readable(captured_at))


def _block_text(body: str, captured_at: float) -> str:
    return "{}\n{}\n{}\n".format(_begin(captured_at), _neutralize(body).rstrip("\n"), END)


def _neutralize(body: str) -> str:
    """If the body itself contains the marker, structure breaks. Neutralize it."""
    return body.replace(END, END.replace("<!--", "<!_-")).replace(
        BEGIN_PREFIX, BEGIN_PREFIX.replace("<!--", "<!_-")
    )


def _write_target(path: str) -> str:
    """The path actually written to. If `path` is a symlink, return its target.

    os.replace swaps the directory entry (the link itself) even when the
    destination is a symlink — the real file the link pointed at stays put,
    only the link gets replaced by a plain file. If AGENTS.md is deliberately
    symlinked to share with CLAUDE.md, without this function collapse() would
    sever that shared wiring.
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
    """Overwrite a hard-linked file in place (does not change the inode).

    fsio.write_atomic is tmp + os.replace, which swaps only the directory
    entry for a new inode — if CLAUDE.md and AGENTS.md share an inode via a
    hard link, this would make only this name see the new inode (updated
    content) while the other name keeps seeing the old inode (still holding
    the block). Opening with r+ and writing directly into the same inode is
    what keeps the two names from diverging.
    """
    with open(target, "r+", encoding="utf-8", newline="") as fh:
        fh.write(content)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())


def _write(target: str, content: str) -> None:
    if _has_multiple_links(target):
        _write_shared(target, content)
    else:
        fsio.write_atomic(target, content)


def _without_block(existing: str) -> str:
    """Returns "pure user content" — `existing` with only the omhc block
    removed. Returned as-is if there's no block.

    No separator blank line is inserted around the block (see splice below) —
    review defect: an earlier version tried to restore that blank line by
    guessing which side (before/after) was "real user content" based on
    position (top/bottom), but that guess was wrong whenever the user added
    lines above the block (making `before` non-empty even in a top placement)
    or an old bottom placement had content after the block, and it dropped
    one side wholesale (#33 review). Without a separator blank line to begin
    with, this ambiguity doesn't exist — simply concatenating before/after as
    they are never loses a single user byte."""
    return _BLOCK.sub("", existing, count=1)


def splice(path: str, body: str, *, captured_at: float, file_header: str = "") -> None:
    """Idempotently replaces the marker block and moves it to the top of the
    file. Written atomically.

    Codex only reads AGENTS.md from the top up to `project_doc_max_bytes`
    (default 32768 bytes, measured #33) — appending the block at the end
    means it can be truncated away entirely in a large AGENTS.md. So the
    block is kept at the top, and a block previously planted at the bottom is
    moved to the top on the next splice. No separator blank line is inserted
    between the block and the rest of the content — the block string itself
    already ends in a newline so the shape stays intact, and `_without_block`
    never has to restore that blank line (avoiding the review defect above,
    see `_without_block`). Uses tmp + fsync + os.replace — a file a human may
    be editing must never be left half-written. If the target is a
    hard-linked shared file, `_write` substitutes an in-place edit instead
    (see `_write_shared`).
    """
    block = _block_text(body, captured_at)

    existing = ""
    created = True
    try:
        # newline="" — reading an AGENTS.md a human wrote with CRLF in text
        # mode's default (universal newline translation) would drop the \r,
        # making strip() return different bytes than the original. Keep it
        # untouched here and write it back exactly as read.
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            existing = fh.read()
        created = False
    except OSError:
        existing = ""

    rest = _without_block(existing)
    prefix = file_header if created and file_header else ""
    updated = "{}{}{}".format(prefix, block, rest)

    _write(_write_target(path), updated)


def prospective_block_end_bytes(path: str, body: str, *, captured_at: float,
                                 file_header: str = "") -> int:
    """The UTF-8 byte offset where the block would end if `splice(path, body,
    ...)` actually ran. Since the block is always at the top of the file
    (see splice above), this is just `len((prefix + block).encode("utf-8"))`
    — independent of the existing file content (`rest`) size. Used only to
    estimate before writing whether the budget (Codex's
    `project_doc_max_bytes`) would be exceeded — never actually writes."""
    block = _block_text(body, captured_at)
    created = not os.path.exists(path)
    prefix = file_header if created and file_header else ""
    return len((prefix + block).encode("utf-8"))


def strip(path: str) -> bool:
    """Removes the marker block. Deletes the file if it held nothing else.

    If `path` is a symlink or a hard-linked shared file, the name/inode is
    kept and only the content is emptied — deleting or replacing the link
    would sever shared wiring (e.g. CLAUDE.md -> AGENTS.md). If the file
    belongs to omhc alone, leaving an empty real file is less surprising
    than deleting or splitting a shared file.
    """
    try:
        is_link = os.path.islink(path)
    except OSError:
        is_link = False
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            existing = fh.read()
    except OSError:
        return False
    if not _BLOCK.search(existing):
        return False
    rest = _without_block(existing)
    target = _write_target(path)
    shared = is_link or _has_multiple_links(target)
    if not rest.strip():
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
    _write(target, rest)
    return True


def strip_if_captured(path: str, expected_captured: float) -> bool:
    """A conditional version of `strip` (#36 review) — removes the block only
    if its `captured` still equals `expected_captured`. Between the caller
    already judging "this value means stale" and calling this function
    (check-then-act), if another process (e.g. a `brief` running in parallel
    within the same SessionStart) has already overwritten it with a new
    block, that value will have changed, so this leaves it alone.

    There are two race windows: (a) between the caller's judgment and this
    function's first read — the first read below already sees the new value,
    so it's naturally filtered out by the `expected_captured` comparison.
    (b) between this function's first read and the actual delete-write — read
    once more via `installed_captured_at` right before writing to recheck
    nothing changed in between. Without file locking, (b) can't be closed
    completely even in theory (a brief window remains between the recheck
    and the write), but moving the recheck right up against the write
    minimizes the window that's actually left — file locking is overkill at
    this tool's scale (personal use, v1 assumes sequential use). What remains
    is the gap from the recheck to os.replace — a few milliseconds, since
    temp-file writing and fsync sit inside it (confirmed by instrumenting
    fsio during review). A block written in that window can still be lost."""
    try:
        is_link = os.path.islink(path)
    except OSError:
        is_link = False
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            existing = fh.read()
    except OSError:
        return False
    m = _BLOCK.search(existing)
    if not m:
        return False
    try:
        captured = float(m.group("captured"))
    except (TypeError, ValueError):
        return False
    if captured != expected_captured:
        return False
    if installed_captured_at(path) != captured:
        return False
    rest = _without_block(existing)
    target = _write_target(path)
    shared = is_link or _has_multiple_links(target)
    if not rest.strip():
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
    _write(target, rest)
    return True


def installed_block_end_bytes(path: str) -> Optional[int]:
    """The UTF-8 byte offset where the installed block ends, or None if there's no block.

    For comparing against Codex's `project_doc_max_bytes` budget (#33) — gives
    the actual measured offset regardless of where the block currently sits
    (normally the top; an old bottom placement can remain until the next splice)."""
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    except OSError:
        return None
    m = _BLOCK.search(text)
    if not m:
        return None
    return len(text[: m.end()].encode("utf-8"))


def installed_captured_at(path: str) -> Optional[float]:
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
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
