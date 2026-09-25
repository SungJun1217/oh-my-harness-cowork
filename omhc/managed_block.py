from __future__ import annotations

import os
import re
from typing import Optional

from . import fsio

MARKER_ID = "omhc"
BEGIN_PREFIX = "<!-- {}:begin".format(MARKER_ID)
END = "<!-- {}:end -->".format(MARKER_ID)

# 24시간. 지나면 붕괴시킨다 — 어제의 표식이 오늘의 지시처럼 읽히면 안 된다.
STALE_AFTER_SECONDS = 24 * 3600

_BLOCK = re.compile(
    re.escape(BEGIN_PREFIX) + r'\s+captured="(?P<captured>[0-9.]+)"\s*-->'
    r".*?" + re.escape(END) + r"\n?",
    re.S,
)


def _begin(captured_at: float) -> str:
    return '{} captured="{:.0f}" -->'.format(BEGIN_PREFIX, captured_at)


def _block_text(body: str, captured_at: float) -> str:
    return "{}\n{}\n{}\n".format(_begin(captured_at), _neutralize(body).rstrip("\n"), END)


def _neutralize(body: str) -> str:
    """본문이 마커를 담으면 구조가 깨진다. 무해하게 바꿔 둔다."""
    return body.replace(END, END.replace("<!--", "<!_-")).replace(
        BEGIN_PREFIX, BEGIN_PREFIX.replace("<!--", "<!_-")
    )


def _write_target(path: str) -> str:
    """실제로 덮어쓸 경로. `path` 가 심볼릭 링크면 그 대상을 돌려준다.

    os.replace 는 목적지가 심볼릭 링크여도 디렉터리 엔트리(링크 자체)를 바꿔치기
    한다 — 링크가 가리키던 실제 파일은 그대로 두고 링크만 평범한 파일로
    대체돼 버린다. AGENTS.md 가 CLAUDE.md 와 공유하려고 일부러 둔 심링크라면
    이 함수가 없으면 collapse() 가 공유 배선을 끊는다.
    """
    try:
        if os.path.islink(path):
            return os.path.realpath(path)
    except OSError:
        pass
    return path


def _has_multiple_links(path: str) -> bool:
    try:
        return os.stat(path).st_nlink > 1
    except OSError:
        return False


def _write_shared(target: str, content: str) -> None:
    """하드링크가 걸린 파일을 그 자리에서 덮어쓴다 (inode 를 바꾸지 않는다).

    fsio.write_atomic 은 tmp + os.replace 라 디렉터리 엔트리만 새 inode 로
    바꿔치기한다 — CLAUDE.md 와 AGENTS.md 가 하드링크로 같은 inode 를 공유할 때
    이걸 쓰면 이 이름만 새 inode(수정된 내용)를 보고 다른 이름은 옛 inode(블록이
    남은 원본)를 계속 본다. r+ 로 열어 같은 inode 에 직접 써야 두 이름이
    갈라지지 않는다.
    """
    with open(target, "r+", encoding="utf-8", newline="") as fh:
        fh.write(content)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())


def _write(target: str, content: str) -> None:
    if _has_multiple_links(target):
        _write_shared(target, content)
    else:
        fsio.write_atomic(target, content)


def _without_block(existing: str) -> str:
    """`existing` 에서 omhc 구간만 제거한 "순수 사용자 콘텐츠"를 돌려준다.
    블록이 없으면 그대로.

    구간 앞뒤에 구분용 빈 줄을 끼워 넣지 않는다(아래 splice) — 리뷰 결함:
    이전 버전은 그 빈 줄을 되돌리려고 위치(맨 앞/맨 끝)로 앞/뒤 중 어느
    쪽이 "진짜 사용자 콘텐츠"인지 추측했는데, 사용자가 구간 위에 줄을
    더하거나(맨 앞 배치에서 `before` 가 비지 않게 됨) 예전 배치의 구간
    뒤에 콘텐츠가 더 있으면 그 추측이 틀려 한쪽을 통째로 버렸다(#33 리뷰).
    구분용 빈 줄이 애초에 없으면 이 모호함 자체가 없다 — 앞뒤를 있는
    그대로 이어붙이기만 하면 사용자 바이트를 한 번도 잃지 않는다."""
    return _BLOCK.sub("", existing, count=1)


def splice(path: str, body: str, *, captured_at: float, file_header: str = "") -> None:
    """마커 구간을 멱등하게 교체하고, 파일 맨 앞으로 옮긴다. 원자적으로 쓴다.

    Codex 는 AGENTS.md 를 `project_doc_max_bytes`(기본 32768바이트) 만큼만
    머리부터 읽는다(#33 실측) — 구간을 파일 끝에 붙이면 큰 AGENTS.md 에서
    통째로 잘려 보이지 않는다. 그래서 구간을 맨 앞에 두고, 예전에 끝에
    심어졌던 구간도 다음 splice 에서 앞으로 옮긴다. 구간과 나머지 콘텐츠
    사이에 구분용 빈 줄을 넣지 않는다 — 블록 문자열 자체가 이미 개행으로
    끝나 형태는 안 깨지고, `_without_block` 이 그 빈 줄을 되돌릴 필요가
    아예 없어진다(리뷰 결함 회피, 위 `_without_block` 참고). tmp + fsync +
    os.replace 를 쓴다 — 사람이 편집 중인 파일을 반쯤 쓴 상태로 남기면 안
    된다. 다만 대상이 하드링크로 공유된 파일이면 `_write` 가 그 자리 수정으로
    대신한다(`_write_shared` 참고).
    """
    block = _block_text(body, captured_at)

    existing = ""
    created = True
    try:
        # newline="" — 사람이 CRLF 로 쓴 AGENTS.md 를 텍스트 모드 기본값(보편
        # 개행 번역)으로 읽으면 \r 이 사라져 strip() 이 원본과 다른 바이트를
        # 돌려준다. 여기서 안 건드리고 그대로 들고 있다가 그대로 되돌려 쓴다.
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            existing = fh.read()
        created = False
    except OSError:
        existing = ""

    rest = _without_block(existing)
    prefix = file_header if created and file_header else ""
    updated = "{}{}{}".format(prefix, block, rest)

    _write(_write_target(path), updated)


def prospective_block_end_bytes(path: str, body: str, *, captured_at: float,
                                 file_header: str = "") -> int:
    """`splice(path, body, ...)` 를 실제로 실행하면 구간이 끝나는 지점의
    UTF-8 바이트 오프셋. 구간이 항상 파일 맨 앞이므로(위 splice) 이는 곧
    `len((prefix + block).encode("utf-8"))` — 기존 파일 내용(`rest`) 크기와
    무관하다. 쓰기 전에 예산(Codex 의 `project_doc_max_bytes`)을 넘는지 미리
    가늠하는 용도라 실제로 쓰지 않는다."""
    block = _block_text(body, captured_at)
    created = not os.path.exists(path)
    prefix = file_header if created and file_header else ""
    return len((prefix + block).encode("utf-8"))


def strip(path: str) -> bool:
    """마커 구간을 제거한다. 그것만 있던 파일이면 파일을 지운다.

    `path` 가 심볼릭 링크거나 하드링크로 공유된 파일이면 이름/inode 를 살려두고
    내용만 비운다 — 링크를 지우거나 바꿔치기하면 공유 배선(예: CLAUDE.md ->
    AGENTS.md)이 끊긴다. 이 파일이 omhc 만의 것이면 비어 있는 실제 파일이
    남는 편이, 공유 파일을 없애거나 갈라 버리는 것보다 덜 놀랍다.
    """
    try:
        is_link = os.path.islink(path)
    except OSError:
        is_link = False
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            existing = fh.read()
    except OSError:
        return False
    if not _BLOCK.search(existing):
        return False
    rest = _without_block(existing)
    target = _write_target(path)
    shared = is_link or _has_multiple_links(target)
    if not rest.strip():
        if shared:
            try:
                _write(target, "")
            except OSError:
                return False
            return True
        try:
            os.unlink(path)
        except OSError:
            return False
        return True
    _write(target, rest)
    return True


def installed_block_end_bytes(path: str) -> Optional[int]:
    """설치된 구간이 끝나는 지점의 UTF-8 바이트 오프셋. 구간이 없으면 None.

    Codex 의 `project_doc_max_bytes` 예산(#33)과 비교하는 용도 — 지금 구간이
    어디 있든(정상은 맨 앞, 다음 splice 전까지는 예전 배치도 남아 있을 수
    있다) 실측 오프셋을 그대로 낸다."""
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    except OSError:
        return None
    m = _BLOCK.search(text)
    if not m:
        return None
    return len(text[: m.end()].encode("utf-8"))


def installed_captured_at(path: str) -> Optional[float]:
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            existing = fh.read()
    except OSError:
        return None
    m = _BLOCK.search(existing)
    if not m:
        return None
    try:
        return float(m.group("captured"))
    except (TypeError, ValueError):
        return None


def is_stale(path: str, *, now: float, ttl: float = STALE_AFTER_SECONDS) -> bool:
    captured = installed_captured_at(path)
    if captured is None:
        return False
    return (now - captured) > ttl
