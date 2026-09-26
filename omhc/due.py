from __future__ import annotations

import collections
import os
from typing import Callable, List, Optional

from . import fsio, ledger, locate

# v2 phase 1 cap (#41): "every undelivered session at SessionStart", capped so
# a burst of foreign sessions doesn't grow the handoff or the hook's read time
# without bound. Proposed value from docs/v2-concurrency.md's open question 1.
MAX_SESSIONS = 3

# This namedtuple and the index TSV's paths column together form the concurrency seam.
Watermark = collections.namedtuple(
    "Watermark", "repo_key harness session_id path event epoch"
)

DELIVERED_NAME = "delivered.tsv"
OFF_MARKER = "off"
OFF_ENV = "OMHC_OFF"

# A value that never appears in delivered.tsv's 2nd column (to_harness) —
# harness ids are only the adapter ids registered in the registry, so they
# never collide with this string. Using this slot keeps the line itself in
# the old 4-column format (session, marker, from, epoch), so any old reader
# unaware of this field just sees a mismatch on "parts[1] == to_harness" and
# moves on (#22: resume must be able to revive an already-delivered session).
REOPEN_MARKER = "reopen"

# The 6th column mark_delivered writes for a delivery that only produced an
# ALSO line (v2 phase 1, #41) — lets `omhc status` skip it from
# injections/pull-rate the same way it already skips REOPEN_MARKER rows.
ALSO_MARKER = "also"

# A foreign session older than this is not treated as ongoing work. Injecting
# a week-old session as "just happened" would have the next agent redo
# finished work.
MAX_AGE_SECONDS = 7 * 24 * 3600


def is_off(state_dir: str) -> bool:
    """The off switch. Env var or marker file."""
    if os.environ.get(OFF_ENV, "").strip() not in ("", "0", "false", "False"):
        return True
    return os.path.exists(os.path.join(state_dir, OFF_MARKER))


def off_reason(state_dir: str) -> Optional[str]:
    """None if on; if off, what turned it off. Checked in the same order as
    is_off — `omhc status` must show "off" without treating it as a FAIL,
    while still showing why (being off can be a deliberate human choice)."""
    val = os.environ.get(OFF_ENV, "").strip()
    if val not in ("", "0", "false", "False"):
        return "{}={}".format(OFF_ENV, val)
    marker = os.path.join(state_dir, OFF_MARKER)
    if os.path.exists(marker):
        return "marker {}".format(marker)
    return None


def _delivered_path(state_dir: str) -> str:
    return os.path.join(state_dir, DELIVERED_NAME)


def _delivered_lines(state_dir: str) -> List[List[str]]:
    """Reads delivered.tsv once as split fields. due() needs both
    "already delivered" and "ever delivered" for every candidate row while
    walking a list (v2 phase 1) — re-opening the file per session/per-check
    would be an O(n^2) read of a file that's small but not free."""
    try:
        with open(_delivered_path(state_dir), encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n").split("\t") for line in fh if line.strip()]
    except OSError:
        return []


def _already_delivered(lines: List[List[str]], session_id: str, to_harness: str) -> bool:
    delivered = False
    for parts in lines:
        if len(parts) < 2 or parts[0] != session_id:
            continue
        if parts[1] == REOPEN_MARKER:
            delivered = False
        elif parts[1] == to_harness:
            delivered = True
    return delivered


def _ever_delivered(lines: List[List[str]], session_id: str, to_harness: str) -> bool:
    """Was this session **ever** delivered to this harness, reopen or not.

    Unlike _already_delivered, a later reopen line doesn't undo this — it's
    used by due() to decide when to stop the v2 list: a session that was
    delivered and then reopened must still end the list (see due()'s
    docstring), even though _already_delivered() says "not delivered" for it
    right now.
    """
    return any(len(parts) >= 2 and parts[0] == session_id and parts[1] == to_harness
               for parts in lines)


def already_delivered(state_dir: str, session_id: str, to_harness: str) -> bool:
    """Has this session been delivered to this harness — **the last in file
    order** wins (invariant 6). If resume has written a reopen for this
    session, that reopen comes after the earlier delivered line, so "not yet
    delivered" wins — a resumed turn must be able to be handed off again (#22)."""
    return _already_delivered(_delivered_lines(state_dir), session_id, to_harness)


def ever_delivered(state_dir: str, session_id: str, to_harness: str) -> bool:
    """Public form of _ever_delivered, reading fresh (one-off callers only —
    due() reads delivered.tsv itself and reuses the lines, see its docstring)."""
    return _ever_delivered(_delivered_lines(state_dir), session_id, to_harness)


def last_delivered(state_dir: str) -> Optional[str]:
    """The most recently delivered session id for this repo — scan
    delivered.tsv backward for the first **delivered** line encountered
    (append order, invariant 6). Reopen lines are skipped since they aren't a
    delivery — otherwise right after a resume, "most recently delivered"
    would point at a session that hasn't even been re-sent yet. None if
    nothing's ever been delivered."""
    try:
        with open(_delivered_path(state_dir), encoding="utf-8", errors="replace") as fh:
            lines = [line for line in fh if line.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 2 and parts[1] == REOPEN_MARKER:
            continue
        if parts and parts[0]:
            return parts[0]
    return None


def delivered_order(state_dir: str) -> List[str]:
    """The session ids delivered to this repo — in the order of their **last**
    appearance in delivered.tsv. Uses append order (invariant 6). This is
    what `omhc log`'s session ordering is based on (#18 review).

    Must use last appearance to agree with last_delivered() — if a session is
    delivered again to a second target, its index grows too at that point, so
    keying on first appearance would put log's tail out of sync with `show`'s
    default session and drop that session's new lines from `--last N`.

    Reopen lines aren't a delivery, so they don't count toward ranking — a
    session that's only been resumed and not yet re-delivered must not jump
    to the end of log just because of a reopen line."""
    try:
        with open(_delivered_path(state_dir), encoding="utf-8", errors="replace") as fh:
            lines = [line for line in fh if line.strip()]
    except OSError:
        return []
    last = {}
    for pos, line in enumerate(lines):
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 2 and parts[1] == REOPEN_MARKER:
            continue
        if parts and parts[0]:
            last[parts[0]] = pos
    return sorted(last, key=last.get)


def mark_delivered(
    state_dir: str,
    watermark,
    *,
    to_harness: str,
    epoch: float,
    offset: Optional[int] = None,
    role: Optional[str] = None,
) -> None:
    """Records that this session was delivered to this harness. Keeps the same thing from being pushed twice.

    `offset` is how far into the source session this delivery read (the last
    included event's offset+length) — appended as the 5th column (#27). Every
    reader of the old 4-column lines (already_delivered, last_delivered,
    delivered_order, status, cmd_log) only looks at parts[0]/parts[1], so
    they keep working even with an extra column. If offset is None (caller
    doesn't know), leave it as 4 columns — exactly the same shape old readers expect.

    `role="also"` (v2 phase 1, #41) marks a delivery that only produced an
    ALSO line, not the main slot layout — written as an optional 6th column
    so `omhc status`'s injection/pull-rate counting can skip it the same way
    it already skips reopen lines (one handoff with 3 rows must count as one
    injection, not three). If offset is None, an empty 5th column is written
    first so the 6th column lands in a fixed position regardless."""
    if watermark is None:
        return
    fields = [watermark.session_id, to_harness, watermark.harness, "{:.0f}".format(epoch)]
    if offset is not None:
        fields.append(str(offset))
    if role == ALSO_MARKER:
        if offset is None:
            fields.append("")
        fields.append(role)
    fsio.append_line(_delivered_path(state_dir), "\t".join(fields))


def last_delivery_offset(state_dir: str, session_id: str, to_harness: str) -> Optional[int]:
    """The 5th column (offset) from the last delivery of this session to this harness.

    reopen is only a hint (#27's root cause), so brief must verify a new
    human turn actually exists — this value is that baseline. If no matching
    line exists, or it does but is the old 4-column format, returns None —
    meaning "baseline unknown", and the caller must keep today's behavior
    (send again unconditionally)."""
    try:
        with open(_delivered_path(state_dir), encoding="utf-8", errors="replace") as fh:
            lines = [line for line in fh if line.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2 or parts[0] != session_id or parts[1] != to_harness:
            continue
        if len(parts) >= 5:
            try:
                return int(parts[4])
            except ValueError:
                return None
        return None
    return None


def mark_reopened(state_dir: str, session_id: str, from_harness: str, epoch: float) -> None:
    """Records that this session was resumed and gained a new turn — makes
    already_delivered() return False again even if it was already delivered
    (#22: `codex exec resume` appends to the same rollout, and if that
    session had been delivered before, due() would stop there and the
    resumed turn would never go out). Called from `mark`'s hook path — the
    caller wraps it to uphold invariant 2."""
    if not session_id:
        return
    line = "\t".join((session_id, REOPEN_MARKER, from_harness or "", "{:.0f}".format(epoch)))
    fsio.append_line(_delivered_path(state_dir), line)


def due(
    repo_key: str,
    my_harness: str,
    my_session_id: str,
    now: float,
    *,
    home: Optional[str] = None,
    eligible: Optional[Callable[[Watermark], bool]] = None,
    limit: int = MAX_SESSIONS,
) -> List[Watermark]:
    """Every foreign session to tell this session about, newest first,
    capped at `limit`. **This is the concurrency seam.**

    v1's entire sequential-use assumption used to live inside this function
    as "just the newest one". v2 phase 1 (#41) is done: it now returns a
    list — every eligible, undelivered session since the last handoff, not
    just the newest — and `due_one()` (below) keeps giving callers that only
    want the v1 behavior a single Optional[Watermark].

    Upstream (ledger, adapters, index, pin, guard) and downstream (mint,
    brief, agents_md) are already order-independent, and since a reader
    streams an append-only log from byte 0 to EOF, they already tolerate a
    still-growing file. The ordering key is neither file mtime nor a
    per-record timestamp — it's the ledger's session-start epoch and the
    byte offset within the source file. This machine's largest transcript
    has 254 instances of timestamps going backward.

    cli._backfill_foreign_sessions uses the same key — when discover() finds
    and backfills a harness's sessions missing from the ledger (because an
    untrusted hook never ran), it compares that session's start epoch (the
    adapter reads it from session_meta etc.) against that harness's latest
    start epoch already in the ledger to decide append order. **Still not
    mtime** — this is exactly the same rule as this function, "order only by
    session-start epoch"; the only new part is comparing epochs across
    different files (ledger vs. rollout).

    **Never revives a session older than the last delivered one.** Walking
    back stops the moment it reaches a session already delivered to
    my_harness — continuing past it would inject yesterday's session as
    "just happened" once today's is gone (a stale marker is worse than no
    marker). A session that was delivered and later reopened (#22) can still
    be *included* — already_delivered() reports False for it again — but it
    always **ends** the list: letting the walk continue past a reopened
    session would revive whatever came before it, which v1 deliberately
    never sent.

    **Each session id appears at most once.** The ledger can hold several
    `start` rows for the same session (`cmd_mark` appends one on
    `source:"resume"`, `_reactivate_grown_sessions` appends one when a
    session grows, and a late compact can too) — without deduping, the same
    session would be built into two different Watermarks (one per row) and
    walk straight into the list twice: once as the head and again as its own
    ALSO line, with its own failures double-tagged and delivered.tsv gaining
    two rows for one delivery (#41 review finding 1). Only the **first**
    (newest) occurrence is considered; every older row for the same session
    is skipped outright, regardless of what that first occurrence decided.
    """
    state = locate.state_dir(repo_key, home=home)
    if is_off(state):
        return []

    delivered_lines = _delivered_lines(state)
    results: List[Watermark] = []
    seen_sessions = set()
    for row in reversed(ledger.read(repo_key=repo_key, home=home)):
        if row.get("event") != "start":
            continue
        harness = row.get("harness")
        session = row.get("session")
        if not harness or not session:
            continue
        if harness == my_harness:
            continue
        if session == my_session_id:
            continue
        session_id = str(session)
        if session_id in seen_sessions:
            continue
        seen_sessions.add(session_id)
        if _already_delivered(delivered_lines, session_id, my_harness):
            break

        epoch = float(row.get("epoch") or 0.0)
        if epoch and now and (now - epoch) > MAX_AGE_SECONDS:
            # Too old to be work worth continuing, and everything before it is older still.
            break

        mark = Watermark(
            repo_key=repo_key,
            harness=str(harness),
            session_id=session_id,
            path=str(row.get("path") or ""),
            event="start",
            epoch=epoch,
        )
        # Whether a session is a human conversation is judged by the caller
        # asking the adapter (#21). Holding an entrypoint vocabulary here
        # would mean the same rule lives in two modules with only one kept up
        # to date. When this judgment was made at mark time and written to
        # the ledger, the Claude transcript hadn't been written yet and it
        # was barely recorded — which let a single headless session occupy
        # the "most recent foreign session" slot and block the real session
        # before it. If ineligible, don't stop — move to the row before it.
        if eligible is not None and not eligible(mark):
            continue
        results.append(mark)
        if _ever_delivered(delivered_lines, session_id, my_harness) or len(results) >= limit:
            break
    return results


def due_one(
    repo_key: str,
    my_harness: str,
    my_session_id: str,
    now: float,
    *,
    home: Optional[str] = None,
    eligible: Optional[Callable[[Watermark], bool]] = None,
) -> Optional[Watermark]:
    """v1 semantics: the single newest due session, or None. Exactly `due()`'s
    old return shape — kept for callers that only care about the one, most
    recent handoff."""
    marks = due(repo_key, my_harness, my_session_id, now, home=home,
                eligible=eligible, limit=1)
    return marks[0] if marks else None
