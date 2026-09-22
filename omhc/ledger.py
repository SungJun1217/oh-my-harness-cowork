from __future__ import annotations

import json
import os
from typing import List, Optional

LEDGER_NAME = "ledger.jsonl"

# PIPE_BUF(4096) 이하의 단일 write(2) 는 O_APPEND 에서 원자적이다. 400바이트
# 상한이 그 보장의 근거이며, 여러 세션이 동시에 써도 부분 레코드가 생기지 않는다.
MAX_LINE = 400

# 값이 길어질 수 있는 키. 상한 초과 시 이 순서로 잘라낸다.
_TRIMMABLE = ("path", "cwd", "repo", "session")


def _path(home: Optional[str]) -> str:
    return os.path.join(home or os.path.expanduser("~"), ".omhc", LEDGER_NAME)


def _encode(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def append(record: dict, home: Optional[str] = None) -> None:
    """한 줄을 O_APPEND 단일 write(2) 로 쓴다. 상한을 넘으면 값을 잘라 맞춘다."""
    line = _encode(record)
    if len(line.encode("utf-8")) + 1 > MAX_LINE:
        trimmed = dict(record)
        for key in _TRIMMABLE:
            value = trimmed.get(key)
            if isinstance(value, str) and len(value) > 40:
                trimmed[key] = value[:40]
            line = _encode(trimmed)
            if len(line.encode("utf-8")) + 1 <= MAX_LINE:
                break
        raw = line.encode("utf-8")
        if len(raw) + 1 > MAX_LINE:
            # 마지막 수단. 잘린 줄은 read() 가 건너뛰므로 원장이 죽지는 않는다.
            line = raw[: MAX_LINE - 1].decode("utf-8", "ignore")
    path = _path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def read(limit: int = 2000, home: Optional[str] = None) -> List[dict]:
    """마지막 limit 줄을 파싱한다. 깨진 줄은 건너뛴다(fail-open)."""
    try:
        with open(_path(home), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    rows: List[dict] = []
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


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
    for row in reversed(read(home=home)):
        if row.get("repo") != repo_key:
            continue
        if event and row.get("event") != event:
            continue
        if harness and row.get("harness") != harness:
            continue
        if exclude_harness and row.get("harness") == exclude_harness:
            continue
        return row
    return None
