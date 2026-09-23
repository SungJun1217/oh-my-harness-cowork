"""실물 세션을 픽스처로 수확하고 골든 카운트를 독립 계산한다.

어댑터를 부르지 않는다. 골든을 피검증 코드로 생성하면 순환 검증이 되고,
파서 버그가 골든에 그대로 각인된다. 여기서는 최소한의 독립 카운터로만 센다.

픽스처는 한 번 얼리면 덮지 않는다. 라이브 트랜스크립트는 지금 이 세션이라서
계속 자라므로(실측: 798 → 1022 레코드), 매번 덮으면 골든이 함께 움직여
테스트가 무의미해진다. 다시 수확하려면 --force 를 준다.

사용: python3 tests/harvest.py [--force]
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import sys

HOME = os.path.expanduser("~")
REPO = "/home/ec2-user/capstone/oh-my-harness-cowork"
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SLUG = re.sub(r"[^a-zA-Z0-9]", "-", REPO)
CLAUDE_DIR = os.path.join(HOME, ".claude", "projects", SLUG)

# 최상위 XML 봉투 하나를 앞에서 떼어내는 패턴. 태그 이름 목록이 아니라 구조로
# 판정하므로 새로 생긴 태그도 자동으로 걸린다.
_ONE_TAG = re.compile(r"^<([a-zA-Z][\w:-]*)(\s[^>]*)?>.*?</\1>\s*", re.S)

# 사람이 쓰지 않았는데 사람 턴처럼 보이는 합성 문자열. 실물에서 목격된 것만 넣는다.
SYNTHETIC = ("[Request interrupted by user]",)

# 슬래시 명령 봉투 안의 <command-args> 는 사람이 실제로 타이핑한 말이므로 회수한다.
# omhc.guard 와 같은 규칙을 의도적으로 중복 구현한다 — 이 스크립트가 어댑터를
# import 하면 골든이 피검증 코드로 생성되어 순환 검증이 된다.
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def is_envelope(text: str) -> bool:
    """내용 전체가 최상위 XML 봉투들로만 이뤄졌는가(태그 밖 산문 없음)."""
    rest = text.strip()
    if not rest.startswith("<"):
        return False
    while rest:
        m = _ONE_TAG.match(rest)
        if not m:
            return False
        rest = rest[m.end() :].strip()
    return True


def human_text(row: dict):
    """사람의 말이면 그 텍스트, 아니면 None.

    Claude Code는 tool_result 도 type:"user" 로 되돌리므로 타입만 보면 안 된다.
    이 머신 실측: user 95개 중 isMeta 10개를 빼면 85개, 그중 67개가 tool_result,
    텍스트를 가진 19개 중 7개가 슬래시 명령 봉투와 명령 출력이다.
    """
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return None
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        if kinds != {"text"}:
            return None
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    else:
        return None
    if is_envelope(text):
        for body in _COMMAND_ARGS.findall(text):
            if body.strip():
                return " ".join(body.split())[:600]
        return None
    if text.strip() in SYNTHETIC:
        return None
    return text


FORCE = False


def _freeze(src: str, dst: str) -> bool:
    """픽스처를 한 번만 복사한다. 이미 있으면 건드리지 않는다(--force 제외).

    라이브 트랜스크립트는 계속 자라므로, 덮으면 골든이 같이 움직여 회귀를
    잡지 못한다.
    """
    if os.path.exists(dst) and not FORCE:
        return False
    shutil.copyfile(src, dst)
    return True


def _iter_json(path: str):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            try:
                yield i, json.loads(line)
            except ValueError:
                continue


def harvest_claude() -> dict:
    tops = sorted(
        (
            os.path.join(CLAUDE_DIR, n)
            for n in os.listdir(CLAUDE_DIR)
            if n.endswith(".jsonl")
        ),
        key=os.path.getsize,
        reverse=True,
    )
    src = tops[0]
    frozen = os.path.join(FIX, "claude", "live.jsonl")
    _freeze(src, frozen)

    # 반드시 얼린 사본에서 센다. 라이브 소스에서 세면 골든과 픽스처가 갈라진다.
    types: collections.Counter = collections.Counter()
    atts: collections.Counter = collections.Counter()
    first_cwd = None
    no_cwd = 0
    humans = 0
    for i, row in _iter_json(frozen):
        types[str(row.get("type"))] += 1
        if row.get("type") == "attachment":
            atts[str((row.get("attachment") or {}).get("type"))] += 1
        if row.get("cwd"):
            if first_cwd is None:
                first_cwd = i
        else:
            no_cwd += 1
        if human_text(row) is not None:
            humans += 1

    session_id = os.path.basename(src)[: -len(".jsonl")]
    sub_root = os.path.join(CLAUDE_DIR, session_id, "subagents")
    # agent-*.jsonl 만 고른다. 같은 트리의 journal.jsonl 은 워크플로우 부기이고
    # (launched/started/result) 트랜스크립트가 아니다 — 서브체인 드롭을 검증하려면
    # type:"user" isSidechain:true 레코드를 담은 파일이어야 한다.
    nested = 0
    picked = None
    for dirpath, _dirs, names in os.walk(sub_root):
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            nested += 1
            if picked is None and name.startswith("agent-"):
                path = os.path.join(dirpath, name)
                if os.path.getsize(path) > 0:
                    picked = path
    if picked:
        _freeze(picked, os.path.join(FIX, "claude", "subagent.jsonl"))

    return {
        "source_name": os.path.basename(src),
        "records": sum(types.values()),
        "types": dict(types),
        "first_cwd_index": first_cwd,
        "no_cwd": no_cwd,
        "human_turns": humans,
        "attachment_subtypes": dict(atts),
        "nested_subagent_files": nested,
    }


def harvest_codex() -> dict:
    root = os.path.join(HOME, ".codex", "sessions")
    rollouts = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                rollouts.append(os.path.join(dirpath, name))
    if not rollouts:
        return {"records": 0, "types": {}, "cwd": None, "source_name": None}
    rollouts.sort(key=os.path.getmtime, reverse=True)
    src = rollouts[0]
    frozen = os.path.join(FIX, "codex", "exec.jsonl")
    _freeze(src, frozen)

    types: collections.Counter = collections.Counter()
    cwd = None
    for _i, row in _iter_json(frozen):
        kind = str(row.get("type"))
        if kind == "response_item":
            kind += ":" + str((row.get("payload") or {}).get("type"))
        types[kind] += 1
        if row.get("type") == "session_meta":
            cwd = (row.get("payload") or {}).get("cwd")
    return {
        "source_name": os.path.basename(src),
        "records": sum(types.values()),
        "types": dict(types),
        "cwd": cwd,
    }


def main(argv=None) -> int:
    global FORCE
    argv = list(sys.argv[1:] if argv is None else argv)
    FORCE = "--force" in argv
    os.makedirs(os.path.join(FIX, "claude"), exist_ok=True)
    os.makedirs(os.path.join(FIX, "codex"), exist_ok=True)
    expected = {"claude": harvest_claude(), "codex": harvest_codex()}
    with open(os.path.join(FIX, "expected.json"), "w", encoding="utf-8") as fh:
        json.dump(expected, fh, ensure_ascii=False, indent=2, sort_keys=True)
    c = expected["claude"]
    print("claude: records={} types={} first_cwd={} no_cwd={} humans={} nested={}".format(
        c["records"], len(c["types"]), c["first_cwd_index"], c["no_cwd"],
        c["human_turns"], c["nested_subagent_files"]))
    print("codex: records={} types={} cwd={}".format(
        expected["codex"]["records"], len(expected["codex"]["types"]),
        expected["codex"]["cwd"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
