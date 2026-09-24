from __future__ import annotations

import os
import tempfile
from typing import Optional

# PIPE_BUF 는 파이프 전용이라(POSIX, macOS 는 512) 여기 쓰기엔 근거가 아니다
# (#22). 일반 파일에 O_APPEND 로 연 fd 에 대한 **단일 write(2) 호출**은
# POSIX 상 "파일 끝으로 이동 + 쓰기" 가 하나의 원자 연산이라고 보장된다 —
# 크기 상한이 없다. append_line 이 매번 write() 를 정확히 한 번만 부르는 것
# 자체가 그 보장을 지키는 방법이다; 줄 단위로 덧붙이는 모든 파일(원장,
# delivered.tsv, 색인)이 여기 의존한다. 아래 상수는 그 보장과 무관하게 "한
# write() 호출로 무리 없이 끝나는 크기" 를 넉넉히 잡은 값일 뿐이다.
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


def replace_preserving(path: str, text: str) -> None:
    """사람이 손으로 관리하는 설정 파일(hooks.json/settings.json)을 원자적으로
    갈아끼운다. `path` 가 심링크면 심링크 자체는 그대로 두고 실물만 바꾼다 —
    install.sh 의 uninstall 경로가 이미 같은 규칙을 쓰고 있었다(#7, hookconf.merge/strip).

    권한은 realpath 의 현재 mode 를 그대로 물려받는다. 파일이 아직 없으면(예:
    Codex 는 원래 hooks.json 이 없다) 0644 로 새로 만든다 — mkstemp 의 기본
    0600 을 그대로 두면 새로 만든 설정 파일만 유독 접근 권한이 좁아진다.
    """
    real_target = os.path.realpath(path)
    directory = os.path.dirname(real_target) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        mode = os.stat(real_target).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".omhc-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, real_target)
    except Exception:
        unlink_quiet(tmp_path)
        raise


def append_line(path: str, line: str, *, mode: int = 0o600) -> None:
    """한 줄을 O_APPEND 단일 write(2) 로 덧붙인다.

    여러 세션이 동시에 써도 부분 레코드가 생기지 않는다 — POSIX 가 O_APPEND
    fd 에 대한 단일 write(2) 호출의 원자성을 보장하기 때문이고(위 모듈 주석,
    #22 리뷰), PIPE_BUF 와는 무관하다(파이프 전용). 호출자가 책임질 것은
    `line` 을 (개행 붙인 채로) **한 번의 write() 호출**로 보낼 수 있게 유지하는
    것뿐이다 — 현실적 상한은 PIPE_BUF 가 아니라 커널이 한 write() 로 처리하는
    크기다(ledger.MAX_LINE 처럼 훨씬 작게 잡는 건 원자성이 아니라 다른 이유다).
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


def line_aligned_size(path: str, size: int, window: int = 65536,
                      fallback: Optional[int] = None) -> int:
    """`size`(보통 `os.stat().st_size`)를 그 앞의 마지막 완전한 줄 끝으로
    스냅한다. JSONL append-only 로그를 쓰는 프로세스가 마침 그 순간 긴
    레코드 하나를 쓰는 중이면 `size` 자체가 레코드 중간일 수 있다(#22 리뷰)
    — 그걸 그대로 baseline 으로 쓰고 나중에 그 레코드가 마저 쓰인 뒤 거기서
    부터 읽으면, offset 기반 리더(`read_session_since` 류)의 줄 스냅 로직이
    그 레코드 전체를 건너뛴다.

    파일 끝에서 최대 `window` 바이트만 거꾸로 훑어 마지막 개행을 찾는다 —
    JSONL 한 줄이 그보다 긴 경우는 드물다. 못 찾으면(또는 파일을 못 읽으면)
    `fallback` 을 돌려준다 — 주지 않으면(기본 `None`) `size` 를 그대로
    돌려준다(레코드 중간일 위험을 감수하는 옛 동작; window 보다 긴 단일
    레코드가 실제로 있는 극히 드문 경우에만 해당한다). 호출자가 이전에 알던
    baseline 을 `fallback` 으로 주면 못 찾았을 때 그 자리에 둔다 — 다음 판정이
    그 구간을 다시 볼 뿐 레코드를 건너뛰지 않는다. 이전 baseline 이 없을 때
    `0` 을 주면 안 된다: 다음 판정이 처음부터 읽어 원래의 사람 턴을 새 턴으로
    착각하고 옛 내용을 다시 넘긴다(#22 리뷰에서 재현).
    """
    if size <= 0:
        return 0
    start = max(0, size - window)
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(size - start)
    except OSError:
        return size if fallback is None else fallback
    idx = chunk.rfind(b"\n")
    if idx == -1:
        return size if fallback is None else fallback
    return start + idx + 1


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
