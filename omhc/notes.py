"""Stamped notes, explicit expiry, and stable content IDs for manual management."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
import re
import time

from . import due, fsio, locate

STAMP = re.compile(r"^(\d{9,11})\t(.*)$")
_EXPIRY = re.compile(r"^!expires=(\d{9,11})\t(.*)$")
_TTL = re.compile(r"^([1-9]\d*)([smhd])$")


class NoteError(ValueError):
    pass


def parse_ttl(value: str) -> int:
    match = _TTL.fullmatch(value)
    if not match:
        raise NoteError("TTL must be a positive duration such as 30m, 12h, or 1d")
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    if seconds > due.MAX_AGE_SECONDS:
        raise NoteError("TTL cannot exceed 7d")
    return seconds


def _path(state):
    return os.path.join(state, locate.NOTES_NAME)


def _lines(state):
    try:
        with open(_path(state), encoding="utf-8") as fh:
            return fh.readlines()
    except FileNotFoundError:
        return []


def _record(line, now):
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return None
    match = STAMP.fullmatch(raw)
    created = int(match[1]) if match else None
    text = match[2] if match else raw.strip()
    expires = created + due.MAX_AGE_SECONDS if created is not None else None
    if match:
        explicit = _EXPIRY.fullmatch(text)
        if explicit:
            expires = min(expires, int(explicit[1]))
            text = explicit[2]
    return {"id": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
            "text": text.strip(), "created_at": created, "expires_at": expires,
            "expired": expires is not None and now > expires,
            "legacy": created is None, "copies": 1}


def list_notes(state, *, now=None):
    stamp = time.time() if now is None else now
    records = {}
    for line in _lines(state):
        record = _record(line, stamp)
        if record:
            if record["id"] in records:
                records[record["id"]]["copies"] += 1
            else:
                records[record["id"]] = record
    return list(records.values())


def recent(state, limit=2, *, now=None):
    # Hooks tolerate unreadable note files; manual commands report the error.
    try:
        stamp = time.time() if now is None else now
        records = [_record(line, stamp) for line in _lines(state)]
        kept = [r["text"] for r in records if r and r["text"] and not r["expired"]]
        return kept[-limit:] if limit else []
    except (OSError, UnicodeError):
        return []


@contextmanager
def _locked(state):
    import fcntl
    os.makedirs(state, exist_ok=True)
    path = os.path.join(state, "notes.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def add(state, text, *, ttl=None, now=None):
    text = " ".join(text.split())
    if not text:
        raise NoteError("nothing to note")
    stamp = round(time.time() if now is None else now)
    if ttl is not None and not 0 < ttl <= due.MAX_AGE_SECONDS:
        raise NoteError("TTL must be positive and cannot exceed 7d")
    body = "!expires={}\t{}".format(stamp + ttl, text) if ttl is not None else text
    line = "{}\t{}".format(stamp, body)
    with _locked(state):
        _lines(state)  # Reject unreadable existing data before reporting a partial append.
        fsio.append_line(_path(state), line)
    return _record(line, stamp)


def delete(state, note_id):
    if not re.fullmatch(r"[0-9a-f]{4,16}", note_id):
        raise NoteError("use a note ID (at least 4 hex characters) from `omhc note list`")
    with _locked(state):
        lines = _lines(state)  # Strict UTF-8: never rewrite unrelated corrupt bytes.
        records = [_record(line, time.time()) for line in lines]
        matches = {r["id"] for r in records if r and r["id"].startswith(note_id)}
        if len(matches) != 1:
            raise NoteError("ambiguous note ID" if matches else "unknown note ID")
        selected = next(iter(matches))
        kept = [line for line, r in zip(lines, records) if not r or r["id"] != selected]
        fsio.replace_preserving(_path(state), "".join(kept))
        return len(lines) - len(kept)
