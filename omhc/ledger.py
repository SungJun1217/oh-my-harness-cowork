from __future__ import annotations

import json
import os
from typing import List, Optional

LEDGER_NAME = "ledger.jsonl"

# PIPE_BUF(4096) 이하의 단일 write(2) 는 O_APPEND 에서 원자적이다. 400바이트
# 상한이 그 보장의 근거이며, 여러 세션이 동시에 써도 부분 레코드가 생기지 않는다.
MAX_LINE = 400

# 상한 초과 시 잘라낼 키. **순서가 중요하다.**
#
# path 와 session 은 신원 필드다 — 자르면 문법상 유효하지만 아무것도 가리키지
# 않는 줄이 되어, 핸드오프가 조용히 실패하고 guard.log 에도 남지 않는다.
# 그래서 장식용 필드(cwd, repo)를 먼저 줄이고, 그래도 안 되면 줄 자체를 버린다.
_TRIMMABLE = ("cwd",)


def _path(home: Optional[str]) -> str:
    return os.path.join(home or os.path.expanduser("~"), ".omhc", LEDGER_NAME)


def _encode(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def append(record: dict, home: Optional[str] = None) -> bool:
    """한 줄을 O_APPEND 단일 write(2) 로 쓴다.

    상한에 맞출 수 없으면 **쓰지 않고 False 를 돌려준다.** 신원 필드를 잘라
    아무것도 가리키지 않는 줄을 남기는 것보다 정직하다 — 잘린 path 는 조용한
    실패를 만들고, 바이트 단위로 자른 JSON 은 read() 가 건너뛰므로 어차피
    사라진다.
    """
    line = _encode(record)
    if len(line.encode("utf-8")) + 1 > MAX_LINE:
        trimmed = dict(record)
        for key in _TRIMMABLE:
            value = trimmed.get(key)
            if isinstance(value, str) and len(value) > 24:
                trimmed[key] = value[:24] + "…"
            line = _encode(trimmed)
            if len(line.encode("utf-8")) + 1 <= MAX_LINE:
                break
        if len(line.encode("utf-8")) + 1 > MAX_LINE:
            return False
    path = _path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return True


def read(limit: int = 2000, home: Optional[str] = None,
         repo_key: Optional[str] = None) -> List[dict]:
    """원장을 읽는다. 깨진 줄은 건너뛴다(fail-open).

    **repo 필터를 limit 보다 먼저 적용한다.** 원장은 머신 전체가 공유하는 한
    파일이므로, 먼저 마지막 limit 줄로 자르면 레포 20개를 오가는 사람에게서
    이 레포의 start 줄이 창 밖으로 밀려나 핸드오프가 조용히 멈춘다.
    """
    try:
        with open(_path(home), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    rows: List[dict] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if repo_key is not None and row.get("repo") != repo_key:
            continue
        rows.append(row)
    return rows[-limit:] if limit else rows


def newest(
    repo_key: str,
    harness: Optional[str] = None,
    exclude_harness: Optional[str] = None,
    event: Optional[str] = "start",
    home: Optional[str] = None,
) -> Optional[dict]:
    """조건에 맞는 가장 최근 줄.

    순서는 파일 내 위치이며 트랜스크립트 타임스탬프가 아니다 — 이 머신의 최대
    트랜스크립트에는 타임스탬프 역행 지점이 254개(최대 52ms) 있다.
    """
    for row in reversed(read(home=home, repo_key=repo_key)):
        if event and row.get("event") != event:
            continue
        if harness and row.get("harness") != harness:
            continue
        if exclude_harness and row.get("harness") == exclude_harness:
            continue
        return row
    return None
