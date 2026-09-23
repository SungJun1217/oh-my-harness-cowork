from __future__ import annotations

import errno
import os
import signal
import time
from typing import Dict, List, Optional

from . import adapters, index, locate, pin

LOCK_NAME = "watch.lock"
POLL_SECONDS = 5.0

# 유휴 자동 종료. 아무 세션도 자라지 않은 채로 이만큼 지나면 스스로 끝낸다 —
# 사용자가 요청하지 않은 프로세스가 영구히 떠 있으면 안 된다.
IDLE_EXIT_SECONDS = 30 * 60


class LockBusy(Exception):
    """이미 다른 watcher 가 이 레포를 보고 있다."""


def _lock_path(state_dir: str) -> str:
    return os.path.join(state_dir, LOCK_NAME)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def read_lock(state_dir: str) -> Optional[int]:
    """살아 있는 watcher 의 pid, 없으면 None. 죽은 락은 청소한다."""
    path = _lock_path(state_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            pid = int(fh.read().strip() or "0")
    except (OSError, ValueError):
        return None
    if pid and _pid_alive(pid):
        return pid
    try:
        os.unlink(path)
    except OSError:
        pass
    return None


def acquire(state_dir: str) -> None:
    os.makedirs(state_dir, exist_ok=True)
    if read_lock(state_dir) is not None:
        raise LockBusy("watcher already running")
    path = _lock_path(state_dir)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise LockBusy("watcher already running")
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))


def release(state_dir: str) -> None:
    try:
        os.unlink(_lock_path(state_dir))
    except OSError:
        pass


def sweep(repo_root: str, state_dir: str, *, home: Optional[str] = None) -> int:
    """한 번 훑어 색인을 따라잡는다. 새로 쓴 행 수를 돌려준다.

    **정확성을 담당하지 않는다.** 이것이 한 번도 돌지 않아도 brief 는 인라인
    파싱으로 같은 산출물을 만든다 — 느려질 뿐이다. 그래서 데몬이 죽어도 결과가
    바뀌지 않고, 이득은 "갈아타는 순간 큰 트랜스크립트를 훅 안에서 파싱하지
    않는다"는 지연시간뿐이다.
    """
    written = 0
    # detect 와 read 가 같은 home 을 봐야 한다. present() 에 home 을 넘기지 않으면
    # 탐지는 실제 $HOME 을, 읽기는 지정된 home 을 보게 되어 대체 home 을 가리킨
    # 데몬이 아무것도 못 찾거나 엉뚱한 곳을 색인한다.
    homes = {aid: home for aid in adapters.REGISTRY} if home else None
    for adapter_id in adapters.present(homes=homes, now=time.time):
        try:
            adapter = adapters.get(adapter_id, home=home)
            refs = adapter.list_sessions(repo_root)
        except Exception:
            continue
        for ref in refs:
            try:
                idx = os.path.join(state_dir, "index", ref.session_id + ".idx")
                seen = index.last_seq(idx)
                read = adapter.read_session(ref)
                fresh = [e for e in read.events if e.seq > seen]
                if fresh:
                    written += index.append_rows(idx, fresh)
                    pin.pin_session(state_dir, ref)
            except Exception:
                continue
    return written


def lag(state_dir: str) -> List[Dict[str, object]]:
    """세션별로 마지막 색인 이벤트 뒤에 남은 바이트 수.

    이것은 "데몬이 뒤처졌다"는 뜻이 **아니다**. 세션 파일 꼬리에는 Event 가 되지
    않는 레코드(Codex 의 world_state / turn_context, Claude 의 attachment 등)가
    있으므로 정상 상태에서도 0 이 아니다. 유용한 신호는 절대값이 아니라 **한 번
    훑은 뒤에도 줄지 않는가** 다 — 그때가 기계가 죽은 때다.
    """
    out = []
    directory = os.path.join(state_dir, "index")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".idx"):
            continue
        session = name[: -len(".idx")]
        watermark = index.watermark(os.path.join(directory, name))
        source = os.path.join(pin.pinned_dir(state_dir, session), "source.jsonl")
        try:
            size = os.path.getsize(source)
        except OSError:
            size = 0
        out.append({"session": session, "watermark": watermark, "size": size,
                    "lag_bytes": size - watermark})
    return out


def run(
    repo_root: Optional[str] = None,
    *,
    home: Optional[str] = None,
    poll: float = POLL_SECONDS,
    idle_exit: float = IDLE_EXIT_SECONDS,
    max_sweeps: Optional[int] = None,
    now=time.time,
) -> int:
    """가속기 루프. 유휴 시간이 넘으면 스스로 끝낸다."""
    root = locate.resolve_repo_root(repo_root)
    state = locate.state_dir(locate.repo_key(root), home=home)
    acquire(state)
    stop = {"flag": False}

    def _handle(_signum, _frame):
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass

    last_change = now()
    sweeps = 0
    try:
        while not stop["flag"]:
            if sweep(root, state, home=home):
                last_change = now()
            sweeps += 1
            if max_sweeps is not None and sweeps >= max_sweeps:
                break
            if now() - last_change > idle_exit:
                break
            time.sleep(poll)
    finally:
        release(state)
    return 0
