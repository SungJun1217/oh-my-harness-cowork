from __future__ import annotations

import os
from typing import Optional

# PIPE_BUF(4096) 이하의 단일 write(2) 는 O_APPEND 에서 원자적이다. 줄 단위로
# 덧붙이는 모든 파일(원장, delivered.tsv, 색인)이 이 보장에 의존한다.
PIPE_BUF_SAFE = 4096


def _ensure_parent(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def write_atomic(path: str, text: str, *, fsync: bool = True,
                 suffix: str = ".tmp") -> None:
    """tmp 에 쓰고 fsync 한 뒤 os.replace 로 갈아끼운다.

    사람이 편집 중인 파일(AGENTS.md)이나 훅이 읽어갈 파일(omhc.txt)을 반쯤 쓴
    상태로 남기면 안 된다. 이 규칙이 여섯 곳에 손으로 복제돼 있었고 그중 두 곳은
    fsync 가 빠져 있었다 — 한 곳에 모아 그 차이를 없앤다.
    """
    _ensure_parent(path)
    tmp = path + suffix
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        if fsync:
            os.fsync(fh.fileno())
    os.replace(tmp, path)


def append_line(path: str, line: str, *, mode: int = 0o600) -> None:
    """한 줄을 O_APPEND 단일 write(2) 로 덧붙인다.

    여러 세션이 동시에 써도 부분 레코드가 생기지 않는다 — 단 한 줄이
    PIPE_BUF 이하일 때만 성립하므로 호출자가 길이를 책임진다.
    """
    _ensure_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def append_blob(path: str, blob: str, *, mode: int = 0o600) -> None:
    """여러 줄을 한 번에 덧붙인다. 색인처럼 배치로 쓰는 경우."""
    _ensure_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        os.write(fd, blob.encode("utf-8"))
    finally:
        os.close(fd)


def read_text(path: str, default: str = "") -> str:
    """읽기 실패를 예외가 아니라 기본값으로 돌려준다. 훅 경로용."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def size_of(path: str, default: int = 0) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return default


def unlink_quiet(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def claim_exclusive(path: str, contents: str = "", *, mode: int = 0o600) -> bool:
    """O_CREAT|O_EXCL 선점. 같은 파일시스템 안에서 원자적이다.

    훅 경로에서 불리므로 **던지지 않는다** — 부모 디렉터리를 만들 수 없는 경우까지
    포함해 실패는 False 다.
    """
    try:
        _ensure_parent(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        if contents:
            os.write(fd, contents.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def listdir_suffix(directory: str, suffix: str) -> list:
    """정렬된 전체 경로 목록. 디렉터리가 없으면 빈 목록."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names if n.endswith(suffix)]


def same_inode(a: str, b: str) -> Optional[bool]:
    try:
        return os.stat(a).st_ino == os.stat(b).st_ino
    except OSError:
        return None
