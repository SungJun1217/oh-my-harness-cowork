from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

IR_VERSION = 1

# 닫힌 중립 동사. 툴 어휘 교집합이 공집합이므로(Claude Code: Read/Edit/Bash/Task,
# Codex: shell/apply_patch/update_plan) 벤더 이름을 담을 자리를 아예 두지 않는다.
# 어휘 누출을 노력으로 막는 대신 스키마로 불가능하게 만든다.
VERBS = frozenset({"said", "inspected", "modified", "ran", "delegated", "researched"})

# 3값 판별자. "human" 만 축자 중계된다.
# 이 머신에서 서브에이전트 워크플로우 파일 137개에 type:"user" 레코드가 1766개
# 있었고, role 기반 허용목록은 그것을 전부 사람의 말로 판정해 그대로 중계한다.
# author 를 3값으로 두는 것이 그것을 구조적으로 막는 유일한 장치다.
AUTHORS = frozenset({"human", "agent", "harness"})


# slots=True 는 이 머신의 Python 3.9.25 에 존재하지 않는다(TypeError).
# frozen=True 단독으로 미선언 속성 할당까지 FrozenInstanceError 로 막히므로
# "어댑터가 이물질을 런타임에 붙일 수 없다"는 보장은 그대로 유지된다.
@dataclass(frozen=True)
class Event:
    """벤더 중립 레코드. 이 필드 목록이 곧 외래 물질의 공격 표면이다."""

    seq: int
    epoch: float
    author: str
    verb: str
    ok: bool
    text: str
    arg: str
    paths: Tuple[str, ...]
    offset: int
    length: int

    def __post_init__(self) -> None:
        if self.author not in AUTHORS:
            raise ValueError(
                "author must be one of {}: {!r}".format(sorted(AUTHORS), self.author)
            )
        if self.verb not in VERBS:
            raise ValueError(
                "verb must be one of {}: {!r}".format(sorted(VERBS), self.verb)
            )


def tally(events: Iterable[Event]) -> Dict[str, object]:
    """MORE 슬롯의 공개 의무가 쓰는 집계."""
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
