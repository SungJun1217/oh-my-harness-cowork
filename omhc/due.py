from __future__ import annotations

import collections
import os
from typing import Optional

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
        # 비대화형 판정은 mark 시점에 어댑터가 내려 기록한다. 여기서 entrypoint
        # 어휘를 다시 들고 있으면 같은 규칙이 두 모듈에 살면서 한쪽만 갱신되는
        # 반쪽 필터가 된다 — 어휘는 그것을 아는 어댑터에만 있어야 한다.
        if row.get("interactive") is False:
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

        return Watermark(
            repo_key=repo_key,
            harness=str(harness),
            session_id=str(session),
            path=str(row.get("path") or ""),
            event="start",
            epoch=epoch,
        )
    return None
