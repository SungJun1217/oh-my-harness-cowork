from __future__ import annotations

import contextlib
import os
import time
from typing import Dict, List, Optional

from . import adapters, due, fsio, ledger, locate
from .adapter import SessionRef

# fcntl is POSIX-only. CI runs ubuntu/macOS (both have it), but this module
# must not raise on an exotic platform that lacks it — missing fcntl just
# means every caller falls back to "proceed without the lock" (mark still
# collects; brief skips collecting this round, per acquire_lock's contract).
try:
    import fcntl
except ImportError:  # pragma: no cover - not exercised on POSIX CI
    fcntl = None

# #50: both harnesses start every SessionStart hook at (measured) the same
# instant, so the omhc hook fragments' baked-in assumption "mark runs first,
# then brief" is false for both Claude Code and Codex CLI — not just Codex
# (docs/limits.md's older, Codex-only measurement). `mark` and `brief` both
# need the rows this module writes about *foreign* sessions (backfill,
# resume detection, the #51 bounce check) before `due()` can see them; when
# `brief` wins the race, the handoff simply arrives one session start late
# (never lost, never duplicated — same gate/ledger/#27 guard as always).
# This lock only **orders** the two runs (whichever gets it first finishes
# before the other starts) — it does not make the second run a no-op: the
# second holder still repeats the same `discover()` scan and finds it all
# already known, so the cost isn't eliminated, only the two scans no longer
# overlap. What correctness actually needs is narrower: reactivation
# (`_reactivate_grown_sessions`) must not run twice concurrently (review
# finding 1 below) — everything else is idempotent by construction and would
# be harmless run twice even with no lock at all.
LOCK_NAME = "collect.lock"

# Bounded wait for the lock — a non-blocking poll loop, not a blocking
# LOCK_EX, so a stuck holder (or a platform without fcntl) can never hang
# the hook path (invariant 2). 150ms leaves room for the collection itself
# within the ~150ms-per-phase hook budget the callers already budget for.
LOCK_WAIT_BUDGET = 0.15
LOCK_POLL_INTERVAL = 0.01


@contextlib.contextmanager
def try_lock(state: str, timeout: float = LOCK_WAIT_BUDGET):
    """Yields True if the exclusive lock was acquired within `timeout`, False
    otherwise — never raises, never blocks past `timeout`. Callers decide
    what "couldn't get the lock" means for them (see collect_foreign_state's
    docstring): mark still backfills and runs the #51 bounce check even on a
    timeout (redundant with whatever the lock holder is doing, but idempotent
    and therefore harmless), while it skips reactivation specifically
    (`reactivate=False`) — the one piece that isn't safe to run twice at the
    same time (review finding 1: two concurrent `_reactivate_grown_sessions`
    runs can each append their own `grew` start row for the same session,
    reproducing the #49 duplicate-row bug). brief skips collecting entirely
    on a timeout (fails open — the handoff just lags one start)."""
    if fcntl is None:
        yield False
        return
    path = os.path.join(state, LOCK_NAME)
    try:
        fsio._ensure_parent(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield False
        return
    deadline = time.monotonic() + timeout
    got = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_INTERVAL)
        yield got
    finally:
        if got:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


# --- foreign-session collection (moved from cli.py, #50) --------------------

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

    Hook path, so never raises — the caller (collect_foreign_state) wraps it
    entirely.
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
        # verdict are different things — an adapter whose implementation
        # always returns None (contractually "can't tell"), would pile
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


def _session_transcript_path(key: str, harness: str, session_id: str, home) -> Optional[str]:
    """The transcript path from this session's own `start` row(s) in the
    ledger (#51) — cheaper than a full list_sessions scan (Claude's measured
    at 249ms, 34MB) when the only thing needed is one path to hand the
    adapter's delivery_reached_model. Last non-empty path wins, in case of
    multiple start rows (resume/backfill) — always the same file in practice."""
    path = None
    try:
        for row in ledger.read(repo_key=key, home=home):
            if (row.get("event") == "start" and row.get("harness") == harness
                    and row.get("session") == session_id):
                p = row.get("path")
                if p:
                    path = str(p)
    except Exception:
        return None
    return path


def _bounce_check(harness: str, key: str, state: str, home, now: float,
                  session: Optional[str]) -> None:
    """#51: mark/brief always run before this harness's model call, so a
    session whose model call then fails (401 / "Not logged in" / rate
    limit) still claims the handoff in delivered.tsv, and the next
    session's due() walk hits that already-delivered source and stops —
    the handoff is silently lost. Checked here, at the *next* session of
    this harness starting, because only then is there a "next" transcript
    to compare the failed one against, and it's cheap (one small tsv scan,
    one small transcript read capped by the adapter). Never raises — the
    caller (collect_foreign_state) wraps it entirely (invariant 2).

    `session` is "the session this collection call is running inside of" —
    both `cmd_mark` and `brief.compute` pass their own current session id, so
    a delivery just claimed by *this* session is never bounced back on
    itself just because collection ran again in the same start."""
    if not session:
        return
    group = due.latest_delivered_group(state, harness)
    if group is None:
        return
    recipient, sources = group
    # recipient == session: this harness's own SessionStart hook fires
    # several times per session (measured: 6x) — the session that just
    # received a handoff must never bounce its own delivery just because
    # collection ran again.
    if not recipient or recipient == session or not sources:
        return
    head = sources[-1]
    # Checked by file position (group_already_bounced), not already_delivered
    # — a plain reopen of the head between the failed delivery and now also
    # makes already_delivered False with no bounce at all, which would
    # wrongly suppress the bounce and lose the ALSO sources (#51 review
    # finding 2).
    if due.group_already_bounced(state, harness, head):
        return
    path = _session_transcript_path(key, harness, recipient, home)
    reached = None
    if path:
        reached = adapters.get(harness, home=home).delivery_reached_model(path)
    if reached is False:
        for src in sources:
            due.mark_bounced(state, src, harness, now)


def collect_foreign_state(harness: str, root: str, key: str, state: str, home,
                          now: float, *, session: Optional[str] = None,
                          deadline: Optional[float] = None,
                          reactivate: bool = True) -> None:
    """The one entry point both `cmd_mark` and `brief.compute` call to bring
    the ledger's view of *foreign* sessions up to date before `due()` runs
    (#50) — the #51 bounce check, then the two backfill phases (new-session
    scan, resume detection), sharing one hook time budget.

    Never raises (hook path, invariant 2) — every step here was already
    wrapped individually in `cmd_mark`; consolidated into one try/except
    here so both callers get the same guarantee for free.

    Idempotent within one SessionStart for the bounce check and the
    new-session backfill: run twice in the same instant, the second call
    finds almost nothing to do — backfill skips session ids already known,
    and the bounce check is idempotent via `group_already_bounced`. Running
    either of these twice concurrently is redundant (repeats the same
    `discover()` scan) but harmless.

    `reactivate=False` (review finding 1) skips `_reactivate_grown_sessions`
    entirely — **this one is not safe to run concurrently with itself**: two
    processes racing past the same baseline can each independently decide
    "this session grew, no one else has recorded that yet" and each append
    their own `grew` start row (plus a `reopen` line) for the same session,
    landing two adjacent rows the #49 walk then stops at the older of —
    reproducing the exact bug #49 fixed. `cmd_mark` passes `reactivate=False`
    whenever it didn't get `try_lock` (the lock holder — `brief`, or another
    `mark` — is the one actually running it); `brief.compute` only calls
    this at all when it *did* get the lock, so it always leaves this at the
    default `True`."""
    if deadline is None:
        deadline = time.time() + BACKFILL_TIME_BUDGET
    try:
        _bounce_check(harness, key, state, home, now, session)
    except Exception:
        pass
    try:
        if not due.is_off(state):
            fresh = _backfill_foreign_sessions(harness, root, key, state, home,
                                               now, deadline=deadline)
            if reactivate:
                _reactivate_grown_sessions(harness, root, key, state, home,
                                           now, deadline, fresh)
    except Exception:
        pass
