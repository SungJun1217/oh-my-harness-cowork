from __future__ import annotations

import hashlib
import os
from typing import Optional


def resolve_repo_root(start: Optional[str] = None) -> str:
    """레포 루트의 THE 정의. `.git` 을 만나는 첫 조상, 없으면 realpath(cwd).

    호출 지점마다 다르게 정의하면 서브디렉터리에서 세션 목록이 조용히 0건이
    된다. Codex 는 rollout 에 레포 루트를 기록하므로 equal-or-descendant 판정과
    짝을 이뤄야 한다.

    `git rev-parse --show-toplevel` 을 쓰지 않는다. 훅 경로에서 프로세스당 한 번
    이상 불리는데 fork/exec 가 약 2.7ms 이고 subprocess import 가 약 3.7ms 라
    합쳐서 예산의 4% 를 먹는다. 상향 탐색은 stat 몇 번이고 워크트리·서브모듈처럼
    `.git` 이 파일인 경우도 같이 잡는다.
    """
    base = os.path.realpath(start or os.getcwd())
    current = base
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return base
        current = parent


def repo_key(repo_root: str) -> str:
    """사람이 읽을 수 있는 basename + 경로 해시. 다른 경로의 동명 레포를 구분한다."""
    digest = hashlib.sha1(repo_root.encode("utf-8")).hexdigest()[:8]
    return "{}-{}".format(os.path.basename(repo_root.rstrip("/")), digest)


def is_within(repo_root: str, candidate: str) -> bool:
    """equal-or-descendant. list_sessions 의 cwd 일치 규칙."""
    root = os.path.realpath(repo_root).rstrip("/")
    cand = os.path.realpath(candidate).rstrip("/")
    return cand == root or cand.startswith(root + "/")


def relativize(repo_root: str, path: str) -> Optional[str]:
    """절대경로 → 레포 상대 POSIX 경로. 레포 밖이면 None."""
    if not is_within(repo_root, path):
        return None
    root = os.path.realpath(repo_root).rstrip("/")
    rel = os.path.realpath(path)[len(root) :].lstrip("/")
    return rel or "."


# 상태 파일 이름의 단일 정의. 네 모듈에 재선언돼 있었고, 하나가 어긋나면
# brief 가 쓰는 파일과 status/clear 가 보는 파일이 갈라져 핸드오프가 조용히
# 보이지 않게 된다.
ROOT_NAME = ".omhc"
ARTIFACT_NAME = "omhc.txt"
NOTES_NAME = "notes.txt"


def omhc_root(home: Optional[str] = None) -> str:
    """모든 omhc 상태의 루트. home=None 이면 실제 홈.

    이 폴백을 세 모듈이 각자 결정하고 있었다 — 루트가 옮겨지면 guard.log 와
    ledger.jsonl 이 레포별 상태 디렉터리와 다른 곳에 남아, 훅 경로의 유일한
    실패 로그를 찾을 수 없게 된다.
    """
    return os.path.join(home or os.path.expanduser("~"), ROOT_NAME)


def state_dir(key: str, home: Optional[str] = None) -> str:
    """이 레포의 상태 루트. 작업 트리를 오염시키지 않도록 홈 아래에 둔다."""
    return os.path.join(omhc_root(home), key)


def artifact_path(state_dir_path: str) -> str:
    return os.path.join(state_dir_path, ARTIFACT_NAME)
