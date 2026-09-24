from __future__ import annotations

import os
import re
import time
from typing import Optional

from . import managed_block
from .adapter import InstallReceipt, NoInjectionChannel

FILE_NAME = "AGENTS.md"
CLAUDE_FILE_NAME = "CLAUDE.md"
EXCLUDE_REL = os.path.join(".git", "info", "exclude")
EXCLUDE_MARK = "# omhc: Codex 핸드오프 관리 구간이 들어가는 파일"

# Claude Code 가 읽는 지침 파일 후보. 전부 repo_root 상대경로.
_CLAUDE_CANDIDATES = (
    "CLAUDE.md",
    os.path.join(".claude", "CLAUDE.md"),
    "CLAUDE.local.md",
)

# CLAUDE.md 류를 훑어 @AGENTS.md 임포트를 찾는 범위. 이 파일들은 짧은 지침
# 파일이지 로그가 아니므로, 몇 KB 만 읽어도 임포트 줄을 놓치지 않는다.
_CLAUDE_SCAN_BYTES = 64 * 1024

# `@AGENTS.md`, `@./AGENTS.md`, `.claude/CLAUDE.md` 안의 `@../AGENTS.md`,
# 문장 끝 구두점이 붙은 `@AGENTS.md.` 까지 잡는다. 상대경로가 실제로 이
# 레포의 AGENTS.md 로 resolve 되는지까지는 확인하지 않는다 — 오탐은 outbox 로
# fail-safe 하므로 정규식 수준의 근사로 충분하다.
_IMPORT_RE = re.compile(r"(?<![\w@])@(?:\.{1,2}/)*AGENTS\.md\b")


def shared_with_claude(repo_root: str) -> Optional[str]:
    """AGENTS.md 가 Claude Code 로도 새는 배선이면 그 이유를, 아니면 None.

    실패는 모두 '공유 아님'으로 접는다 — 이 판정은 훅 경로(install → deliver)에서
    불리므로 예외를 던지면 세션 시작이 깨진다.
    """
    agents_path = path_for(repo_root)
    try:
        if os.path.islink(agents_path):
            return "AGENTS.md is a symlink"
    except OSError:
        pass

    for rel in _CLAUDE_CANDIDATES:
        claude_path = os.path.join(repo_root, rel)
        try:
            if (os.path.islink(claude_path)
                    and os.path.realpath(claude_path) == os.path.realpath(agents_path)):
                return "{} is a symlink to AGENTS.md".format(rel)
        except OSError:
            pass
        try:
            # 하드링크: symlink 는 아니지만 같은 inode. os.path.samefile 은 둘 다
            # 존재해야 하므로 하드링크는 애초에 그 조건을 만족한다.
            if (os.path.exists(claude_path) and os.path.exists(agents_path)
                    and os.path.samefile(claude_path, agents_path)):
                return "{} is hard-linked to AGENTS.md".format(rel)
        except OSError:
            pass
        try:
            with open(claude_path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(_CLAUDE_SCAN_BYTES)
        except OSError:
            continue
        if _IMPORT_RE.search(head):
            return "{} imports @AGENTS.md".format(rel)
    return None


# 파일 상단 설명 주석을 쓰지 않는다. omhc 가 만든 파일에 omhc 가 아닌 내용이
# 한 줄이라도 남으면, 구간을 붕괴시킨 뒤에도 파일이 잔여물로 남는다. begin 마커와
# 본문 첫 줄("[omhc] … not instructions")이 이미 자기 설명적이다.
FILE_HEADER = ""


def path_for(repo_root: str) -> str:
    return os.path.join(repo_root, FILE_NAME)


def _is_tracked(repo_root: str) -> bool:
    """git 이 이미 추적 중인가. 추적 중이면 exclude 는 무효이고 건드리면 안 된다."""
    # subprocess 는 select/selectors/threading 을 끌어와 import 에 약 4ms 든다.
    # 이 함수는 Codex Path B 를 쓸 때만 불리므로 훅 경로의 대부분은 지불하지 않는다.
    import subprocess

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
    reason = shared_with_claude(bundle.repo_root)
    if reason:
        raise NoInjectionChannel(
            "AGENTS.md is shared with Claude Code ({}); writing would leak a Codex "
            "handoff into Claude sessions and mutate the shared file".format(reason)
        )

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
