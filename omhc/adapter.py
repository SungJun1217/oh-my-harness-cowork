from __future__ import annotations

import enum
from typing import Dict, NamedTuple, Optional, Tuple

from .event import Event


class Capability(enum.Enum):
    """선언된 능력. 두 개뿐이며, 실물 하네스가 요구하지 않는 플래그는 두지 않는다.

    읽기와 쓰기는 독립이다. Cursor 처럼 세션 훅이 없는 하네스는 읽기 전용
    어댑터가 정상 상태이며 결함이 아니다.
    """

    READ = "read"
    WRITE = "write"


class OmhcAdapterError(Exception):
    """어댑터 계층의 모든 예외의 기반."""


class AdapterUnavailable(OmhcAdapterError):
    """모르는 adapter_id, 또는 이 머신에 설치되지 않은 하네스."""


class UnsupportedFormat(OmhcAdapterError):
    """읽을 수는 있었으나 형식을 알아볼 수 없었다. 조용히 넘기지 않는다."""


class NoInjectionChannel(OmhcAdapterError):
    """쓰기 능력이 없거나 모든 주입 경로가 막혔다. 호출자가 보편 바닥으로 보낸다."""


class HarnessPresence(NamedTuple):
    present: bool
    note: str = ""


class SessionRef(NamedTuple):
    """한 세션 파일을 가리키는 포인터.

    cwd 가 None 일 수 있다 — 작업 디렉터리 개념이 없는 하네스가 존재하며,
    지금 필드 하나를 허용하는 비용이 나중에 Protocol 을 바꾸는 비용보다 싸다.
    """

    adapter_id: str
    session_id: str
    source_path: str
    cwd: Optional[str]
    epoch: float
    size: int


class SessionRead(NamedTuple):
    """한 세션을 중립 Event 로 읽은 결과.

    unparsed/dropped 는 조용한 열화를 관측 가능하게 만든다. 모르는 레코드에서
    예외를 던지면 훅 경로가 죽고, 조용히 버리면 유실을 아무도 모른다.
    """

    ref: SessionRef
    events: Tuple[Event, ...]
    unparsed: int
    # 기본값을 주지 않는다 — NamedTuple 의 기본값은 인스턴스 간에 공유되므로
    # 가변 dict 를 기본값으로 두면 한 어댑터의 집계가 다른 어댑터에 새어든다.
    dropped: Dict[str, int]


class HandoffBundle(NamedTuple):
    body_md: str
    repo_root: str
    to_adapter_id: str


class InstallReceipt(NamedTuple):
    """모든 주입 경로는 receipt 로 끝난다. 조용한 실패를 만들지 않는다."""

    channel: str
    paths_written: Tuple[str, ...] = ()
    consumed_on_read: bool = False
    cleanup_hint: str = ""


class HarnessAdapter:
    """하네스 하나(정확히는 하나의 표면)에 대한 어댑터. 메서드 5개가 전부다.

    부수 규칙:
    - `__init__` 에서 I/O 를 하지 않는다. `home=` 과 `now=` 를 키워드로 받는다.
    - `detect()` 는 싸야 하고 **예외를 던지지 않는다**.
    - 모르는 레코드 타입은 기본 DROP 이며 `SessionRead.dropped` 에 보고된다.
    - `author == "human"` 은 **최상위 세션 파일에서만** 나온다.
    - 글롭은 깊이 1만. 중첩 `<session>/subagents/**` 는 읽지 않는다.

    typing.Protocol 을 쓰지 않는 이유: 3.9 에서 런타임 검사가 제한적이고,
    적합성 스위트가 실제 인스턴스로 계약을 검증하므로 명목 기반 클래스가 더
    솔직하다.
    """

    adapter_id: str = ""
    capabilities: frozenset = frozenset()

    # 주입 JSON 형식. 하네스별 사실 중 가장 하네스별인 것이므로 어댑터가 소유한다.
    #   "claude" : {"hookSpecificOutput": {"hookEventName": …, "additionalContext": …}}
    #   "cursor" : {"additional_context": …}
    #   "sdk"    : {"additionalContext": …}   (SDK 표준 / Copilot CLI)
    # 코어에 harness→wire 표를 두면 새 어댑터가 코어를 고쳐야 하고, 고치지 않으면
    # 자기 하네스가 무시하는 필드를 조용히 내보낸다(receipt 도 남지 않는다).
    wire: str = "sdk"

    def __init__(self, *, home: Optional[str] = None, now=None) -> None:
        raise NotImplementedError

    def detect(self) -> HarnessPresence:
        raise NotImplementedError

    def list_sessions(self, repo_root: Optional[str]):
        raise NotImplementedError

    def read_session(self, ref: SessionRef) -> SessionRead:
        raise NotImplementedError

    def native_resume_hint(self, ref: SessionRef) -> Optional[str]:
        raise NotImplementedError

    def install_handoff(self, bundle: HandoffBundle) -> InstallReceipt:
        raise NotImplementedError

    def classify(self, source_path: str) -> bool:
        """이 트랜스크립트가 **사람이 대화한 세션**인가.

        하네스별 지식이므로 어댑터가 소유한다. 코어가 한 어댑터의 파서로 다른
        하네스의 세션을 판정하면 안 된다 — Claude 의 entrypoint/isSidechain 을
        Codex rollout 에서 찾으면 아무것도 없어 필터가 조용히 no-op 가 된다.
        """
        raise NotImplementedError

    def fallback_channels(self):
        """install_handoff 가 실패했을 때 순서대로 시도할 채널들.

        각 항목은 HandoffBundle 을 받아 InstallReceipt 를 돌려주거나
        NoInjectionChannel 을 던진다. 라우터가 벤더 문자열을 들고 있으면
        "어댑터 추가는 파일 하나" 라는 계약이 문자 그대로 깨진다 — 세 번째
        WRITE 어댑터가 자기 파일 기반 폴백(Cursor 의 rules, kimi-code 의 메모리)을
        선언할 방법이 없어진다.
        """
        return ()

    def ref_for_path(self, source_path: str, session_id: str,
                     cwd: Optional[str] = None) -> Optional[SessionRef]:
        """알려진 경로 하나를 SessionRef 로 만든다. 부적격이면 None.

        원장이 경로를 기록해 두므로, 세션 목록을 전부 스캔하지 않고 바로 그 파일을
        열 수 있다 — 실측에서 list_sessions 는 130개 파일 34MB 를 읽어 1건을
        남겼고 그것이 훅 예산 150ms 의 1.7배였다.
        """
        raise NotImplementedError
