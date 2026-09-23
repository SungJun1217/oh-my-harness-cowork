from __future__ import annotations

import time
from typing import Dict, List, Optional

from ..adapter import AdapterUnavailable

# 클래스의 리터럴 dict. 인스턴스가 아니라 클래스인 이유는 레포별 하네스 home 을
# 나중에 CLI 에서 한 줄로 꽂을 수 있게 하기 위함이다.
#
# entry_points 도, 디렉터리 스캐닝도, 자동 등록도 없다. 어댑터가 실제로 세 개가
# 되는 날 그때 만든다 — 지금 만들면 두 개를 보고 그린 추상이 되고, 세 번째에서
# 깨진다.
REGISTRY: Dict[str, type] = {}


def _register(cls: type) -> type:
    REGISTRY[cls.adapter_id] = cls
    return cls


def get(adapter_id: str, *, home: Optional[str] = None, now=time.time):
    """어댑터 하나를 만든다. 모르는 id 는 조용히 넘기지 않는다."""
    try:
        cls = REGISTRY[adapter_id]
    except KeyError:
        raise AdapterUnavailable(
            "unknown adapter_id {!r}; known: {}".format(adapter_id, sorted(REGISTRY))
        )
    return cls(home=home, now=now)


def present(*, homes: Optional[Dict[str, str]] = None, now=time.time) -> List[str]:
    """이 머신에 설치된 어댑터 id. 결정적 순서.

    한 어댑터의 나쁜 하루가 전체를 죽이면 안 된다 — detect() 가 던지거나
    생성이 실패하면 그 어댑터만 빠진다.
    """
    homes = homes or {}
    found: List[str] = []
    for adapter_id in sorted(REGISTRY):
        try:
            inst = get(adapter_id, home=homes.get(adapter_id), now=now)
            if inst.detect().present:
                found.append(adapter_id)
        except Exception:
            continue
    return found


# v1 어댑터를 등재한다. 모듈 맨 아래에서 import 하는 이유는 각 어댑터가
# `from . import _register` 로 이 모듈을 되참조하기 때문이다 — _register 가
# 이미 정의된 뒤라야 순환 import 가 성립한다.
#
# 자동 스캐닝을 두지 않는 것은 의도적이다. 어댑터가 세 개가 되는 날 그때
# 만든다. 지금은 이 두 줄이 레지스트리의 전부이고, 새 어댑터를 붙이는 비용도
# 여기에 한 줄 추가하는 것이다.
from . import claude_code  # noqa: E402,F401  (등록 부작용)
from . import codex_cli  # noqa: E402,F401  (등록 부작용)
