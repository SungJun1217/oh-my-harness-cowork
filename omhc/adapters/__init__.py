from __future__ import annotations

import calendar
import os
import re
import time
from typing import Dict, List, Optional

from .. import fsio, locate
from ..adapter import AdapterUnavailable, InstallReceipt

# 헤드리스 오버라이드. 샌드박스에서 `claude -p`/`codex exec` 를 실제 세션으로
# 받아들이기 위한 스위치다. 두 하네스가 공유하므로 이름에 벤더 문자열이 없다.
# 서브에이전트/사이드체인은 이걸로도 절대 풀리지 않는다 — 그건 발화자가 다른
# 문제이지 대화형/비대화형 문제가 아니다.
HEADLESS_ENV = "OMHC_ALLOW_HEADLESS"


def allow_headless() -> bool:
    return os.environ.get(HEADLESS_ENV, "").strip() not in ("", "0", "false", "False")

# 클래스의 리터럴 dict. 인스턴스가 아니라 클래스인 이유는 레포별 하네스 home 을
# 나중에 CLI 에서 한 줄로 꽂을 수 있게 하기 위함이다.
#
# entry_points 도, 디렉터리 스캐닝도, 자동 등록도 없다. 어댑터가 실제로 세 개가
# 되는 날 그때 만든다 — 지금 만들면 두 개를 보고 그린 추상이 되고, 세 번째에서
# 깨진다.
REGISTRY: Dict[str, type] = {}


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")


def iso_epoch(value) -> float:
    """ISO 타임스탬프 → epoch. 알아볼 수 없으면 0.0.

    **순서의 근거로 쓰지 않는다** — 이 머신의 최대 트랜스크립트에 타임스탬프
    역행이 254건(최대 52ms) 있다. 순서는 원장 epoch 와 바이트 오프셋이다.

    두 어댑터가 이것을 각자 구현하고 있었다(한쪽은 정규식, 한쪽은 고정 폭 슬라이싱).
    받아들이는 입력이 달라서, 한쪽이 파싱하는 형식을 다른 쪽은 0.0 으로 돌려주고
    mint 의 헤더가 하네스에 따라 다르게 나왔다.
    """
    if not isinstance(value, str):
        return 0.0
    m = _ISO.match(value)
    if not m:
        return 0.0
    try:
        return float(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)))
    except (ValueError, OverflowError):
        return 0.0


def install_state_artifact(bundle, *, home: Optional[str] = None) -> InstallReceipt:
    """훅이 읽어갈 자리에 산출물을 둔다. push 가 아니라 pull 이다.

    두 어댑터의 install_handoff 본문이 바이트 단위로 같았다 — 범용 코드를 어댑터가
    들고 있으면 계층이 뒤집힌다. 여기 한 곳에 두고 어댑터는 한 줄로 위임한다.
    """
    state = locate.state_dir(locate.repo_key(bundle.repo_root), home=home)
    path = locate.artifact_path(state)
    fsio.write_atomic(path, bundle.body_md)
    return InstallReceipt(
        channel="sessionstart-hook",
        paths_written=(path,),
        consumed_on_read=True,
        cleanup_hint="omhc clear",
    )


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
