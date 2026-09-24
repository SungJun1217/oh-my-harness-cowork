from __future__ import annotations

import collections
import os
from typing import Callable, Optional

from . import fsio, ledger, locate

# 이 namedtuple 과 색인 TSV 의 paths 열이 함께 동시성 seam 을 이룬다.
Watermark = collections.namedtuple(
    "Watermark", "repo_key harness session_id path event epoch"
)

DELIVERED_NAME = "delivered.tsv"
OFF_MARKER = "off"
OFF_ENV = "OMHC_OFF"

# 이보다 오래된 외래 세션은 이어갈 작업으로 보지 않는다. 일주일 전 세션을
# "방금 일어난 일"처럼 주입하면 다음 에이전트가 끝난 일을 다시 한다.
MAX_AGE_SECONDS = 7 * 24 * 3600


def is_off(state_dir: str) -> bool:
    """끄기 스위치. 환경변수 또는 마커 파일."""
    if os.environ.get(OFF_ENV, "").strip() not in ("", "0", "false", "False"):
        return True
    return os.path.exists(os.path.join(state_dir, OFF_MARKER))


def off_reason(state_dir: str) -> Optional[str]:
    """켜져 있으면 None, 꺼져 있으면 무엇이 껐는지. is_off 와 같은 순서로 확인한다
    — `omhc status` 가 "off" 를 FAIL 로 보이지 않으면서도 왜 꺼졌는지는 보여줘야
    한다(끔은 사람이 의도한 상태일 수 있다)."""
    val = os.environ.get(OFF_ENV, "").strip()
    if val not in ("", "0", "false", "False"):
        return "{}={}".format(OFF_ENV, val)
    marker = os.path.join(state_dir, OFF_MARKER)
    if os.path.exists(marker):
        return "marker {}".format(marker)
    return None


def _delivered_path(state_dir: str) -> str:
    return os.path.join(state_dir, DELIVERED_NAME)


def already_delivered(state_dir: str, session_id: str, to_harness: str) -> bool:
    try:
        with open(_delivered_path(state_dir), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2 and parts[0] == session_id and parts[1] == to_harness:
                    return True
    except OSError:
        return False
    return False


def last_delivered(state_dir: str) -> Optional[str]:
    """이 레포에 가장 최근 전달된 세션 id — delivered.tsv 의 마지막 줄. append
    순서를 쓴다(invariant 6, 타임스탬프 아님). 아무것도 전달된 적 없으면 None."""
    try:
        with open(_delivered_path(state_dir), encoding="utf-8", errors="replace") as fh:
            lines = [line for line in fh if line.strip()]
    except OSError:
        return None
    if not lines:
        return None
    parts = lines[-1].rstrip("\n").split("\t")
    return parts[0] if parts and parts[0] else None


def mark_delivered(state_dir: str, watermark, *, to_harness: str, epoch: float) -> None:
    """이 세션을 이 하네스에 전달했다고 기록한다. 같은 것을 두 번 밀지 않기 위함."""
    if watermark is None:
        return
    line = "\t".join(
        (watermark.session_id, to_harness, watermark.harness, "{:.0f}".format(epoch))
    )
    fsio.append_line(_delivered_path(state_dir), line)


def due(
    repo_key: str,
    my_harness: str,
    my_session_id: str,
    now: float,
    *,
    home: Optional[str] = None,
    eligible: Optional[Callable[[Watermark], bool]] = None,
) -> Optional[Watermark]:
    """이 세션에 알려줄 외래 세션이 있는가. **여기가 동시성 seam이다.**

    v1 의 순차 사용 가정 전체가 이 함수 안에 있다. 상류(ledger, 어댑터, index,
    pin, guard)와 하류(mint, brief, agents_md)는 이미 순서 무관이며, 리더가
    append-only 로그를 바이트 0부터 EOF 까지 스트리밍하므로 여전히 자라는 파일을
    이미 견딘다.

    v2 는 반환형을 List[Watermark] 로 바꾸고 omhc/stale.py 를 같은 스트림의 두
    번째 소비자로 **추가**한다(기존 코드 수정이 아니다). 순서 키는 파일 mtime 도
    레코드별 타임스탬프도 아니다 — 원장의 세션 시작 epoch 와 소스 파일 내 바이트
    오프셋이다. 이 머신의 최대 트랜스크립트에 타임스탬프 역행이 254건 있다.

    cli._backfill_foreign_sessions 도 같은 키를 쓴다 — 신뢰 안 된 훅 때문에
    원장에 없는 하네스의 세션을 discover() 로 찾아 채울 때, 그 세션의 시작
    epoch(어댑터가 session_meta 등에서 읽음)를 원장에 이미 있는 그 하네스의
    최신 시작 epoch 와 비교해 append 순서를 정한다. **여전히 mtime 은 아니다**
    — "세션 시작 epoch 로만 순서를 정한다"는 이 함수와 완전히 같은 규칙이고,
    다른 파일(원장 대 rollout)의 epoch 를 비교한다는 점만 새롭다.
    """
    state = locate.state_dir(repo_key, home=home)
    if is_off(state):
        return None

    for row in reversed(ledger.read(repo_key=repo_key, home=home)):
        if row.get("event") != "start":
            continue
        harness = row.get("harness")
        session = row.get("session")
        if not harness or not session:
            continue
        if harness == my_harness:
            continue
        if session == my_session_id:
            continue
        # **가장 최근 외래 세션에서 멈춘다.** 이미 전달했다면 None 이다.
        #
        # 계속 거슬러 올라가면 어제 세션을 "방금 일어난 일"처럼 주입한다 —
        # cx3(수) 전달 후 다음 세션이 cx2(화)를, 그 다음이 cx1(월)을 받는
        # 식으로 점점 낡은 핸드오프가 나간다. 낡은 표식은 없는 표식보다 나쁘다.
        if already_delivered(state, str(session), my_harness):
            return None

        epoch = float(row.get("epoch") or 0.0)
        if epoch and now and (now - epoch) > MAX_AGE_SECONDS:
            # 너무 오래된 것은 이어갈 작업이 아니다.
            return None

        mark = Watermark(
            repo_key=repo_key,
            harness=str(harness),
            session_id=str(session),
            path=str(row.get("path") or ""),
            event="start",
            epoch=epoch,
        )
        # 사람이 대화한 세션인지는 호출자가 어댑터에게 물어 판정한다(#21). 여기서
        # entrypoint 어휘를 들고 있으면 같은 규칙이 두 모듈에 살면서 한쪽만
        # 갱신되는 반쪽 필터가 된다. 판정을 mark 시점에 원장에 적던 때는 Claude
        # 트랜스크립트가 아직 쓰이기 전이라 거의 기록되지 않았고, 그러면 헤드리스
        # 세션 하나가 가장 최근 외래 세션 자리를 차지해 그 앞의 진짜 세션까지
        # 막았다. 부적격이면 멈추지 않고 그 앞 행으로 간다.
        if eligible is not None and not eligible(mark):
            continue
        return mark
    return None
