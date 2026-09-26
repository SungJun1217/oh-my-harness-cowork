"""v2 phase 2 (#42): the overlap warning on each human turn, and v2 phase 3
(#43): the other side's progress, live (docs/v2-concurrency.md). `due()`/the
index `paths` column were already the v2 concurrency seam left by phase 1 —
this module is the second consumer of that same stream, not a change to the
SessionStart pipeline. `omhc/turn.py` is the thin hook entry point; this
module holds the actual comparison logic so it can be unit-tested without
going through stdin/argv.

Design rule carried over from mint.py: never summarize. Phase 2's note is
either empty or a direct list of paths the two sessions both touched. Phase
3 (opt-in, `OMHC_LIVE=1`) adds verbatim human `said` text and machine-observed
failures from a still-running foreign session — never the other agent's
words, and never a PLAN?-equivalent: "a running session adopting another
agent's unverified plan is laundering" (docs/v2-concurrency.md) — a stricter
rule than mint()'s own handoff, which does surface PLAN? once a session ends.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Set, Tuple

from . import adapters, due, fsio, gate, ledger, locate, mint
from .adapter import SessionRef

# v2 phase 3 (#43): opt-in, same truthy convention as
# adapters.HEADLESS_ENV/OMHC_ALLOW_HEADLESS and due.OFF_ENV/OMHC_OFF.
LIVE_ENV = "OMHC_LIVE"

# mint's own SAID/FAIL clip widths (mint._SLOTS) — reused here so a live
# note's byte budgeting matches the handoff's, not an independently chosen
# number (`mint._clip` also flattens to one line, satisfying "one line").
_SAID_CLIP = 190
_FAIL_CLIP = 60

# ≤300 bytes (docs/v2-concurrency.md, "Phase 2") — far under mint's 900, since
# this rides on every human turn, not just SessionStart.
NOTE_BUDGET = 300

STATE_SUBDIR = "turn"

# v2-concurrency.md: "cap 3" foreign sessions considered per turn.
FOREIGN_CANDIDATES = 3

# 1 MiB — same cap cli._reactivate_grown_sessions uses for the same reason
# (measured: cost scales with tail size, 396.6ms for 17.7MB, which blows any
# per-turn budget).
READ_CAP_BYTES = 1_000_000

# Hard time budget for the foreign-session reading loop (v2-concurrency.md:
# "150ms p95 per human turn, fast path under 80ms" — this budgets only the
# part proportional to how many foreign sessions grew, mirroring
# brief.OLDER_SESSION_TIME_BUDGET's role for brief's own older-session loop).
TIME_BUDGET = 0.12

# Bounds the touched-paths list kept in the per-session state file — a
# session that edits hundreds of distinct files over its lifetime must not
# make this file (rewritten every turn) grow without bound. Insertion order
# is kept (review #1 finding 6) and the *oldest* entry is dropped once this
# is exceeded — sorting alphabetically and truncating (the original
# implementation) silently and permanently loses every path that sorts after
# the cut, no matter how recently it was touched.
TOUCHED_CAP = 500


def _live_enabled() -> bool:
    """v2 phase 3 (#43), opt-in. Same truthy convention (env var, checked
    directly — a single `os.environ.get` is the "cheap" read the design
    calls for, no ledger/state involved) as `adapters.allow_headless()`/
    `due.is_off()`. `OMHC_OFF` already returns before this is ever consulted
    (`check()`'s existing `due.is_off` check), so "OMHC_OFF still wins" holds
    structurally, not by ordering convention alone."""
    return os.environ.get(LIVE_ENV, "").strip() not in ("", "0", "false", "False")


def _live_said(events) -> Optional[str]:
    """The newest new human `said` event's text in `events` (already the
    *new* tail since this session's last turn — see `check()`), or None.
    Author-gated to human only (invariant 3: never the other agent's words,
    never a PLAN?-equivalent unverified claim) and skipped outright when
    it's an approval-style turn (`mint._is_ack` — reused, not
    reimplemented): "계속 진행해" carries no information without the proposal
    it approves, which this note never shows."""
    humans = [e for e in events if e.author == "human" and e.verb == "said" and e.text]
    if not humans:
        return None
    newest = humans[-1].text
    if mint._is_ack(newest):
        return None
    return mint._clip(newest, _SAID_CLIP)


def _live_fail(events) -> Optional[str]:
    """Up to one new *unresolved* failure's `arg` in `events` (the new tail),
    using mint's own resolution rule (`mint._unresolved_failures` — a later
    success whose first 40 arg characters match counts as fixed) rather than
    reimplementing it. The newest unresolved failure, if any — "since your
    last turn" framing favors recency the same way `_live_said` does. No
    failure tag (`[E1]`, …): unlike mint's own FAIL slot, this session was
    never delivered/indexed, so there's no `omhc show <tag>` for it to
    resolve to."""
    unresolved, _fixed = mint._unresolved_failures(list(events))
    if not unresolved:
        return None
    return mint._clip(unresolved[-1].arg, _FAIL_CLIP)


def _state_path(state_dir: str, harness: str, session_id: str) -> str:
    return os.path.join(state_dir, STATE_SUBDIR, "{}.{}.json".format(harness, session_id))


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"own_offset": 0, "touched": [], "foreign": {}}


def _save_state(path: str, data: dict) -> None:
    # Per-process tmp suffix — concurrent hooks (both harnesses' own
    # UserPromptSubmit, or two overlapping turns) must not clobber each
    # other's tmp file (same reasoning as brief.record_read).
    fsio.write_atomic(path, json.dumps(data, ensure_ascii=False), fsync=False,
                      suffix=".{}.tmp".format(os.getpid()))


def _relativize(repo_root: str, path: str) -> Optional[str]:
    """Same rule as mint._relativize — a path outside the repo must not be
    silently reported as if it were a repo file."""
    if not path:
        return None
    rel = locate.relativize(repo_root, path)
    if rel is not None:
        return rel
    return None if path.startswith("/") else path


class _Touched:
    """Insertion-ordered, deduplicated, capped set of repo-relative paths
    (review #1 finding 6) — a plain `set` has no defined order to drop from
    fairly, and the original `sorted(...)[:CAP]` dropped whatever sorted
    alphabetically last, forever, regardless of how recently it was touched."""

    def __init__(self, initial: List[str]):
        self._order: List[str] = list(initial)
        self._set: Set[str] = set(initial)

    def __contains__(self, item: str) -> bool:
        return item in self._set

    def add(self, item: str) -> None:
        if item in self._set:
            return
        self._order.append(item)
        self._set.add(item)
        if len(self._order) > TOUCHED_CAP:
            oldest = self._order.pop(0)
            self._set.discard(oldest)

    def to_list(self) -> List[str]:
        return list(self._order)


def _foreign_candidates(rows: List[dict], my_harness: str,
                        my_session_id: str) -> List[Tuple[str, str, str]]:
    """The other harness's newest distinct `start` rows for this repo, most
    recent first, capped at FOREIGN_CANDIDATES. `rows` is a bounded tail of
    the shared ledger (`ledger.read_tail`, fetched once by the caller and
    reused for `_own_start_position`/`_first_baseline` too — see its
    docstring for the size rationale). Returns (harness, session_id, path) —
    a row with no recorded path is skipped (nothing to stat). Classifying
    whether a given candidate is "genuinely new" is `_first_baseline`'s job,
    over the *whole* `rows` list, not just this newest row (a session can
    have several rows — backfill, reactivate, its own resume)."""
    out = []
    seen: Set[str] = set()
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        if row.get("event") != "start":
            continue
        harness = row.get("harness")
        sid = row.get("session")
        if not harness or not sid or harness == my_harness:
            continue
        sid = str(sid)
        if sid == my_session_id or sid in seen:
            continue
        seen.add(sid)
        path = str(row.get("path") or "")
        if not path:
            continue
        out.append((str(harness), sid, path))
        if len(out) >= FOREIGN_CANDIDATES:
            break
    return out


def _own_start_position(rows: List[dict], my_harness: str, my_session_id: str) -> Optional[int]:
    """This session's own `start` row's position in `rows` (append order),
    or None if it isn't in this bounded tail at all. Used to judge, by
    ledger append order (invariant 6 — never timestamps), whether a foreign
    session we're observing for the first time already existed when this
    session started (in which case anything before now is presumed already
    handled by the SessionStart handoff) or only appeared afterward (in
    which case its whole file is new territory to this hook)."""
    for i, row in enumerate(rows):
        if (row.get("event") == "start" and row.get("harness") == my_harness
                and str(row.get("session")) == my_session_id):
            return i
    return None


def _genuine_row_exists(rows: List[dict], sid: str, after: Optional[int] = None) -> bool:
    """Is there a **genuine** `start` row (no `via`, no `grew` — an
    adapter's own SessionStart hook actually firing, not backfill/reactivate
    bookkeeping) for `sid` in `rows`? `after`, if given, restricts this to
    rows positioned strictly after that index."""
    for i, row in enumerate(rows):
        if row.get("event") != "start" or str(row.get("session")) != sid:
            continue
        if after is not None and i <= after:
            continue
        if row.get("via") != "scan" and not row.get("grew"):
            return True
    return False


def _oldest_start_size_after(rows: List[dict], sid: str,
                             after: Optional[int] = None) -> Optional[int]:
    """The `start_size` recorded (by `cli.cmd_mark`, review #1 finding 1,
    round 5) on the **oldest** genuine `start` row for `sid` positioned
    after `after` (or anywhere in `rows`, if `after` is None). Oldest, not
    newest (round 6 fix — round 5's version used the newest, which is wrong):
    a session judged genuinely new can still pick up a *later* genuine row
    after its first one — e.g. a `compact` row, whose own `start_size` can
    already be much larger, having grown across everything the session did
    between its actual start and that compact — using that later, larger
    size as the baseline would skip real edits made in between. `rows` is in
    append order, so the first qualifying row found while scanning forward
    is already the oldest — returned immediately. None if no genuine row for
    `sid` (after `after`) carries this field at all (rows written by an omhc
    version before this field existed)."""
    for i, row in enumerate(rows):
        if row.get("event") != "start" or str(row.get("session")) != sid:
            continue
        if after is not None and i <= after:
            continue
        if row.get("via") == "scan" or row.get("grew"):
            continue
        size = row.get("start_size")
        if size is None:
            continue
        try:
            return int(size)
        except (TypeError, ValueError):
            continue
    return None


def _is_old_by_position(rows: List[dict], my_pos: int, sid: str) -> bool:
    """The position-based old/new judgment (invariant 6 — ledger append
    order, never timestamps), given that `my_pos` is available: any row for
    `sid` positioned *before* `my_pos` → old, unconditionally (a later
    genuine row never overrides an earlier one). Otherwise a genuine row
    *after* `my_pos` → new (False). Otherwise (only `via:"scan"`/`grew`
    bookkeeping rows after `my_pos`, nothing before — repro3's shape) → old.
    Shared by `_position_baseline` (a single session, when its baseline is
    actually needed) and `_old_foreign_session_ids` (every foreign session
    id at once, when establishing `known_foreign` — review #1 finding,
    round 7)."""
    before = any(
        row.get("event") == "start" and str(row.get("session")) == sid and i < my_pos
        for i, row in enumerate(rows)
    )
    if before:
        return True
    return not _genuine_row_exists(rows, sid, after=my_pos)


def _position_baseline(rows: List[dict], my_pos: int, sid: str,
                       path: str, cur_size: int) -> int:
    """`_first_baseline`'s judgment when `my_pos` is available, factored out
    so both the plain `my_pos`-only path and the `known_foreign` path (round
    7 part (b) — a session not in the snapshot can still resolve via a
    *current* `my_pos`, not just a blanket "genuine row exists anywhere")
    apply the identical rule."""
    if _is_old_by_position(rows, my_pos, sid):
        return fsio.line_aligned_size(path, cur_size)
    recorded = _oldest_start_size_after(rows, sid, after=my_pos)
    return recorded if recorded is not None else 0


def _old_foreign_session_ids(rows: List[dict], my_harness: str, my_session_id: str,
                             my_pos: Optional[int]) -> Set[str]:
    """Every distinct foreign session id visible in `rows` that
    `_is_old_by_position` classifies as old — used once, to build
    `known_foreign` when this session's per-turn state is first established
    (review #1 finding, round 7 part (a)). Ledger rows only — no stat, no
    read, same cost class as everything else at establishment time.

    Round 6's version only added a session actually run through
    `_first_baseline` that turn — but `_foreign_candidates` caps at
    FOREIGN_CANDIDATES (3) and can also be cut short by the time budget, so
    an old session sitting outside the top-3 (repro7: 4 real Codex sessions
    started before this one, the oldest edited a file, and — being the
    *oldest* of the four — sits outside the top 3 newest candidates) was
    never classified at all, stayed out of `known_foreign`, and then read as
    brand new the moment a later resume/grew row for it became a top-3
    candidate. Classifying *every* visible foreign session id up front (not
    just the ones the candidate loop actually reached) costs nothing extra —
    it's pure in-memory ledger inspection over the same `rows` already read.

    Excludes sessions `_is_old_by_position` calls new (a genuine row after
    `my_pos`, nothing before) so a genuinely new session that just hasn't
    been baselined yet is still absent from the snapshot and judged fresh
    whenever it's first actually observed (keeps round 5's fix)."""
    sids: Set[str] = set()
    for row in rows:
        if row.get("event") != "start":
            continue
        row_harness = row.get("harness")
        sid = row.get("session")
        if not row_harness or not sid or row_harness == my_harness:
            continue
        sid = str(sid)
        if sid != my_session_id:
            sids.add(sid)
    if my_pos is None:
        # Can't establish order for anything -- conservative: every visible
        # foreign session counts as old (same default as elsewhere).
        return sids
    return {sid for sid in sids if _is_old_by_position(rows, my_pos, sid)}


def _first_baseline(rows: List[dict], my_pos: Optional[int], sid: str,
                    path: str, cur_size: int,
                    known_foreign: Optional[Set[str]]) -> int:
    """The baseline for the *first* observation of foreign session `sid`.

    review #1 finding 1 (round 6 — round 5's rule was wrong): round 5 used
    `sid`'s recorded `start_size` *before* deciding old vs. new at all, so an
    everyday, already-old Codex session (a real hook `startup` mark, small
    `start_size`, that ran and edited a file *before* this session even
    started) had its entire history read on this session's first turn and
    reported as brand new — the common post-upgrade case, worse than the bug
    `start_size` was added to fix. The classification must always come
    first, exactly as round 4 did (`known_foreign`/`my_pos`-based, below) —
    `start_size` only ever refines what a session already judged **new**
    uses as its baseline; a session judged **old** always uses the file's
    current line-aligned size, `start_size` ignored entirely.

    review #1 finding 1 (round 3 — round 2's rule was wrong): round 2 used
    only the single newest `start` row's position to decide "did this
    session start after mine", but the ledger's append order for a session
    isn't the same as when that session actually started: `cmd_mark` appends
    this session's own row first, and only *afterward* does
    `_backfill_foreign_sessions` append rows for already-existing older
    foreign sessions (marked `via:"scan"`), and `_reactivate_grown_sessions`
    appends a `grew:1` row (also `via:"scan"`) whenever an old, already-known
    session grows. Both land *after* this session's own row in append order
    despite describing a session that's actually old — round 2 mistook that
    for "started after me" and reported a dead session's pre-existing
    content as if it had just changed (repro3). So only a **genuine** row
    (`_genuine_row_exists`) counts as evidence "this session started after
    mine" — `via:"scan"` bookkeeping only reflects when *this machine
    noticed* a session, never when it actually started.

    review #1 finding 3 (round 4): position alone (`my_pos`) only works
    while this session's own `start` row is still inside the bounded ledger
    tail (`ledger.read_tail`) — a long-lived session, or heavy multi-repo
    ledger traffic, can push it out entirely on a later turn. `known_foreign`
    is the fallback ground truth for that case: the set of foreign session
    ids classified old the *first* time this per-session state was ever
    established (`_old_foreign_session_ids`, persisted in the state file by
    `check()`, independent of `my_pos` staying resolvable later). `None`
    only on that very first, establishing turn, when `my_pos` is presumably
    still fresh:
      - `known_foreign` given (not the establishing turn): `sid` in it → old
        (it was already classified old back when we first looked, regardless
        of whether `my_pos` still resolves now). Not in it — round 7 part
        (b): if `my_pos` still resolves this turn, judge it exactly like the
        plain `my_pos`-only path below (`_position_baseline`) instead of
        assuming new outright — a session can be missing from the snapshot
        simply because it wasn't a *candidate* yet at establishment time
        (e.g. a resume/grew row only just brought it into the top
        `FOREIGN_CANDIDATES`), not because it's actually new. If `my_pos` no
        longer resolves either, fall back to "new if a genuine row for it
        exists anywhere in the tail" (no position reference left at all).
      - `known_foreign is None` (the establishing turn) and `my_pos is None`
        (can't establish order even now): never assume new — old.
      - Otherwise: `_position_baseline` (any row for `sid` positioned
        *before* `my_pos` → old, unconditionally; otherwise a genuine row
        *after* `my_pos` → new; otherwise — only bookkeeping rows after,
        nothing before, repro3 — old).

    review #1 finding 2 (round 4): when judged old, the baseline is
    **always** the file's current line-aligned size — never a `via:"scan"`
    row's own recorded `size` field (round 3's shortcut, and a different
    field from `start_size` above — see cmd_mark's comment on why they're
    named differently). That field can be stale (repro4): growth already
    handed off via an earlier session's SessionStart before this session
    even started must never be re-reported just because this hook is
    looking for the first time.

    review #1 finding 1 (round 5, corrected by round 6): when judged **new**,
    the baseline is the *oldest* genuine row's recorded `start_size` found
    after the point this session confirmed "new" from (after `my_pos` when
    it's available; anywhere in the tail only in the rare case `known_foreign`
    is given but `my_pos` no longer resolves) — for a resumed session that's
    the resume row itself (nothing else qualifies as genuine-and-new there);
    for a freshly started one, its own `startup` row, not a possible later
    `compact` row (`_oldest_start_size_after`'s own docstring). Falls back to
    0 (round 4's rule) if no such row carries the field at all (pre-upgrade
    rows).
    """
    if known_foreign is not None:
        if sid in known_foreign:
            return fsio.line_aligned_size(path, cur_size)
        if my_pos is not None:
            return _position_baseline(rows, my_pos, sid, path, cur_size)
        if _genuine_row_exists(rows, sid):
            recorded = _oldest_start_size_after(rows, sid)
            return recorded if recorded is not None else 0
        return fsio.line_aligned_size(path, cur_size)

    if my_pos is None:
        return fsio.line_aligned_size(path, cur_size)
    return _position_baseline(rows, my_pos, sid, path, cur_size)


def _render(records) -> str:
    """Renders every foreign session with something new within NOTE_BUDGET.
    Each record is (harness, session_id, [rel paths], said, fail) — `said`/
    `fail` (v2 phase 3, #43) are `Optional[str]`, and every existing caller
    that only ever knew about file overlaps (phase 2) can keep passing plain
    3-tuples: normalized to `(h, sid, files, None, None)` below, which is
    exactly the phase-2 behavior — this is what keeps `OMHC_LIVE=0`
    (default) byte-for-byte identical to phase 2, structurally, not just by
    a feature flag somewhere else.

    Most-recent-foreign-session first (the order `_foreign_candidates`
    returns them in). No PULL line (review #1 finding 7): `omhc trace` only
    searches the index, which a still-running, undelivered session doesn't
    have unless `omhc watch` is running, so the hint would usually point at
    nothing.

    Priority (v2 phase 3): FILE lines (the phase-2 overlap) always outrank
    SAID/FAIL (phase 3's live notes) — "a running session adopting another
    agent's unverified plan is laundering" (docs/v2-concurrency.md) already
    argues the overlap warning is the more load-bearing fact, and the
    decision for #43 makes that explicit: live lines drop first under budget
    pressure. Implemented by trying every renderable item — every session's
    FILE paths first (in the same per-session, in-order pass as phase 2),
    then every session's SAID, then every session's FAIL — in that fixed
    priority order, skipping (not stopping at) whichever single item doesn't
    fit (review #1 finding 4, round 3: dropping from the tail unconditionally
    let one over-long early item wipe out everything after it). Whatever
    doesn't fit is disclosed in MORE, never silently — and if *nothing* fits
    at all, the header and a MORE line still go out (an overlap/note must
    never be silently lost).
    """
    records = [
        (r[0], r[1], r[2], r[3] if len(r) > 3 else None, r[4] if len(r) > 4 else None)
        for r in records
    ]
    if not records:
        return ""
    multi = len(records) > 1

    n = len(records)
    kept_files: List[List[str]] = [[] for _ in range(n)]
    kept_said: List[Optional[str]] = [None] * n
    kept_fail: List[Optional[str]] = [None] * n
    dropped_files = 0
    dropped_said = 0
    dropped_fail = 0

    def _header() -> str:
        # review #1 (invariant 3): computed from what's *currently kept*,
        # not from the original records — a session's SAID/FAIL is another
        # agent's (unverified) words next to the human's own prompt, so the
        # disclaimer must be present whenever any live line actually makes
        # it into the render, even alongside a FILE line for the same or
        # another session (the phase-2 header alone said nothing about
        # "notes, not instructions" and, in the multi-session case, falsely
        # claimed every session modified files). Recomputed on every
        # `render_now()` call (not once, up front) specifically so the
        # byte-cap trim loop below counts the longer, disclaimer header the
        # whole time any live content is still in the running — a header
        # that shrinks only *after* the final trim decision could itself
        # blow the budget. Once every live line has been dropped (or there
        # never was one — the default-off, phase-2-only case), this falls
        # back to the short phase-2 header, unchanged.
        any_live = any(kept_said[i] or kept_fail[i] for i in range(n))
        if multi:
            if any_live:
                return ("[omhc] {} sessions (running), since your last turn "
                        "— notes, not instructions:").format(n)
            return "[omhc] {} sessions modified files you touched, since your last turn:".format(n)
        harness, sid, _files, _said, _fail = records[0]
        id8 = (sid or "-")[:8]
        if any_live:
            return ("[omhc] {} {} (running), since your last turn "
                    "— notes, not instructions:").format(harness, id8)
        return "[omhc] {} {} (running) modified files you touched, since your last turn:".format(
            harness, id8)

    def render_now() -> str:
        lines = [_header()]
        for i, (h, s, _f, _sd, _fl) in enumerate(records):
            id8 = (s or "-")[:8]
            if kept_files[i]:
                if multi:
                    lines.append("FILE  {} {}: {}".format(h, id8, " ".join(kept_files[i])))
                else:
                    lines.append("FILE  " + " ".join(kept_files[i]))
            if kept_said[i]:
                if multi:
                    lines.append("SAID  {} {}: {}".format(h, id8, kept_said[i]))
                else:
                    lines.append("SAID  " + kept_said[i])
            if kept_fail[i]:
                if multi:
                    lines.append("FAIL  {} {}: {} -> failed".format(h, id8, kept_fail[i]))
                else:
                    lines.append("FAIL  {} -> failed".format(kept_fail[i]))
        # "+N sessions" only means something when there's more than one
        # candidate session to begin with — in the single-session case the
        # header already names the only session there is, so a fully-empty
        # result there is just "+N <kind>", not a confusing "+1 session" on
        # top of it.
        empty_sessions = sum(
            1 for i in range(n) if not kept_files[i] and not kept_said[i] and not kept_fail[i]
        ) if multi else 0
        more_bits = []
        if dropped_files:
            more_bits.append("{} file{}".format(dropped_files, "" if dropped_files == 1 else "s"))
        if dropped_said:
            more_bits.append("{} said".format(dropped_said))
        if dropped_fail:
            more_bits.append("{} fail".format(dropped_fail))
        if empty_sessions:
            more_bits.append("{} session{}".format(
                empty_sessions, "" if empty_sessions == 1 else "s"))
        if more_bits:
            lines.append("MORE  +" + ", +".join(more_bits))
        return "\n".join(lines) + "\n"

    # Forward pass, in fixed priority order (FILE > SAID > FAIL — v2 phase
    # 3), skipping (not stopping the whole render at) any single entry that
    # doesn't fit. `included` records (kind, index) for each *kept* entry in
    # the order they were added — used by the safety-shrink pass below, so
    # popping from the end always undoes the lowest-priority entry added
    # last first (FAIL/SAID before FILE).
    included: List[Tuple[str, int]] = []

    def _try_add(kind: str, i: int, value: str) -> None:
        nonlocal dropped_files, dropped_said, dropped_fail
        if kind == "file":
            kept_files[i].append(value)
        elif kind == "said":
            kept_said[i] = value
        else:
            kept_fail[i] = value
        if len(render_now().encode("utf-8")) <= NOTE_BUDGET:
            included.append((kind, i))
            return
        if kind == "file":
            kept_files[i].pop()
            dropped_files += 1
        elif kind == "said":
            kept_said[i] = None
            dropped_said += 1
        else:
            kept_fail[i] = None
            dropped_fail += 1

    for i, (_h, _s, files, _said, _fail) in enumerate(records):
        for p in files:
            _try_add("file", i, p)
    for i, (_h, _s, _f, said, _fail) in enumerate(records):
        if said:
            _try_add("said", i, said)
    for i, (_h, _s, _f, _said, fail) in enumerate(records):
        if fail:
            _try_add("fail", i, fail)

    out = render_now()
    # Safety net: the MORE line's own text grows as more drops are tallied
    # while later entries are being tried, so a drop discovered *after* an
    # earlier entry was accepted can retroactively push the final render
    # (with the fully up-to-date counts) a few bytes over budget even though
    # every individual step's own check passed at the time. Shrink from the
    # most recently *kept* entry backward (lowest priority first, since FAIL/
    # SAID entries were appended to `included` after FILE ones), re-rendering
    # the whole (current) state each time — same discipline as mint()'s own
    # drop loop: never trust an earlier snapshot as final.
    while len(out.encode("utf-8")) > NOTE_BUDGET and included:
        kind, i = included.pop()
        if kind == "file":
            kept_files[i].pop()
            dropped_files += 1
        elif kind == "said":
            kept_said[i] = None
            dropped_said += 1
        else:
            kept_fail[i] = None
            dropped_fail += 1
        out = render_now()

    if len(out.encode("utf-8")) > NOTE_BUDGET:
        # Even the header + MORE alone doesn't fit — unreachable in practice
        # (header/session ids are short and bounded), but if it ever
        # happened there's nothing safe left to send.
        return ""
    # Rechecked right before returning, same discipline as mint() — a bug
    # upstream must not be able to inject an oversized note (invariant 1's
    # spirit, applied to this smaller budget).
    assert len(out.encode("utf-8")) <= NOTE_BUDGET, "turn note exceeded its own budget"
    return out


def check(*, harness: str, stdin_text: str = "", home: Optional[str] = None,
         now: Optional[float] = None, deadline: Optional[float] = None) -> str:
    """The whole per-turn check. Returns the note text, or "" when there's
    nothing to say. Never raises — the caller (turn.emit) also wraps this,
    but this function is written to fail closed on its own (same principle
    as hookconf.inspect).
    """
    if not harness:
        return ""
    stamp = time.time() if now is None else now
    payload = gate.hook_payload(stdin_text)
    session_id = gate.session_id_from_payload(payload) or ""
    if not session_id:
        # No session id to key state on — a hand-run `omhc turn` with no
        # hook payload has nothing to compare (mirrors brief's #39 rule).
        return ""
    cwd = str(payload.get("cwd") or "") or None
    root = locate.resolve_repo_root(cwd)
    if locate.refused_root(root):
        return ""
    key = locate.repo_key(root)
    state_dir = locate.state_dir(key, home=home)
    if due.is_off(state_dir):
        return ""
    live = _live_enabled()

    transcript_path = str(payload.get("transcript_path") or payload.get("transcriptPath") or "")

    clock_start = time.monotonic()
    hard_deadline = clock_start + TIME_BUDGET if deadline is None else deadline

    state_path = _state_path(state_dir, harness, session_id)
    state = _load_state(state_path)
    touched = _Touched(state.get("touched") or [])
    try:
        own_offset = int(state.get("own_offset") or 0)
    except (TypeError, ValueError):
        own_offset = 0
    foreign_baselines: Dict[str, int] = dict(state.get("foreign") or {})

    # --- this session's own touched files (paths a FILE line could ever match) ---
    if transcript_path:
        try:
            my_adapter = adapters.get(harness, home=home)
        except Exception:
            my_adapter = None
        reader = getattr(my_adapter, "read_session_since", None) if my_adapter else None
        if reader is not None:
            ref = SessionRef(adapter_id=harness, session_id=session_id,
                             source_path=transcript_path, cwd=root, epoch=stamp,
                             size=fsio.size_of(transcript_path))
            try:
                since = reader(ref, own_offset, max_bytes=READ_CAP_BYTES)
            except Exception:
                since = None
            if since is not None:
                for ev in since.events:
                    if ev.verb not in ("modified", "inspected"):
                        continue
                    for p in (ev.paths or ()):
                        rel = _relativize(root, p)
                        if rel:
                            touched.add(rel)
                own_offset = since.end_offset

    # --- foreign candidates: has anything grown since our last observation? ---
    rows = ledger.read_tail(home=home, repo_key=key)
    my_pos = _own_start_position(rows, harness, session_id)

    # review #1 finding 3 (round 4): `known_foreign`, once established, is
    # the ground truth for "was this session already there when I first
    # looked" — it survives this session's own `start` row later scrolling
    # out of the bounded ledger tail (a long-lived session, or heavy
    # multi-repo traffic), which `my_pos` alone can't. `None` means this is
    # the establishing turn (the state file has never recorded one yet) —
    # `_first_baseline` falls back to `my_pos`-based judgment for it, which
    # is presumably still fresh on a session's first few turns.
    raw_known_foreign = state.get("known_foreign")
    establishing = not isinstance(raw_known_foreign, list)
    known_foreign: Optional[Set[str]] = None if establishing else set(raw_known_foreign)
    if establishing:
        # review #1 finding, round 7 (part a): classify *every* foreign
        # session id visible in the tail up front, not just the ones the
        # candidate loop below actually reaches this turn (round 6's
        # version) — the loop is capped at FOREIGN_CANDIDATES and can also
        # be cut short by the time budget, so an old session sitting outside
        # the top few (repro7: several real sessions started before this
        # one, and the *oldest* — the one with the interesting history —
        # sits outside the newest-N cut) was never classified at all, and
        # then read as brand new the moment a later resume/grew row brought
        # it into the top N. This costs nothing extra: it's pure in-memory
        # ledger inspection over the same `rows` already read, no stat/read
        # calls (see `_old_foreign_session_ids`'s own docstring).
        state["known_foreign"] = sorted(
            _old_foreign_session_ids(rows, harness, session_id, my_pos))

    records: List[Tuple[str, str, List[str], Optional[str], Optional[str]]] = []
    for foreign_harness, sid, path in _foreign_candidates(rows, harness, session_id):
        if time.monotonic() > hard_deadline:
            break
        cur_size = fsio.size_of(path, default=-1)
        if cur_size < 0:
            continue
        baseline = foreign_baselines.get(sid)
        if baseline is None:
            # First observation of this foreign session — see
            # _first_baseline's docstring for the full reasoning (review #1
            # findings 1-3, round 5).
            baseline = _first_baseline(rows, my_pos, sid, path, cur_size, known_foreign)
            foreign_baselines[sid] = baseline
            # No `continue` here even for an "old" classification (round
            # 4's `if baseline != 0: continue` — review #1 finding 1, round
            # 5, retired it): with the `start_size`-based baseline (a
            # genuinely fresh session's recorded size can be a small
            # *nonzero* number, not literally 0, if its transcript already
            # had a turn by mark time), testing for exact zero no longer
            # tells "new" apart from "old" the way it used to. Falling
            # straight into the `cur_size <= baseline` check below handles
            # both uniformly and correctly: an "old" baseline was just set
            # to this exact `cur_size` a moment ago, so that check is
            # trivially true and short-circuits with the same effect the
            # explicit `continue` had; a "new"/fresh baseline that's already
            # behind `cur_size` (content added before this first look, or
            # simply nonzero-but-stale) falls through and gets read this
            # same turn (review #1 finding 2, round 3) instead of waiting
            # for the next one.
        if cur_size <= int(baseline):
            continue
        try:
            foreign_adapter = adapters.get(foreign_harness, home=home)
        except Exception:
            continue
        f_reader = getattr(foreign_adapter, "read_session_since", None)
        if f_reader is None:
            continue
        ref = SessionRef(adapter_id=foreign_harness, session_id=sid, source_path=path,
                         cwd=root, epoch=stamp, size=cur_size)
        try:
            since = f_reader(ref, int(baseline), max_bytes=READ_CAP_BYTES)
        except Exception:
            since = None
        if since is None:
            # Can't judge this round — leave the baseline untouched and
            # retry next turn (same rule as cli._reactivate_grown_sessions).
            continue
        modified = []
        for ev in since.events:
            if ev.verb != "modified":
                continue
            for p in (ev.paths or ()):
                rel = _relativize(root, p)
                if rel and rel not in modified:
                    modified.append(rel)
        # Baseline advances regardless of whether this round produces a
        # record — review #1 finding 2: every candidate that was actually
        # read here either lands in `records` (shown) or is silently within
        # budget (nothing new, or new but not worth a line) — in neither
        # case is there anything left un-disclosed, so it's safe to never
        # look at this same growth again.
        foreign_baselines[sid] = since.end_offset
        overlap = [p for p in modified if p in touched]

        # v2 phase 3 (#43), opt-in: collected from the exact same `since`
        # read above — no extra ledger/session reads. Wrapped defensively
        # (mint's helpers operate on a shape they already trust, but this is
        # still the hook path, invariant 2): a failure here must cost this
        # session's live note, never the whole turn.
        said = fail = None
        if live:
            try:
                said = _live_said(since.events)
                fail = _live_fail(since.events)
            except Exception:
                said = fail = None

        if overlap or said or fail:
            records.append((foreign_harness, sid, overlap, said, fail))

    note = _render(records)

    state["own_offset"] = own_offset
    state["touched"] = touched.to_list()
    state["foreign"] = foreign_baselines
    # `state["known_foreign"]` was already set above, at the top of the
    # establishing turn (`_old_foreign_session_ids`) — persisted here as
    # part of the same `state` dict, even if it came out empty (no foreign
    # sessions were visible in the tail at all yet). An empty list is still
    # a valid "established" marker (`isinstance(..., list)`, distinct from
    # `None`/missing), so the *next* turn doesn't re-run this establishing
    # logic and instead correctly judges any brand-new arrival as new.
    try:
        _save_state(state_path, state)
    except Exception:
        pass
    return note
