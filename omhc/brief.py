from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from typing import Optional

from . import adapters, deliver, due, fsio, gate, index, locate, mint, pin
from .adapter import HandoffBundle

GUARD_LOG = "guard.log"
NOTES_NAME = "notes.txt"
ARTIFACT_NAME = "omhc.txt"
LAST_READ_NAME = "last_read.json"

# v2 phase 1 (#41): time budget for reading sessions **beyond the head**.
# Measured: reading one 13MB Claude transcript alone took 105ms, already
# close to the ~150ms SessionStart hook budget — so older sessions are only
# read while there's clearly room left. A session skipped this way (or one
# whose read raised) is neither listed nor marked delivered, and it is
# **not** a candidate next time — once the head that came after it is
# delivered, due()'s "never revive older than the last delivered one" rule
# means it can never be reached again. It is only disclosed, once, in that
# handoff's MORE line ("N sessions unread") — see docs/handoff.md.
OLDER_SESSION_TIME_BUDGET = 0.090


def log_failure(home: Optional[str], detail: str) -> None:
    """Records a failure but never raises. Dying on the hook path breaks session start."""
    try:
        root = locate.omhc_root(home)
        os.makedirs(root, exist_ok=True)
        # A non-UTF-8 filename (surrogate) mixed into the path blows up the
        # write with UnicodeEncodeError (ValueError). A log line must never
        # break delivery.
        with open(os.path.join(root, GUARD_LOG), "a", encoding="utf-8",
                  errors="backslashreplace") as fh:
            fh.write("--- {}\n{}\n".format(
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), detail))
    except OSError:
        pass


def record_read(state_dir: str, read, now: float, home: Optional[str] = None) -> None:
    """Records how the last session read went, for `omhc status` (#37). Never raises.

    Invariant 7 asks for degradation to be reported in status, but the
    `SessionRead` tallies were thrown away right after mint(). `unparsed` only
    counts lines that aren't JSON objects (measured: 0 in 58 real sessions);
    unknown record types land in `dropped` by name, by design. So the useful
    signal is how many events came out of a non-empty session, next to how
    much was skipped. A per-process tmp suffix keeps concurrent SessionStart
    hooks (Codex runs them in parallel) from replacing each other's tmp file.
    """
    try:
        summary = {
            "harness": read.ref.adapter_id,
            "session": read.ref.session_id,
            "events": len(read.events),
            "unparsed": int(read.unparsed),
            "skipped": sum(int(v) for v in read.dropped.values()),
            "skipped_types": len(read.dropped),
            "epoch": round(now),
        }
        fsio.write_atomic(os.path.join(state_dir, LAST_READ_NAME),
                          json.dumps(summary, sort_keys=True) + "\n",
                          fsync=False, suffix=".{}.tmp".format(os.getpid()))
    except Exception as exc:  # a status aid must never break the hook path
        log_failure(home, "last_read record failed: {}".format(exc))


def read_last_read(state_dir: str) -> Optional[dict]:
    """The summary record_read left, or None if missing or unreadable."""
    try:
        with open(os.path.join(state_dir, LAST_READ_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# Line format `omhc note` writes: "<epoch>\t<text>". Old lines (no timestamp) are also read.
_NOTE_STAMP = re.compile(r"^(\d{9,11})\t(.*)$")


def _notes(state_dir: str, limit: int = 2, now: Optional[float] = None) -> list:
    """Recent notes to attach to the handoff. Drops notes past the age limit
    (due.MAX_AGE_SECONDS) (#36) — notes persist as a file and reattach to
    every handoff, so last week's note must not read as "current fact" in
    today's session. Matched to the handoff's own age limit. An old line with
    no timestamp has no known write time, so it's treated as old."""
    try:
        with open(os.path.join(state_dir, NOTES_NAME), encoding="utf-8",
                  errors="replace") as fh:
            lines = [line.strip("\r\n") for line in fh if line.strip()]
    except OSError:
        return []
    stamp = time.time() if now is None else now
    kept = []
    for line in lines:
        m = _NOTE_STAMP.match(line)
        if m:
            if stamp - float(m.group(1)) > due.MAX_AGE_SECONDS:
                continue
            text = m.group(2).strip()
        else:
            text = line.strip()
        if text:
            kept.append(text)
    return kept[-limit:]


# Must never emit two of the three formats at once. Claude Code reads both
# additional_context and hookSpecificOutput without deduping (confirmed in
# an installed superpowers hook's comments), so the handoff would inject
# twice — reviving at the wire level the very duplication the gate blocks.
# Which format to use is **declared by the adapter** (adapter.wire).
DEFAULT_WIRE = "sdk"


def _ref_for(adapter, watermark, repo_root: str):
    """Picks the one session to hand off. Uses the path the ledger already
    recorded **first**.

    Measured: on this machine, list_sessions reads 130 files / 34.3MB down to
    1 hit, and that alone takes 249ms — 1.7x the 150ms hook budget. The
    ledger row already has that file's path recorded, so it can be opened
    directly without scanning.

    Eligibility judgment is **owned by the adapter**. If the core judged a
    Codex rollout with Claude's parser, the fields it looks for wouldn't
    exist and the filter would silently become a no-op.
    """
    if watermark.path:
        ref = adapter.ref_for_path(watermark.path, watermark.session_id, repo_root)
        if ref is not None:
            return [ref]
        if os.path.exists(watermark.path):
            # File exists but the adapter rejected it — headless/subagent
            # session. Falling to a scan reaches the same conclusion but
            # costs 249ms. due calls this function for every ineligible
            # ledger row, so this must stop here.
            return []
    # Falls back to a full scan only when the ledger has no path, or that file is gone.
    return [r for r in adapter.list_sessions(repo_root)
            if r.session_id == watermark.session_id]


def _wire_for(harness: str, home: Optional[str]) -> str:
    """The injection format for this harness. Uses whatever the adapter declares."""
    try:
        return getattr(adapters.get(harness, home=home), "wire", DEFAULT_WIRE)
    except Exception:
        return DEFAULT_WIRE


def hook_wire(text: str, wire: str = "claude") -> str:
    """Emits only the one specified format."""
    if wire == "cursor":
        payload = {"additional_context": text}
    elif wire == "sdk":
        payload = {"additionalContext": text}
    else:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }
    return json.dumps(payload, ensure_ascii=False)


def compute(
    *,
    my_harness: str,
    my_session_id: str,
    repo_root: str,
    home: Optional[str] = None,
    now: Optional[float] = None,
    budget: int = mint.BUDGET,
    force: bool = False,
    dry_run: bool = False,
    from_hook: bool = False,
) -> str:
    """The marker body to deliver, or an empty string if there's nothing to send.

    With dry_run, only builds the body and writes nothing — skips gate,
    archive, delivery, and the delivered record. If a single manual check
    consumed that session's delivery, nothing would go out at the next real
    SessionStart (#17).

    This function can raise — the caller (run) wraps it. Tests need to call
    this directly to see failures.
    """
    stamp = time.time() if now is None else now
    key = locate.repo_key(repo_root)
    state = locate.state_dir(key, home=home)

    # Eligibility is judged by the adapter at brief time (#21). The judgment
    # that picks a session is the same judgment that opens it, so the result
    # is kept and reused as-is.
    found = {}
    # v2 phase 1 (#41): due() now keeps walking past one eligible mark to
    # collect up to MAX_SESSIONS, but a mark that's eligible with **no
    # usable ref** (file gone, empty, or unknown shape) is still the same
    # "can't be sure what's newer, stop entirely" case v1 had — once hit,
    # every older row is skipped without even calling _ref_for, so this
    # keeps the exact same cost (at most one full scan) v1 had.
    stopped = False

    def eligible(mark) -> bool:
        nonlocal stopped
        if stopped:
            return False
        adapter = adapters.get(mark.harness, home=home)
        refs = _ref_for(adapter, mark, repo_root)
        found[mark.session_id] = (adapter, refs)
        if refs:
            return True
        # Only skip (and keep walking) when the adapter positively judges
        # headless/subagent. Stop here if the file is gone, empty, or its
        # shape is unknown — going past this point would send yesterday's
        # session out as "just happened" past a session the user actually
        # continued in, and a full scan (249ms) would run for every such row.
        if not (mark.path and os.path.exists(mark.path)):
            stopped = True
            return True
        verdict = adapter.classify(mark.path)
        if verdict:
            stopped = True
        return verdict

    marks = due.due(key, my_harness, my_session_id, stamp, home=home,
                    eligible=eligible)
    if not marks:
        return ""

    def new_human_turn(session_id: str, events) -> bool:
        # reopen is only a hint mark leaves saying "this may have reopened",
        # not a guarantee that a new human turn actually exists (#27) — an
        # empty-prompt resume (`codex exec resume <id> ""`) emits
        # source:"resume" and leaves a reopen, but a mark/brief concurrent-run
        # race also leaves only a reopen with no human turn at all. mark's
        # reopen record is left as-is — it's still the only signal that
        # makes due() treat this session as a candidate again. Instead, here
        # we check "is there a human said event past the point previously
        # delivered to this harness" and only emit when it's actually new.
        # (An empty prompt itself is already filtered by guard.safe so it
        # never even produces a said event — see codex_cli.py's `text =
        # ...text_of(...).strip()`; `if not guard.safe(...)` — so comparing
        # offsets alone is enough.)
        prior_offset = due.last_delivery_offset(state, session_id, my_harness)
        if prior_offset is None:
            return True
        return any(e.verb == "said" and e.author == "human" and e.offset >= prior_offset
                   for e in events)

    # Builds (mark, ref, read) for every mark, newest first, up to due()'s
    # cap (v2 phase 1, #41). A mark with no ref (file vanished, or headless
    # with no evidence) **cuts the list** here — going past it would surface
    # a session older than the one that was actually just worked in, the
    # same "a stale handoff is worse than none" rule v1 already applies to a
    # single session. The #27 guard, in contrast, only drops **that one**
    # session — an ALSO session's phantom reopen must not block the others.
    #
    # A session that's skipped because reading it ran out of time or raised
    # is **not retried later** — it's disclosed via `unread` (MORE) instead.
    # due()'s own "never revive older than the last delivered one" rule means
    # once the head is marked delivered, an unread session sitting behind it
    # can never be reached again; the alternative (delivering an older
    # session while skipping a newer, unread one in between) would be worse
    # — a gap in the middle with no way to tell the receiving agent about it
    # (#41 review finding 2).
    clock_start = time.monotonic()
    processed = []
    unread = 0
    for i, mark in enumerate(marks):
        adapter, refs = found[mark.session_id]
        if not refs:
            break
        ref = refs[0]
        if i == 0:
            # The main session's read is allowed to raise — compute() itself
            # can raise (see docstring); the hook wrapper (emit) is what
            # upholds invariant 2. Never disable the main handoff to protect
            # a read that already failed once.
            read = adapter.read_session(ref)
            # Recorded right after the read, before the #27 guard below, so
            # `omhc status`'s last-read row still updates even when the head
            # turns out to be a phantom reopen (matches v1; #41 review finding 4).
            if not dry_run:
                record_read(state, read, stamp, home=home)
        else:
            # Reading 3 sessions can exceed the hook's latency budget
            # (measured 105ms for one 13MB transcript alone) — only spend
            # time on older sessions while there's room, and never let one
            # of them break the main handoff (invariant 2).
            if (time.monotonic() - clock_start) > OLDER_SESSION_TIME_BUDGET:
                unread = len(marks) - i
                break
            try:
                read = adapter.read_session(ref)
            except Exception as exc:
                if not dry_run:
                    log_failure(home, "also-session {} read failed: {}".format(
                        mark.session_id, exc))
                unread = len(marks) - i
                break

        if not new_human_turn(mark.session_id, read.events):
            continue
        processed.append((mark, ref, read))

    if not processed:
        return ""

    head_mark, head_ref, head_read = processed[0]
    # An older session with nothing verbatim-human said has nothing to hand
    # off (invariant 3) — filtered out here, before mint() and before
    # anything below marks it delivered or tags it (#41 review finding 3).
    older = [(m, r, rd) for (m, r, rd) in processed[1:] if mint.has_human_turn(rd)]
    kept = [processed[0]] + older

    also_reads = [r for (_m, _ref, r) in older]
    body = mint.mint(head_read, to_adapter_id=my_harness, budget=budget, now=stamp,
                     notes=_notes(state, now=stamp), also=also_reads, unread=unread)
    if not body:
        # Don't consume the gate if there's nothing to send. If a first fire
        # burns the slot empty-handed, data that arrives milliseconds later
        # can never be received for that session.
        return ""

    if dry_run:
        return body

    if not force and not gate.claim(state, my_harness, my_session_id):
        # Measured: the SessionStart hook fired 6 times in one session.
        return ""

    # Archive after the marker is made — the marker must still go out even
    # if this fails. Each session's own pin/index failure is logged and
    # skipped without touching the others — one bad archive must not cost
    # the whole handoff its refs.
    tag_sessions = mint.all_tags(head_read, also=also_reads)
    all_triples = []
    for (mark_i, ref_i, read_i), tags_i in zip(kept, tag_sessions):
        try:
            pin_result = pin.pin_session_result(state, ref_i)
            if not pin_result.linked:
                # Passing this silently would let `omhc status`'s archive row
                # report PASS with no pin (review defect) — it has to land in
                # the hook path's only failure log so a human can find the
                # cause. Must not raise here (invariant 2), so just log.
                log_failure(home, "pin failed: {}".format(pin_result.error))
            # Uses the same incremental rule as watch.sweep. Re-appending
            # everything would double-index the same events while the daemon is
            # running, making `omhc log` show duplicates and `omhc show #N`
            # point at a stale row.
            idx = os.path.join(state, "index", ref_i.session_id + ".idx")
            index.append_new(idx, read_i.events)
            all_triples.extend(tags_i)
        except OSError as exc:
            log_failure(home, "archive failed for {}: {}".format(ref_i.session_id, exc))
    try:
        index.write_refs(state, all_triples)
    except OSError as exc:
        log_failure(home, "write_refs failed: {}".format(exc))

    # deliver routes delivery. Writing a file directly here would bypass the
    # channel abstraction, leaving receipt/Path B/the universal floor working
    # only in unit tests. Keeping the body as a file too is that first
    # channel's job — `cat ~/.omhc/<key>/omhc.txt` must be able to show what
    # went in.
    try:
        receipt = deliver.deliver(
            HandoffBundle(body_md=body, repo_root=repo_root, to_adapter_id=my_harness,
                          from_hook=from_hook),
            home=home, now=stamp,
        )
        if receipt.channel == "nowhere":
            log_failure(home, "delivery found no channel: " + receipt.cleanup_hint)
    except Exception as exc:  # deliver must not raise, but can't be allowed to break the hook
        log_failure(home, "delivery failed: {}".format(exc))

    # Older sessions first, head last — last_delivered()/delivered_order()
    # both key off the **last** line in file order, and `omhc show`/`omhc
    # log`'s default target must keep resolving to the main session, not
    # whichever ALSO session happened to be appended last.
    for mark_i, _ref_i, read_i in older:
        offset_i = max((e.offset + e.length for e in read_i.events), default=0)
        due.mark_delivered(state, mark_i, to_harness=my_harness, epoch=stamp,
                           offset=offset_i, role=due.ALSO_MARKER)
    end_offset = max((e.offset + e.length for e in head_read.events), default=0)
    due.mark_delivered(state, head_mark, to_harness=my_harness, epoch=stamp,
                       offset=end_offset)
    return body


def _called_from_hook(payload: dict, session_id: str) -> bool:
    """Is brief running inside a real SessionStart hook (#38).

    Both harnesses send a session id and `source` (startup/resume/compact) —
    `mark` already relies on `source`, measured on both. A hand-piped
    `{"cwd": ...}` has neither, so manual calls keep the config check.
    """
    return bool(session_id) and bool(payload.get("source") or payload.get("hook_event_name"))


def emit(
    *,
    harness: str,
    stdin_text: str = "",
    budget: int = mint.BUDGET,
    wire: str = "",
    force: bool = False,
    as_text: bool = False,
    dry_run: bool = False,
    home: Optional[str] = None,
    now: Optional[float] = None,
    out=None,
    err=None,
) -> int:
    """Hook entrypoint. **Never raises; on any failure, empty stdout + exit 0.**

    Breaking session start is this tool's worst possible outcome. Failing to
    inject anything is nothing by comparison.

    Does not re-parse argv. Previously cli re-serialized the argparse result
    into a string list and a hand-rolled parser here read it back — the
    command surface ended up defined at two depths whose defaults had to be
    kept in sync by hand, and the hand-rolled parser silently ignored unknown
    tokens.
    """
    if not harness:
        return 0
    stream = sys.stdout if out is None else out
    try:
        payload = gate.hook_payload(stdin_text)
        session_id = gate.session_id_from_payload(payload) or ""
        if not session_id and not dry_run and not force:
            # #39: without a session id the gate refuses, so a hand-run
            # `omhc brief --text` used to exit silently — indistinguishable
            # from "nothing to hand off". A real hook always sends a session
            # id, and stdout stays empty either way (invariant 2).
            (sys.stderr if err is None else err).write(
                "omhc brief: no hook payload with a session id, so nothing was delivered."
                " Preview with --dry-run, or deliver anyway with --force.\n")
            return 0
        repo_root = locate.resolve_repo_root(str(payload.get("cwd") or "") or None)
        if locate.refused_root(repo_root):
            return 0
        body = compute(
            my_harness=harness,
            my_session_id=session_id,
            repo_root=repo_root,
            home=home,
            now=now,
            budget=budget,
            force=force,
            dry_run=dry_run,
            from_hook=_called_from_hook(payload, session_id),
        )
        if not body:
            return 0
        if len(body.encode("utf-8")) > budget:
            # Rechecked right before printing. Stops a bug from injecting an oversized payload.
            log_failure(home, "body exceeded budget at print time; suppressed")
            return 0
        chosen = wire or _wire_for(harness, home)
        stream.write(body if as_text or dry_run else hook_wire(body, chosen) + "\n")
        return 0
    except Exception:
        log_failure(home, traceback.format_exc())
        return 0
