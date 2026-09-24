from __future__ import annotations

import json
import os
import time
from typing import List, Optional

from . import fsio, locate

LEDGER_NAME = "ledger.jsonl"
# append() 가 상한에 맞출 수 없어 행을 버릴 때 그 사실만 남기는 곳(#22). 원본
# 레코드는 담지 않는다 — 잘리면 의미가 없는 신원 필드를 또 잘라 담아 봐야 소용
# 없다. `omhc status` 의 `ledger rejects` 행이 이 파일을 읽는다.
REJECTED_NAME = "ledger.rejected"

# read() 의 기본 limit. 호출자가 repo 로 거른 결과를 직접 슬라이스할 때도
# (cli.cmd_status 처럼 한 번만 읽고 메모리에서 거를 때) 같은 값을 참조해야
# read(repo_key=...) 를 두 번 부르는 것과 동일한 결과가 나온다.
DEFAULT_LIMIT = 2000

# 이 상한은 원자성과 무관하다(#22 리뷰: 예전 주석이 틀렸다) — PIPE_BUF 는
# 파이프 전용이고(macOS 는 512), 일반 파일에 O_APPEND 로 여는 한 write(2) 를
# 한 번만 부르면(fsio.append_line 이 그렇게 한다) 크기와 무관하게 "파일 끝으로
# 이동 + 쓰기" 가 원자적이라고 POSIX 가 보장한다.
#
# 진짜 이유는 실측 경로 길이다: 홈 경로가 길면(사용자명이 길거나 회사
# 표준 홈이 깊으면, 실측 ~80~100자) Claude 의 `~/.claude/projects/<cwd 를
# 대시로 이어붙인 슬러그>/<uuid>.jsonl` 경로가 400바이트를 훌쩍 넘겨 cwd 를
# 잘라도 못 맞춘다 — backfill 행(Codex scan)이 전부 조용히 버려지고 status 에도
# 안 드러났다(#22). 800은 그런 홈에서도 여유 있게 맞고(실측: 홈 150자·레포명
# 40자에서도 트림 후 약 625바이트), 그래도 안 맞으면 `_note_rejection` 이 남긴다.
MAX_LINE = 800

# 상한 초과 시 잘라낼 키. **순서가 중요하다.**
#
# path 와 session 은 신원 필드다 — 자르면 문법상 유효하지만 아무것도 가리키지
# 않는 줄이 되어, 핸드오프가 조용히 실패하고 guard.log 에도 남지 않는다.
# 그래서 장식용 필드(cwd, repo)를 먼저 줄이고, 그래도 안 되면 줄 자체를 버린다.
_TRIMMABLE = ("cwd",)


def _path(home: Optional[str]) -> str:
    return os.path.join(locate.omhc_root(home), LEDGER_NAME)


def _encode(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def _rejected_path(home: Optional[str]) -> str:
    return os.path.join(locate.omhc_root(home), REJECTED_NAME)


def _note_rejection(record: dict, size: int, home: Optional[str]) -> None:
    """행을 버렸다는 사실만 싸게 남긴다(#22) — 훅 경로(mark)에서도 불릴 수
    있으므로 절대 던지지 않는다(invariant 2). 실패해도 append() 의 반환값은
    이미 False 라 호출자는 어차피 안다; 이건 나중에 사람이 볼 수 있게 하는
    부가 기록일 뿐이다.

    #22 리뷰: 원장에 못 들어간 행은 `known_sessions`(cli._backfill_foreign_sessions)
    에도 안 잡혀 mark 마다 계속 재시도된다 — session 이 있으면 같은
    (repo, harness, session) 을 이미 적었는지 먼저 보고, 있으면 다시 적지
    않는다. 그래야 한 세션이 "3번 버려짐" 으로 부풀지 않고 파일도 무한히
    자라지 않는다. session 이 없는 행(옛 포맷·비정상 레코드)은 구분할 근거가
    없어 매번 적는다 — 드문 경우고, 있어도 status 는 아래에서 distinct 로 센다.
    """
    session = record.get("session")
    try:
        if session:
            repo = record.get("repo")
            harness = record.get("harness")
            for row in read_rejected(home=home, repo_key=repo):
                if row.get("harness") == harness and row.get("session") == session:
                    return
        row = {"repo": record.get("repo"), "harness": record.get("harness"),
              "session": session, "event": record.get("event"),
              "epoch": round(time.time(), 0), "bytes": size}
        fsio.append_line(_rejected_path(home), _encode(row))
    except OSError:
        pass


def append(record: dict, home: Optional[str] = None) -> bool:
    """한 줄을 O_APPEND 단일 write(2) 로 쓴다.

    상한에 맞출 수 없으면 **쓰지 않고 False 를 돌려준다.** 신원 필드를 잘라
    아무것도 가리키지 않는 줄을 남기는 것보다 정직하다 — 잘린 path 는 조용한
    실패를 만들고, 바이트 단위로 자른 JSON 은 read() 가 건너뛰므로 어차피
    사라진다. 그 대신 거부 자체는 `_note_rejection` 으로 남겨 `omhc status` 가
    보여줄 수 있게 한다(#22) — 예전엔 반환값을 호출자가 버려서 조용히
    사라졌다.
    """
    line = _encode(record)
    size = len(line.encode("utf-8")) + 1
    if size > MAX_LINE:
        trimmed = dict(record)
        for key in _TRIMMABLE:
            value = trimmed.get(key)
            if isinstance(value, str) and len(value) > 24:
                trimmed[key] = value[:24] + "…"
            line = _encode(trimmed)
            size = len(line.encode("utf-8")) + 1
            if size <= MAX_LINE:
                break
        if size > MAX_LINE:
            _note_rejection(record, size, home)
            return False
    fsio.append_line(_path(home), line)
    return True


def read_rejected(home: Optional[str] = None, repo_key: Optional[str] = None,
                  limit: int = DEFAULT_LIMIT) -> List[dict]:
    """`_note_rejection` 이 남긴 행. `read()` 와 같은 규칙(repo 필터가 limit 보다
    먼저)을 쓴다 — 이유도 같다(여러 레포를 오가면 옛 레포의 거부가 이 레포의
    거부를 창 밖으로 밀 수 있다)."""
    try:
        with open(_rejected_path(home), encoding="utf-8", errors="replace") as fh:
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


def clear_rejected(repo_key: str, home: Optional[str] = None) -> int:
    """이 레포의 거부 기록만 지운다(다른 레포 줄은 그대로). `omhc clear` 가
    부른다 — 상한을 올리거나 원인을 고친 뒤에도 `ledger rejects` 가 영원히
    FAIL 로 남으면 안 된다(#22 리뷰). 훅 경로가 아니므로 던져도 된다."""
    path = _rejected_path(home)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0
    kept: List[str] = []
    removed = 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if isinstance(row, dict) and row.get("repo") == repo_key:
            removed += 1
            continue
        kept.append(line)
    if removed:
        text = "".join(l + "\n" for l in kept)
        fsio.replace_preserving(path, text)  # append_line 이 만든 0600 을 유지한다
    return removed


def read(limit: int = DEFAULT_LIMIT, home: Optional[str] = None,
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
