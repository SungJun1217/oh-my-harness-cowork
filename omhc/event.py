from __future__ import annotations

import collections
from typing import Dict, Iterable, Tuple

# Width of Event.arg. This is a property of the IR, not of the index TSV, so
# it lives here — if the adapter and the index each kept their own 120, raising
# one wouldn't do anything.
ARG_LIMIT = 120

# Closed, neutral verb set. The two harnesses' tool vocabularies don't intersect
# (Claude Code: Read/Edit/Bash/Task, Codex: shell/apply_patch/update_plan), so
# there's no field for a vendor name to land in. Leakage is prevented by the
# schema, not by discipline.
VERBS = frozenset({"said", "inspected", "modified", "ran", "delegated", "researched"})

# 3-valued discriminator. Only "human" is relayed verbatim.
# On this machine, 137 subagent workflow files had 1766 type:"user" records —
# a role-based allowlist would judge all of those as human speech and relay
# them as-is. Keeping author 3-valued is the only thing that blocks that
# structurally.
AUTHORS = frozenset({"human", "agent", "harness"})


# NamedTuple for import cost. dataclasses pulls in inspect/ast/dis/tokenize/
# linecache/copy — 8ms just to import on this machine, and the hook path's two
# processes pay that on every session start (11% of the 150ms budget). The
# invariant guarantee is the same either way — neither declared fields nor
# undeclared names can be assigned. typing.NamedTuple forbids overriding
# __new__, so this subclasses collections.namedtuple instead. __slots__ = ()
# removes the instance dict, keeping the same guarantee: no declared field and
# no undeclared name can be assigned.
_EventBase = collections.namedtuple(
    "Event", "seq epoch author verb ok text arg paths offset length"
)


class Event(_EventBase):
    """Vendor-neutral record. This field list is the attack surface for
    foreign material.

    Fields: seq:int epoch:float author:str verb:str ok:bool text:str arg:str
    paths:Tuple[str, ...] offset:int length:int
    """

    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls, *args, **kwargs)
        if self.author not in AUTHORS:
            raise ValueError(
                "author must be one of {}: {!r}".format(sorted(AUTHORS), self.author)
            )
        if self.verb not in VERBS:
            raise ValueError(
                "verb must be one of {}: {!r}".format(sorted(VERBS), self.verb)
            )
        return self


def tally(events: Iterable[Event]) -> Dict[str, object]:
    """Tally used by the MORE slot's disclosure duty."""
    by_verb: collections.Counter = collections.Counter()
    by_author: collections.Counter = collections.Counter()
    failures = 0
    total = 0
    for ev in events:
        total += 1
        by_verb[ev.verb] += 1
        by_author[ev.author] += 1
        if not ev.ok:
            failures += 1
    return {
        "total": total,
        "by_verb": dict(by_verb),
        "by_author": dict(by_author),
        "failures": failures,
    }
