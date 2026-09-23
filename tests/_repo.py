"""테스트가 쓰는 레포 경로의 단일 정의.

절대 경로를 하드코딩하면 다른 체크아웃·다른 머신에서 `list_sessions()` 가 0건을
돌려주고, 세션을 순회하는 불변식들이 **단정을 하나도 실행하지 않은 채 PASS** 가
된다 — "외래 물질이 새지 않는다" 는 보장이 검증됐다고 보고되면서 실제로는 아무것도
검사되지 않는, 가장 위험한 종류의 통과다.
"""
from __future__ import annotations

import os

# tests/_repo.py → 레포 루트
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIXTURES = os.path.join(REPO, "tests", "fixtures")


def have_sessions(refs) -> bool:
    """세션을 하나도 못 찾았으면 그 테스트는 아무것도 검증하지 않았다."""
    return bool(list(refs))
