from __future__ import annotations

import argparse
import json
import os
import re
import select
import sys
import time
from typing import Dict, List, Optional, Tuple

from . import (
    adapters, agents_md, brief, deliver, due, fsio, gate, hookconf, index, ledger,
    locate, managed_block, pin, watch,
)
from .adapter import AdapterUnavailable, SessionRef

PROG = "omhc"
NOTES_NAME = "notes.txt"
ARTIFACT_NAME = "omhc.txt"

# The "recent deliveries" window pull rate looks at (§9, #25). Leaving the
# denominator as all of delivered.tsv would keep old deliveries in it
# forever, making the pull rate look like it's slowly falling.
PULL_RATE_WINDOW = 20


def _stdin_text() -> str:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    except Exception:
        return ""


# Upper bound (seconds) on how long --dry-run waits for stdin. Unrelated to
# the hook budget — this is a path a human calls by hand.
_DRY_RUN_STDIN_TIMEOUT = 0.2
# A hook payload is a bit over 1KB. A writer that never stops (like `yes |`)
# always has data ready, so the timeout would never fire — cap by size too
# (review).
_DRY_RUN_STDIN_CAP = 1 << 20


def _dry_run_stdin_text() -> str:
    """stdin read for `--dry-run` (without `--stdin`) — reads, but doesn't
    block forever (#27 review). One documented use pipes a payload from a
    different cwd, like `echo '{"cwd": R}' | omhc brief --dry-run` — not
    reading at all would break that. But using plain `sys.stdin.read()`
    would hang forever waiting for EOF if the other end of the pipe sends one
    line and just leaves it open (isatty() is False, not a TTY). So this
    only asks select "is there anything to read right now", reads it if so,
    and stops if no more data arrives within the timeout — EOF (writing and
    closing, like echo does) also counts as "something to read" and returns
    immediately."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        fd = sys.stdin.fileno()
        chunks = []
        total = 0
        while total < _DRY_RUN_STDIN_CAP:
            ready, _w, _x = select.select([fd], [], [], _DRY_RUN_STDIN_TIMEOUT)
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        # select doesn't work on pipes on Windows; fileno()/read() can also
        # raise on a closed stream. Never raise here even though this isn't
        # the hook path.
        return ""


def _state_for(home: Optional[str], start: Optional[str] = None):
    root = locate.resolve_repo_root(start)
    key = locate.repo_key(root)
    return root, key, locate.state_dir(key, home=home)


# --- mark -------------------------------------------------------------------


# An untrusted Codex hook silently skips mark (measured, see codex_cli.py).
# That leaves that Codex session out of the ledger forever, and since due()
# only reads the ledger, Codex->Claude breaks even when Claude's own hook is
# working fine. So **Claude's** mark piggybacks to backfill the other
# harness's (Codex's) sessions into the ledger — due() itself is unchanged.
BACKFILL_CAP = 5
# #22: overflow past the cap, and the rest that discover()'s time budget
# never got to see, are never backfilled by any later mark — the next
# call's newest_start is already the most recent of what got picked this
# round, so an older un-backfilled session is forever caught by "older than
# the ledger's latest start". This is harmless anyway because two things
# line up: due() only needs to see the single most recent eligible foreign
# session, and discover() uses the **same** headless filter
# (allow_headless()) as brief's eligible (#21) — so what discover() fills in
# and what due() wants are usually the same set. The only way it breaks is
# OMHC_ALLOW_HEADLESS changing between the mark and a later brief (then an
# interactive session that appeared in between could sit behind a headless
# dummy, never get backfilled, and get pushed out by the cap) — an
# infrequent config change, accepted for v1.
# Only spends part of the hook budget (150ms). The expensive part is
# discover() itself (Codex scans date directories), so this deadline is
# passed straight through to discover() too, letting the adapter cut its own
# scan short — measuring only here would let the discover() call itself
# finish late and delay this mark call, and session start with it.
BACKFILL_TIME_BUDGET = 0.08

# How many recent sessions to check at once for one harness (used both by
# `_backfill_foreign_sessions`'s rebaselining and `_reactivate_grown_sessions`).
# Unlike discover()/list_sessions()'s full scan (#22: a session started more
# than 14 days ago must still be able to have its resume caught — that's
# outside discover()'s SCAN_DAYS window), this only stats paths already
# recorded in the ledger, so 20 is negligible within the hook budget.
REACTIVATE_SCAN_CAP = 20


def _ref_repo_key(ref) -> Optional[str]:
    """The repo key this ref actually belongs to. Delegates to
    `locate.owning_repo_key` (logic that originally lived in this function —
    codex_cli.health() uses the same filter)."""
    return locate.owning_repo_key(ref.cwd)


# The rebase marker's event value. It's a ledger row with no session/path
# (it doesn't belong to any one session), so due()/log rank/known_sessions/
# newest_start/codex health all filter it out automatically via their
# existing "event=='start'"-only filters — not adding the field at all is
# safer than enforcing "unknown readers ignore it" purely by convention
# (#28).
REBASE_EVENT = "rebase"


def _append_rebase_marker(adapter_id: str, key: str, home, now: float) -> None:
    """A single row declaring, for every known session of `adapter_id`, "the
    baseline position is at least here from this point on". Leaves just this
    one row instead of individual seen rows (#22 symptom: every backfill
    round used to add one row per known session) — since there's no
    session/path, known_sessions/newest_start/log rank/codex health all
    ignore it as-is (all of them filter on event=='start' or on a session
    field existing)."""
    ledger.append({
        "repo": key, "harness": adapter_id, "event": REBASE_EVENT, "via": "scan",
        "epoch": now,
    }, home=home)


def _distinct_sessions_with_path(rows: List[dict], *, exclude=frozenset()) -> List[str]:
    """Walks `rows` backward and returns distinct session ids with a path,
    most-recent first — both "candidates to look at this round" (the
    writer, cut to at most `REACTIVATE_SCAN_CAP`) and "the set of sessions
    the marker actually covers" (the reader, reconstructed with the same
    function after slicing `own_rows` to the segment **before** the marker)
    must use the same ranking (#28 review round 2) — implementing them
    separately would reintroduce the exact bug this fixed the moment they
    diverge: "the marker covers sessions it never actually checked"."""
    out = []
    seen = set()
    for row in reversed(rows):
        sid = row.get("session")
        if not sid or sid in exclude or sid in seen or not row.get("path"):
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _rebaseline_after_fresh_start(adapter_id: str, key: str, root: str, home,
                                  own_rows: List[dict], added: set,
                                  now: float, deadline: float) -> None:
    """Review (#22 re-examined): this round just backfilled a newer session
    (B) for `adapter_id` into the ledger. If other existing sessions' (A's)
    baselines still sit before B's start row, the next
    `_reactivate_grown_sessions` round compares A's "current size" wholesale
    against that stale baseline — lumping in even a **genuine** resume that
    happened to A after B arrived as "already superseded by B", absorbing it
    into a seen row, and never judging it again (even though what got
    absorbed was actually A's one new turn).

    At **the very moment** B just arrived (when A is most likely not to have
    grown further yet), this re-stats the A's and leaves a seen row for them
    after B — so any real growth in A afterward gets compared against this
    new baseline (already after B) and judged cleanly.

    Review (round 3, t5): this function is only called when B arrived via
    **backfill** (newly caught by discover -> known_sessions) — if B leaves
    its own start row directly through its own trusted hook,
    `_backfill_foreign_sessions` sees that session as "already known"
    (already in known_sessions) and doesn't backfill it again, so this
    function never gets called at all. That path is instead caught by the
    lazy rebaseline inside `_reactivate_grown_sessions` (see its comment) —
    this function isn't removed because, on the backfill origin, it stamps
    at **the exact moment B arrives** (when A is most likely not to have
    grown yet), so the ambiguous window that could get absorbed (until the
    next mark) is shorter than the lazy path's — the two paths converge on
    the same result, but this one gets there earlier.

    #28: among the A's that need rebaselining here, ones that never actually
    grew (size unchanged) are absorbed into one `rebase` marker instead of
    individual seen rows — measured (#22): every backfill round that filled
    5 of 20 new Codex sessions re-stat'd up to 20 other sessions and left
    seen rows, adding 34 rows. A session that actually grew (agent
    monologue, etc.) still gets its own seen row — **written before the
    marker**: the marker only covers "sessions confirmed not to have grown
    this round", and a grown session updates its own position, so the marker
    doesn't cloud its judgment.

    Review (#28 round 1): the marker is a declaration that "**the sessions
    actually scanned this round** were confirmed not to have grown, at least
    up to here" — if the deadline cuts it off midway (`break`), a stat fails
    with a transient OSError, or the seen row itself gets dropped by
    `ledger.append`'s cap, this round is not "everything scanned got
    confirmed" — applying the marker to an unconfirmed session anyway would
    wrongly push its position back (to something more recent) than reality.
    Repro: if A already grew before B (including a human turn) but wasn't
    confirmed this round due to deadline/OSError, and the marker still gets
    stamped because of some other session (which didn't grow), then next
    round A's position gets pushed past the marker, and that ambiguous
    pre-growth (which should have been absorbed) gets misjudged as a clear
    resume — due() returns A instead of B. So this tracks whether the round
    was exhaustive (`complete`), and only folds into one marker when it was;
    otherwise it leaves individual seen rows (the old way) for only what was
    confirmed.

    Review (#28 round 2): `complete` is **unrelated to exceeding the cap** —
    a session that fell outside this round's candidates entirely because of
    the cap (`REACTIVATE_SCAN_CAP`) isn't "unconfirmed", it's "this marker
    never promised anything about it in the first place" (the range the
    marker covers is itself reconstructed on the `_reactivate_grown_sessions`
    side as "the top-N at the time this marker was written" — see its
    comment). Tying the cap to `complete` (a mistake in round 1) would
    permanently fall back to individual seen rows for any repo with more
    than 20 known sessions, reproducing exactly the row explosion this was
    meant to fix (measured: at n=25/60, same 84 rows/80 seen as HEAD)."""
    others = _distinct_sessions_with_path(own_rows, exclude=added)[:REACTIVATE_SCAN_CAP]
    complete = True
    unchanged = []  # (sid, path, size) — confirmed not to have grown this round
    for sid in others:
        if time.time() > deadline:
            complete = False
            break
        # This session's last path/size — used as the fallback for review (round 3 #3).
        path = None
        prior_size = None
        for row in own_rows:
            if row.get("session") != sid:
                continue
            if row.get("path"):
                path = row.get("path")
            if "size" in row:
                try:
                    prior_size = int(row["size"])
                except (TypeError, ValueError):
                    pass
        if not path:
            continue
        try:
            cur_size = os.stat(path).st_size
        except OSError:
            # Transient failure — couldn't confirm this session "didn't grow" (#28 review).
            complete = False
            continue
        # fallback: the previously known baseline, if any — so the baseline
        # doesn't get pushed mid-record even when no newline is found within
        # 64KB. None if there isn't one (leaves size as-is). Substituting 0
        # would re-read from the start and falsely reactivate on an old human
        # turn (reproduced in review).
        aligned = fsio.line_aligned_size(path, cur_size, fallback=prior_size)
        if prior_size is not None and aligned == prior_size:
            # Didn't grow — if this round turns out complete, one marker
            # suffices for this session (#28). Otherwise falls back below to
            # the old way (individual seen).
            unchanged.append((sid, path, aligned))
            continue
        if not ledger.append({
            "repo": key, "harness": adapter_id, "session": sid,
            "event": "seen", "via": "scan",
            "size": aligned,
            "epoch": now, "path": path, "cwd": root,
        }, home=home):
            complete = False
    if unchanged:
        if complete:
            _append_rebase_marker(adapter_id, key, home, now)
        else:
            for sid, path, size in unchanged:
                ledger.append({
                    "repo": key, "harness": adapter_id, "session": sid,
                    "event": "seen", "via": "scan", "size": size,
                    "epoch": now, "path": path, "cwd": root,
                }, home=home)


def _backfill_foreign_sessions(harness: str, root: str, key: str, state: str,
                                home, now: float, *,
                                deadline: Optional[float] = None) -> Dict[str, set]:
    """If `deadline` isn't given, times itself off this call's own budget
    (the old behavior, kept for standalone calls/test compatibility).
    `cmd_mark` needs to share the **same** budget as
    `_reactivate_grown_sessions` instead of doubling the hook time, so it
    passes its own deadline through.

    Returns: {adapter_id: {newly backfilled session_id, ...}} —
    `_reactivate_grown_sessions` uses this to judge "this mark just
    backfilled a newer session for this harness" (condition b)."""
    if deadline is None:
        deadline = time.time() + BACKFILL_TIME_BUDGET
    fresh: Dict[str, set] = {}
    rows = ledger.read(repo_key=key, home=home)
    for adapter_id in sorted(adapters.REGISTRY):
        if adapter_id == harness:
            continue
        if time.time() > deadline:
            break
        try:
            refs = list(adapters.get(adapter_id, home=home).discover(
                root, deadline=deadline))
        except Exception:
            continue
        if not refs:
            continue
        own_rows = [r for r in rows if r.get("harness") == adapter_id]
        known_sessions = {str(r.get("session")) for r in own_rows if r.get("session")}
        newest_start = 0.0
        for r in own_rows:
            # A grew row's epoch is not the session's actual start time — it's
            # the "now" when the resume was detected (_reactivate_grown_sessions)
            # — mixing this into newest_start would make a genuinely older,
            # not-yet-backfilled session get caught by "already older than
            # the latest" and never backfilled.
            if r.get("event") == "start" and not r.get("grew"):
                newest_start = max(newest_start, float(r.get("epoch") or 0.0))

        # First filter to only the eligible ones, then sort **over the whole
        # set** — cutting from the oldest end first (a review defect) would
        # leave 5 of 8 all old, so due() would return the 4th-most-recent
        # session instead of the latest. The newest N must be selected.
        #
        # "Already known" is filtered by **id only** (known_sessions) — not
        # by epoch. session_meta.timestamp is in whole seconds, so two
        # distinct sessions can start in the same second; judging by epoch
        # (#22, half-open <=) would kill one of them even though the ids
        # differ. So genuinely-new is judged **strictly** against
        # newest_start (<), and "already in the ledger" is checked separately
        # by id.
        eligible = []
        for ref in refs:
            if not ref.session_id or ref.session_id in known_sessions:
                continue
            if ref.epoch < newest_start:
                continue
            if now and (now - ref.epoch) > due.MAX_AGE_SECONDS:
                continue
            if _ref_repo_key(ref) != key:
                continue
            eligible.append(ref)
        eligible.sort(key=lambda r: r.epoch)
        selected = eligible[-BACKFILL_CAP:]

        # Once picked, append **in ascending order** — the ledger's append
        # order needs to match start order for due() (which reads the ledger
        # backward to pick "the most recent") to return the actually latest
        # session. Since the newest N are already picked, the time budget
        # doesn't cut this loop short from here on — cutting it short could
        # leave only the older ones instead of the just-picked latest session
        # (a review defect). Only up to BACKFILL_CAP rows get written anyway,
        # so the cost is negligible.
        for ref in selected:
            row = {
                "repo": key,
                "harness": adapter_id,
                "session": ref.session_id,
                "event": "start",
                "epoch": ref.epoch,
                "path": ref.source_path,
                "cwd": root,
                # health() excludes this value from the evidence for "the
                # hook actually ran" (codex_cli.py) — backfill must not be
                # able to disguise an untrusted hook.
                #
                # #22: even though these sessions' real start times may
                # precede this mark's own start row, they get appended
                # **after** it in the ledger (mark writes its own row first,
                # backfill comes next). due() scans the ledger per-harness
                # (skipping rows where harness == my_harness), so this is
                # harmless — the only effect is on append order within the
                # same harness, but what's appended here belongs to a
                # different harness.
                "via": "scan",
                # Baseline for `_reactivate_grown_sessions` — discover()
                # already read this via os.path.getsize, so there's no extra
                # stat cost.
                "size": ref.size,
            }
            if ledger.append(row, home=home):
                fresh.setdefault(adapter_id, set()).add(ref.session_id)

        if fresh.get(adapter_id):
            # Just backfilled a new session for this harness — move other
            # existing sessions' baselines to right after B, on the spot
            # (review #1, see _rebaseline_after_fresh_start above).
            _rebaseline_after_fresh_start(adapter_id, key, root, home, own_rows,
                                          fresh[adapter_id], now, deadline)
    return fresh


# Cap on how much of a grown tail to read. Measured (17.7MB tail): 396.6ms —
# without a cap, reading scales directly with the growth and blows through
# the hook budget (150ms). 1MB is about 22ms at the same measured rate
# (~22.4us/KB) — stop_at_human_turn usually cuts things off much earlier, so
# this cap only guards the worst case: "a big growth with no human turn in
# it at all".
REACTIVATE_TAIL_CAP = 1_000_000


def _reactivate_grown_sessions(harness: str, root: str, key: str, state: str,
                               home, now: float, deadline: float,
                               fresh: Dict[str, set]) -> None:
    """#22's last gap: on an untrusted Codex hook, `codex exec resume`
    doesn't create a new rollout — it **appends to the same file** — and
    doesn't rewrite `session_meta` either. `_backfill_foreign_sessions`
    orders by discover()'s first-line start time, so it misses this resume
    (the original start time is unchanged); if the session was already
    delivered, `already_delivered` stops due() right there.

    File size growth only tells us "something changed" (invariant 6: it's a
    change-detection signal, not an ordering basis — ledger append order is
    still the only source of order). Whether the growth contains a new human
    turn is confirmed directly via `read_session_since` (agent monologue,
    turn_aborted alone aren't treated as a resume).

    Skipped if this harness is in `fresh` (sessions this mark just backfilled
    via `_backfill_foreign_sessions`) — if a newer session already came in
    during the same call, a resume row isn't stacked after it too (ledger
    append order is what due() uses to judge "most recent", so stacking it
    would let the resumed old session win over the just-backfilled newer
    one).

    #28: under condition (a), a session that's superseded but didn't grow
    used to get its own seen row on every single mark (the lazy rebaseline
    behind a trusted hook B runs every time) — such sessions get folded into
    one `_append_rebase_marker` instead. A grown session (the one case that
    splits off from "didn't grow") still gets its own seen row, written
    before the marker.

    Review (#28 round 1): if the deadline cuts a session off midway
    (`break`), a stat fails with a transient OSError, or a seen/grew row gets
    dropped by `ledger.append`'s cap, this round is not "everything scanned
    got confirmed" — applying the marker to an unconfirmed session anyway
    would wrongly push its position forward (past the marker). So this uses
    the same `complete` rule as `_rebaseline_after_fresh_start` — a marker
    only when complete, otherwise individual seen rows for only what was
    confirmed.

    Review (#28 round 2): `complete` is unrelated to exceeding the cap
    (`REACTIVATE_SCAN_CAP`) — the cap is instead handled on the **reading
    side** (`marker_covers`, below). A marker doesn't mean "all of this
    harness's known sessions" — it means "**the top-N at the time this
    marker was written** (picked by the same ranking function) were
    confirmed not to have grown". So whether a given session's baseline
    position may be advanced by the marker has to be judged by whether that
    session was in the top-N **at the marker's write time**
    (reconstructed by slicing `own_rows` to before the marker and running
    the same `_distinct_sessions_with_path` — this yields exactly the same
    set the writer actually scanned: a session that was already in the
    top-N within that segment, and grows this round to get a new row, only
    shifts rank **within** that same top-N, it doesn't push another session
    out — a session outside the cap was never a candidate this round to
    begin with, so it can't get a new row).

    Repro (review round 2, "x-far"): a session outside the cap had already
    grown before B (including a human turn), was never confirmed by this
    marker, but later re-enters via its own hook and gets a new start row
    appended to the end of `own_rows` — that row sits in the segment
    **after** the marker, so it's not caught by the top-N reconstruction at
    marker-write time (`own_rows[:last_marker_pos]`). `sid in marker_covers`
    stays False, the baseline position is left unchanged, and the
    superseded judgment stays accurate — due() doesn't misjudge that
    ambiguous prior growth as a resume.

    Hook path, so never raises — the caller (cmd_mark) wraps it entirely.
    """
    rows = ledger.read(repo_key=key, home=home)
    for adapter_id in sorted(adapters.REGISTRY):
        if adapter_id == harness:
            continue
        if time.time() > deadline:
            return
        if fresh.get(adapter_id):
            continue
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            continue
        reader = getattr(inst, "read_session_since", None)
        if reader is None:
            continue
        # Review: having the method and this adapter actually rendering a
        # verdict are different things — an implementation like Claude's,
        # which always returns None (contractually "can't tell"), would pile
        # up meaningless seen rows on every growth (measured: mark x4 -> seen
        # x4). Check **once** per adapter, and skip the whole adapter if it's
        # None — decided before stat'ing any real path.
        try:
            probe = reader(
                SessionRef(adapter_id=adapter_id, session_id="", source_path="",
                          cwd=None, epoch=0.0, size=0),
                0,
            )
        except Exception:
            probe = None
        if probe is None:
            continue

        own_rows = [r for r in rows if r.get("harness") == adapter_id]
        if not own_rows:
            continue

        # Position of the marker that advances, all at once, the position of
        # sessions confirmed "didn't grow" this round (#28) — having no
        # session/path keeps it out of the known_sessions/newest_start-style
        # filters above, but it's used here to compute baseline **position**
        # (condition a).
        last_marker_pos = -1
        for i, row in enumerate(own_rows):
            if row.get("event") == REBASE_EVENT:
                last_marker_pos = i

        # #28 review round 2: the set of sessions this marker actually
        # covers — reconstructed by cutting own_rows to just the segment
        # before the marker (`[:last_marker_pos]`) and applying the same
        # ranking function to get the top-N **at write time** (see the "x-far"
        # repro in this function's docstring above). If there's no marker
        # (never stamped yet), it covers nothing, naturally.
        marker_covers = (
            set(_distinct_sessions_with_path(
                own_rows[:last_marker_pos])[:REACTIVATE_SCAN_CAP])
            if last_marker_pos >= 0 else set()
        )

        # This harness's recent distinct sessions (most recent first), only
        # those with a path — no discover(), no date window. A session
        # started 14 days ago still gets its resume caught as long as the
        # ledger still holds a path for it.
        recent_sessions = _distinct_sessions_with_path(own_rows)[:REACTIVATE_SCAN_CAP]
        complete = True  # the cap no longer affects completeness (#28 round 2).

        unchanged = []  # (sid, path, size) — confirmed not to have grown this round
        for sid in recent_sessions:
            if time.time() > deadline:
                complete = False
                break
            path = None
            baseline = None
            baseline_pos = -1
            for i, row in enumerate(own_rows):
                if row.get("session") != sid:
                    continue
                if row.get("path"):
                    path = row.get("path")
                if "size" in row:
                    # Garbage size must not break mark either — skip it and
                    # keep the previous baseline.
                    try:
                        baseline = int(row["size"])
                        baseline_pos = i
                    except (TypeError, ValueError):
                        pass
            if not path:
                continue
            try:
                cur_size = os.stat(path).st_size
            except OSError:
                # Transient failure — couldn't confirm this session "didn't grow" (#28 review).
                complete = False
                continue

            if baseline is None:
                # First observation — resume status unknown yet. Just leaves
                # a baseline for the next mark to compare against. Snaps to
                # a line boundary instead of using the raw os.stat size
                # (review) — a baseline mid-record would make the
                # skip-to-newline logic skip the whole record once it
                # finishes being written.
                if not ledger.append({
                    "repo": key, "harness": adapter_id, "session": sid,
                    "event": "seen", "via": "scan",
                    # First observation, so there's no prior baseline. Don't
                    # substitute 0 — the next judgment would read from the
                    # start and re-surface an old human turn as if it were
                    # new (reproduced in review). If none is found, leaves
                    # size as-is (a known limitation only while writing a
                    # record over 64KB).
                    "size": fsio.line_aligned_size(path, cur_size),
                    "epoch": now, "path": path, "cwd": root,
                }, home=home):
                    complete = False
                continue

            # Condition (a): if a **different** session's start row for the
            # same harness landed after this baseline (however it got there
            # — this harness's own backfill, or that session's own trusted
            # hook), this session has already been superseded by something
            # newer — don't reactivate it. Review (round 3, t5): this check
            # runs **even if nothing grew** — otherwise the baseline would
            # sit before B's start row forever, and if B entered directly
            # through a trusted hook that `_rebaseline_after_fresh_start`
            # never saw, this session would never get judged again (the
            # moment that hook writes to the ledger, this function isn't
            # even called — args.harness is that harness itself, so it's
            # excluded from `_reactivate_grown_sessions`'s targets from the
            # start).
            #
            # Just skipping past it (the old bug) would leave this baseline
            # unchanged forever, and no later mark could ever judge this
            # session again — so it's absorbed by raising the baseline via a
            # seen row. If A grows **again** after B (the baseline is now
            # past that seen row, no longer before B's start row), that gets
            # caught fresh as a new judgment.
            #
            # **Known limitation:** if A had already grown between B's start
            # row and this round (unlike the "didn't grow" case), there's no
            # way to tell whether that growth was before or after B — size
            # alone can't order them, so it's absorbed (a remaining
            # limitation, noted in the README).
            #
            # #28: baseline **position** is whichever is later — this
            # session's own last size row, or this harness's last rebase
            # marker (which can be later, since this session may have been
            # confirmed "didn't grow" and absorbed by a marker in a previous
            # round). After the marker, this session's real position is at
            # least that far along, so growth after it can be judged
            # unambiguously (the marker itself doesn't know whether this
            # session actually grew — so size is left untouched and only
            # used for position math).
            #
            # #28 round 2: but only if that marker actually covered **this**
            # session (`marker_covers`, above) — otherwise a session that was
            # outside the cap and never confirmed at marker-write time
            # ("x-far") could turn its ambiguous prior growth into a clear
            # resume just by re-entering later through its own hook (review
            # repro, this function's docstring above).
            effective_pos = (
                max(baseline_pos, last_marker_pos)
                if sid in marker_covers else baseline_pos
            )
            superseded = any(
                row.get("event") == "start" and row.get("session") != sid
                for row in own_rows[effective_pos + 1:]
            )
            if superseded:
                if cur_size > baseline:
                    if not ledger.append({
                        "repo": key, "harness": adapter_id, "session": sid,
                        "event": "seen", "via": "scan",
                        # fallback=previous baseline (review round 3 #3) —
                        # even if not found, the baseline doesn't get pushed
                        # backward (mid-record).
                        "size": fsio.line_aligned_size(path, cur_size,
                                                       fallback=baseline),
                        "epoch": now, "path": path, "cwd": root,
                    }, home=home):
                        complete = False
                else:
                    # Didn't grow — if this round turns out complete, absorb
                    # via one marker at the end of the round instead of an
                    # individual seen row (#28).
                    unchanged.append((sid, path, baseline))
                continue
            if cur_size <= baseline:
                continue

            since = None
            try:
                since = reader(
                    SessionRef(
                        adapter_id=adapter_id, session_id=sid,
                        source_path=path, cwd=root, epoch=now, size=cur_size,
                    ),
                    baseline,
                    max_bytes=REACTIVATE_TAIL_CAP,
                    stop_at_human_turn=True,
                )
            except Exception:
                since = None
            if since is None:
                # Can't judge this round — leaves the baseline untouched and
                # retries on the next mark (safer than leaving a wrong
                # baseline).
                continue
            found_human_turn = any(
                ev.verb == "said" and ev.author == "human" for ev in since.events
            )

            if found_human_turn:
                # Review: uses `since.end_offset`, not `cur_size` — if the
                # last line ended without a newline (may still be mid-write),
                # that record hasn't been safely read in full, so putting it
                # into the baseline would make the next read skip that whole
                # line (review #2).
                if not ledger.append({
                    "repo": key, "harness": adapter_id, "session": sid,
                    "event": "start", "via": "scan", "size": since.end_offset,
                    "grew": 1, "epoch": now, "path": path, "cwd": root,
                }, home=home):
                    complete = False
                try:
                    if due.already_delivered(state, sid, harness):
                        due.mark_reopened(state, sid, adapter_id, now)
                except Exception:
                    pass
                continue

            # There was growth, but not a human turn (agent monologue,
            # turn_aborted, task_complete, etc.) — review: uses
            # `since.end_offset` (not `cur_size`). If the cap (`max_bytes`)
            # kept the whole tail from being read, `end_offset` reflects only
            # what was actually read, so the next mark picks up the rest —
            # using `cur_size` as-is would permanently skip over any human
            # turn that might be in between.
            if not ledger.append({
                "repo": key, "harness": adapter_id, "session": sid,
                "event": "seen", "via": "scan", "size": since.end_offset,
                "epoch": now, "path": path, "cwd": root,
            }, home=home):
                complete = False

        if unchanged:
            if complete:
                _append_rebase_marker(adapter_id, key, home, now)
            else:
                for sid, path, size in unchanged:
                    ledger.append({
                        "repo": key, "harness": adapter_id, "session": sid,
                        "event": "seen", "via": "scan", "size": size,
                        "epoch": now, "path": path, "cwd": root,
                    }, home=home)


def _recent_hook_start(key: str, harness: str, session: str, home,
                       now: float) -> bool:
    """Is there a **hook-written** (not via:"scan") start row for this
    session within due()'s window, and younger than half the age limit
    (#30 review)?

    - Window: read with the same default limit as due(). Skipping based on a
      row pushed outside the window would make due() not see that session at
      all.
    - via:"scan" doesn't count: if only a backfill-written row exists, a hook
      still needs to leave evidence it actually ran (codex health).
    - Age: due() ages a session by its most recent start row's epoch. Never
      leaving a compact row would drop a session in use for days once it
      exceeds MAX_AGE — if it's older than half that, leave one to refresh
      the age (this is only a deadline judgment, not an ordering basis).
    False if unreadable — when in doubt, leave the row (the old behavior)."""
    try:
        for r in ledger.read(home=home, repo_key=key):
            if (r.get("event") == "start" and r.get("harness") == harness
                    and r.get("session") == session and r.get("via") != "scan"):
                epoch = float(r.get("epoch") or 0)
                if now - epoch < due.MAX_AGE_SECONDS / 2:
                    return True
    except Exception:
        return False
    return False


def cmd_mark(args, *, home=None, out=sys.stdout) -> int:
    """Records a session start in the ledger. Called by the hook. About one
    220-byte line."""
    raw = args.stdin if args.stdin is not None else _stdin_text()
    payload = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            payload = {}
    start = str(payload.get("cwd") or "") or None
    root, key, state = _state_for(home, start)
    if locate.refused_root(root):
        # Hook path — leave nothing in the ledger and exit quietly (invariant 2).
        return 0
    session = gate.session_id_from_hook_payload(raw) or ""
    row = {
        "repo": key,
        "harness": args.harness,
        "session": session,
        "event": args.event,
        "epoch": round(time.time(), 0),
        "path": str(payload.get("transcript_path") or ""),
        "cwd": root,
    }
    # Whether a session had a human turn isn't judged here (#21). At
    # SessionStart time, Claude's transcript isn't written yet so the
    # judgment always had to fail open, and Codex could be permanently
    # marked non-interactive if it had no rollout yet. The judgment happens
    # at brief time instead, once the adapter can look at the real file
    # (the eligible set brief.compute passes to due).
    #
    # After auto-compaction, SessionStart fires again on the same session
    # with source:"compact" (measured on Codex, #30). That's not a new human
    # turn, so if the hook already left a row for this session recently, no
    # new start row is left (condition: _recent_hook_start) — the gate and
    # brief's redelivery conditions kept this harmless, but it was still
    # inflating the machine-wide shared ledger for nothing. If the session
    # isn't in the ledger yet (e.g. omhc was installed mid-session and
    # missed startup), this row is its first record, so it's still left.
    compact = str(payload.get("source") or "") == "compact"
    if not (compact and session and _recent_hook_start(
            key, args.harness, session, home, row["epoch"])):
        ledger.append(row, home=home)
    # SessionStart's `source` vocabulary is shared between Claude Code and
    # Codex (both measured). "resume" means a new turn was appended to the
    # same session — if that session had already been delivered to the other
    # harness, due() would stop at already_delivered() and never surface the
    # resumed turn (#22). "compact" doesn't carry the same signal — it only
    # compressed context, with no new human turn, so there's nothing to
    # redeliver. This reopen is still only a hint that "it may have reopened"
    # — an empty-prompt resume emits source:"resume" without leaving a new
    # human turn, and if mark/brief run concurrently this reopen could even
    # land after the turn brief just delivered (#27). Still, removing this
    # wouldn't fix the bridge — it's the only signal that gets due() to look
    # at this session as a candidate again. Whether it's actually new is
    # judged by brief.compute, comparing delivered.tsv's offset (5th column)
    # against this session's human said events — mark only decides "should
    # this be a candidate", brief decides "is there anything to send". The
    # two have different responsibilities.
    if session and str(payload.get("source") or "") == "resume":
        try:
            due.mark_reopened(state, session, args.harness, row["epoch"])
        except Exception:
            pass
    # Backfills the other harness's sessions into the ledger (see comment
    # above). Hook path, so even on failure mark itself must always exit 0
    # with empty stdout (invariant 2). The two backfill phases (new-session
    # scan, resume detection) share the same hook budget — timing each
    # separately would double the total spent.
    try:
        if not due.is_off(state):
            deadline = time.time() + BACKFILL_TIME_BUDGET
            fresh = _backfill_foreign_sessions(args.harness, root, key, state, home,
                                               row["epoch"], deadline=deadline)
            _reactivate_grown_sessions(args.harness, root, key, state, home,
                                       row["epoch"], deadline, fresh)
    except Exception:
        pass
    # Collapses a stale AGENTS.md block on any omhc call.
    try:
        agents_md.collapse(root)
    except Exception:
        pass
    # When this harness reads AGENTS.md-style files before its own hook fires
    # at session start (#36), the judgment "it's already been read, so drop
    # it" is harness-specific knowledge, so it's delegated to the adapter (an
    # optional method, the same pattern as discover/health — see
    # on_session_start_mark in adapter.py). Not called on compact — that's
    # not a new human turn, just the same session continuing, so there's no
    # reason to re-judge a block that session already consumed.
    if not compact:
        try:
            adapters.get(args.harness, home=home).on_session_start_mark(
                root, source=str(payload.get("source") or ""), epoch=row["epoch"])
        except Exception:
            pass
    # Removes old (>24h) omhc outbox files (#36) — hook path, so only the
    # cheap version (one listdir, returns immediately if empty).
    try:
        deliver.prune_outbox(root, now=time.time())
    except Exception:
        pass
    if args.verbose:
        out.write("marked {} {} in {}\n".format(args.harness, args.event, key))
    return 0


# --- note -------------------------------------------------------------------


def cmd_note(args, *, home=None, out=sys.stdout, err=None) -> int:
    err = err or sys.stderr
    root, _key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        err.write("{}\n".format(reason))
        return 2
    os.makedirs(state, exist_ok=True)
    path = os.path.join(state, NOTES_NAME)
    text = " ".join(args.text).strip()
    if not text:
        out.write("nothing to note\n")
        return 0
    # Attaches the write time — a note older than 7 days doesn't ride along in the handoff (#36).
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{:.0f}\t{}\n".format(time.time(), text.replace("\t", " ")))
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = [line for line in fh if line.strip()]
    out.write("noted ({} notes, {}B)\n".format(len(lines), os.path.getsize(path)))
    return 0


# --- pull rate ---------------------------------------------------------------


def _record_pull(key: str, state: str, via: str, home, session: Optional[str] = None,
                 tag: Optional[str] = None) -> None:
    """Records in the ledger that `show`/`log`/`trace` pulled an artifact.
    §9 pull-rate accounting.

    Never adds a `harness` key — due() only looks at event=="start",
    backfill filters own_rows by harness+start, and codex health() also
    counts only event=="start" as evidence "the hook ran". A pull row must
    never get mixed into any of those three. Failure here must never affect
    show/log/trace's result, so it's swallowed entirely (not the hook path,
    but fail-open is kept anyway).

    show passes the session it actually read. `#N` can land in an old
    session's index, so approximating with "most recently delivered" would
    inflate the pull rate for a session never actually looked at. Same for
    trace — it passes each session that had a match actually printed on
    screen (#35 review: if there are zero matches or they're ambiguous, this
    isn't called at all — approximating with "most recently delivered" would
    count an unseen session toward the pull rate). Only log, whose target
    isn't narrowed to one session, is charged to the most recently delivered
    session (delivered.tsv's last line)."""
    try:
        session = session or due.last_delivered(state)
        if not session:
            return
        row = {"repo": key, "event": "pull", "via": via, "session": session,
               "epoch": round(time.time(), 0)}
        if tag:
            row["tag"] = tag
        ledger.append(row, home=home)
    except Exception:
        pass


# --- log --------------------------------------------------------------------


def _index_files(state: str) -> List[str]:
    directory = os.path.join(state, "index")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names if n.endswith(".idx")]


def _all_session_ids(state: str) -> List[str]:
    return [os.path.basename(p)[: -len(".idx")] for p in _index_files(state)]


def _unique_prefix_len(ids: List[str], minlen: int = 8) -> int:
    """The shortest prefix length (>=minlen) that tells these ids apart.
    Codex's UUIDv7 only changes its first 8 chars every ~65 seconds, so a
    fixed 8-char cut collides often for sessions started in quick succession
    in the same repo (#15c). Around 13 chars is a nice-looking cap, but
    that's purely a display preference — it keeps growing past that until
    unique if it still hasn't split them. Otherwise `show` would reject a
    ref `log` just printed as "ambiguous", while the candidate list itself
    shows the same string twice (a review defect)."""
    uniq = list(dict.fromkeys(ids))
    n = minlen
    longest = max((len(i) for i in uniq), default=minlen)
    while n < longest and len({i[:n] for i in uniq}) < len(uniq):
        n += 1
    return n


def _session_log_rank(key: str, state: str, home):
    """The ranking function that gives `log`'s session ordering basis.

    Top priority is delivered.tsv appearance order (due.delivered_order —
    the same source as last_delivered(), so log's tail matches `show '#N'`'s
    default session). Ledger first-`start`-row order **can't** be used —
    mark writes its own session's row first and only backfills the earlier-
    started foreign session afterward (cmd_mark), so for every marked/
    backfilled session pair, ledger appearance order is inverted relative to
    actual delivery order (#18 review defect). A session never delivered
    (only indexed by watch) falls back deterministically to ledger
    first-start-row order, and failing that, index filename order."""
    delivered_rank = {sid: i for i, sid in enumerate(due.delivered_order(state))}
    ledger_rank = {}
    for i, row in enumerate(ledger.read(home=home, limit=0, repo_key=key)):
        if row.get("event") != "start":
            continue
        session = row.get("session")
        if session and session not in ledger_rank:
            ledger_rank[session] = i
    # Ascending index filename (=session id) order — even sessions with
    # neither ordering basis above need a stable order among themselves
    # (_index_files already sorts them that way).
    fallback_rank = {sid: n for n, sid in enumerate(_all_session_ids(state))}

    def _rank(session):
        if session in delivered_rank:
            return (0, delivered_rank[session])
        if session in ledger_rank:
            return (1, ledger_rank[session])
        return (2, fallback_rank.get(session, 0))

    return _rank


def cmd_log(args, *, home=None, out=sys.stdout) -> int:
    root, key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        out.write("{}\n".format(reason))
        return 1
    _record_pull(key, state, "log", home)
    _session_rank = _session_log_rank(key, state, home)

    rows = []
    for path in _index_files(state):
        session = os.path.basename(path)[: -len(".idx")]
        for row in index.rows(path):
            rows.append((session, row))
    # Sessions in _session_log_rank order, within a session by index seq —
    # timestamps aren't used as an ordering basis (invariant 6, #18).
    rows.sort(key=lambda pair: (_session_rank(pair[0]), pair[1].seq))

    if args.verb:
        rows = [r for r in rows if r[1].verb == args.verb]
    if args.grep:
        needle = args.grep.lower()
        rows = [r for r in rows if needle in r[1].arg.lower()]
    if args.file:
        rows = [r for r in rows if any(args.file in p for p in r[1].paths)]
    # A falsy-zero check would make `--last 0` dump everything — the exact
    # unbounded output F7 exists to prevent. A negative value would also
    # produce a nonsensical cut from the front.
    if args.last is not None and args.last >= 0:
        rows = rows[len(rows) - args.last :] if args.last else []

    # Made unique across the whole state directory, not just this batch —
    # otherwise a prefix shortened by filtering could collide with another
    # session that's off-screen, and passing that ref straight to `show`
    # would make it ambiguous (#10).
    n = _unique_prefix_len(_all_session_ids(state)) if rows else 8

    for session, row in rows:
        ref = "{}#{}".format(session[:n], row.seq)
        content = row.arg or ",".join(row.paths)
        if not content and row.verb == "said":
            # The index doesn't hold body text (to avoid duplicating the
            # archive) — show a hint instead of a blank line (#15a).
            content = "(text: omhc show {})".format(ref)
        out.write(
            "{} {} {} {}\n".format(
                ref,
                row.verb,
                "ok" if row.ok else "FAIL",
                content,
            )
        )
    if not rows:
        out.write("no events (run a session in another harness first)\n")
    return 0


# --- trace ------------------------------------------------------------------


# Default is `modified` only — like sessionwiki's file->session reverse
# index, "which session touched this file" is the primary question. `--all`
# widens it to traces where this file was merely **mentioned** (a path in a
# read, or in a command's arguments) — `inspected`/`ran` are the only other
# two verbs that populate `paths` even without modifying a file
# (codex_cli.py `_item_fact`, claude_code.py `_paths_of`).
_TRACE_DEFAULT_VERBS = frozenset({"modified"})
_TRACE_ALL_VERBS = frozenset({"modified", "inspected", "ran"})


def _norm_posix(path: str) -> Tuple[str, ...]:
    """Splits a path into '/'-delimited segments. The unit for suffix
    comparison (how many trailing segments match) — lets a comparison work
    on the raw string even where os.sep differs (e.g. Windows Codex logs)."""
    return tuple(seg for seg in path.replace("\\", "/").split("/") if seg not in ("", "."))


def _path_suffix_match(target_segs: Tuple[str, ...], candidate: str) -> bool:
    cand_segs = _norm_posix(candidate)
    return bool(target_segs) and len(cand_segs) >= len(target_segs) \
        and cand_segs[-len(target_segs):] == target_segs


def _normalize_against(base: str, raw: str) -> str:
    """Folds `raw` into an absolute path (realpath) — relative to `base` if
    it's a relative path. Even if the file doesn't currently exist (an old,
    deleted commit), realpath only normalizes and doesn't raise."""
    p = raw if os.path.isabs(raw) else os.path.join(base, raw)
    return os.path.realpath(p)


def cmd_trace(args, *, home=None, out=sys.stdout) -> int:
    """Finds indexed events that touched `<path>`, across sessions (#35,
    following sessionwiki's `trace` precedent). The index is only built at
    delivery time or by `watch`, so if neither harness has ever delivered or
    been watched in this repo, even a file changed just now won't show up —
    the result message itself says so."""
    root, key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        out.write("{}\n".format(reason))
        return 1

    # The argument can be relative to cwd or absolute, and the path recorded
    # in the index can be relative to the repo root (Codex FileChange's diff
    # header) or absolute (Claude Code's file_path) — both are folded via
    # realpath to compare on the same footing.
    target_abs = _normalize_against(os.getcwd(), args.path)
    target_segs = _norm_posix(args.path)

    verbs = _TRACE_ALL_VERBS if args.all else _TRACE_DEFAULT_VERBS

    session_harness: Dict[str, str] = {}
    for row in ledger.read(home=home, limit=0, repo_key=key):
        if row.get("event") == "start" and row.get("session"):
            session_harness.setdefault(row["session"], row.get("harness") or "?")

    exact: List[Tuple[str, object]] = []
    # normalized absolute path -> list of (session, row) that mentioned that
    # file. Falls back to suffix matching only when there's no exact match at
    # all, and even then, never uses it if it spans multiple distinct files
    # (ambiguous) — better to say "nothing" than to show the wrong file's
    # history (guideline: "allow only when unambiguous").
    #
    # Review (#35): a single row can contain two different suffix-matching
    # paths at once (e.g. paths=("omhc/adapters/__init__.py",
    # "omhc/__init__.py"), both ending in `__init__.py`) — stopping at the
    # first match would lose the fact that this row doesn't know which of
    # two candidate files it actually refers to, and a different row
    # containing just one of them would wrongly be judged "there's only one
    # bucket" (repro: sessions s1/s2). So every matching path goes into its
    # own bucket — the same row can land split across multiple buckets, and
    # that's fine (if ambiguous, no bucket is used at all to begin with).
    suffix_buckets: Dict[str, List[Tuple[str, object]]] = {}
    for idx_path in _index_files(state):
        session = os.path.basename(idx_path)[: -len(".idx")]
        for row in index.rows(idx_path):
            if row.verb not in verbs or not row.paths:
                continue
            matched_exact = False
            for p in row.paths:
                if _normalize_against(root, p) == target_abs:
                    exact.append((session, row))
                    matched_exact = True
                    break
            if matched_exact:
                continue
            for p in row.paths:
                if _path_suffix_match(target_segs, p):
                    suffix_buckets.setdefault(_normalize_against(root, p), []).append(
                        (session, row))

    matches = exact
    if not matches:
        if len(suffix_buckets) == 1:
            matches = next(iter(suffix_buckets.values()))
        elif len(suffix_buckets) > 1:
            candidates = sorted(
                locate.relativize(root, p) or p for p in suffix_buckets)
            if args.json:
                # Don't mix a guidance sentence into the JSON stream (review)
                # — emit an object so the consumer can re-query with the
                # candidates. Its shape differs from the result list (array),
                # so ambiguity isn't confused with an empty result.
                out.write(json.dumps({"ambiguous": candidates}, ensure_ascii=False) + "\n")
            else:
                out.write("ambiguous: {} matches {} — pass a longer path\n".format(
                    args.path, ", ".join(candidates)))
            return 0

    _session_rank = _session_log_rank(key, state, home)
    matches.sort(key=lambda pair: (_session_rank(pair[0]), pair[1].seq))

    if args.last is not None and args.last >= 0:
        matches = matches[len(matches) - args.last :] if args.last else []

    if not matches:
        if args.json:
            out.write("[]\n")
        else:
            out.write(
                "no indexed events touched {}; only delivered or watched sessions "
                "are indexed\n".format(args.path))
        return 0

    # #35 review: pull accounting is charged only to sessions **actually
    # printed on screen** — falling back to due.last_delivered() even with
    # zero matches or ambiguity would inflate the pull rate as if "most
    # recently delivered" got pulled every time, contradicting the README's
    # description ("a session actually looked at") (review). `log` uses that
    # approximation because its target isn't narrowed to one session, but
    # trace already has its sessions narrowed down, so there's no reason to
    # approximate.
    delivered = set(due.delivered_order(state))
    for session in dict.fromkeys(s for s, _ in matches):
        if session in delivered:
            _record_pull(key, state, "trace", home, session=session)

    # Made unique across the whole state directory, not just this batch, for
    # the same reason as `log` (#10) — a prefix shortened by filtering could
    # collide with another off-screen session, making that ref ambiguous
    # once passed to `show`.
    n = _unique_prefix_len(_all_session_ids(state))

    if args.json:
        out.write(json.dumps([
            {
                "ref": "{}#{}".format(session[:n], row.seq),
                "session": session,
                "harness": session_harness.get(session, "?"),
                "verb": row.verb,
                "ok": row.ok,
                "arg": row.arg,
                "paths": list(row.paths),
            }
            for session, row in matches
        ], ensure_ascii=False, indent=2) + "\n")
        return 0

    for session, row in matches:
        ref = "{}#{}".format(session[:n], row.seq)
        content = row.arg or ",".join(row.paths)
        out.write(
            "{} {} {} {} {}\n".format(
                ref, session_harness.get(session, "?"), row.verb,
                "ok" if row.ok else "FAIL", content,
            )
        )
    return 0


# --- show -------------------------------------------------------------------


def _pinned_path(state: str, session_id: str, fallback: str) -> str:
    pinned = os.path.join(pin.pinned_dir(state, session_id), "source.jsonl")
    return pinned if os.path.exists(pinned) else fallback


_SEQ_REF_RE = re.compile(r"^([^#]*)#(\d+)$")


def _default_log_session(state: str) -> Optional[str]:
    """The session pull accounting charges `log` to — the most recently
    delivered session (due.last_delivered, §9). `log` itself has no "default
    session" (it lists the whole index); when given just `#N`, this reuses
    that same accounting rule to narrow it to one (#10)."""
    return due.last_delivered(state)


def _resolve_seq_ref(state: str, prefix: str, seq: int):
    """Resolves `#N` or `<prefix>#N` to (entry, note). note is the sentence
    telling the human which session got auto-picked, or None. On failure,
    (None, error message)."""
    ids = _all_session_ids(state)
    note = None
    if prefix:
        matches = [sid for sid in ids if sid.startswith(prefix)]
        if not matches:
            return None, "unknown session prefix {!r}".format(prefix)
        if len(matches) > 1:
            # Shortening by prefix could itself be ambiguous again (a review
            # defect) — candidates are always shown as full ids.
            candidates = ", ".join(sorted(matches))
            return None, "ambiguous session prefix {!r}; candidates: {}".format(
                prefix, candidates)
        session = matches[0]
    else:
        session = _default_log_session(state)
        if session is None:
            return None, ("no default session yet (nothing delivered here) — "
                          "use `<session-prefix>#{}` or `omhc log --last 30`".format(seq))
        if session not in ids:
            return None, ("most recently delivered session {} has no index yet — "
                          "use `<session-prefix>#{}` or `omhc log --last 30`".format(
                              session, seq))
        n = _unique_prefix_len(ids)
        note = "{} -> {}#{} (most recently delivered session)".format(
            "#{}".format(seq), session[:n], seq)

    path = os.path.join(state, "index", session + ".idx")
    row = index.find(path, seq)
    if row is None:
        return None, "no event #{} in session {}".format(seq, session)
    entry = {"session_id": session, "source_path": "",
             "offset": row.offset, "length": row.length, "seq": seq}
    return entry, note


def cmd_show(args, *, home=None, out=sys.stdout, err=None) -> int:
    err = err or sys.stderr
    _root, key, state = _state_for(home)
    target = args.target.strip()
    refs = index.read_refs(state)

    entry = refs.get(target) or refs.get(target.upper())
    note = None
    if entry is None:
        m = _SEQ_REF_RE.match(target)
        if m:
            entry, err_or_note = _resolve_seq_ref(state, m.group(1), int(m.group(2)))
            if entry is None:
                err.write("{}\n".format(err_or_note))
                return 1
            note = err_or_note
    if entry is None:
        err.write("unknown reference {!r}; try `omhc log --last 30`\n".format(target))
        return 1

    if note:
        # stdout must be the raw original bytes as-is (`omhc show '#3'
        # --full | jq .` must not break) — a note about the auto-picked
        # session goes to stderr only.
        err.write("# {}\n".format(note))

    source = _pinned_path(state, entry["session_id"], entry.get("source_path") or "")
    if not source or not os.path.exists(source):
        err.write("source bytes are gone for {} (session {})\n".format(
            target, entry["session_id"]))
        return 1
    with open(source, "rb") as fh:
        fh.seek(entry["offset"])
        raw = fh.read(entry["length"] if not args.full else -1)
    buf = getattr(out, "buffer", None)
    if buf is not None:
        # A real stdout — writes the raw bytes as-is (`show '#3' --full | jq
        # .` must not break). Doesn't add a missing newline: it's raw bytes
        # as they are.
        buf.write(raw)
    else:
        # A stream with no .buffer, like a test's io.StringIO — can only be
        # compared as text, so decode it and fix up the newline for human
        # readability.
        out.write(raw.decode("utf-8", "replace"))
        if not raw.endswith(b"\n"):
            out.write("\n")
    _record_pull(key, state, "show", home, session=entry["session_id"], tag=target)
    return 0


# --- status -----------------------------------------------------------------


# Only three values are used: True (PASS, gating), False (FAIL, gating),
# None (`----`, non-gating). Not SKIP — this is a third label meant to show
# "nothing has happened yet" as itself, without disguising it as either
# success or failure (#8).
def _verdict_word(verdict: Optional[bool]) -> str:
    if verdict is True:
        return "PASS"
    if verdict is False:
        return "FAIL"
    return "----"


def _check(out, label: str, verdict: Optional[bool], detail: str) -> None:
    out.write("{:<4} {:<22} {}\n".format(_verdict_word(verdict), label, detail))


def _status_json_empty() -> dict:
    """All of `status --json`'s top-level keys, empty. Used by a path that
    can't produce diagnostics, like `/`. Any key added to the normal path
    must be added here too — test_status checks that both paths' key sets
    match (#19 review: a key #25 added was missing only on the `/` path)."""
    return {
        "repo_root": None, "repo_key": None, "state_dir": None,
        "adapters": [], "ledger_rows": 0, "ledger_rejects": 0,
        "archive": [],
        "injections": 0, "pulls": 0,
        "pull_rate_window": PULL_RATE_WINDOW,
        "recent_injections": 0, "recent_pulls": 0,
        "off": False, "watcher_pid": None,
        "instruction_files": {"shared": None, "stale_block": False},
        "last_read": None,
        "health": [],
        "rows": [],
    }


def _last_read_detail(summary: Optional[dict]) -> str:
    """One line for status's `last read` row (#37)."""
    if not summary:
        return "nothing read yet — brief records each session it reads"
    try:
        events = int(summary.get("events", 0))
        skipped = int(summary.get("skipped", 0))
        detail = "{} {}: {} events, {} unparsed lines, {} records of {} types skipped ({})".format(
            summary.get("harness", "?"), str(summary.get("session", "?"))[:8], events,
            int(summary.get("unparsed", 0)), skipped, int(summary.get("skipped_types", 0)),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(float(summary.get("epoch", 0)))))
    except (TypeError, ValueError):
        return "unreadable summary"
    if events == 0 and skipped:
        detail += (" — no events from a non-empty session; if it had turns, the session"
                   " format may have changed (use the 'Harness format change' issue form)")
    return detail


def cmd_status(args, *, home=None, out=sys.stdout) -> int:
    root, key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        # status at `/` is a misuse — shown as a gating FAIL, not SKIP. If
        # state already exists (a trace of a prior wrong run), reports it as
        # orphaned. The rest of the normal path's diagnostics
        # (ledger.read, adapters.present, health, ...) all hinge on `root`
        # and are meaningless at `/` — the minimal fields where text/json
        # diverge here (refused, orphaned_state) are added on top.
        orphaned = state if os.path.isdir(state) else None
        if args.json:
            # Keeps the same top-level key set as the normal path — even
            # empty values need to be there so a consumer doesn't KeyError
            # only on `/` (#19). The values themselves are meaningless
            # (the normal path's diagnostics all hinge on `root` and can't be
            # produced here).
            payload = _status_json_empty()
            payload.update({
                "repo_root": root, "repo_key": key, "state_dir": state,
                "refused": reason, "orphaned_state": orphaned,
                "rows": [{"label": "root", "verdict": "fail", "detail": reason}],
            })
            out.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        else:
            _check(out, "root", False, reason)
            if orphaned:
                out.write("orphaned state dir: {}\n".format(orphaned))
        return 1
    installed = adapters.present(now=time.time)
    # The ledger needs to be read unlimited once more anyway, for the
    # health_rows below (global session ids mean it can't be filtered by
    # repo) — parsing alone takes about 0.5s at 100k rows, so reading it
    # twice would double that (#25). Reads it unlimited once, and
    # reconstructs this repo's rows in memory using the same rule
    # read(repo_key=key) used (filter first, limit last) — otherwise, for
    # someone bouncing between multiple repos, this repo's rows would get
    # pushed out of the slice by other repos' rows.
    all_rows = ledger.read(home=home, limit=0)
    rows = [r for r in all_rows if r.get("repo") == key][-ledger.DEFAULT_LIMIT:]
    artifact = os.path.join(state, ARTIFACT_NAME)

    # #22: rows silently dropped by append() failing to meet the cap. Only
    # "recent" ones gate — if it happened once in the past but never
    # recurred since, a human shouldn't be stuck seeing a FAIL they can never
    # clear (same principle as the off switch/archive row, reusing
    # due.MAX_AGE_SECONDS). Also checks `bytes > ledger.MAX_LINE` — if the
    # cap was already raised to fix this (#22 review), an old row that now
    # fits under the current cap tells us "already fixed" without needing to
    # store the cap separately. Counted distinct by `session` —
    # `_note_rejection` already stops writing the same (repo, harness,
    # session) again on retry, but even duplicate lines that piled up before
    # that guard existed shouldn't inflate one session into looking like it
    # was dropped multiple times.
    rejected_now = time.time()
    recent_rejected = [
        r for r in ledger.read_rejected(home=home, repo_key=key)
        if (rejected_now - float(r.get("epoch") or 0.0)) <= due.MAX_AGE_SECONDS
        and int(r.get("bytes") or 0) > ledger.MAX_LINE
    ]
    rejected_sessions = {
        (r.get("harness"), r.get("session")) if r.get("session")
        else (r.get("harness"), i)
        for i, r in enumerate(recent_rejected)
    }

    # watch.lag owns this calculation exactly. Keeping two copies would risk
    # only one getting updated when the pin layout changes.
    lag_rows = watch.lag(state)

    # X = number of distinct sessions pulled at least once among the last
    # PULL_RATE_WINDOW deliveries, N = that window's delivery count (§9
    # "pulled X of N injections"). injections separately keeps the full
    # delivered.tsv line count (used for the archive row's judgment).
    pull_sessions_raw = {r.get("session") for r in rows
                         if r.get("event") == "pull" and r.get("session")}
    delivered = os.path.join(state, due.DELIVERED_NAME)
    # Collected in append order as-is — delivered.tsv's epoch field is a
    # timestamp that can go backward (invariant 6), so line order itself is
    # used instead of it as a sort key.
    delivered_order: List[str] = []
    # An ALSO row's session never appears in delivered_order (below), but
    # `omhc show` can still pull its tag directly — mapped here to the head
    # session of the same handoff (the next non-also, non-reopen line after
    # it; brief.py always writes ALSO rows immediately before their head, #41
    # review finding 6) so that pull still counts toward its handoff.
    also_to_head: Dict[str, str] = {}
    pending_also: List[str] = []
    if os.path.exists(delivered):
        with open(delivered, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                # A reopen line isn't a delivery — counting it would let a
                # session that merely resumed and hasn't been redelivered yet
                # sneak into the injections/pull-rate denominator (#22).
                if len(parts) >= 2 and parts[1] == due.REOPEN_MARKER:
                    continue
                # An ALSO row (v2 phase 1, #41) is one row of a handoff that's
                # already counted via its main (head) row — without this, one
                # handoff covering 3 sessions would count as 3 injections.
                if len(parts) >= 6 and parts[5] == due.ALSO_MARKER:
                    pending_also.append(parts[0])
                    continue
                for sid in pending_also:
                    also_to_head[sid] = parts[0]
                pending_also = []
                delivered_order.append(parts[0])
    injections = len(delivered_order)
    # Add the head a pulled ALSO session belongs to without dropping the
    # session's own id: the same session can later be a handoff head itself
    # (resumed and re-delivered), and that delivery must count as pulled too.
    pull_sessions = set(pull_sessions_raw) | {
        also_to_head[s] for s in pull_sessions_raw if s in also_to_head}

    # Leaving the pull rate's denominator as all of delivered.tsv would make
    # it grow unboundedly the longer a repo is used, so the rate would look
    # like it's slowly dropping (#25) — an old delivery's pull row already
    # gets pushed outside the ledger window (rows, DEFAULT_LIMIT), but the
    # denominator wouldn't shrink to match. So the denominator itself is
    # bound to the last N via "how many of the last N deliveries were
    # pulled". Matched by session id — a pull row already carries the
    # session field (pull_sessions above), so no position-based approximation
    # is needed.
    recent_window = delivered_order[-PULL_RATE_WINDOW:]
    recent_injections = len(recent_window)
    recent_sessions = {s for s in recent_window if s}
    recent_pulls = len(recent_sessions & pull_sessions)
    # JSON's `pulls` keeps its old meaning (sessions pulled out of all
    # deliveries) — so a consumer computing a ratio as `pulls / injections`
    # doesn't silently go wrong once the window is introduced; the
    # windowed value is reported separately as the `recent_pulls` /
    # `recent_injections` pair.
    pulls = len({s for s in delivered_order if s} & pull_sessions)

    # If AGENTS.md is shared with CLAUDE.md, Codex Path B must never be used
    # — the only failure reason is a stale managed block planted *before*
    # that file became shared wiring.
    shared = agents_md.shared_with_claude(root)
    leaked = bool(shared) and managed_block.installed_captured_at(
        agents_md.path_for(root)) is not None

    # Optional adapter diagnostics (e.g. Codex's untrusted hook). Session ids
    # are globally unique, so all_rows (already read unlimited above), not
    # filtered by repo, is passed straight through — pre-filtering by this
    # repo like `rows` above does would make a session started inside a
    # nested worktree/submodule with its own .git (recorded under a
    # different repo key) look permanently "never ran" here. It's a known
    # limitation that the limit (default 2000, shared machine-wide) may not
    # cover a 14-day window — this is a personal tool and status isn't the
    # hook path, so it's read unlimited here when needed. Wrapped per adapter
    # so one adapter dying doesn't kill the rest of status.
    health_rows = []
    for adapter_id in installed:
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            # If the adapter itself can't even be constructed, there's no basis to judge health.
            continue
        try:
            health_rows.extend(getattr(inst, "health", lambda *a: ())(root, all_rows))
        except Exception:
            pass

    # The hooks row covers more than `installed` (what detect() found) —
    # right after a curl install, before the harness has ever run, detect()'s
    # session directory may not exist yet, but the harness's own config
    # directory (~/.claude, ~/.codex) may already exist if it was run before
    # or a human created it in advance, and "is it installed" still needs to
    # be shown in that case (hook_config_targets, #7 review 1). Independent
    # of health — one dying must not suppress the other's row too (a review
    # defect: health's exception used to skip the hooks judgment itself).
    hook_rows = []
    for adapter_id in hook_config_targets(home):
        try:
            inst = adapters.get(adapter_id, home=home)
            hc = getattr(inst, "hook_config", lambda: None)()
            if hc is not None:
                # If the adapter implements `hooks_status()` (e.g. codex-cli
                # — a judgment that also looks at config.toml's inline
                # [hooks], not just hooks.json, #32), use that — the core
                # (hookconf.inspect) only knows about one hooks.json
                # location.
                custom = getattr(inst, "hooks_status", None)
                if callable(custom):
                    ok, detail = custom()
                else:
                    fragment = hookconf.load_fragment(hc.fragment_name)
                    ok, detail = hookconf.inspect(hc.config_path, fragment, inst.home)
                hook_rows.append(("{} hooks".format(adapter_id), ok, detail))
        except Exception as exc:
            # Doesn't drop this silently — the judgment itself dying is a
            # FAIL row (e.g. load_fragment failing because hooks/ doesn't exist).
            hook_rows.append(("{} hooks".format(adapter_id), False,
                              "cannot check hooks ({})".format(exc)))
    watcher = watch.read_lock(state)

    # Rows are built once and text/JSON render the same list — building them
    # separately risks fixing only one (#8, the old defect where
    # status --json always exited 0).
    checks = []
    checks.append(("adapters", bool(installed), ", ".join(installed) or "none found"))
    checks.append(("ledger", None,
                   "{} rows for this repo".format(len(rows)) if rows
                   else "no sessions recorded here yet — start either harness in this repo"))
    if recent_rejected:
        checks.append(("ledger rejects", False,
                       "{} session(s) dropped (too long for MAX_LINE={}) since {}"
                       " — fixed it? run `omhc clear` to drop this repo's record".format(
                           len(rejected_sessions), ledger.MAX_LINE,
                           time.strftime("%Y-%m-%d", time.localtime(
                               min(float(r.get("epoch") or 0.0) for r in recent_rejected))))))
    else:
        checks.append(("ledger rejects", None, "none recently"))

    # lag_rows's size/lag_bytes come out as 0 even when pinned/<sid>/source.jsonl
    # doesn't exist — without distinguishing "size 0" from "pinned
    # successfully, zero-byte tail", a session whose pinning failed would
    # also show archive PASS (a review defect). watch.lag's `pinned` is used
    # to check whether it actually exists.
    pinned_rows = [r for r in lag_rows if r.get("pinned")]
    unpinned_rows = [r for r in lag_rows if not r.get("pinned")]
    # A fixed 8 chars collides often, since Codex's UUIDv7 only changes its
    # first 8 chars every ~65 seconds (#15c) — only needs to be unique among
    # the ids currently visible in this repo.
    archive_n = _unique_prefix_len([r["session"] for r in lag_rows]) if lag_rows else 8
    if pinned_rows:
        archive_verdict = True
        archive_detail = "; ".join(
            "{} tail={}B".format(r["session"][:archive_n], r["lag_bytes"])
            for r in pinned_rows)
        if unpinned_rows:
            archive_detail += "; unpinned: " + ", ".join(
                r["session"][:archive_n] for r in unpinned_rows)
    elif injections:
        # brief.compute only creates a pin *after* delivery (brief.py) — a
        # person with a ledger row but no delivery yet must not be left
        # seeing archive as a permanent FAIL, but if a delivery did happen
        # (injections>0) and there's no pin at all, that's a genuine defect.
        archive_verdict = False
        archive_detail = "{} injections but nothing pinned".format(injections)
        log_path = os.path.join(locate.omhc_root(home), brief.GUARD_LOG)
        if os.path.exists(log_path):
            archive_detail += "; details may be in {}".format(log_path)
    else:
        archive_verdict = None
        archive_detail = "nothing handed off to this repo yet"
    checks.append(("archive", archive_verdict, archive_detail))

    # #37: invariant 7's "report degradation in status". Informational only —
    # a session started and closed with no turn legitimately yields 0 events,
    # so this can't gate; it makes a thinning handoff visible instead.
    last_read = brief.read_last_read(state)
    checks.append(("last read", None, _last_read_detail(last_read)))

    off_reason = due.off_reason(state)
    checks.append(("off switch", None,
                   "on" if off_reason is None else "off ({})".format(off_reason)))

    if leaked:
        checks.append(("instruction files", False,
                       "{}; stale omhc block in AGENTS.md would leak into Claude — "
                       "run `omhc clear`".format(shared)))
    elif shared:
        checks.append(("instruction files", True,
                       "{} -> Codex Path B disabled, falls to .omhc/outbox".format(shared)))
    else:
        checks.append(("instruction files", True,
                       "AGENTS.md not shared with CLAUDE.md"
                       if os.path.exists(agents_md.path_for(root))
                       else "no AGENTS.md"))

    for label, health_ok, detail in health_rows:
        checks.append((label, health_ok, detail))
    for label, hook_ok, detail in hook_rows:
        checks.append((label, hook_ok, detail))

    checks.append(("pull rate", None,
                   "pulled {} of {} recent injections (window {})".format(
                       recent_pulls, recent_injections, PULL_RATE_WINDOW)))
    checks.append(("watcher (optional)", None,
                   "running pid {}".format(watcher) if watcher
                   else "not running — brief falls back to inline parsing"))

    code = 1 if any(verdict is False for _label, verdict, _detail in checks) else 0

    if args.json:
        def _verdict_json(verdict: Optional[bool]) -> Optional[str]:
            if verdict is True:
                return "pass"
            if verdict is False:
                return "fail"
            return None

        out.write(json.dumps({
            "repo_root": root, "repo_key": key, "state_dir": state,
            "adapters": installed, "ledger_rows": len(rows),
            "ledger_rejects": len(rejected_sessions),
            "archive": lag_rows,
            "injections": injections, "pulls": pulls,
            "pull_rate_window": PULL_RATE_WINDOW,
            "recent_injections": recent_injections, "recent_pulls": recent_pulls,
            "off": due.is_off(state), "watcher_pid": watcher,
            "instruction_files": {"shared": shared, "stale_block": leaked},
            "last_read": last_read,
            "health": [{"label": label, "ok": ok, "detail": detail}
                       for label, ok, detail in health_rows],
            "rows": [{"label": label, "verdict": _verdict_json(verdict), "detail": detail}
                     for label, verdict, detail in checks],
        }, ensure_ascii=False, indent=2) + "\n")
        return code

    out.write("repo   {}\nkey    {}\nstate  {}\n\n".format(root, key, state))
    for label, verdict, detail in checks:
        _check(out, label, verdict, detail)
    verbs = {}
    for path in _index_files(state):
        for row in index.rows(path):
            verbs[row.verb] = verbs.get(row.verb, 0) + 1
    if verbs:
        out.write("\nevents  {}\n".format(
            " ".join("{}={}".format(k, verbs[k]) for k in sorted(verbs))))
    out.write("artifact {}\n".format(
        "{}B".format(os.path.getsize(artifact)) if os.path.exists(artifact)
        else "none"))
    return code


# --- brief ------------------------------------------------------------------


def cmd_brief(args, *, home=None, out=sys.stdout) -> int:
    # --dry-run is a path a human calls by hand to check things (a hook
    # never passes --dry-run). One documented use pipes a payload from a
    # different cwd, like `echo '{"cwd": R}' | omhc brief --dry-run` — not
    # reading at all would break that (review). But using plain
    # `_stdin_text()` would hang waiting for EOF if the other end of the pipe
    # is just left open having written nothing yet (isatty() is False, not a
    # TTY) (#27). `_dry_run_stdin_text()` splits that gap by asking select
    # "is there anything to read right now" first. The real hook path
    # (dry_run=False) still reads exactly as it does today.
    if args.dry_run and args.stdin is None:
        stdin_text = _dry_run_stdin_text()
    else:
        stdin_text = args.stdin if args.stdin is not None else _stdin_text()
    return brief.emit(
        harness=args.harness,
        stdin_text=stdin_text,
        budget=args.budget,
        wire=args.wire,
        force=args.force,
        as_text=args.text or args.dry_run,
        dry_run=args.dry_run,
        home=home,
        out=out,
    )


# --- hooks --------------------------------------------------------------


def _adapters_with_hook_config(home) -> List[str]:
    """Every registered adapter id that implements `hook_config()` (i.e. has
    the notion of a SessionStart hook). Unrelated to detection — this is the
    list shown as "pick from these" when no target could be found without
    `--harness`."""
    ids = []
    for adapter_id in sorted(adapters.REGISTRY):
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            continue
        if getattr(inst, "hook_config", lambda: None)() is not None:
            ids.append(adapter_id)
    return ids


def hook_config_targets(home) -> List[str]:
    """Adapter ids that have `hook_config()` and either got detected on this
    machine (`detect()`) or already have a config directory. `omhc status`'s
    `<adapter-id> hooks` row and `omhc hooks install`'s default target share
    this rule.

    Right after a curl install, before any harness has ever run, the session
    directory detect() looks at (`~/.claude/projects`, `~/.codex/sessions`)
    doesn't exist yet — but the harness's own config directory
    (`~/.claude`, `~/.codex`) may exist if it was ever run before, or a
    human created it in advance. Without this rule, `hooks install` would
    silently do nothing to a first-time user, saying "found nothing", and
    `status` wouldn't show the hooks row at all (#7 review 1)."""
    ids = []
    for adapter_id in sorted(adapters.REGISTRY):
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            continue
        hc = getattr(inst, "hook_config", lambda: None)()
        if hc is None:
            continue
        try:
            detected = inst.detect().present
        except Exception:
            detected = False
        if detected or os.path.isdir(os.path.dirname(hc.config_path)):
            ids.append(adapter_id)
    return ids


def cmd_hooks(args, *, home=None, out=sys.stdout, err=None) -> int:
    """`omhc hooks install|uninstall`. Actually performs the install that
    status's `<adapter-id> hooks` row points at. The core doesn't know
    vendor names — the targets are adapters that implement `hook_config()`
    and were either detected on this machine or have a config directory."""
    if not getattr(args, "hooks_action", None):
        # argparse convention: called with no action, usage goes to stderr, exit 2 (#19).
        (err or sys.stderr).write(
            "usage: omhc hooks install|uninstall [--harness ID]\n")
        return 2

    targets = [args.harness] if args.harness else hook_config_targets(home)
    if not targets:
        known = _adapters_with_hook_config(home)
        out.write("no harness found -- run with --harness <id> ({})\n".format(
            ", ".join(known) if known else "no adapter declares a hook config"))
        return 1

    had_error = False
    for adapter_id in targets:
        try:
            inst = adapters.get(adapter_id, home=home)
        except AdapterUnavailable as exc:
            out.write("{}\n".format(exc))
            had_error = True
            continue

        hc = getattr(inst, "hook_config", lambda: None)()
        if hc is None:
            if args.harness:
                out.write("{}: no hook config for this harness\n".format(adapter_id))
            continue

        try:
            if args.hooks_action == "install":
                fragment = hookconf.load_fragment(hc.fragment_name)
                custom_status = getattr(inst, "hooks_status", None)
                # If the adapter implements `inline_hook_present()`
                # (codex-cli — config.toml's inline [hooks] can also hold a
                # runnable omhc call, #32), this asks first whether one
                # already exists in a layer other than hooks.json —
                # "exists" and "matches the shipped fragment" are different
                # questions (review #1): if it exists, even if that layer is
                # broken (e.g. missing mark), this doesn't overlay
                # hooks.json on top of it — overlaying would leave the
                # harness loading both layers and warning. Instead, if it's
                # broken, this reports that and marks the exit as a failure.
                inline_present = getattr(inst, "inline_hook_present", None)
                if callable(inline_present) and inline_present():
                    ok, detail = (custom_status() if callable(custom_status)
                                 else (True, "inline install present"))
                    if ok is False:
                        out.write("{}: inline install exists but differs -- {}\n".format(
                            adapter_id, detail))
                        out.write("{}: not writing {} (would duplicate)\n".format(
                            adapter_id, hc.config_path))
                        had_error = True
                    else:
                        out.write("{}: already up to date -- {}\n".format(adapter_id, detail))
                    continue
                if callable(custom_status):
                    pre_ok, pre_detail = custom_status()
                else:
                    pre_ok, pre_detail = hookconf.inspect(hc.config_path, fragment, inst.home)
                if pre_ok is not False:
                    out.write("{}: already up to date -- {}\n".format(adapter_id, pre_detail))
                    continue
                had_backup = os.path.exists(hc.config_path)
                changed = hookconf.merge(hc.config_path, fragment, inst.home)
                if changed:
                    out.write("{}: installed -> {}\n".format(adapter_id, hc.config_path))
                    if had_backup:
                        out.write("{}: backup {}\n".format(
                            adapter_id, hc.config_path + ".omhc-bak"))
                    if hc.post_write_note:
                        out.write("{}: {}\n".format(adapter_id, hc.post_write_note))
                else:
                    out.write("{}: already up to date\n".format(adapter_id))
                if callable(custom_status):
                    ok, detail = custom_status()
                else:
                    ok, detail = hookconf.inspect(hc.config_path, fragment, inst.home)
                out.write("{}: {} -- {}\n".format(
                    adapter_id, "PASS" if ok else "FAIL", detail))
                if not ok:
                    # The file has already been (re)written — if the
                    # re-check still FAILs anyway (e.g. the binary still
                    # can't be found), a problem remains for the human to
                    # fix, so this also signals it via the exit code
                    # (#7 review 2).
                    had_error = True
            else:
                changed = hookconf.strip(hc.config_path)
                if changed:
                    out.write("{}: removed from {}\n".format(adapter_id, hc.config_path))
                    out.write("{}: backup {}\n".format(
                        adapter_id, hc.config_path + ".omhc-bak"))
                else:
                    out.write("{}: nothing to remove\n".format(adapter_id))
        except hookconf.HookConfigError as exc:
            out.write("{}: {}\n".format(adapter_id, exc))
            had_error = True
        except Exception as exc:  # never show a traceback — not the hook path, but this command is also for humans.
            out.write("{}: unexpected error ({})\n".format(adapter_id, exc))
            had_error = True

    return 1 if had_error else 0


# --- clear ------------------------------------------------------------------


def cmd_clear(args, *, home=None, out=sys.stdout) -> int:
    root, key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        out.write("{}\n".format(reason))
        return 1
    removed = []
    if agents_md.collapse(root, force=True):
        removed.append(agents_md.path_for(root))
    artifact = os.path.join(state, ARTIFACT_NAME)
    if os.path.exists(artifact):
        os.unlink(artifact)
        removed.append(artifact)
    # #22 review: once the cause is fixed (e.g. MAX_LINE raised), `ledger
    # rejects` must not stay FAIL forever — clears only this repo's reject record.
    rejected_cleared = ledger.clear_rejected(key, home=home)
    if rejected_cleared:
        removed.append("{} ledger reject row(s)".format(rejected_cleared))
    outbox_removed = deliver.prune_outbox(root, now=time.time(), force=True)
    if outbox_removed:
        removed.append("{} outbox file(s)".format(len(outbox_removed)))
    out.write("cleared {}\n".format(", ".join(removed) if removed else "nothing"))
    return 0


# --- watch ------------------------------------------------------------------


def cmd_watch(args, *, home=None, out=sys.stdout) -> int:
    """Accelerator daemon. Not responsible for correctness, so its death doesn't change the result."""
    root, _key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        out.write("{}\n".format(reason))
        return 1
    if args.stop:
        pid = watch.read_lock(state)
        if pid is None:
            out.write("no watcher running\n")
            return 0
        import signal as _signal

        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError as exc:
            out.write("could not stop {}: {}\n".format(pid, exc))
            return 1
        out.write("stopped {}\n".format(pid))
        return 0
    if args.once:
        written = watch.sweep(root, state, home=home)
        out.write("indexed {} new events\n".format(written))
        return 0
    try:
        return watch.run(root, home=home, poll=args.poll,
                         idle_exit=args.idle_exit)
    except watch.LockBusy:
        out.write("watcher already running (pid {})\n".format(
            watch.read_lock(state)))
        return 1


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Carries working context across harnesses")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("brief", help="Hook path: prints the handoff to stdout")
    p.add_argument("--harness", required=True)
    p.add_argument("--budget", type=int, default=brief.mint.BUDGET)
    p.add_argument("--wire", default="", choices=("", "claude", "cursor", "sdk"),
                   help="Injection JSON format. Defaults to what --harness implies")
    p.add_argument("--force", action="store_true")
    p.add_argument("--text", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="Shows only the body as text; leaves gate/archive/delivery untouched")
    p.add_argument("--stdin", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("mark", help="Records a session start in the ledger")
    p.add_argument("--harness", required=True)
    p.add_argument("--event", default="start", choices=("start", "end"))
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--stdin", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("show", help="Looks up the original bytes for a handoff tag")
    p.add_argument("target", help="A tag like E1, or the "
                   "<session>#N / #N reference `omhc log` prints")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("log", help="Indexed events, one per line")
    p.add_argument("--last", type=int, default=30)
    p.add_argument("--grep", default="")
    p.add_argument("--verb", default="")
    p.add_argument("--file", default="")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser(
        "trace",
        help="Shows indexed events that touched a file, across both harnesses' sessions",
        description="Lists indexed events that touched this file, with references "
                     "`show` can open, oldest first (most recent on the last line, "
                     "same order as `log`). The index is only built at delivery time "
                     "or by `omhc watch`, so a session never delivered or watched "
                     "isn't caught here. A session that's only been indexed by `watch` "
                     "and has no start row in the ledger yet shows `?` in the harness "
                     "column.")
    p.add_argument("path", help="Relative or absolute path, either works")
    p.add_argument("--all", action="store_true",
                   help="Includes inspected/ran mentions too, not just modified")
    p.add_argument("--last", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("note", help="Leaves a note (an agent can call this too)")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("status", help="The one human dashboard")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="Accelerator daemon (optional — the result is the same without it)")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--once", action="store_true", help="Sweeps once and exits")
    p.add_argument("--poll", type=float, default=watch.POLL_SECONDS)
    p.add_argument("--idle-exit", type=float, default=watch.IDLE_EXIT_SECONDS,
                   dest="idle_exit")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("clear", help="Removes installed handoff artifacts")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("hooks", help="Installs/removes omhc's own SessionStart hooks")
    p.set_defaults(func=cmd_hooks, hooks_action=None)
    hooks_sub = p.add_subparsers(dest="hooks_action")
    p_install = hooks_sub.add_parser("install", help="Merges hooks into detected harnesses")
    p_install.add_argument("--harness", default=None)
    p_install.set_defaults(func=cmd_hooks)
    p_uninstall = hooks_sub.add_parser("uninstall", help="Removes only omhc's own hooks")
    p_uninstall.add_argument("--harness", default=None)
    p_uninstall.set_defaults(func=cmd_hooks)

    return parser


def main(argv=None, *, home=None, out=None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "func", None):
        parser.print_help(out or sys.stdout)
        return 0
    stream = out or sys.stdout
    # --harness is resolved **here**. Letting a free-form string flow through
    # would degrade a typo silently, differently at three different depths —
    # the wire table would fall back to its default, the same vendor
    # shorthand would stop matching, and adapters.get would raise inside
    # brief's bare except and print nothing. Better to fail once, at the
    # boundary.
    harness = getattr(args, "harness", None)
    if harness:
        try:
            adapters.get(harness, home=home)
        except AdapterUnavailable as exc:
            stream.write("{}\n".format(exc))
            return 1
    try:
        return args.func(args, home=home, out=stream)
    except AdapterUnavailable as exc:
        stream.write("{}\n".format(exc))
        return 1
    except BrokenPipeError:
        return 0
