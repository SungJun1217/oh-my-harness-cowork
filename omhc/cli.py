from __future__ import annotations

import argparse
import json
import os
import re
import select
import sys
import time
from typing import Dict, List, Optional

from . import (
    adapters, agents_md, brief, due, fsio, gate, hookconf, index, ledger, locate,
    managed_block, pin, watch,
)
from .adapter import AdapterUnavailable, SessionRef

PROG = "omhc"
NOTES_NAME = "notes.txt"
ARTIFACT_NAME = "omhc.txt"

# pull rate 가 보는 "최근 전달" 창(§9, #25). 분모를 delivered.tsv 전체로 두면
# 오래된 전달이 영원히 분모에 남아 인출률이 서서히 낮아 보인다.
PULL_RATE_WINDOW = 20


def _stdin_text() -> str:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    except Exception:
        return ""


# --dry-run 이 stdin 을 기다리는 시간의 상한(초). 훅 예산과는 무관하다 —
# 사람이 손으로 부르는 경로다.
_DRY_RUN_STDIN_TIMEOUT = 0.2
# 훅 payload 는 1KB 남짓이다. `yes |` 처럼 끝없이 쓰는 쪽이면 데이터가 늘
# 준비돼 있어 타임아웃이 안 걸리므로 크기로도 끊는다(리뷰).
_DRY_RUN_STDIN_CAP = 1 << 20


def _dry_run_stdin_text() -> str:
    """`--dry-run`(`--stdin` 없이)용 stdin 읽기 — 읽되 멈추지는 않는다(#27
    리뷰). 문서화된 쓰임 하나가 `echo '{"cwd": R}' | omhc brief --dry-run`
    처럼 다른 cwd 에서 payload 를 파이프로 넘기는 것이라 아예 안 읽으면 그
    쓰임이 깨진다. 그렇다고 `sys.stdin.read()` 를 그대로 쓰면 파이프의 다른
    쪽 끝이 한 줄 보내고 열어만 둔 채로 있어도(TTY 가 아니라 isatty() 는
    False) EOF 를 영영 못 만나 멈춘다. 그래서 select 로 "지금 읽을 게 있는가"
    만 묻고, 있으면 읽고, 다음 데이터가 타임아웃 안에 안 오면 거기서 멈춘다
    — EOF(echo 처럼 쓰고 닫음)도 "읽을 게 있다"로 잡혀 즉시 반환된다."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        fd = sys.stdin.fileno()
        chunks = []
        total = 0
        while total < _DRY_RUN_STDIN_CAP:
            ready, _w, _x = select.select([fd], [], [], _DRY_RUN_STDIN_TIMEOUT)
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        # select 는 Windows 에서 파이프에 못 쓴다; fileno()/read() 도 닫힌
        # 스트림이면 던질 수 있다. 훅 경로가 아니어도 절대 던지지 않는다.
        return ""


def _state_for(home: Optional[str], start: Optional[str] = None):
    root = locate.resolve_repo_root(start)
    key = locate.repo_key(root)
    return root, key, locate.state_dir(key, home=home)


# --- mark -------------------------------------------------------------------


# 신뢰되지 않은 Codex 훅은 mark 를 조용히 건너뛴다(실측, codex_cli.py 참고).
# 그러면 그 Codex 세션은 원장에 영영 없고, due() 는 원장만 읽으므로 Claude
# 쪽 훅이 멀쩡히 돌아도 Codex→Claude 가 죽는다. 그래서 **Claude 의** mark 가
# 얹혀서 다른 하네스(Codex)의 세션을 원장에 채운다 — due() 자체는 안 바뀐다.
BACKFILL_CAP = 5
# #22: cap 을 넘는 초과분과, discover() 의 시간 예산에 밀려 못 본 나머지는
# 이후 어떤 mark 도 다시 채우지 않는다 — 다음 호출의 newest_start 가 이미
# 이번에 고른 것 중 가장 최근 것이라 그보다 오래된 미채움 세션은 "원장의
# 최신 start 보다 오래됨" 판정에 영영 걸린다. 그런데도 무해한 건 두 가지가
# 겹쳐서다: due() 는 가장 최근 자격 있는 외래 세션 하나만 보면 되고,
# discover() 는 brief 의 eligible(#21)과 **같은** 헤드리스 필터
# (allow_headless())를 쓴다 — 그래서 보통 discover() 가 채우는 것과 due() 가
# 원하는 것이 같은 집합이다. 유일하게 깨지는 경우는 mark 시점과 이후 brief
# 시점 사이에 OMHC_ALLOW_HEADLESS 가 달라지는 것뿐이다(그러면 그 사이에 생긴
# 대화형 세션이 헤드리스 더미 뒤에 있다가 채워지지 않은 채로 cap 에 밀릴 수
# 있다) — 흔치 않은 설정 변경이라 v1 에서는 감수한다.
# 훅 예산(150ms) 의 일부만 쓴다. 비싼 부분은 discover() 자체(Codex 는 날짜
# 디렉터리 스캔)이므로 이 deadline 을 discover() 에도 그대로 넘겨 어댑터가
# 스스로 스캔을 끊게 한다 — 여기서만 재고 있으면 discover() 호출 자체가
# 늦게 끝나 이 mark 호출이 세션 시작을 지연시킬 수 있다.
BACKFILL_TIME_BUDGET = 0.08

# 한 하네스에서 한 번에 확인하는 최근 세션 수(`_backfill_foreign_sessions` 의
# 재기준점 찍기, `_reactivate_grown_sessions` 둘 다 쓴다). discover()/
# list_sessions() 처럼 전체 스캔을 하지 않고(#22: 시작한 지 14일 넘은 세션도
# 여전히 재개가 잡혀야 한다 — discover() 의 SCAN_DAYS 창 밖이다) 원장에 이미
# 적힌 path 로만 stat 하므로 20개는 훅 예산 안에서 무시할 만하다.
REACTIVATE_SCAN_CAP = 20


def _ref_repo_key(ref) -> Optional[str]:
    """이 ref 가 실제로 속한 레포 키. `locate.owning_repo_key` 로 위임한다
    (원래 이 함수에 있던 로직 — codex_cli.health() 도 같은 필터를 쓴다)."""
    return locate.owning_repo_key(ref.cwd)


# rebase 마커의 event 값. session/path 가 없는(자기 세션이 아닌) 원장 행이므로
# due()/log 랭크/known_sessions/newest_start/codex health 모두 "event=='start'"
# 만 보는 기존 필터에 자동으로 걸러진다 — 필드 자체를 안 넣는 편이 "모르는
# 리더는 무시한다"를 코드로 강제하는 것보다 안전하다(#28).
REBASE_EVENT = "rebase"


def _append_rebase_marker(adapter_id: str, key: str, home, now: float) -> None:
    """`adapter_id` 의 알려진 모든 세션에 대해 "이 지점 이후로 baseline 위치가
    최소 여기"라고 한 번에 선언하는 행. 개별 seen 행(#22 증상: backfill 마다
    알려진 세션 수만큼 늘던 것) 대신 이것 하나만 남긴다 — session/path 가
    없으므로 known_sessions/newest_start/log 랭크/codex health 는 그대로
    무시한다(모두 event=='start' 나 session 존재를 전제로 거른다)."""
    ledger.append({
        "repo": key, "harness": adapter_id, "event": REBASE_EVENT, "via": "scan",
        "epoch": now,
    }, home=home)


def _distinct_sessions_with_path(rows: List[dict], *, exclude=frozenset()) -> List[str]:
    """`rows` 를 뒤에서부터 훑어 path 있는 distinct session id 를 최신순으로
    돌려준다 — "이번 라운드에 볼 후보"(writer, 최대 `REACTIVATE_SCAN_CAP`
    개로 자름)와 "마커가 실제로 덮은 세션 집합"(reader, `own_rows` 를 마커
    **이전** 구간으로 슬라이스한 뒤 같은 함수로 재구성) 둘 다 반드시 같은
    순위를 써야 한다(#28 2차 리뷰) — 각자 따로 구현하면 어긋나는 순간
    "마커가 확인 못 한 세션까지 덮는다"는 바로 그 버그가 재발한다."""
    out = []
    seen = set()
    for row in reversed(rows):
        sid = row.get("session")
        if not sid or sid in exclude or sid in seen or not row.get("path"):
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _rebaseline_after_fresh_start(adapter_id: str, key: str, root: str, home,
                                  own_rows: List[dict], added: set,
                                  now: float, deadline: float) -> None:
    """리뷰(#22 재검토): 이 라운드가 `adapter_id` 에 더 최신 세션(B)을 방금
    원장에 채웠다. 다른 기존 세션(A)들의 baseline 이 여전히 B 의 start 행
    앞에 남아 있으면, 다음 `_reactivate_grown_sessions` 라운드가 A 의
    "지금 크기"를 그 낡은 baseline 과 통째로 비교한다 — 그 사이(B 가 들어온
    뒤)에 A 에 생긴 **진짜** 재개까지 "이미 B 에 밀렸다"(superseded)로
    뭉뚱그려 seen 행으로 흡수해 버리고, 다시는 재판정하지 못한다(그 흡수가
    실제로는 A 의 유일한 새 턴을 삼킨 것이었어도).

    B 가 막 들어온 **이 순간**(아직 A 가 더 자라지 않았을 가능성이 높은
    시점) A 들을 다시 stat 해 seen 행을 B 뒤에 남긴다 — 그러면 이후 A 에
    생기는 진짜 성장은 이 새 baseline(이미 B 뒤에 있다) 과 비교되어 깨끗하게
    새 판정을 받는다.

    리뷰(3차, t5): 이 함수는 B 가 **백필**(discover→known_sessions 에 새로
    잡힌 경우)로 들어왔을 때만 불린다 — B 가 자기 자신의 신뢰된 훅으로
    직접 start 행을 남기면 `_backfill_foreign_sessions` 는 그 세션을
    "이미 안다"고 보고(known_sessions 에 이미 있다) 다시 안 채우므로 이
    함수가 아예 안 불린다. 그 경로는 `_reactivate_grown_sessions` 안의
    지연(lazy) 재기준점이 대신 잡는다(그쪽 주석 참고) — 이 함수를 없애지
    않는 이유는 백필 origin 에서는 **B 가 들어온 바로 그 순간**(아직 A 가
    안 자랐을 가능성이 가장 높은 시점) 찍으므로, 지연 경로보다 흡수될
    애매구간(다음 mark 까지의 창)이 짧기 때문이다 — 두 경로가 같은 결과로
    수렴하지만 이쪽이 더 이르다.

    #28: 여기서 재기준이 필요한 A 들 중 실제로 자란 적 없는(size 가 그대로인)
    세션은 개별 seen 행 대신 한 번의 `rebase` 마커로 흡수한다 — 실측(#22):
    새 Codex 세션 20개가 5개씩 채워지는 backfill 마다 다른 세션 최대 20개를
    다시 stat 해 seen 행을 남겨 34개가 늘었다. 실제로 자란(agent 혼잣말 등)
    세션은 여전히 자기 seen 행을 받는다 — **그 행을 마커보다 먼저 쓴다**:
    마커는 "이 라운드에서 안 자란 것으로 확인된 세션들"에만 해당하고, 자란
    세션은 자기 위치를 스스로 갱신하므로 마커가 걔들의 판정을 흐리지 않는다.

    리뷰(#28 1차): 마커는 "**이번에 실제로 훑은** 세션들이 최소 여기까지는
    안 자란 채 확인됐다" 는 선언이다 — deadline 이 중간에 끊거나(`break`),
    stat 이 일시적 OSError 로 실패하거나, seen 행 자체가 `ledger.append`
    상한에 걸려 버려지면, 이번 라운드는 "훑은 것 전부 확인" 이 아니다 —
    확인 못 한 세션도 마커가 똑같이 덮어버려 그 세션의 실제 위치를 실제보다
    뒤로(더 최신으로) 잘못 민다. 재현: A 가 B 전에 이미 자란 채(사람 턴 포함)
    deadline/OSError 때문에 이번 라운드에 확인 안 됐는데 다른 세션(안 자람)
    때문에 마커가 찍히면, 다음 라운드에 A 의 위치가 마커 뒤로 밀려 그 애매한
    사전 성장이 (흡수돼야 할 것이) 명확한 재개로 오판된다 — due() 가 B 대신
    A 를 돌려준다. 그래서 전수 확인 여부(`complete`)를 추적해, 완전할 때만
    마커 하나로 묶고, 아니면 확인된 만큼만(옛 방식대로) 개별 seen 행을
    남긴다.

    리뷰(#28 2차): `complete` 는 **cap 초과와 무관하다** — cap
    (`REACTIVATE_SCAN_CAP`)에 걸려 이번 라운드 후보에서 아예 빠진 세션은
    "확인 못 한 것" 이 아니라 "원래 이번 마커가 아무것도 약속하지 않는
    것"이다(마커가 덮는 범위 자체가 `_reactivate_grown_sessions` 쪽에서
    "이번 마커를 쓸 때의 top-N" 으로 재구성된다 — 그쪽 주석 참고). cap 을
    `complete` 에 얹으면(1차 버전의 실수) 알려진 세션이 20개를 넘는 레포에서
    영원히 개별 seen 으로 되돌아가 애초에 고치려던 행 폭증이 그대로
    재현된다(실측: n=25/60 에서 HEAD 와 같은 84행/80seen)."""
    others = _distinct_sessions_with_path(own_rows, exclude=added)[:REACTIVATE_SCAN_CAP]
    complete = True
    unchanged = []  # (sid, path, size) — 이 라운드에서 안 자란 것으로 확인됨
    for sid in others:
        if time.time() > deadline:
            complete = False
            break
        # 이 세션의 마지막 path/size — 리뷰(3차 #3)의 fallback 으로 쓴다.
        path = None
        prior_size = None
        for row in own_rows:
            if row.get("session") != sid:
                continue
            if row.get("path"):
                path = row.get("path")
            if "size" in row:
                try:
                    prior_size = int(row["size"])
                except (TypeError, ValueError):
                    pass
        if not path:
            continue
        try:
            cur_size = os.stat(path).st_size
        except OSError:
            # 일시적 실패 — 이 세션의 "안 자람" 을 확인 못 했다(#28 리뷰).
            complete = False
            continue
        # fallback: 이전에 알던 baseline 이 있으면 그것 — 64KB 안에 개행을
        # 못 찾아도 baseline 이 레코드 중간으로 밀리지 않는다. 없으면 None
        # (size 그대로). 0 으로 대체하면 처음부터 다시 읽어 옛 사람 턴으로
        # 거짓 재활성화한다(리뷰에서 재현).
        aligned = fsio.line_aligned_size(path, cur_size, fallback=prior_size)
        if prior_size is not None and aligned == prior_size:
            # 안 자랐다 — complete 로 밝혀지면 이 세션은 마커 하나로 충분하다
            # (#28). 아니라면 아래에서 옛 방식(개별 seen)으로 되돌린다.
            unchanged.append((sid, path, aligned))
            continue
        if not ledger.append({
            "repo": key, "harness": adapter_id, "session": sid,
            "event": "seen", "via": "scan",
            "size": aligned,
            "epoch": now, "path": path, "cwd": root,
        }, home=home):
            complete = False
    if unchanged:
        if complete:
            _append_rebase_marker(adapter_id, key, home, now)
        else:
            for sid, path, size in unchanged:
                ledger.append({
                    "repo": key, "harness": adapter_id, "session": sid,
                    "event": "seen", "via": "scan", "size": size,
                    "epoch": now, "path": path, "cwd": root,
                }, home=home)


def _backfill_foreign_sessions(harness: str, root: str, key: str, state: str,
                                home, now: float, *,
                                deadline: Optional[float] = None) -> Dict[str, set]:
    """`deadline` 을 안 주면 이 호출 하나만의 예산으로 스스로 잰다(예전 동작,
    독립 호출·테스트 호환용). `cmd_mark` 는 `_reactivate_grown_sessions` 와
    **같은** 예산을 나눠 써야 훅 시간을 두 배로 쓰지 않으므로 자신의
    deadline 을 넘겨준다.

    반환값: {adapter_id: {새로 채운 session_id, ...}} — `_reactivate_grown_sessions`
    가 이걸로 "이번 mark 가 이 하네스에 더 최신 세션을 방금 채웠다"(조건 b)를
    판정한다."""
    if deadline is None:
        deadline = time.time() + BACKFILL_TIME_BUDGET
    fresh: Dict[str, set] = {}
    rows = ledger.read(repo_key=key, home=home)
    for adapter_id in sorted(adapters.REGISTRY):
        if adapter_id == harness:
            continue
        if time.time() > deadline:
            break
        try:
            refs = list(adapters.get(adapter_id, home=home).discover(
                root, deadline=deadline))
        except Exception:
            continue
        if not refs:
            continue
        own_rows = [r for r in rows if r.get("harness") == adapter_id]
        known_sessions = {str(r.get("session")) for r in own_rows if r.get("session")}
        newest_start = 0.0
        for r in own_rows:
            # grew 행의 epoch 는 세션의 실제 시작 시각이 아니라 재개를 감지한
            # "now" 다(_reactivate_grown_sessions) — 이걸 newest_start 에 섞으면
            # 아직 못 채운, 진짜로 더 오래된 세션이 "이미 최신보다 오래됨"
            # 판정에 걸려 영영 안 채워진다.
            if r.get("event") == "start" and not r.get("grew"):
                newest_start = max(newest_start, float(r.get("epoch") or 0.0))

        # 먼저 자격 있는 것만 걸러 **전체를 놓고** 정렬한다 — 오래된 것부터
        # 자르면(리뷰 결함) 8개 중 5개가 죄다 옛것이 되어 due() 가 최신 대신
        # 4번째로 최신인 세션을 돌려준다. 최신 N개를 골라야 한다.
        #
        # "이미 아는 세션"은 **id 로만** 거른다(known_sessions) — epoch 로
        # 거르지 않는다. session_meta.timestamp 는 초 단위라 같은 초에 시작한
        # 서로 다른 두 세션이 있을 수 있고, 그걸 epoch 로 판정했다면(#22,
        # 반개구간 <=) id 가 다른데도 하나가 죽는다. 그래서 진짜 새 것인지는
        # newest_start 와 **엄격히** 비교하고(<), 이미 원장에 있는지는 id 로
        # 따로 본다.
        eligible = []
        for ref in refs:
            if not ref.session_id or ref.session_id in known_sessions:
                continue
            if ref.epoch < newest_start:
                continue
            if now and (now - ref.epoch) > due.MAX_AGE_SECONDS:
                continue
            if _ref_repo_key(ref) != key:
                continue
            eligible.append(ref)
        eligible.sort(key=lambda r: r.epoch)
        selected = eligible[-BACKFILL_CAP:]

        # 고른 뒤에는 **오름차순으로 붙인다** — 원장의 append 순서가 시작
        # 순서와 일치해야 due() 가(원장을 거꾸로 읽어 "가장 최근"을 고른다)
        # 진짜 최신 세션을 돌려준다. 이미 최신 N개로 골랐으므로 여기서부터는
        # 시간 예산으로 중간에 끊지 않는다 — 끊으면 방금 고른 최신 세션이
        # 아니라 그보다 오래된 것만 남을 수 있다(리뷰 결함). 어차피 최대
        # BACKFILL_CAP 줄만 쓰므로 비용은 무시할 만하다.
        for ref in selected:
            row = {
                "repo": key,
                "harness": adapter_id,
                "session": ref.session_id,
                "event": "start",
                "epoch": ref.epoch,
                "path": ref.source_path,
                "cwd": root,
                # health() 가 이 값을 보고 "훅이 실제로 돌았다" 는 증거에서 뺀다
                # (codex_cli.py) — 백필이 신뢰 없는 훅을 가려버리면 안 된다.
                #
                # #22: 이 세션들은 실제 시작 시각이 이 mark 자신의 시작 행보다
                # 앞서더라도 원장에는 이 행 **뒤에** 붙는다(mark 는 자기 행부터
                # 적고 백필은 그다음이라). due() 는 하네스별로 원장을 훑으므로
                # (harness == my_harness 인 행은 건너뜀) 무해하다 — 영향은
                # 같은 하네스 내부의 append 순서뿐인데, 여기서 붙이는 건
                # 다른 하네스 행이다.
                "via": "scan",
                # `_reactivate_grown_sessions` 의 baseline — discover() 가 이미
                # os.path.getsize 로 읽은 값이라 추가 stat 비용이 없다.
                "size": ref.size,
            }
            if ledger.append(row, home=home):
                fresh.setdefault(adapter_id, set()).add(ref.session_id)

        if fresh.get(adapter_id):
            # 방금 이 하네스에 새 세션을 채웠다 — 다른 기존 세션들의
            # baseline 을 그 자리에서 바로 B 뒤로 옮긴다(리뷰 #1, 위
            # _rebaseline_after_fresh_start 참고).
            _rebaseline_after_fresh_start(adapter_id, key, root, home, own_rows,
                                          fresh[adapter_id], now, deadline)
    return fresh


# 늘어난 꼬리를 얼마나 읽을지의 상한. 실측(17.7MB 꼬리): 396.6ms — 캡 없이
# 읽으면 늘어난 크기에 그대로 비례해 훅 예산(150ms)을 넘긴다. 1MB 는 같은
# 실측 비율(~22.4us/KB)로 약 22ms — stop_at_human_turn 이 보통 훨씬 일찍
# 끊어 주므로 이 캡은 "사람 턴이 하나도 없는 큰 성장"의 최악 경우만 막는다.
REACTIVATE_TAIL_CAP = 1_000_000


def _reactivate_grown_sessions(harness: str, root: str, key: str, state: str,
                               home, now: float, deadline: float,
                               fresh: Dict[str, set]) -> None:
    """#22 마지막 구멍: 신뢰 안 된 Codex 훅에서 `codex exec resume` 은 새
    rollout 을 만들지 않고 **같은 파일에 이어 쓴다** — `session_meta` 도 다시
    안 쓴다. `_backfill_foreign_sessions` 는 discover() 의 첫 줄 시작 시각으로
    순서를 매기므로 이 재개를 못 본다(원본 시작 시각이 그대로다); 이미
    전달됐던 세션이면 `already_delivered` 가 due() 를 거기서 멈춘다.

    파일 크기 성장은 "뭔가 바뀌었다"만 알려준다(불변식 6: 순서의 근거가
    아니라 변화 감지 신호일 뿐이다 — 원장 append 순서가 여전히 유일한 순서
    기준이다). 성장분에 사람의 새 턴이 있는지는 `read_session_since` 로
    직접 확인한다(에이전트 혼잣말·turn_aborted 만으로는 재개로 보지 않는다).

    `fresh`(이번 mark 가 `_backfill_foreign_sessions` 로 방금 채운 세션들)에
    이 하네스가 있으면 건너뛴다 — 같은 호출에서 더 최신 세션이 이미 들어왔다면
    재개 행을 그 뒤에 또 얹지 않는다(원장 append 순서가 due() 의 "가장 최근"
    판정이므로, 얹으면 방금 채운 더 최신 세션 대신 재개된 낡은 세션이 이긴다).

    #28: 조건 (a) 에서 superseded 이면서 안 자란 세션도 매 mark 마다 개별
    seen 행을 받았다(신뢰된 훅 B 뒤에서 지연 재기준이 매번 돈다) — 그런
    세션들은 `_append_rebase_marker` 하나로 묶는다. 자란 세션(안 자란 것과
    갈리는 그 자리)은 여전히 자기 seen 행을 마커보다 먼저 받는다.

    리뷰(#28 1차): deadline 이 세션 중간에 끊거나(`break`), stat 이 일시적
    OSError 로 실패하거나, seen/grew 행이 `ledger.append` 상한에 걸려
    버려지면 이번 라운드는 "훑은 것 전부 확인" 이 아니다 — 확인 못 한
    세션에도 마커가 똑같이 적용되면 그 세션의 위치를 실제보다 앞당겨(마커
    뒤로) 잘못 민다. 그래서 `_rebaseline_after_fresh_start` 와 같은
    `complete` 규칙을 쓴다 — 완전할 때만 마커, 아니면 확인된 만큼만 개별
    seen.

    리뷰(#28 2차): `complete` 는 cap(`REACTIVATE_SCAN_CAP`) 초과와 무관하다
    — cap 은 대신 **읽는 쪽**(`marker_covers`, 아래)에서 다룬다. 마커는
    "이 하네스의 알려진 세션 전부" 가 아니라 "**그 마커를 쓸 당시 top-N**
    (같은 순위 함수로 뽑은)이 안 자란 채 확인됐다" 는 뜻이다 — 그래서
    어떤 세션의 baseline 위치에 마커를 반영해도 되는지는, 그 세션이 마커
    **작성 시점**의 top-N 에 있었는지로 판정해야 한다(`own_rows` 를 마커
    이전 구간으로 슬라이스해 같은 `_distinct_sessions_with_path` 로
    재구성 — writer 가 실제로 훑은 후보와 정확히 같은 집합이 나온다: 그
    구간 안에서 이미 top-N 안이었던 세션이 이 라운드에 자라 새 행을 얻어도
    같은 top-N **안에서** 순위만 바뀔 뿐 다른 세션을 밀어내지 않는다 —
    cap 밖에 있던 세션은 애초에 이 라운드에 후보가 아니었으므로 새 행을
    받을 수 없다).

    재현(리뷰 2차, "x-far"): cap 밖에 있던 세션이 B 전에 이미 자란 채(사람
    턴 포함) 이번 마커에 확인된 적이 없는데, 나중에 자기 훅으로 재진입하며
    `own_rows` 맨 뒤에 새 start 행을 얻으면 — 그 행은 "마커 **이후**" 구간에
    있으므로 마커 작성 시점 top-N 재구성(`own_rows[:last_marker_pos]`)에는
    안 잡힌다. `sid in marker_covers` 가 False 로 남아 baseline 위치가 그대로
    유지되고, superseded 판정이 여전히 정확하다 — due() 는 그 애매한 사전
    성장을 재개로 오판하지 않는다.

    훅 경로이므로 절대 던지지 않는다 — 호출자(cmd_mark)가 통째로 감싼다.
    """
    rows = ledger.read(repo_key=key, home=home)
    for adapter_id in sorted(adapters.REGISTRY):
        if adapter_id == harness:
            continue
        if time.time() > deadline:
            return
        if fresh.get(adapter_id):
            continue
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            continue
        reader = getattr(inst, "read_session_since", None)
        if reader is None:
            continue
        # 리뷰: 메서드가 있다는 것과 이 어댑터가 실제로 판정을 낸다는 것은
        # 다르다 — Claude 처럼 항상 None 을 돌려주는(계약상 "구분할 수
        # 없다") 구현이면 매 성장마다 의미 없는 seen 행만 쌓인다(측정: mark
        # 4번 → seen 4번). 어댑터당 **한 번만** 확인하고, None 이면 이
        # 어댑터는 통째로 건너뛴다 — 실제 경로를 stat 하기 전에 결정한다.
        try:
            probe = reader(
                SessionRef(adapter_id=adapter_id, session_id="", source_path="",
                          cwd=None, epoch=0.0, size=0),
                0,
            )
        except Exception:
            probe = None
        if probe is None:
            continue

        own_rows = [r for r in rows if r.get("harness") == adapter_id]
        if not own_rows:
            continue

        # 이 라운드에서 "안 자랐다" 로 확인될 세션들의 자리를 한 번에 미는
        # 마커의 위치(#28) — session/path 가 없어 위 known_sessions·newest_start
        # 류의 필터에 안 걸리지만, 여기서는 baseline **위치**(조건 a) 계산에
        # 쓴다.
        last_marker_pos = -1
        for i, row in enumerate(own_rows):
            if row.get("event") == REBASE_EVENT:
                last_marker_pos = i

        # #28 2차 리뷰: 이 마커가 실제로 덮는 세션 집합 — 마커를 **쓸
        # 당시**의 top-N 을, 그때와 같은 순위 함수로 own_rows 를 마커
        # 이전 구간(`[:last_marker_pos]`)만 잘라 재구성한다(위 함수
        # docstring 의 "x-far" 재현 참고). 마커가 없으면(아직 한 번도 안
        # 찍혔으면) 당연히 아무것도 안 덮는다.
        marker_covers = (
            set(_distinct_sessions_with_path(
                own_rows[:last_marker_pos])[:REACTIVATE_SCAN_CAP])
            if last_marker_pos >= 0 else set()
        )

        # 이 하네스의 최근 distinct 세션(최신 먼저), path 있는 것만 — no
        # discover(), no 날짜 창. 14일 전에 시작한 세션도 여전히 원장에
        # path 를 들고 있으면 재개를 잡는다.
        recent_sessions = _distinct_sessions_with_path(own_rows)[:REACTIVATE_SCAN_CAP]
        complete = True  # cap 은 더 이상 completeness 에 영향 없다(#28 2차).

        unchanged = []  # (sid, path, size) — 이 라운드에서 안 자란 것으로 확인됨
        for sid in recent_sessions:
            if time.time() > deadline:
                complete = False
                break
            path = None
            baseline = None
            baseline_pos = -1
            for i, row in enumerate(own_rows):
                if row.get("session") != sid:
                    continue
                if row.get("path"):
                    path = row.get("path")
                if "size" in row:
                    # 가비지 size 도 mark 를 깨면 안 된다 — 건너뛰고 이전
                    # baseline 을 유지한다.
                    try:
                        baseline = int(row["size"])
                        baseline_pos = i
                    except (TypeError, ValueError):
                        pass
            if not path:
                continue
            try:
                cur_size = os.stat(path).st_size
            except OSError:
                # 일시적 실패 — 이 세션의 "안 자람" 을 확인 못 했다(#28 리뷰).
                complete = False
                continue

            if baseline is None:
                # 첫 관측 — 재개 여부를 아직 모른다. 다음 mark 부터 비교할
                # 기준만 남긴다. os.stat 크기를 그대로 쓰지 않고 줄 경계로
                # 스냅한다(리뷰) — 레코드 중간을 baseline 으로 잡으면 그
                # 레코드가 마저 쓰인 뒤 skip-to-newline 로직이 통째로
                # 건너뛴다.
                if not ledger.append({
                    "repo": key, "harness": adapter_id, "session": sid,
                    "event": "seen", "via": "scan",
                    # 첫 관측이라 이전 baseline 이 없다. 0 으로 대체하지
                    # 않는다 — 다음 판정이 처음부터 읽어 원래의 사람 턴으로
                    # 옛 내용을 다시 넘긴다(리뷰에서 재현). 못 찾으면 size
                    # 그대로(64KB 넘는 레코드를 쓰는 중일 때만, 알려진 한계).
                    "size": fsio.line_aligned_size(path, cur_size),
                    "epoch": now, "path": path, "cwd": root,
                }, home=home):
                    complete = False
                continue

            # 조건 (a): baseline 이후 같은 하네스의 **다른** 세션 start 행이
            # 붙었다면(어떤 경로로 그 행이 생겼든 — 이 하네스의 백필이든,
            # 그 세션 자신의 신뢰된 훅이든) 이미 더 최신 것에 밀린 세션이다
            # — 되살리지 않는다. 리뷰(3차, t5): **자라지 않았어도** 이 검사를
            # 한다 — 안 그러면 baseline 이 B 의 start 행보다 앞에 영원히
            # 남아, B 가 신뢰된 훅으로 직접 들어와 `_rebaseline_after_fresh_start`
            # 가 못 본 경우 이 세션은 다시는 판정되지 않는다(그 훅이 원장에
            # 적는 순간엔 이 함수가 아예 안 불린다 — args.harness 가 그
            # 하네스 자신이라 `_reactivate_grown_sessions` 의 대상에서 원천
            # 빠진다).
            #
            # 그렇다고 그냥 넘어가면(옛 버그) 이 baseline 이 영원히 그대로
            # 남아 이후 어떤 mark 도 이 세션을 다시는 판정하지 못한다 —
            # seen 행으로 기준만 올려서 흡수한다. B 이후에 A 가 **다시**
            # 자라면(baseline 이 그 seen 행 뒤로 옮겨졌으므로 B 의 start 행
            # 보다 앞이 아니다) 그건 새 판정으로 다시 잡힌다.
            #
            # **알려진 한계:** B 의 start 행과 이 라운드 사이에 A 가 이미
            # 자랐다면(자라지 않은 경우와 달리) 그 성장이 B 전인지 후인지
            # 알 도리가 없다 — size 하나로는 순서를 못 가리므로 흡수한다
            # (README 의 남은 한계).
            #
            # #28: baseline **위치** 는 이 세션 자신의 마지막 size 행이거나,
            # (이 세션이 이전 라운드에 "안 자랐다"로 확인돼 마커로 흡수됐을
            # 수 있으므로) 그보다 나중일 수 있는 이 하네스의 마지막 rebase
            # 마커 — 둘 중 더 뒤엣것이다. 마커 뒤에는 이 세션의 진짜 위치가
            # 최소 거기까지 왔다는 뜻이라, 그 뒤에 생긴 성장을 애매함 없이
            # 바로 판정할 수 있다(마커 자신은 이 세션이 실제로 자랐는지는
            # 모른다 — 그래서 size 는 안 건드리고 위치 계산에만 쓴다).
            #
            # #28 2차: 단, 그 마커가 **이** 세션을 실제로 덮었을 때만
            # (`marker_covers`, 위) — 안 그러면 마커를 쓸 당시 cap 밖에
            # 있어 확인된 적 없는 세션("x-far")이 나중에 자기 훅으로
            # 재진입하는 것만으로 애매한 사전 성장이 명확한 재개로
            # 둔갑한다(리뷰 재현, 위 함수 docstring).
            effective_pos = (
                max(baseline_pos, last_marker_pos)
                if sid in marker_covers else baseline_pos
            )
            superseded = any(
                row.get("event") == "start" and row.get("session") != sid
                for row in own_rows[effective_pos + 1:]
            )
            if superseded:
                if cur_size > baseline:
                    if not ledger.append({
                        "repo": key, "harness": adapter_id, "session": sid,
                        "event": "seen", "via": "scan",
                        # fallback=이전 baseline(리뷰 3차 #3) — 못 찾아도
                        # baseline 이 뒤로(레코드 중간 쪽) 밀리지 않는다.
                        "size": fsio.line_aligned_size(path, cur_size,
                                                       fallback=baseline),
                        "epoch": now, "path": path, "cwd": root,
                    }, home=home):
                        complete = False
                else:
                    # 안 자랐다 — complete 로 밝혀지면 개별 seen 대신 이
                    # 라운드가 끝날 때 한 번의 마커로 흡수한다(#28).
                    unchanged.append((sid, path, baseline))
                continue
            if cur_size <= baseline:
                continue

            since = None
            try:
                since = reader(
                    SessionRef(
                        adapter_id=adapter_id, session_id=sid,
                        source_path=path, cwd=root, epoch=now, size=cur_size,
                    ),
                    baseline,
                    max_bytes=REACTIVATE_TAIL_CAP,
                    stop_at_human_turn=True,
                )
            except Exception:
                since = None
            if since is None:
                # 이 라운드는 판정할 수 없다 — baseline 을 건드리지 않고
                # 다음 mark 에서 다시 시도한다(잘못된 baseline 을 남기는
                # 것보다 안전하다).
                continue
            found_human_turn = any(
                ev.verb == "said" and ev.author == "human" for ev in since.events
            )

            if found_human_turn:
                # 리뷰: `cur_size` 가 아니라 `since.end_offset` 을 쓴다 — 마지막
                # 줄이 개행 없이 끝났으면(막 쓰는 중이었을 수 있다) 그 레코드는
                # 아직 안전히 다 읽은 게 아니라서 baseline 에 넣으면 다음 읽기가
                # 그 줄을 통째로 건너뛴다(리뷰 #2).
                if not ledger.append({
                    "repo": key, "harness": adapter_id, "session": sid,
                    "event": "start", "via": "scan", "size": since.end_offset,
                    "grew": 1, "epoch": now, "path": path, "cwd": root,
                }, home=home):
                    complete = False
                try:
                    if due.already_delivered(state, sid, harness):
                        due.mark_reopened(state, sid, adapter_id, now)
                except Exception:
                    pass
                continue

            # 성장은 있었지만 사람의 턴이 아니다(에이전트 혼잣말,
            # turn_aborted, task_complete 등) — 리뷰: `since.end_offset` 을
            # 쓴다(`cur_size` 가 아니라). 캡(`max_bytes`)에 걸려 꼬리 전부를
            # 못 읽었으면 `end_offset` 이 실제로 읽은 데까지만 반영하므로,
            # 다음 mark 가 못 읽은 나머지를 이어서 본다 — `cur_size` 를 그대로
            # 썼다면 그 사이에 있었을 수도 있는 사람 턴을 영영 건너뛴다.
            if not ledger.append({
                "repo": key, "harness": adapter_id, "session": sid,
                "event": "seen", "via": "scan", "size": since.end_offset,
                "epoch": now, "path": path, "cwd": root,
            }, home=home):
                complete = False

        if unchanged:
            if complete:
                _append_rebase_marker(adapter_id, key, home, now)
            else:
                for sid, path, size in unchanged:
                    ledger.append({
                        "repo": key, "harness": adapter_id, "session": sid,
                        "event": "seen", "via": "scan", "size": size,
                        "epoch": now, "path": path, "cwd": root,
                    }, home=home)


def cmd_mark(args, *, home=None, out=sys.stdout) -> int:
    """세션 시작을 원장에 남긴다. 훅이 부른다. 약 220바이트 한 줄."""
    raw = args.stdin if args.stdin is not None else _stdin_text()
    payload = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            payload = {}
    start = str(payload.get("cwd") or "") or None
    root, key, state = _state_for(home, start)
    if locate.refused_root(root):
        # 훅 경로다 — 원장에 아무것도 남기지 않고 조용히 나간다(invariant 2).
        return 0
    session = gate.session_id_from_hook_payload(raw) or ""
    row = {
        "repo": key,
        "harness": args.harness,
        "session": session,
        "event": args.event,
        "epoch": round(time.time(), 0),
        "path": str(payload.get("transcript_path") or ""),
        "cwd": root,
    }
    # 사람이 대화한 세션인지는 여기서 판정하지 않는다(#21). SessionStart 시점에는
    # Claude 트랜스크립트가 아직 쓰이지 않아 판정이 늘 fail-open 했고, Codex 는
    # rollout 이 없으면 영구히 비대화형으로 적힐 수 있었다. 판정은 brief 시점에
    # 어댑터가 실제 파일을 보고 내린다(brief.compute 가 due 에 넘기는 eligible).
    ledger.append(row, home=home)
    # SessionStart 의 `source` 어휘는 Claude Code 와 Codex 가 공유한다(둘 다
    # 실측). "resume" 은 같은 세션에 새 턴이 이어붙었다는 뜻이다 — 그 세션이
    # 이미 다른 하네스에 전달됐었다면 due() 가 already_delivered() 에서 멈춰
    # resumed 턴을 영영 못 내보낸다(#22). "compact" 는 같은 신호를 주지 않는다
    # — 컨텍스트만 압축했을 뿐 사람의 새 턴이 없으므로 재전달할 것이 없다.
    # 이 reopen 은 여전히 "다시 열렸을 수 있다"는 힌트일 뿐이다 — 빈 프롬프트
    # resume 은 source:"resume" 을 내면서도 새 사람 턴을 안 남기고, mark/brief
    # 동시 실행이면 이 reopen 이 brief 가 방금 내보낸 턴 뒤에 붙을 수도 있다
    # (#27). 그래도 지운다고 브리지를 고치는 게 아니다 — due() 가 이 세션을
    # 다시 후보로 보게 하는 유일한 신호가 이것이기 때문이다. 실제로 새로운지는
    # brief.compute 가 delivered.tsv 의 offset(5번째 열)과 이 세션의 사람 said
    # 이벤트를 비교해 판정한다 — mark 는 "후보로 볼까"만 결정하고, brief 는
    # "보낼 게 있나"를 결정한다. 둘의 책임이 다르다.
    if session and str(payload.get("source") or "") == "resume":
        try:
            due.mark_reopened(state, session, args.harness, row["epoch"])
        except Exception:
            pass
    # 다른 하네스의 세션을 원장에 백필한다(위 주석). 훅 경로이므로 실패해도
    # mark 자체는 항상 exit 0, 빈 stdout 이어야 한다(invariant 2). 두 백필
    # 단계(신규 세션 스캔, 재개 감지)가 같은 훅 예산을 나눠 쓴다 — 따로 재면
    # 합쳐 두 배를 쓴다.
    try:
        if not due.is_off(state):
            deadline = time.time() + BACKFILL_TIME_BUDGET
            fresh = _backfill_foreign_sessions(args.harness, root, key, state, home,
                                               row["epoch"], deadline=deadline)
            _reactivate_grown_sessions(args.harness, root, key, state, home,
                                       row["epoch"], deadline, fresh)
    except Exception:
        pass
    # 어떤 omhc 호출에서든 오래된 AGENTS.md 구간을 붕괴시킨다.
    try:
        agents_md.collapse(root)
    except Exception:
        pass
    if args.verbose:
        out.write("marked {} {} in {}\n".format(args.harness, args.event, key))
    return 0


# --- note -------------------------------------------------------------------


def cmd_note(args, *, home=None, out=sys.stdout, err=None) -> int:
    err = err or sys.stderr
    root, _key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        err.write("{}\n".format(reason))
        return 2
    os.makedirs(state, exist_ok=True)
    path = os.path.join(state, NOTES_NAME)
    text = " ".join(args.text).strip()
    if not text:
        out.write("nothing to note\n")
        return 0
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = [line for line in fh if line.strip()]
    out.write("noted ({} notes, {}B)\n".format(len(lines), os.path.getsize(path)))
    return 0


# --- pull rate ---------------------------------------------------------------


def _record_pull(key: str, state: str, via: str, home, session: Optional[str] = None,
                 tag: Optional[str] = None) -> None:
    """`show`/`log` 로 산출물을 인출했다는 행을 원장에 남긴다. §9 인출률 회계.

    `harness` 키를 절대 넣지 않는다 — due() 는 event=="start" 만 보고,
    backfill 은 harness+start 로 own_rows 를 거르고, codex health() 도
    event=="start" 만 "훅이 돌았다"는 증거로 센다. 이 세 곳 중 어디에도
    pull 행이 섞여 들면 안 된다. 실패는 show/log 의 결과에 영향을 주면
    안 되므로 통째로 삼킨다(훅 경로는 아니지만 fail-open 을 유지한다).

    show 는 실제로 읽은 세션을 넘긴다. `#N` 은 옛 세션의 색인에 떨어질 수 있어
    "가장 최근 전달" 로 두면 보지 않은 세션의 인출률이 오른다. 대상이 하나로
    정해지지 않는 log 만 가장 최근 전달 세션(delivered.tsv 마지막 줄)에 돌린다."""
    try:
        session = session or due.last_delivered(state)
        if not session:
            return
        row = {"repo": key, "event": "pull", "via": via, "session": session,
               "epoch": round(time.time(), 0)}
        if tag:
            row["tag"] = tag
        ledger.append(row, home=home)
    except Exception:
        pass


# --- log --------------------------------------------------------------------


def _index_files(state: str) -> List[str]:
    directory = os.path.join(state, "index")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names if n.endswith(".idx")]


def _all_session_ids(state: str) -> List[str]:
    return [os.path.basename(p)[: -len(".idx")] for p in _index_files(state)]


def _unique_prefix_len(ids: List[str], minlen: int = 8) -> int:
    """이 id 들을 서로 구분하는 가장 짧은 접두 길이(>=minlen). Codex 의 UUIDv7 은
    앞 8자가 ~65초마다만 바뀌어 고정폭 8자 자르기로는 같은 레포에서 짧게 연달아
    시작한 세션들이 자주 충돌한다(#15c). 13자쯤이 보기 좋은 상한이지만 그건
    표시상 취향일 뿐이다 — 그 안에서 안 갈리면 유일해질 때까지 계속 늘린다.
    안 그러면 log 가 찍은 ref 를 show 가 "모호하다"며 거부하면서, 정작 후보
    목록에는 똑같은 문자열이 두 번 찍히는 리뷰 결함이 생긴다."""
    uniq = list(dict.fromkeys(ids))
    n = minlen
    longest = max((len(i) for i in uniq), default=minlen)
    while n < longest and len({i[:n] for i in uniq}) < len(uniq):
        n += 1
    return n


def _session_log_rank(key: str, state: str, home):
    """`log` 의 세션 정렬 근거를 주는 랭크 함수.

    최우선은 delivered.tsv 등장 순(due.delivered_order — last_delivered() 와
    같은 소스라 log 의 끝이 `show '#N'` 의 기본 세션과 일치한다). 원장 첫
    `start` 행 등장 순은 **못 쓴다** — mark 가 제 세션을 먼저 적고 나서
    backfill 이 더 일찍 시작한 외래 세션을 뒤늦게 적으므로(cmd_mark), 마킹된
    세션과 백필된 세션의 쌍마다 원장 등장 순이 실제 전달 순과 뒤집힌다(#18
    리뷰 결함). 전달된 적 없는 세션(watch 로만 색인된 경우)은 원장 첫 start
    행 순서로, 그것도 없으면 색인 파일명 순으로 결정적으로 둔다."""
    delivered_rank = {sid: i for i, sid in enumerate(due.delivered_order(state))}
    ledger_rank = {}
    for i, row in enumerate(ledger.read(home=home, limit=0, repo_key=key)):
        if row.get("event") != "start":
            continue
        session = row.get("session")
        if session and session not in ledger_rank:
            ledger_rank[session] = i
    # 색인 파일명(=세션 id) 오름차순 — 위 두 근거가 다 없는 세션끼리도 흔들리지
    # 않는 순서가 필요하다(_index_files 가 이미 그렇게 정렬해서 준다).
    fallback_rank = {sid: n for n, sid in enumerate(_all_session_ids(state))}

    def _rank(session):
        if session in delivered_rank:
            return (0, delivered_rank[session])
        if session in ledger_rank:
            return (1, ledger_rank[session])
        return (2, fallback_rank.get(session, 0))

    return _rank


def cmd_log(args, *, home=None, out=sys.stdout) -> int:
    root, key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        out.write("{}\n".format(reason))
        return 1
    _record_pull(key, state, "log", home)
    _session_rank = _session_log_rank(key, state, home)

    rows = []
    for path in _index_files(state):
        session = os.path.basename(path)[: -len(".idx")]
        for row in index.rows(path):
            rows.append((session, row))
    # 세션은 _session_log_rank 순, 세션 안에서는 색인 seq 순 — 타임스탬프는
    # 순서의 근거로 쓰지 않는다(불변식 6, #18).
    rows.sort(key=lambda pair: (_session_rank(pair[0]), pair[1].seq))

    if args.verb:
        rows = [r for r in rows if r[1].verb == args.verb]
    if args.grep:
        needle = args.grep.lower()
        rows = [r for r in rows if needle in r[1].arg.lower()]
    if args.file:
        rows = [r for r in rows if any(args.file in p for p in r[1].paths)]
    # falsy-zero 검사를 쓰면 `--last 0` 이 전부를 쏟는다 — F7 이 막으려던 바로 그
    # 무한 출력이다. 음수도 앞에서 자르는 엉뚱한 동작이 된다.
    if args.last is not None and args.last >= 0:
        rows = rows[len(rows) - args.last :] if args.last else []

    # 이 배치가 아니라 상태 디렉터리 전체에서 유일하게 만든다 — 안 그러면
    # 필터링으로 짧아진 접두사가 화면 밖의 다른 세션과 겹칠 수 있고, 그 ref 를
    # `show` 에 그대로 넘기면 모호해진다(#10).
    n = _unique_prefix_len(_all_session_ids(state)) if rows else 8

    for session, row in rows:
        ref = "{}#{}".format(session[:n], row.seq)
        content = row.arg or ",".join(row.paths)
        if not content and row.verb == "said":
            # index 는 본문을 담지 않는다(아카이브 이중화 방지) — 빈 줄 대신
            # 힌트를 보여준다(#15a).
            content = "(text: omhc show {})".format(ref)
        out.write(
            "{} {} {} {}\n".format(
                ref,
                row.verb,
                "ok" if row.ok else "FAIL",
                content,
            )
        )
    if not rows:
        out.write("no events (run a session in another harness first)\n")
    return 0


# --- show -------------------------------------------------------------------


def _pinned_path(state: str, session_id: str, fallback: str) -> str:
    pinned = os.path.join(pin.pinned_dir(state, session_id), "source.jsonl")
    return pinned if os.path.exists(pinned) else fallback


_SEQ_REF_RE = re.compile(r"^([^#]*)#(\d+)$")


def _default_log_session(state: str) -> Optional[str]:
    """pull 회계가 `log` 를 돌리는 세션 — 가장 최근 전달된 세션(due.last_delivered,
    §9). `log` 자체엔 "기본 세션" 이 없다(색인 전부를 나열한다); `#N` 만 받았을 때
    그 회계 규칙을 그대로 재사용해 하나로 좁힌다(#10)."""
    return due.last_delivered(state)


def _resolve_seq_ref(state: str, prefix: str, seq: int):
    """`#N` 또는 `<prefix>#N` 을 (entry, note) 로 푼다. note 는 자동으로 고른
    세션을 사람에게 알려줄 문구, 없으면 None. 못 풀면 (None, error message)."""
    ids = _all_session_ids(state)
    note = None
    if prefix:
        matches = [sid for sid in ids if sid.startswith(prefix)]
        if not matches:
            return None, "unknown session prefix {!r}".format(prefix)
        if len(matches) > 1:
            # 접두사로 줄이면 그 자체가 다시 모호해질 수 있다(리뷰 결함) — 후보는
            # 항상 전체 id 로 보여준다.
            candidates = ", ".join(sorted(matches))
            return None, "ambiguous session prefix {!r}; candidates: {}".format(
                prefix, candidates)
        session = matches[0]
    else:
        session = _default_log_session(state)
        if session is None:
            return None, ("no default session yet (nothing delivered here) — "
                          "use `<session-prefix>#{}` or `omhc log --last 30`".format(seq))
        if session not in ids:
            return None, ("most recently delivered session {} has no index yet — "
                          "use `<session-prefix>#{}` or `omhc log --last 30`".format(
                              session, seq))
        n = _unique_prefix_len(ids)
        note = "{} -> {}#{} (most recently delivered session)".format(
            "#{}".format(seq), session[:n], seq)

    path = os.path.join(state, "index", session + ".idx")
    row = index.find(path, seq)
    if row is None:
        return None, "no event #{} in session {}".format(seq, session)
    entry = {"session_id": session, "source_path": "",
             "offset": row.offset, "length": row.length, "seq": seq}
    return entry, note


def cmd_show(args, *, home=None, out=sys.stdout, err=None) -> int:
    err = err or sys.stderr
    _root, key, state = _state_for(home)
    target = args.target.strip()
    refs = index.read_refs(state)

    entry = refs.get(target) or refs.get(target.upper())
    note = None
    if entry is None:
        m = _SEQ_REF_RE.match(target)
        if m:
            entry, err_or_note = _resolve_seq_ref(state, m.group(1), int(m.group(2)))
            if entry is None:
                err.write("{}\n".format(err_or_note))
                return 1
            note = err_or_note
    if entry is None:
        err.write("unknown reference {!r}; try `omhc log --last 30`\n".format(target))
        return 1

    if note:
        # stdout 은 원본 바이트 그대로여야 한다(`omhc show '#3' --full | jq .` 가
        # 깨지면 안 된다) — 자동으로 고른 세션을 알리는 메모는 stderr 로만 보낸다.
        err.write("# {}\n".format(note))

    source = _pinned_path(state, entry["session_id"], entry.get("source_path") or "")
    if not source or not os.path.exists(source):
        err.write("source bytes are gone for {} (session {})\n".format(
            target, entry["session_id"]))
        return 1
    with open(source, "rb") as fh:
        fh.seek(entry["offset"])
        raw = fh.read(entry["length"] if not args.full else -1)
    buf = getattr(out, "buffer", None)
    if buf is not None:
        # 진짜 stdout — 원본 바이트를 그대로 쓴다(`show '#3' --full | jq .` 가
        # 깨지면 안 된다). 없는 줄바꿈을 붙이지 않는다: 그 자체가 원본 바이트다.
        buf.write(raw)
    else:
        # 테스트의 io.StringIO 처럼 .buffer 가 없는 스트림 — 텍스트로만 비교할
        # 수 있으므로 디코드하고, 사람이 읽기 좋게 줄바꿈을 보정한다.
        out.write(raw.decode("utf-8", "replace"))
        if not raw.endswith(b"\n"):
            out.write("\n")
    _record_pull(key, state, "show", home, session=entry["session_id"], tag=target)
    return 0


# --- status -----------------------------------------------------------------


# 세 값만 쓴다: True(PASS, 게이팅), False(FAIL, 게이팅), None(`----`, 게이팅
# 안 함). SKIP 이 아니다 — "아직 아무 일도 안 일어났다"를 실패로도 성공으로도
# 위장하지 않고 그대로 보여주려는 세 번째 라벨이다(#8).
def _verdict_word(verdict: Optional[bool]) -> str:
    if verdict is True:
        return "PASS"
    if verdict is False:
        return "FAIL"
    return "----"


def _check(out, label: str, verdict: Optional[bool], detail: str) -> None:
    out.write("{:<4} {:<22} {}\n".format(_verdict_word(verdict), label, detail))


def _status_json_empty() -> dict:
    """`status --json` 의 최상위 키 전부를 빈 값으로. `/` 처럼 진단을 못 내는
    경로가 쓴다. 정상 경로에 키를 더하면 여기에도 더해야 한다 —
    test_status 가 두 경로의 키 집합이 같은지 확인한다(#19 리뷰: #25 가 더한
    키가 `/` 에서만 빠졌다)."""
    return {
        "repo_root": None, "repo_key": None, "state_dir": None,
        "adapters": [], "ledger_rows": 0, "ledger_rejects": 0,
        "archive": [],
        "injections": 0, "pulls": 0,
        "pull_rate_window": PULL_RATE_WINDOW,
        "recent_injections": 0, "recent_pulls": 0,
        "off": False, "watcher_pid": None,
        "instruction_files": {"shared": None, "stale_block": False},
        "health": [],
        "rows": [],
    }


def cmd_status(args, *, home=None, out=sys.stdout) -> int:
    root, key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        # `/` 에서의 status 는 오사용이다 — SKIP 이 아니라 게이팅되는 FAIL 로
        # 보여준다. state 가 이미 있다면(예전에 잘못 돈 흔적) 고아라고 알린다.
        # 정상 경로의 나머지 진단(ledger.read, adapters.present, health, ...)은
        # 전부 `root` 에 걸려 있어 `/` 에서 의미가 없다 — text/json 이 여기서만
        # 갈라지는 최소한의 필드(refused, orphaned_state)를 덧붙인다.
        orphaned = state if os.path.isdir(state) else None
        if args.json:
            # 정상 경로와 같은 최상위 키 집합을 유지한다 — 빈 값이라도 있어야
            # 소비자가 `/` 에서만 KeyError 로 죽지 않는다(#19). 값 자체는 의미가
            # 없다(정상 경로의 진단은 전부 `root` 에 걸려 있어 여기선 못 낸다).
            payload = _status_json_empty()
            payload.update({
                "repo_root": root, "repo_key": key, "state_dir": state,
                "refused": reason, "orphaned_state": orphaned,
                "rows": [{"label": "root", "verdict": "fail", "detail": reason}],
            })
            out.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        else:
            _check(out, "root", False, reason)
            if orphaned:
                out.write("orphaned state dir: {}\n".format(orphaned))
        return 1
    installed = adapters.present(now=time.time)
    # 원장은 아래 health_rows 를 위해 어차피 무제한으로 한 번 더 읽어야 한다
    # (전역 세션 id 때문에 레포로 못 거름) — 10만 행에서 파싱만 약 0.5초라
    # 두 번 읽으면 배가된다(#25). 한 번 무제한으로 읽고, 이 레포용 rows 는
    # read(repo_key=key) 가 하던 것과 같은 규칙(필터 먼저, limit 은 나중에)을
    # 메모리에서 재현한다 — 안 그러면 여러 레포를 오가는 사람에게서 이 레포의
    # 행이 다른 레포 행들에 밀려 슬라이스 밖으로 나간다.
    all_rows = ledger.read(home=home, limit=0)
    rows = [r for r in all_rows if r.get("repo") == key][-ledger.DEFAULT_LIMIT:]
    artifact = os.path.join(state, ARTIFACT_NAME)

    # #22: append() 가 상한을 못 맞춰 조용히 버린 행. "최근" 만 게이팅한다 —
    # 예전에 한 번 있었지만 그 뒤로 반복되지 않았다면 사람이 영원히 못 지우는
    # FAIL 을 보게 하면 안 된다(off switch/archive 행과 같은 원칙,
    # due.MAX_AGE_SECONDS 를 재사용한다). `bytes > ledger.MAX_LINE` 도 함께
    # 본다 — 상한을 올려 고친 뒤라면(#22 리뷰) 예전에 적힌 행이 지금 상한으로는
    # 이미 들어가므로 "고쳤다" 라는 사실을 cap 을 따로 저장하지 않고도 안다.
    # `session` 으로 distinct 해서 센다 — `_note_rejection` 이 재시도마다 같은
    # (repo, harness, session) 을 또 적지 않게 막지만, 그 방어가 생기기 전에
    # 이미 쌓인 중복 줄까지 한 세션을 여러 번 버려진 것처럼 부풀리면 안 된다.
    rejected_now = time.time()
    recent_rejected = [
        r for r in ledger.read_rejected(home=home, repo_key=key)
        if (rejected_now - float(r.get("epoch") or 0.0)) <= due.MAX_AGE_SECONDS
        and int(r.get("bytes") or 0) > ledger.MAX_LINE
    ]
    rejected_sessions = {
        (r.get("harness"), r.get("session")) if r.get("session")
        else (r.get("harness"), i)
        for i, r in enumerate(recent_rejected)
    }

    # watch.lag 가 정확히 이 계산을 소유한다. 두 벌로 두면 고정 레이아웃이
    # 바뀔 때 한쪽만 고쳐진다.
    lag_rows = watch.lag(state)

    # X = 최근 PULL_RATE_WINDOW 번 전달 중 최소 한 번 인출된 세션 수(중복
    # 제거), N = 그 창의 전달 횟수(§9 "pulled X of N injections"). injections
    # 는 별도로 delivered.tsv 전체 줄 수(archive 행 판정용)를 유지한다.
    pull_sessions = {r.get("session") for r in rows
                      if r.get("event") == "pull" and r.get("session")}
    delivered = os.path.join(state, due.DELIVERED_NAME)
    # append 순서 그대로 모은다 — delivered.tsv 의 epoch 필드는 타임스탬프라
    # 거꾸로 갈 수 있으므로(불변식 6) 정렬 기준이 아니라 줄 순서 자체를 쓴다.
    delivered_order: List[str] = []
    if os.path.exists(delivered):
        with open(delivered, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                parts = line.split("\t")
                # reopen 줄은 전달이 아니다 — 세면 resume 만 하고 아직 다시
                # 전달되지 않은 세션이 injections/pull rate 분모에 낀다(#22).
                if len(parts) >= 2 and parts[1] == due.REOPEN_MARKER:
                    continue
                delivered_order.append(parts[0])
    injections = len(delivered_order)

    # pull rate 의 분모를 delivered.tsv 전체로 두면, 한 레포를 오래 쓸수록
    # 분모만 무한정 자라 인출률이 서서히 낮아 보인다(#25) — 오래된 전달의
    # pull 행은 이미 원장 창(rows, DEFAULT_LIMIT) 밖으로 밀려났는데 분모는
    # 안 줄기 때문이다. 그래서 "최근 N번 전달 중 몇 번 인출됐는가"로 분모
    # 자체를 최근 N 개로 묶는다. 세션 id 로 맞춘다 — pull 행이 session 필드를
    # 이미 들고 있어(위 pull_sessions) 위치 기반 근사가 필요 없다.
    recent_window = delivered_order[-PULL_RATE_WINDOW:]
    recent_injections = len(recent_window)
    recent_sessions = {s for s in recent_window if s}
    recent_pulls = len(recent_sessions & pull_sessions)
    # JSON 의 `pulls` 는 예전 뜻(전체 전달 중 인출된 세션 수)을 유지한다 —
    # `pulls / injections` 로 비율을 내는 소비자가 창 도입으로 조용히 틀리지
    # 않게, 창 안의 값은 `recent_pulls` / `recent_injections` 짝으로 따로 낸다.
    pulls = len({s for s in delivered_order if s} & pull_sessions)

    # AGENTS.md 가 CLAUDE.md 와 공유되면 Codex Path B 는 절대 쓰면 안 된다 —
    # 그 파일을 공유 배선 만들기 *전에* 심어 둔 낡은 관리 구간만 실패 사유다.
    shared = agents_md.shared_with_claude(root)
    leaked = bool(shared) and managed_block.installed_captured_at(
        agents_md.path_for(root)) is not None

    # 선택적 어댑터 진단(예: codex 신뢰 안 된 훅). 세션 id 는 전역 유일이므로
    # 레포로 거르지 않은 all_rows(위에서 이미 무제한으로 읽어 둔 것)를 그대로
    # 넘긴다 — 위 rows 처럼 이 레포로 미리 거르면 자기 .git 을 가진 중첩
    # 워크트리·서브모듈에서 시작한 세션이 다른 repo 키로 기록돼 여기서 영원히
    # "안 돈 것"으로 보인다. limit(기본 2000, 머신 전체 공유)이 14일 창을 못
    # 덮을 수 있다는 게 알려진 한계다 — 개인용 도구고 status 는 훅 경로가
    # 아니므로 필요하면 여기서만 무제한으로 읽는다. 한 어댑터가 죽어도 나머지
    # status 가 죽으면 안 되므로 어댑터별로 감싼다.
    health_rows = []
    for adapter_id in installed:
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            # 어댑터 생성 자체가 안 되면 health 를 판정할 근거가 없다.
            continue
        try:
            health_rows.extend(getattr(inst, "health", lambda *a: ())(root, all_rows))
        except Exception:
            pass

    # hooks 행은 `installed`(detect() 로 감지된 것)보다 넓다 — curl 설치
    # 직후, 하네스가 한 번도 안 돌아 detect() 가 보는 세션 디렉터리가 아직
    # 없어도 설정 디렉터리(~/.claude, ~/.codex)는 있을 수 있고, 그 경우도
    # "설치됐는지" 는 여전히 보여줘야 한다(hook_config_targets, #7 리뷰 1).
    # health 와 독립이다 — 한쪽이 죽어도 다른 쪽 행은 여전히 나와야 한다
    # (리뷰 결함: 예전엔 health 의 예외가 hooks 판정 자체를 건너뛰었다).
    hook_rows = []
    for adapter_id in hook_config_targets(home):
        try:
            inst = adapters.get(adapter_id, home=home)
            hc = getattr(inst, "hook_config", lambda: None)()
            if hc is not None:
                fragment = hookconf.load_fragment(hc.fragment_name)
                ok, detail = hookconf.inspect(hc.config_path, fragment, inst.home)
                hook_rows.append(("{} hooks".format(adapter_id), ok, detail))
        except Exception as exc:
            # 조용히 버리지 않는다 — 판정이 죽었다는 사실 자체가 FAIL 행이다
            # (예: hooks/ 디렉터리가 없어 load_fragment 가 실패한 경우).
            hook_rows.append(("{} hooks".format(adapter_id), False,
                              "cannot check hooks ({})".format(exc)))
    watcher = watch.read_lock(state)

    # 행을 한 번만 만들고 텍스트·JSON 이 같은 목록을 렌더한다 — 따로 만들면
    # 한쪽만 고쳐질 수 있다(#8, status --json 이 항상 exit 0 이던 결함).
    checks = []
    checks.append(("adapters", bool(installed), ", ".join(installed) or "none found"))
    checks.append(("ledger", None,
                   "{} rows for this repo".format(len(rows)) if rows
                   else "no sessions recorded here yet — start either harness in this repo"))
    if recent_rejected:
        checks.append(("ledger rejects", False,
                       "{} session(s) dropped (too long for MAX_LINE={}) since {}"
                       " — fixed it? run `omhc clear` to drop this repo's record".format(
                           len(rejected_sessions), ledger.MAX_LINE,
                           time.strftime("%Y-%m-%d", time.localtime(
                               min(float(r.get("epoch") or 0.0) for r in recent_rejected))))))
    else:
        checks.append(("ledger rejects", None, "none recently"))

    # lag_rows 의 size/lag_bytes 는 pinned/<sid>/source.jsonl 이 없어도 0 으로
    # 나온다 — "size 0" 과 "고정 성공, 꼬리 0바이트" 를 구분 못 하면 고정이
    # 실패한 세션도 archive PASS 로 보인다(리뷰 결함). watch.lag 의 `pinned` 로
    # 실제 존재 여부를 본다.
    pinned_rows = [r for r in lag_rows if r.get("pinned")]
    unpinned_rows = [r for r in lag_rows if not r.get("pinned")]
    # 고정폭 8자는 Codex UUIDv7 앞 8자가 ~65초마다만 바뀌어 자주 충돌한다
    # (#15c) — 이 레포에 지금 보이는 id 들 사이에서만 유일하면 된다.
    archive_n = _unique_prefix_len([r["session"] for r in lag_rows]) if lag_rows else 8
    if pinned_rows:
        archive_verdict = True
        archive_detail = "; ".join(
            "{} tail={}B".format(r["session"][:archive_n], r["lag_bytes"])
            for r in pinned_rows)
        if unpinned_rows:
            archive_detail += "; unpinned: " + ", ".join(
                r["session"][:archive_n] for r in unpinned_rows)
    elif injections:
        # 핀은 brief.compute 가 전달 *후에만* 만든다(brief.py) — 원장 행은
        # 있지만 아직 한 번도 전달받지 못한 사람에게 archive 를 영영 FAIL 로
        # 두면 안 되지만, 전달은 됐는데(injections>0) 핀이 하나도 없다면
        # 진짜 결함이다.
        archive_verdict = False
        archive_detail = "{} injections but nothing pinned".format(injections)
        log_path = os.path.join(locate.omhc_root(home), brief.GUARD_LOG)
        if os.path.exists(log_path):
            archive_detail += "; details may be in {}".format(log_path)
    else:
        archive_verdict = None
        archive_detail = "nothing handed off to this repo yet"
    checks.append(("archive", archive_verdict, archive_detail))

    off_reason = due.off_reason(state)
    checks.append(("off switch", None,
                   "on" if off_reason is None else "off ({})".format(off_reason)))

    if leaked:
        checks.append(("instruction files", False,
                       "{}; stale omhc block in AGENTS.md would leak into Claude — "
                       "run `omhc clear`".format(shared)))
    elif shared:
        checks.append(("instruction files", True,
                       "{} -> Codex Path B disabled, falls to .omhc/outbox".format(shared)))
    else:
        checks.append(("instruction files", True,
                       "AGENTS.md not shared with CLAUDE.md"
                       if os.path.exists(agents_md.path_for(root))
                       else "no AGENTS.md"))

    for label, health_ok, detail in health_rows:
        checks.append((label, health_ok, detail))
    for label, hook_ok, detail in hook_rows:
        checks.append((label, hook_ok, detail))

    checks.append(("pull rate", None,
                   "pulled {} of {} recent injections (window {})".format(
                       recent_pulls, recent_injections, PULL_RATE_WINDOW)))
    checks.append(("watcher (optional)", None,
                   "running pid {}".format(watcher) if watcher
                   else "not running — brief falls back to inline parsing"))

    code = 1 if any(verdict is False for _label, verdict, _detail in checks) else 0

    if args.json:
        def _verdict_json(verdict: Optional[bool]) -> Optional[str]:
            if verdict is True:
                return "pass"
            if verdict is False:
                return "fail"
            return None

        out.write(json.dumps({
            "repo_root": root, "repo_key": key, "state_dir": state,
            "adapters": installed, "ledger_rows": len(rows),
            "ledger_rejects": len(rejected_sessions),
            "archive": lag_rows,
            "injections": injections, "pulls": pulls,
            "pull_rate_window": PULL_RATE_WINDOW,
            "recent_injections": recent_injections, "recent_pulls": recent_pulls,
            "off": due.is_off(state), "watcher_pid": watcher,
            "instruction_files": {"shared": shared, "stale_block": leaked},
            "health": [{"label": label, "ok": ok, "detail": detail}
                       for label, ok, detail in health_rows],
            "rows": [{"label": label, "verdict": _verdict_json(verdict), "detail": detail}
                     for label, verdict, detail in checks],
        }, ensure_ascii=False, indent=2) + "\n")
        return code

    out.write("repo   {}\nkey    {}\nstate  {}\n\n".format(root, key, state))
    for label, verdict, detail in checks:
        _check(out, label, verdict, detail)
    verbs = {}
    for path in _index_files(state):
        for row in index.rows(path):
            verbs[row.verb] = verbs.get(row.verb, 0) + 1
    if verbs:
        out.write("\nevents  {}\n".format(
            " ".join("{}={}".format(k, verbs[k]) for k in sorted(verbs))))
    out.write("artifact {}\n".format(
        "{}B".format(os.path.getsize(artifact)) if os.path.exists(artifact)
        else "none"))
    return code


# --- brief ------------------------------------------------------------------


def cmd_brief(args, *, home=None, out=sys.stdout) -> int:
    # --dry-run 은 사람이 손으로 확인하려고 부르는 경로다(훅은 --dry-run 을
    # 절대 넘기지 않는다). 문서화된 쓰임 하나가 `echo '{"cwd": R}' | omhc brief
    # --dry-run` 처럼 다른 cwd 에서 payload 를 파이프로 넘기는 것이라, 아예 안
    # 읽으면 그 쓰임이 깨진다(리뷰). 그렇다고 `_stdin_text()` 를 그대로 쓰면
    # 파이프의 다른 쪽 끝이 열려만 있고 아직 아무것도 안 쓴 채면(TTY 가 아니라
    # isatty() 는 False) EOF 를 기다리며 멈춘다(#27). `_dry_run_stdin_text()`
    # 는 select 로 "지금 읽을 게 있는가"만 먼저 물어 그 사이를 가른다. 실제
    # 훅 경로(dry_run=False)는 오늘과 똑같이 그대로 읽는다.
    if args.dry_run and args.stdin is None:
        stdin_text = _dry_run_stdin_text()
    else:
        stdin_text = args.stdin if args.stdin is not None else _stdin_text()
    return brief.emit(
        harness=args.harness,
        stdin_text=stdin_text,
        budget=args.budget,
        wire=args.wire,
        force=args.force,
        as_text=args.text or args.dry_run,
        dry_run=args.dry_run,
        home=home,
        out=out,
    )


# --- hooks --------------------------------------------------------------


def _adapters_with_hook_config(home) -> List[str]:
    """`hook_config()` 를 구현한(=SessionStart 훅 개념이 있는) 등록된 어댑터
    id 전부. 감지 여부와 무관하다 — `--harness` 없이 아무 대상도 못 찾았을 때
    "이 중에서 골라라" 로 보여줄 목록이다."""
    ids = []
    for adapter_id in sorted(adapters.REGISTRY):
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            continue
        if getattr(inst, "hook_config", lambda: None)() is not None:
            ids.append(adapter_id)
    return ids


def hook_config_targets(home) -> List[str]:
    """`hook_config()` 가 있고, 이 머신에서 그 하네스가 감지됐거나(`detect()`)
    설정 디렉터리가 이미 있는 어댑터 id 들. `omhc status` 의 `<adapter-id>
    hooks` 행과 `omhc hooks install` 의 기본 대상이 이 규칙을 공유한다.

    curl 설치 직후, 어느 하네스도 아직 한 번도 안 돈 시점에는 `detect()` 가
    보는 세션 디렉터리(`~/.claude/projects`, `~/.codex/sessions`)가 없다 —
    하지만 하네스 자신의 설정 디렉터리(`~/.claude`, `~/.codex`)는 그 하네스를
    한 번이라도 실행했거나 사람이 미리 만들어 뒀다면 존재할 수 있다. 이 규칙이
    없으면 첫 사용자에게 `hooks install` 이 "찾은 게 없다"며 조용히 아무 일도
    안 하고, `status` 도 훅 행 자체를 안 보여준다(#7 리뷰 1)."""
    ids = []
    for adapter_id in sorted(adapters.REGISTRY):
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            continue
        hc = getattr(inst, "hook_config", lambda: None)()
        if hc is None:
            continue
        try:
            detected = inst.detect().present
        except Exception:
            detected = False
        if detected or os.path.isdir(os.path.dirname(hc.config_path)):
            ids.append(adapter_id)
    return ids


def cmd_hooks(args, *, home=None, out=sys.stdout, err=None) -> int:
    """`omhc hooks install|uninstall`. status 의 `<adapter-id> hooks` 행이
    가리키는 그 설치를 실제로 한다. 코어는 벤더 이름을 모른다 — 대상은
    `hook_config()` 를 구현한, 이 머신에 감지됐거나 설정 디렉터리가 있는
    어댑터들이다."""
    if not getattr(args, "hooks_action", None):
        # argparse 관례: 동작 없이 부르면 사용법은 stderr, exit 2(#19).
        (err or sys.stderr).write(
            "usage: omhc hooks install|uninstall [--harness ID]\n")
        return 2

    targets = [args.harness] if args.harness else hook_config_targets(home)
    if not targets:
        known = _adapters_with_hook_config(home)
        out.write("no harness found -- run with --harness <id> ({})\n".format(
            ", ".join(known) if known else "no adapter declares a hook config"))
        return 1

    had_error = False
    for adapter_id in targets:
        try:
            inst = adapters.get(adapter_id, home=home)
        except AdapterUnavailable as exc:
            out.write("{}\n".format(exc))
            had_error = True
            continue

        hc = getattr(inst, "hook_config", lambda: None)()
        if hc is None:
            if args.harness:
                out.write("{}: no hook config for this harness\n".format(adapter_id))
            continue

        try:
            if args.hooks_action == "install":
                fragment = hookconf.load_fragment(hc.fragment_name)
                had_backup = os.path.exists(hc.config_path)
                changed = hookconf.merge(hc.config_path, fragment, inst.home)
                if changed:
                    out.write("{}: installed -> {}\n".format(adapter_id, hc.config_path))
                    if had_backup:
                        out.write("{}: backup {}\n".format(
                            adapter_id, hc.config_path + ".omhc-bak"))
                    if hc.post_write_note:
                        out.write("{}: {}\n".format(adapter_id, hc.post_write_note))
                else:
                    out.write("{}: already up to date\n".format(adapter_id))
                ok, detail = hookconf.inspect(hc.config_path, fragment, inst.home)
                out.write("{}: {} -- {}\n".format(
                    adapter_id, "PASS" if ok else "FAIL", detail))
                if not ok:
                    # 파일은 이미 (다시) 쓰였다 — 그런데도 재검사가 FAIL 이면
                    # (예: 바이너리를 아직 못 찾음) 사람이 고쳐야 할 문제가
                    # 남아 있다는 뜻이므로 exit code 로도 알린다(#7 리뷰 2).
                    had_error = True
            else:
                changed = hookconf.strip(hc.config_path)
                if changed:
                    out.write("{}: removed from {}\n".format(adapter_id, hc.config_path))
                    out.write("{}: backup {}\n".format(
                        adapter_id, hc.config_path + ".omhc-bak"))
                else:
                    out.write("{}: nothing to remove\n".format(adapter_id))
        except hookconf.HookConfigError as exc:
            out.write("{}: {}\n".format(adapter_id, exc))
            had_error = True
        except Exception as exc:  # 트레이스백은 절대 안 보여준다 — 훅 경로는 아니지만 이 명령도 사람용이다.
            out.write("{}: unexpected error ({})\n".format(adapter_id, exc))
            had_error = True

    return 1 if had_error else 0


# --- clear ------------------------------------------------------------------


def cmd_clear(args, *, home=None, out=sys.stdout) -> int:
    root, key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        out.write("{}\n".format(reason))
        return 1
    removed = []
    if agents_md.collapse(root, force=True):
        removed.append(agents_md.path_for(root))
    artifact = os.path.join(state, ARTIFACT_NAME)
    if os.path.exists(artifact):
        os.unlink(artifact)
        removed.append(artifact)
    # #22 리뷰: 원인을 고친(예: MAX_LINE 을 올린) 뒤에도 `ledger rejects` 가
    # 영원히 FAIL 로 남으면 안 된다 — 이 레포의 거부 기록만 지운다.
    rejected_cleared = ledger.clear_rejected(key, home=home)
    if rejected_cleared:
        removed.append("{} ledger reject row(s)".format(rejected_cleared))
    out.write("cleared {}\n".format(", ".join(removed) if removed else "nothing"))
    return 0


# --- watch ------------------------------------------------------------------


def cmd_watch(args, *, home=None, out=sys.stdout) -> int:
    """가속기 데몬. 정확성을 담당하지 않으므로 죽어도 결과가 바뀌지 않는다."""
    root, _key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        out.write("{}\n".format(reason))
        return 1
    if args.stop:
        pid = watch.read_lock(state)
        if pid is None:
            out.write("no watcher running\n")
            return 0
        import signal as _signal

        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError as exc:
            out.write("could not stop {}: {}\n".format(pid, exc))
            return 1
        out.write("stopped {}\n".format(pid))
        return 0
    if args.once:
        written = watch.sweep(root, state, home=home)
        out.write("indexed {} new events\n".format(written))
        return 0
    try:
        return watch.run(root, home=home, poll=args.poll,
                         idle_exit=args.idle_exit)
    except watch.LockBusy:
        out.write("watcher already running (pid {})\n".format(
            watch.read_lock(state)))
        return 1


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="하네스 간 작업 이어가기")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("brief", help="훅 경로: 전달할 표식을 stdout 으로")
    p.add_argument("--harness", required=True)
    p.add_argument("--budget", type=int, default=brief.mint.BUDGET)
    p.add_argument("--wire", default="", choices=("", "claude", "cursor", "sdk"),
                   help="주입 JSON 형식. 기본값은 --harness 에서 유도한다")
    p.add_argument("--force", action="store_true")
    p.add_argument("--text", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="본문만 텍스트로 보이고 게이트·아카이브·전달을 건드리지 않는다")
    p.add_argument("--stdin", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("mark", help="세션 시작을 원장에 기록")
    p.add_argument("--harness", required=True)
    p.add_argument("--event", default="start", choices=("start", "end"))
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--stdin", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("show", help="표식의 태그로 원본 바이트를 조회")
    p.add_argument("target", help="E1 같은 표식 태그, 또는 `omhc log` 가 출력한 "
                   "<session>#N / #N 참조")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("log", help="색인된 이벤트를 한 줄씩")
    p.add_argument("--last", type=int, default=30)
    p.add_argument("--grep", default="")
    p.add_argument("--verb", default="")
    p.add_argument("--file", default="")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("note", help="메모를 남긴다 (에이전트도 부를 수 있다)")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("status", help="유일한 사람용 대시보드")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="가속기 데몬 (선택. 없어도 결과는 같다)")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--once", action="store_true", help="한 번만 훑고 끝낸다")
    p.add_argument("--poll", type=float, default=watch.POLL_SECONDS)
    p.add_argument("--idle-exit", type=float, default=watch.IDLE_EXIT_SECONDS,
                   dest="idle_exit")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("clear", help="설치된 표식을 제거")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("hooks", help="omhc 자신의 SessionStart 훅을 설치/제거")
    p.set_defaults(func=cmd_hooks, hooks_action=None)
    hooks_sub = p.add_subparsers(dest="hooks_action")
    p_install = hooks_sub.add_parser("install", help="감지된 하네스에 훅을 병합")
    p_install.add_argument("--harness", default=None)
    p_install.set_defaults(func=cmd_hooks)
    p_uninstall = hooks_sub.add_parser("uninstall", help="omhc 자신의 훅만 제거")
    p_uninstall.add_argument("--harness", default=None)
    p_uninstall.set_defaults(func=cmd_hooks)

    return parser


def main(argv=None, *, home=None, out=None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "func", None):
        parser.print_help(out or sys.stdout)
        return 0
    stream = out or sys.stdout
    # --harness 를 **여기서** 해소한다. 자유 문자열로 흘려보내면 오타가 세 깊이에서
    # 서로 다르게 조용히 열화된다 — 와이어 표는 기본값으로 떨어지고, 같은 벤더
    # 단축이 매칭을 멈추고, adapters.get 은 brief 의 bare except 안에서 터져
    # 아무것도 출력하지 않는다. 경계에서 한 번 실패하는 것이 낫다.
    harness = getattr(args, "harness", None)
    if harness:
        try:
            adapters.get(harness, home=home)
        except AdapterUnavailable as exc:
            stream.write("{}\n".format(exc))
            return 1
    try:
        return args.func(args, home=home, out=stream)
    except AdapterUnavailable as exc:
        stream.write("{}\n".format(exc))
        return 1
    except BrokenPipeError:
        return 0
