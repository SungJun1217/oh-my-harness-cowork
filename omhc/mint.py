from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from . import guard, locate

# Injection budget. Hard cap; the last statement of this function is the assert.
BUDGET = 900

# Below this, only the 2-line header and PULL survive — useless. Reject rather
# than silently emit garbage.
MIN_BUDGET = 200

SEP = "  "

# Approval-style turn. Using this as NEXT launders the prior agent's proposal
# into a human instruction.
_ACK_TOKEN = (
    r"(?:응|넵|네|그래|오케이|오키|ok|okay|yes|yep|sure|good|굿|ㅇㅇ|"
    r"계속|진행|진행해|진행해줘|해줘|해|가자|continue|go|ahead|please|"
    r"그대로|알아서|부탁)"
)
# A chain of approval tokens is still approval — "계속 진행해", "응 진행해줘", "go ahead".
_ACK = re.compile(
    r"^{t}(?:[\s,.!~]+{t})*[\s.!~]*$".format(t=_ACK_TOKEN), re.I
)
_ACK_MAX = 30

# Arg prefix length used to judge whether a failure was resolved.
_RESOLVE_PREFIX = 40

_SAID_MAX = 3
_FAIL_MAX = 2
_DID_MAX_PATHS = 4

# Slot priority. Lowest is dropped first.
#
# Confirmed human instruction (NEXT) and goal (GOAL) must never rank below the
# prior agent's unverified claim (PLAN?) — measured: PLAN? actually survived
# and pushed GOAL out.
# {slot: (priority, byte cap)}. Keeping these two tables separate would mean
# fixing both when adding a slot, and a typo in one would surface as a
# KeyError while the other silently drops from output — different failure
# times.
_SLOTS = {
    "NEXT": (60, 300),
    "GOAL": (50, 260),
    "FAIL": (45, 110),
    "NOTE": (40, 180),
    # DID is a verifiable fact (files changed); PLAN? is the prior agent's claim.
    "DID": (38, 180),
    "PLAN?": (35, 190),
    "SAID": (10, 190),
}
_PRIORITY = {k: v[0] for k, v in _SLOTS.items()}
_LIMIT = {k: v[1] for k, v in _SLOTS.items()}


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _is_ack(text: str) -> bool:
    flat = _one_line(text)
    if len(flat) > _ACK_MAX:
        return False
    return bool(_ACK.match(flat))


def _clip(text: str, limit: int) -> str:
    """Clip by bytes, not characters.

    Clipping by character count is wrong — Korean is 3 bytes/char in UTF-8, so
    a single 200-char slot eats 600 bytes and crowds out every other slot in a
    900-byte budget. Measured: a 749-byte output with 151 bytes of headroom
    left still dropped 5 slots.
    """
    flat = _one_line(text)
    raw = flat.encode("utf-8")
    if len(raw) <= limit:
        return flat
    cut = raw[: max(limit - 3, 1)].decode("utf-8", "ignore").rstrip()
    return cut + "…"


def _age(now: float, events) -> str:
    """Time since the last event.

    What matters to the receiving agent is **how old is this**, not how long
    the session ran — work from 10 minutes ago and work from 3 days ago need
    to be picked up differently.
    """
    epochs = [e.epoch for e in events if e.epoch]
    if not epochs or not now:
        return "-"
    seconds = int(now - max(epochs))
    if seconds < 0:
        return "-"
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return "{}m ago".format(seconds // 60)
    if seconds < 86400:
        return "{}h ago".format(seconds // 3600)
    return "{}d ago".format(seconds // 86400)


def _duration(events) -> str:
    epochs = [e.epoch for e in events if e.epoch]
    if len(epochs) < 2:
        return "-"
    seconds = int(max(epochs) - min(epochs))
    if seconds < 60:
        return "{}s".format(seconds)
    if seconds < 3600:
        return "{}m".format(seconds // 60)
    return "{}h{:02d}m".format(seconds // 3600, (seconds % 3600) // 60)


def _relativize(path: str, repo_root: Optional[str]) -> Optional[str]:
    """Repo-relative path, or None outside the repo. Uses locate.relativize's
    single definition.

    Must not fall back to basename — a file outside the repo (e.g. a note in
    the home directory) would then look like a repo file and DID would lie.
    """
    if not repo_root:
        return None if path.startswith("/") else path
    rel = locate.relativize(repo_root, path)
    if rel is not None:
        return rel
    return None if path.startswith("/") else path


def _unresolved_failures(events) -> Tuple[List, int]:
    """If the same thing later succeeded, don't report the failure.

    FAIL is the slot the receiving agent acts on most readily, so it must not
    be the line most likely to be wrong. Saying "3 failed" about a suite
    that's been green for an hour sends the next agent chasing a non-issue.
    """
    failures = [e for e in events if not e.ok]
    if not failures:
        return [], 0
    later_ok = [(e.seq, e.arg[:_RESOLVE_PREFIX]) for e in events if e.ok and e.arg]
    unresolved = []
    fixed = 0
    for fail in failures:
        prefix = fail.arg[:_RESOLVE_PREFIX]
        resolved = bool(prefix) and any(
            seq > fail.seq and ok_prefix == prefix for seq, ok_prefix in later_ok
        )
        if resolved:
            fixed += 1
        else:
            unresolved.append(fail)
    return unresolved, fixed


def failure_tags(read) -> List[Tuple[str, object]]:
    """Pairs of [E1], [E2] … tags with their source Event.

    Kept in one place so mint and the refs.tsv record use the same
    computation — counting separately in two places would leave `omhc show
    E1` pointing at something else.
    """
    unresolved, _fixed = _unresolved_failures(list(read.events))
    return [
        ("E{}".format(i + 1), ev) for i, ev in enumerate(unresolved[:_FAIL_MAX])
    ]


def mint(
    read,
    *,
    to_adapter_id: str,
    budget: int = BUDGET,
    now: float,
    notes: Sequence[str] = (),
) -> str:
    """Turns Events into a ≤budget-byte marker. The single generation point
    for injected text.

    An empty string is a normal "nothing to send" response — same vendor, or
    no Events. Callers must not inject an empty string as-is.
    """
    if budget < MIN_BUDGET:
        raise ValueError(
            "budget {} is below the {}-byte floor; a marker that small carries "
            "nothing but its own header".format(budget, MIN_BUDGET)
        )

    ref = read.ref
    # DID relativizes against the **repo root**, not ref.cwd. ref.cwd is the
    # working directory the session started in and can be a subdirectory
    # (that's why the adapter matches equal-or-descendant) — using it as the
    # base would judge files changed outside it as "outside the repo" and
    # drop them from DID.
    repo_root = locate.resolve_repo_root(ref.cwd) if ref.cwd else None
    # F5: within the same vendor, native resume is lossless and beats this summary.
    if ref.adapter_id == to_adapter_id:
        return ""
    events = list(read.events)
    if not events:
        return ""

    humans = [e for e in events if e.author == "human" and e.text]
    agent_said = [e for e in events if e.author == "agent" and e.verb == "said" and e.text]

    # --- build slots --------------------------------------------------------
    slots: List[Tuple[str, str, int]] = []  # (key, value, priority) lower drops first

    goal = humans[0].text if humans else ""
    last_human = humans[-1] if humans else None

    next_value = ""
    plan_value = ""
    if last_human is not None and last_human is not humans[0] and not _is_ack(last_human.text):
        next_value = last_human.text
    elif last_human is not None and last_human is humans[0] and not _is_ack(last_human.text):
        # A single human turn is both the goal and the next step. Don't duplicate it.
        next_value = ""
    if not next_value and agent_said:
        # No human text, or it's approval-style — fall back to the prior
        # agent's claim. The single '?' byte labels it "unverified claim".
        plan_value = agent_said[-1].text

    # Middle turns are picked longest-first, not most-recent — a sentence
    # carrying requirements is more useful to the next agent than a short
    # question like "how's it going?".
    said_pool = humans[1:-1] if len(humans) > 2 else []
    said_values = [
        e.text for e in sorted(said_pool, key=lambda e: (-len(e.text), -e.seq))
    ][:_SAID_MAX]

    unresolved, fixed_later = _unresolved_failures(events)
    # Clip short so an inline script doesn't eat the whole FAIL line. Full
    # text is looked up by tag — that's the reason tier (b) exists.
    fail_values = [
        "{} -> failed [E{}]".format(_clip(e.arg, 60) or e.verb, i + 1)
        for i, e in enumerate(unresolved[:_FAIL_MAX])
    ]

    # Same file gets modified repeatedly, so dedupe paths — measured 136
    # occurrences down to 55 unique paths, and realpath lstats every path segment.
    modified_paths: List[str] = []
    seen_paths: Dict[str, Optional[str]] = {}
    for e in events:
        if e.verb != "modified":
            continue
        for p in (e.paths or ((e.arg,) if e.arg.startswith("/") else ())):
            if p not in seen_paths:
                seen_paths[p] = _relativize(p, repo_root)
            rel = seen_paths[p]
            if rel and rel not in modified_paths:
                modified_paths.append(rel)
    did_value = " ".join(modified_paths[:_DID_MAX_PATHS])

    def add(key: str, value: str) -> None:
        if value:
            slots.append((key, _clip(value, _LIMIT[key]), _PRIORITY[key]))

    add("GOAL", goal)
    if next_value:
        add("NEXT", next_value)
    else:
        add("PLAN?", plan_value)
    for note in list(notes)[:2]:
        add("NOTE", note)
    for value in said_values:
        add("SAID", value)
    for value in fail_values:
        add("FAIL", value)
    add("DID", did_value)

    # --- header and PULL (never dropped) ------------------------------------
    # The 8 chars here are deliberately fixed-width, unlike the unique prefix
    # (_unique_prefix_len) used by log/status — a longer header line eats
    # directly into the body within the 900-byte budget, so budget wins over
    # collision risk (#15c) here (#19).
    header = [
        # The wording is defined in exactly one place, guard.HEADER_LINE1_FMT —
        # it must not drift from the pattern guard uses to recognize its own
        # echoed text (#24).
        guard.HEADER_LINE1_FMT.format(
            ref.adapter_id,
            (ref.session_id or "-")[:8],
            _duration(events),
            _age(now, events),
        ),
        guard.HEADER_LINE2,
    ]
    pull_bits = ["omhc log --last 30"]
    if fail_values:
        pull_bits.insert(0, "omhc show E1")
    # A single long path can push the PULL line past 100 bytes and crowd out
    # content slots. Only use a short path as the hint.
    short_paths = sorted(modified_paths, key=len)
    if short_paths and len(short_paths[0]) <= 32:
        pull_bits.append("omhc log --file {}".format(short_paths[0]))
    pull = "PULL" + SEP + " · ".join(pull_bits)

    # --- fit the budget ------------------------------------------------------
    hidden_events = len(events) - len(humans) - len(unresolved[:_FAIL_MAX])
    dropped_slots: Dict[str, int] = {}

    def render(active: List[Tuple[str, str, int]], more: str) -> str:
        # Slot order is defined solely by the order add() was called. Dropping
        # preserves relative order, so there's no need to re-sort against a
        # fixed key list — writing the order in two places would mean fixing
        # both when adding a slot.
        lines = list(header)
        for slot_key, value, _prio in active:
            lines.append(slot_key + SEP + value)
        if more:
            lines.append("MORE" + SEP + _clip(more, 160))
        lines.append(pull)
        return "\n".join(lines) + "\n"

    def more_text() -> str:
        bits = []
        for key, count in sorted(dropped_slots.items()):
            bits.append("+{} {}".format(count, key.lower()))
        if fixed_later:
            bits.append("({} fixed later)".format(fixed_later))
        if len(unresolved) > _FAIL_MAX:
            bits.append("+{} fail".format(len(unresolved) - _FAIL_MAX))
        if hidden_events > 0:
            bits.append("{} event{} hidden".format(
                hidden_events, "" if hidden_events == 1 else "s"))
        return ", ".join(bits)

    active = list(slots)
    out = render(active, more_text())
    while len(out.encode("utf-8")) > budget and active:
        # Drop lowest priority first, tallying the drop into MORE.
        victim_index = min(range(len(active)), key=lambda i: (active[i][2], -i))
        key = active[victim_index][0]
        dropped_slots[key] = dropped_slots.get(key, 0) + 1
        del active[victim_index]
        out = render(active, more_text())

    if not active and slots:
        # A marker with no content left is useless. Say at least one thing,
        # even if it means clipping the top-priority slot to fit the budget.
        best = max(slots, key=lambda s: s[2])
        floor = len(render([], more_text()).encode("utf-8"))
        room = budget - floor - len(best[0]) - len(SEP) - 1
        if room >= 24:
            active = [(best[0], _clip(best[1], room), best[2])]
            dropped_slots.pop(best[0], None)
            trimmed = render(active, more_text())
            if len(trimmed.encode("utf-8")) <= budget:
                out = trimmed
            else:
                active = []

    if len(out.encode("utf-8")) > budget:
        # Shrink only line-by-line. Clipping mid-character would cut PULL off
        # in the middle, e.g. 'omhc log --file .git' truncated — a broken
        # command is worse than no command.
        for candidate in (
            render([], more_text()),
            "\n".join(header + [pull]) + "\n",
            "\n".join(header[:1] + [pull]) + "\n",
            header[0] + "\n",
        ):
            if len(candidate.encode("utf-8")) <= budget:
                out = candidate
                break
        else:
            # If even a single header line doesn't fit, there's nothing to send.
            out = ""

    out = guard.redact_b64(out)
    assert len(out.encode("utf-8")) <= budget, "mint exceeded its own budget"
    return out
