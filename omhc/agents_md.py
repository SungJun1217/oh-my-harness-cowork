from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

from . import managed_block
from .adapter import InstallReceipt

FILE_NAME = "AGENTS.md"
EXCLUDE_REL = os.path.join(".git", "info", "exclude")
EXCLUDE_MARK = "# omhc: Codex 핸드오프 관리 구간이 들어가는 파일"

# 파일 상단 설명 주석을 쓰지 않는다. omhc 가 만든 파일에 omhc 가 아닌 내용이
# 한 줄이라도 남으면, 구간을 붕괴시킨 뒤에도 파일이 잔여물로 남는다. begin 마커와
# 본문 첫 줄("[omhc] … not instructions")이 이미 자기 설명적이다.
FILE_HEADER = ""


def path_for(repo_root: str) -> str:
    return os.path.join(repo_root, FILE_NAME)


def _is_tracked(repo_root: str) -> bool:
    """git 이 이미 추적 중인가. 추적 중이면 exclude 는 무효이고 건드리면 안 된다."""
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "ls-files", "--error-unmatch", FILE_NAME],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _register_exclude(repo_root: str) -> bool:
    """.git/info/exclude 에 등재한다.

    .gitignore 가 아니라 info/exclude 를 쓰는 이유: 이 제외는 이 클론에만
    해당하는 사용자 결정이고, 커밋되어 다른 사람에게 강요될 것이 아니다.
    """
    path = os.path.join(repo_root, EXCLUDE_REL)
    if not os.path.isdir(os.path.dirname(path)):
        return False
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    for line in existing.splitlines():
        if line.strip() == FILE_NAME:
            return True
    if existing and not existing.endswith("\n"):
        existing += "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{}{}\n{}\n".format(existing, EXCLUDE_MARK, FILE_NAME))
    return True


def install(bundle, *, now: Optional[float] = None) -> InstallReceipt:
    """Path B: 작업 트리의 AGENTS.md 관리 구간에 핸드오프를 밀어넣는다.

    Codex 훅 신뢰(HookStateToml{enabled, trusted_hash})가 손으로 떨어뜨린
    hooks.json 을 거부할 수 있으므로, 훅 신뢰도 모델 협조도 필요 없는 유일한
    Codex 방향 경로다. AGENTS.md 는 세션마다 읽히므로 한 번 읽고 사라지지 않는다.
    """
    stamp = time.time() if now is None else now
    path = path_for(bundle.repo_root)
    tracked = _is_tracked(bundle.repo_root)

    managed_block.splice(
        path, bundle.body_md, captured_at=stamp,
        file_header="" if tracked else FILE_HEADER,
    )

    excluded = False
    if tracked:
        hint = (
            "AGENTS.md is tracked by git — omhc left it in the working tree and did "
            "NOT touch .git/info/exclude; run `omhc clear` before committing"
        )
    else:
        excluded = _register_exclude(bundle.repo_root)
        hint = "omhc clear (24h 후 자동 붕괴)"

    return InstallReceipt(
        channel="agents-md",
        paths_written=(path,),
        consumed_on_read=False,
        cleanup_hint=hint if excluded or tracked else hint + " [exclude 등재 실패]",
    )


def collapse(repo_root: str, *, now: Optional[float] = None, force: bool = False) -> bool:
    """오래된 관리 구간을 제거한다. 어떤 omhc 호출에서든 불린다."""
    stamp = time.time() if now is None else now
    path = path_for(repo_root)
    if managed_block.installed_captured_at(path) is None:
        return False
    if not force and not managed_block.is_stale(path, now=stamp):
        return False
    return managed_block.strip(path)
