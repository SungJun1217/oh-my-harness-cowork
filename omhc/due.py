from __future__ import annotations

import collections
import os
from typing import Callable, List, Optional

from . import fsio, ledger, locate

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


def already_delivered(state_dir: str, session_id: str, to_harness: str) -> bool:
    """Has this session been delivered to this harness — **the last in file
    order** wins (invariant 6). If resume has written a reopen for this
    session, that reopen comes after the earlier delivered line, so "not yet
    delivered" wins — a resumed turn must be able to be handed off again (#22)."""
    delivered = False
    try:
        with open(_delivered_path(state_dir), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2 or parts[0] != session_id:
                    continue
                if parts[1] == REOPEN_MARKER:
                    delivered = False
                elif parts[1] == to_harness:
                    delivered = True
    except OSError:
        return False
    return delivered


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
) -> None:
    """Records that this session was delivered to this harness. Keeps the same thing from being pushed twice.

    `offset` is how far into the source session this delivery read (the last
    included event's offset+length) — appended as the 5th column (#27). Every
    reader of the old 4-column lines (already_delivered, last_delivered,
    delivered_order, status, cmd_log) only looks at parts[0]/parts[1], so
    they keep working even with an extra column. If offset is None (caller
    doesn't know), leave it as 4 columns — exactly the same shape old readers expect."""
    if watermark is None:
        return
    fields = [watermark.session_id, to_harness, watermark.harness, "{:.0f}".format(epoch)]
    if offset is not None:
        fields.append(str(offset))
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
) -> Optional[Watermark]:
    """Is there a foreign session to tell this session about. **This is the
    concurrency seam.**

    v1's entire sequential-use assumption lives inside this function.
    Upstream (ledger, adapters, index, pin, guard) and downstream (mint,
    brief, agents_md) are already order-independent, and since a reader
    streams an append-only log from byte 0 to EOF, they already tolerate a
    still-growing file.

    v2 changes the return type to List[Watermark] and **adds** omhc/stale.py
    as a second consumer of the same stream (not a modification of existing
    code). The ordering key is neither file mtime nor a per-record
    timestamp — it's the ledger's session-start epoch and the byte offset
    within the source file. This machine's largest transcript has 254
    instances of timestamps going backward.

    cli._backfill_foreign_sessions uses the same key — when discover() finds
    and backfills a harness's sessions missing from the ledger (because an
    untrusted hook never ran), it compares that session's start epoch (the
    adapter reads it from session_meta etc.) against that harness's latest
    start epoch already in the ledger to decide append order. **Still not
    mtime** — this is exactly the same rule as this function, "order only by
    session-start epoch"; the only new part is comparing epochs across
    different files (ledger vs. rollout).
    """
    state = locate.state_dir(repo_key, home=home)
    if is_off(state):
        return None

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
        # **Stop at the most recent foreign session.** If it's already delivered, return None.
        #
        # Continuing to walk back would inject yesterday's session as "just
        # happened" — after cx3 (Wed) is delivered, the next session gets
        # cx2 (Tue), then the one after gets cx1 (Mon), sending increasingly
        # stale handoffs. A stale marker is worse than no marker.
        if already_delivered(state, str(session), my_harness):
            return None

        epoch = float(row.get("epoch") or 0.0)
        if epoch and now and (now - epoch) > MAX_AGE_SECONDS:
            # Too old to be work worth continuing.
            return None

        mark = Watermark(
            repo_key=repo_key,
            harness=str(harness),
            session_id=str(session),
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
        return mark
    return None
