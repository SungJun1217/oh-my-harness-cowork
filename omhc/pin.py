from __future__ import annotations

import glob
import os
from typing import NamedTuple, Optional

# 구조된 사본의 상한. /tmp 참조물은 다른 파일시스템이라 하드링크가 불가능하고
# 재부팅에 죽는다. 그것만 실제 복사한다.
RESCUE_MAX_BYTES = 5 * 1024 * 1024


class PinResult(NamedTuple):
    """고정 결과. 조용히 None 을 돌려주면 아카이브가 깨진 걸 아무도 모른다.

    `omhc status` 가 이 값을 PASS/FAIL 로 보고한다.
    """

    path: Optional[str]
    linked: bool
    sidecars: int
    error: str = ""

    def __bool__(self) -> bool:
        return self.linked


def pinned_dir(state_dir: str, session_id: str) -> str:
    return os.path.join(state_dir, "pinned", session_id)


def pin_session(state_dir: str, ref) -> Optional[str]:
    """원본 세션 바이트를 하드링크로 고정한다.

    재직렬화하지 않는다. 같은 inode 이므로
      - 추가 디스크 0바이트
      - 아직 돌아가는 세션의 append 도 그대로 보이고
      - 원본 디렉터리 항목이 rm 되거나 /clear 돼도 바이트가 살아남는다.
    포맷이 파괴적으로 바뀌어도 포인터는 깨지지 않는다.
    """
    return pin_session_result(state_dir, ref).path


def pin_session_result(state_dir: str, ref) -> PinResult:
    """pin_session 의 관측 가능한 형태. 실패 이유를 담아 돌려준다."""
    if not os.path.exists(ref.source_path):
        return PinResult(None, False, 0, "source missing: {}".format(ref.source_path))
    target_dir = pinned_dir(state_dir, ref.session_id)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        return PinResult(None, False, 0, "mkdir failed: {}".format(exc))
    target = os.path.join(target_dir, "source.jsonl")

    linked = _link_or_none(ref.source_path, target)
    if linked is None:
        try:
            same = os.stat(ref.source_path).st_dev == os.stat(target_dir).st_dev
        except OSError:
            same = False
        reason = (
            "hardlink refused by filesystem"
            if same
            else "cross-device: source and state dir are on different filesystems"
        )
        return PinResult(None, False, 0, reason)
    sidecars = _pin_sidecars(ref, target_dir)
    return PinResult(target, True, sidecars, "")


def _link_or_none(src: str, dst: str) -> Optional[str]:
    if os.path.exists(dst):
        try:
            if os.stat(src).st_ino == os.stat(dst).st_ino:
                return dst
        except OSError:
            return dst
        # 다른 세션 파일이 같은 자리를 차지했다면 교체한다.
        try:
            os.unlink(dst)
        except OSError:
            return dst
    try:
        os.link(src, dst)
        return dst
    except OSError:
        # 다른 장치이거나 하드링크가 막힌 파일시스템. 복사는 하지 않는다 —
        # 이 파일은 계속 자라므로 사본은 곧 낡는다. 없는 것이 낫다.
        return None


def _pin_sidecars(ref, target_dir: str) -> int:
    """externalized tool output 을 같이 고정한다.

    Claude Code 는 큰 tool_result 를 <persisted-output> 스텁으로 치환하고 내용을
    <session>/tool-results/*.txt 로 빼낸다. 사이드카를 고정하지 않으면 스텁이
    해소되지 않아 tier (b) 가 반쪽이 된다.
    """
    base = os.path.dirname(ref.source_path)
    sidecar_root = os.path.join(base, ref.session_id, "tool-results")
    if not os.path.isdir(sidecar_root):
        return 0
    mirror = os.path.join(target_dir, "tool-results")
    os.makedirs(mirror, exist_ok=True)
    count = 0
    for path in glob.glob(os.path.join(sidecar_root, "*")):
        if not os.path.isfile(path):
            continue
        if _link_or_none(path, os.path.join(mirror, os.path.basename(path))):
            count += 1
    return count


def rescue(state_dir: str, session_id: str, src: str) -> Optional[str]:
    """다른 파일시스템의 참조물을 실제로 복사한다. 유일한 진짜 사본이다."""
    try:
        size = os.path.getsize(src)
    except OSError:
        return None
    if size > RESCUE_MAX_BYTES:
        return None
    target_dir = os.path.join(pinned_dir(state_dir, session_id), "rescued")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(src))
    tmp = target + ".tmp"
    # shutil 은 zlib/bz2/lzma 를 끌어와 import 에 약 4ms 든다. rescue 는 훅 경로에서
    # 불리지 않으므로 여기서만 들인다.
    import shutil

    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return target
