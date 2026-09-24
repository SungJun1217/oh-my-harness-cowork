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


class SessionSince(NamedTuple):
    """`read_session_since` 의 결과. `SessionRead` 와 거의 같지만 `end_offset`
    을 더 들고 있다 — 이 읽기가 실제로 다 읽은, **마지막으로 완전한 줄 바로
    뒤**의 바이트 오프셋이다.

    호출자(cli._reactivate_grown_sessions)는 다음 baseline 으로 `os.stat` 의
    크기 대신 이걸 써야 한다 — stat 이 레코드 중간에 걸리면(하네스가 쓰는
    중일 수 있다), 그 크기를 그대로 baseline 으로 삼고 나중에 그 레코드가
    마저 쓰인 뒤 거기서부터 읽으면 skip-to-newline 로직이 그 레코드 전체를
    건너뛴다. `end_offset` 은 실제로 소비한 줄만 반영하므로 이 문제가 없다.
    `max_bytes`/조기 종료로 EOF 전에 멈췄으면 `end_offset` 은 자연히 거기서
    멈춘다 — 다음 호출이 그 지점부터 이어 읽는다."""

    events: Tuple[Event, ...]
    unparsed: int
    dropped: Dict[str, int]
    end_offset: int


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

    def read_session_since(self, ref: SessionRef, offset: int, *,
                           max_bytes: Optional[int] = None,
                           stop_at_human_turn: bool = False) -> Optional[SessionSince]:
        """`offset` 바이트부터 읽는다. 선택 메서드 — `discover`/`health` 와
        같은 패턴이다. 기본값 `None` 은 "이 어댑터는 구분할 수 없다"이고,
        호출자는 그러면 오늘의 전체 스캔 경로를 그대로 쓴다 — **이 판정은
        어댑터당 한 번이다**: 호출자가 아무 인자로나 한 번 불러 `None` 이면
        그 어댑터는 이후 완전히 건너뛴다(콜마다 달라지지 않는다).

        `codex exec resume` 처럼 같은 파일(같은 inode)에 새 턴이 이어붙는데
        `session_meta` 가 다시 쓰이지 않는 하네스가 이걸 구현한다 — 전체
        `read_session` 은 13.8MB rollout 에서 593ms 실측이라 훅 경로에서
        매번 돌리면 예산(150ms)을 넘긴다. `offset` 은 이전에 관측한 파일
        크기(바이트) — 레코드 경계일 필요는 없다: 줄 중간이면 구현이 다음
        개행까지 건너뛴다. 반환하는 이벤트는 `event.offset >= offset` 만
        포함한다(줄 경계로 스냅한 뒤 기준). 절대 던지지 않는다 — 가비지에도
        빈 결과로 열화한다.

        `max_bytes`(선택, 기본 무제한)를 주면 그만큼만 읽고 멈춘다 — 실측
        (17.7MB 꼬리): 늘어난 만큼 비용이 그대로 비례해(396.6ms) 훅 예산을
        넘길 수 있다. `stop_at_human_turn`(선택, 기본 False)이면 사람의
        `said` 이벤트를 하나라도 찾는 즉시 멈춘다 — 존재 여부만 필요한
        호출자(재개 감지)가 나머지를 읽지 않게 한다. 반환값의 `end_offset`
        은 항상 **실제로 다 읽은 마지막 완전한 줄 바로 뒤** 오프셋이다 —
        기본값(무제한, 조기 종료 없음)에서는 EOF 와 같다.
        """
        return None

    def native_resume_hint(self, ref: SessionRef) -> Optional[str]:
        raise NotImplementedError

    def install_handoff(self, bundle: HandoffBundle) -> InstallReceipt:
        raise NotImplementedError

    def classify(self, source_path: str) -> bool:
        """이 트랜스크립트가 **사람이 대화한 세션**인가.

        False 는 헤드리스·서브에이전트라고 확실할 때만 낸다. 판단할 수 없으면
        True 다 — brief 는 False 인 원장 행을 건너뛰고 그 앞 행으로 가므로, 모르는
        파일을 False 로 판정하면 낡은 세션이 나간다(#21).

        하네스별 지식이므로 어댑터가 소유한다. 코어가 한 어댑터의 파서로 다른
        하네스의 세션을 판정하면 안 된다 — Claude 의 entrypoint/isSidechain 을
        Codex rollout 에서 찾으면 아무것도 없어 필터가 조용히 no-op 가 된다.
        """
        raise NotImplementedError

    def discover(self, repo_root: Optional[str], deadline: Optional[float] = None):
        """이 레포에 새로 나타난 세션들. `mark` 의 원장 백필 전용 선택 메서드.

        `fallback_channels`/`health` 와 같은 선택 메서드 패턴이다 — 기본은 빈
        튜플이며, list_sessions 처럼 비싼 전체 스캔을 모든 어댑터가 구현할
        의무는 없다(Claude 는 이 스캔이 249ms 실측이라 훅 경로에서 절대 쓰면
        안 되므로 명시적으로 빈 튜플을 돌려준다).

        구현하는 어댑터는 반환하는 `SessionRef.epoch` 를 **세션 시작 시각**으로
        채워야 한다 — list_sessions 의 epoch(파일 mtime, "최근 것부터" 정렬용)와
        다르다. cmd_mark 가 이 값을 원장의 최신 시작 epoch 와 비교해 append
        순서를 정하므로(invariant 6 이 금지하는 "순서의 근거로 삼는 mtime"이
        아니라, due() 와 같은 키인 세션 시작 epoch 를 그대로 쓰는 것이다 —
        허용된 예외다), mtime 을 여기 섞으면 오래된 세션이 최신으로 오인될
        수 있다.

        `deadline` 은 `time.time()` 과 같은 시계의 절대 시각(선택)이다. 스캔
        비용이 큰 어댑터(Codex 의 날짜 디렉터리 순회)는 루프 안에서 주기적으로
        확인해 넘기면 지금까지 모은 것만 돌려주고 멈춰야 한다 — 안 그러면
        cmd_mark 쪽 시간 예산은 discover() 호출이 끝난 뒤에야 재기 때문에
        무의미해진다. 기본값 `None` 은 "끊지 않는다"이다.
        """
        return ()

    def fallback_channels(self):
        """install_handoff 가 실패했을 때 순서대로 시도할 채널들.

        각 항목은 HandoffBundle 을 받아 InstallReceipt 를 돌려주거나
        NoInjectionChannel 을 던진다. 라우터가 벤더 문자열을 들고 있으면
        "어댑터 추가는 파일 하나" 라는 계약이 문자 그대로 깨진다 — 세 번째
        WRITE 어댑터가 자기 파일 기반 폴백(Cursor 의 rules, kimi-code 의 메모리)을
        선언할 방법이 없어진다.
        """
        return ()

    def health(self, repo_root: Optional[str], ledger_rows) -> Tuple[Tuple[str, Optional[bool], str], ...]:
        """선택적 진단. 기본은 빈 튜플 — 모든 어댑터가 구현할 의무는 없다.

        실측(codex-cli 0.155.1): 신뢰되지 않은 `~/.codex/hooks.json` 훅은 메시지도
        원장 행도 없이 조용히 건너뛰어진다. brief 가 한 번도 안 돌아도 `omhc
        status` 는 전부 PASS 를 보였다 — 사용자가 그 사실을 알 방법이 없었다.
        하네스별 행태 증거가 필요하므로 코어가 아니라 어댑터가 소유한다.

        `fallback_channels` 처럼 선택 메서드 패턴을 따른다 — 구현하지 않는
        어댑터는 이 기본값으로 충분하고, `cmd_status` 가 단정하는 `(label, ok,
        detail)` 형태만 지키면 된다. `ok` 는 `True`/`False`/`None` 이다 — 실제
        판정이 되는 경우만 `True`/`False`(게이팅), 아직 판단할 근거가 없는
        정보성 진단은 `None`(`----`, 게이팅 안 함)을 돌려준다. 절대 예외를
        던지지 않는다 — status 는 진단 도구이고, 진단이 죽으면 열화를 보고할
        방법이 없어진다.

        `ledger_rows` 는 **레포로 거르지 않은** 원장이다 — 세션 id 는 전역
        유일하므로, 호출자가 이 레포 키로 미리 거르면 자기 `.git` 을 가진 중첩
        워크트리·서브모듈에서 시작한 세션이 다른 repo 키로 기록돼 영원히 "안
        돈 것"으로 보인다.
        """
        return ()

    def hook_config(self):
        """이 하네스의 SessionStart 훅 설정 위치. 선택 메서드 — `health`/
        `fallback_channels`/`discover` 와 같은 패턴이다. 기본은 None(훅 개념이
        없는 하네스, 또는 아직 지원하지 않는 하네스).

        구현하면 `omhc.hookconf.HookConfig`(또는 그와 같은 3개 필드짜리 레코드)를
        돌려준다: `config_path`(그 조각이 병합될 하네스 설정의 절대 경로),
        `fragment_name`(`hooks/` 아래 그 하네스 조각 파일명), `post_write_note`
        (설치 직후 사용자가 봐야 할 안내 — 예: Codex 의 훅 신뢰 재승인 경고;
        없으면 빈 문자열). `omhc status` 의 `<adapter-id> hooks` 행과 `omhc
        hooks install` 이 이 레코드 하나로 두 하네스를 동일하게 다룬다 — 코어에
        벤더 이름이 들어가지 않는다.
        """
        return None

    def ref_for_path(self, source_path: str, session_id: str,
                     cwd: Optional[str] = None) -> Optional[SessionRef]:
        """알려진 경로 하나를 SessionRef 로 만든다. 부적격이면 None.

        원장이 경로를 기록해 두므로, 세션 목록을 전부 스캔하지 않고 바로 그 파일을
        열 수 있다 — 실측에서 list_sessions 는 130개 파일 34MB 를 읽어 1건을
        남겼고 그것이 훅 예산 150ms 의 1.7배였다.
        """
        raise NotImplementedError
