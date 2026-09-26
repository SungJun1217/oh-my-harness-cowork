from __future__ import annotations

import json
import os
import time
from typing import List, Optional

from . import fsio, locate

LEDGER_NAME = "ledger.jsonl"
# Where append() records just the fact that it dropped a row because it
# couldn't fit the cap (#22). Doesn't hold the original record — a truncated
# identity field would be meaningless anyway. `omhc status`'s `ledger
# rejects` row reads this file.
REJECTED_NAME = "ledger.rejected"

# The default limit for read(). Callers that slice a repo-filtered result
# themselves in memory (like cli.cmd_status, reading once) must reference the
# same value so the result matches calling read(repo_key=...) twice.
DEFAULT_LIMIT = 2000

# This cap has nothing to do with atomicity (#22 review: an earlier comment
# was wrong) — PIPE_BUF is pipe-only (512 on macOS), and POSIX guarantees a
# single write(2) call to an fd opened O_APPEND on a regular file is an
# atomic "seek to end + write" regardless of size, as long as it's exactly
# one write(2) call (which is what fsio.append_line does).
#
# The real reason is measured path length: with a long home path (long
# username, or a deep company-standard home, measured ~80-100 chars), Claude's
# `~/.claude/projects/<cwd slugified with dashes>/<uuid>.jsonl` path can blow
# well past 400 bytes, and even trimming cwd can't make it fit — backfill rows
# (Codex scan) were all silently dropped and it didn't even show up in status
# (#22). 800 fits comfortably even on such homes (measured: ~625 bytes after
# trimming even with a 150-char home and a 40-char repo name), and if it still
# doesn't fit, `_note_rejection` records it.
MAX_LINE = 800

# Keys to trim when over the cap. **Order matters.**
#
# path and session are identity fields — trimming them produces a
# syntactically valid line that points at nothing, silently failing the
# handoff without even a trace in guard.log. So decorative fields (cwd, repo)
# are shrunk first, and if that's still not enough, drop the whole line.
_TRIMMABLE = ("cwd",)


def _path(home: Optional[str]) -> str:
    return os.path.join(locate.omhc_root(home), LEDGER_NAME)


def _encode(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def _rejected_path(home: Optional[str]) -> str:
    return os.path.join(locate.omhc_root(home), REJECTED_NAME)


def _note_rejection(record: dict, size: int, home: Optional[str]) -> None:
    """Cheaply records only the fact that a row was dropped (#22) — must
    never raise (invariant 2) since this can also be called from the hook
    path (mark). Even on failure, append()'s return value is already False so
    the caller knows regardless; this is just an extra record for a human to
    see later.

    #22 review: a row that never made it into the ledger also isn't caught by
    `known_sessions` (cli._backfill_foreign_sessions), so mark keeps retrying
    it forever — if session is present, first check whether the same
    (repo, harness, session) was already recorded, and skip re-recording if
    so. That keeps one session from inflating into "dropped 3 times" and the
    file from growing without bound. Rows with no session (old format,
    malformed records) have no basis to dedupe on, so they're recorded every
    time — a rare case, and even so status counts them below via distinct.
    """
    session = record.get("session")
    try:
        if session:
            repo = record.get("repo")
            harness = record.get("harness")
            for row in read_rejected(home=home, repo_key=repo):
                if row.get("harness") == harness and row.get("session") == session:
                    return
        row = {"repo": record.get("repo"), "harness": record.get("harness"),
              "session": session, "event": record.get("event"),
              "epoch": round(time.time(), 0), "bytes": size}
        fsio.append_line(_rejected_path(home), _encode(row))
    except OSError:
        pass


def append(record: dict, home: Optional[str] = None) -> bool:
    """Writes one line via a single O_APPEND write(2).

    If it can't fit the cap, **doesn't write and returns False.** More honest
    than trimming identity fields down to a line that points at nothing — a
    truncated path produces a silent failure, and JSON cut mid-byte just gets
    skipped by read() anyway. Instead, the rejection itself is recorded via
    `_note_rejection` so `omhc status` can surface it (#22) — previously the
    return value was discarded by the caller and it just vanished silently.
    """
    line = _encode(record)
    size = len(line.encode("utf-8")) + 1
    if size > MAX_LINE:
        trimmed = dict(record)
        for key in _TRIMMABLE:
            value = trimmed.get(key)
            if isinstance(value, str) and len(value) > 24:
                trimmed[key] = value[:24] + "…"
            line = _encode(trimmed)
            size = len(line.encode("utf-8")) + 1
            if size <= MAX_LINE:
                break
        if size > MAX_LINE:
            _note_rejection(record, size, home)
            return False
    fsio.append_line(_path(home), line)
    return True


def read_rejected(home: Optional[str] = None, repo_key: Optional[str] = None,
                  limit: int = DEFAULT_LIMIT) -> List[dict]:
    """Rows recorded by `_note_rejection`. Uses the same rule as `read()`
    (repo filter before limit) — for the same reason (moving between multiple
    repos could push this repo's rejections out of the window with an older
    repo's rejections)."""
    try:
        with open(_rejected_path(home), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    rows: List[dict] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if repo_key is not None and row.get("repo") != repo_key:
            continue
        rows.append(row)
    return rows[-limit:] if limit else rows


def clear_rejected(repo_key: str, home: Optional[str] = None) -> int:
    """Clears only this repo's rejection records (other repos' lines are
    left as-is). Called by `omhc clear` — `ledger rejects` must not stay FAIL
    forever even after raising the cap or fixing the cause (#22 review). Not
    on the hook path, so it's fine to raise."""
    path = _rejected_path(home)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0
    kept: List[str] = []
    removed = 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if isinstance(row, dict) and row.get("repo") == repo_key:
            removed += 1
            continue
        kept.append(line)
    if removed:
        text = "".join(l + "\n" for l in kept)
        fsio.replace_preserving(path, text)  # keeps the 0600 mode append_line created
    return removed


def read(limit: int = DEFAULT_LIMIT, home: Optional[str] = None,
         repo_key: Optional[str] = None) -> List[dict]:
    """Reads the ledger. Broken lines are skipped (fail-open).

    **Applies the repo filter before the limit.** The ledger is one file
    shared across the whole machine, so truncating to the last `limit` lines
    first would push this repo's start line out of the window for someone
    working across 20 repos, silently stalling the handoff.
    """
    try:
        with open(_path(home), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    rows: List[dict] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if repo_key is not None and row.get("repo") != repo_key:
            continue
        rows.append(row)
    return rows[-limit:] if limit else rows
