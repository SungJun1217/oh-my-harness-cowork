"""훅 설치 상태 판정 + 설치/제거. 벤더 이름을 모른다 — 두 하네스가 같은
스키마를 쓴다: `hooks.<Event>[].hooks[].command`.

`omhc hooks install|uninstall` 과 `omhc status` 의 `<adapter-id> hooks` 행이
여기 하나를 공유한다.
"""
from __future__ import annotations

import collections
import copy
import json
import os
import re
import shlex
import shutil
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import fsio

# install.sh(strip_omhc_hooks, OMHC_CMD)는 **문자열** 정규식으로 omhc 훅을
# 찾는다 — omhc 가 설치돼 있지 않은(또는 그 전) 상태에서도 돌아야 해서 이
# 모듈을 부르지 못하고, 손으로 병합한 hooks.SessionStart 그룹에서 "지울
# 대상"을 넓게 잡아도 안전한 삭제 전용 작업이기 때문이다. 여기(구조 판정)는
# 반대 방향의 실수(진짜로 도는 훅을 아니라고 하거나, 우연히 "omhc brief"라는
# 글자가 들어간 사용자 훅을 설치/제거로 착각하는 것)가 더 위험해서 argv 를
# 구조적으로 판정한다.
#
# 두 판정은 실측으로 갈라지는 게 확인된 경우가 있고(tests/test_hooks_cmd.py
# 의 TestInstallShParity 가 일치하는 입력만 고정한다), 하나로 합치지 않는다:
#   - install.sh 가 더 넓게 지운다: `cd ~ && omhc brief …`(전체 명령의
#     argv[0] 는 "cd"지만 문자열에 "omhc brief"가 들어 있다), `/usr/bin/env
#     omhc mark …`(argv[0] 는 "env") 모두 install.sh 는 지우지만, 여기는
#     argv[0] 의 basename 이 "omhc" 가 아니므로 손대지 않는다.
#   - 여기가 더 넓게 인식한다: `'omhc' 'mark' --harness x`(토큰마다 따옴표)
#     는 shlex 로 풀면 `omhc mark --harness x` 와 같아 여기는 설치로 인정하지만,
#     install.sh 의 정규식(`omhc["']?\s+(mark|brief)`)은 "omhc" 바로 뒤
#     선택적 따옴표 하나만 허용하고 그다음 공백 뒤에는 "mark"/"brief" 리터럴을
#     기대하므로 `'mark`(따옴표로 시작)에는 매칭되지 않는다.
_HOME_RE = re.compile(r"\$\{HOME\}|\$HOME\b")


class HookConfigError(Exception):
    """merge/strip 이 fail-closed 하려고 던지는 예외. 손대지 않았다는 뜻이다."""


class HookConfig(NamedTuple):
    """어댑터의 선택 메서드 `hook_config()` 가 돌려주는 레코드."""

    config_path: str
    fragment_name: str
    post_write_note: str = ""


class OmhcCall(NamedTuple):
    """SessionStart 훅 명령 하나를 구조적으로 판정한 결과."""

    argv: Tuple[str, ...]
    sub: str  # "mark" | "brief"
    flags: Dict[str, str]  # {"--harness": "claude-code", ...} — 순서 무관 집합으로 비교한다.


def fragments_dir() -> str:
    """조각이 실제로 설치된 디렉터리.

    이 모듈 자신의 realpath 기준 `../hooks` 다 — git 체크아웃(`<repo>/hooks`)과
    curl 설치(`~/.local/share/omhc/current/hooks`) 모두에서 성립한다. `bin/omhc`
    가 심링크를 readlink -f 로 먼저 풀고 그 부모를 sys.path 에 꽂으므로, 이
    모듈의 __file__ 도 항상 실물 위치를 가리킨다.
    """
    pkg_dir = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(os.path.dirname(pkg_dir), "hooks")


def load_fragment(name: str) -> Dict[str, list]:
    """조각 파일에서 `hooks` 키만 돌려준다. `_comment` 등 나머지는 사람용이다."""
    path = os.path.join(fragments_dir(), name)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["hooks"]


class _Entry(NamedTuple):
    """`event` 아래 command 훅 하나 — 판정에 필요한 그룹/훅 필드까지 들고 있다."""

    command: str
    matcher: object  # 그룹의 matcher. 없으면 None.
    type: object  # 훅의 type. 없으면 None.


def _extract_entries(hooks_by_event, event: str = "SessionStart") -> List[_Entry]:
    """`event` 아래 그룹들의 command 훅을 순서대로 펼친다. 다른 이벤트는
    본 적도 없다는 듯 무시한다 — "omhc done" 같은 사용자 훅이 다른 이벤트에
    있어도 이 판정에 걸리면 안 된다."""
    out: List[_Entry] = []
    if not isinstance(hooks_by_event, dict):
        return out
    for group in hooks_by_event.get(event) or []:
        if not isinstance(group, dict):
            continue
        matcher = group.get("matcher")
        for h in group.get("hooks") or []:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                out.append(_Entry(h["command"], matcher, h.get("type")))
    return out


_SIMPLE_MATCHER_RE = re.compile(r"^[a-zA-Z0-9_|]+$")


def _matcher_runs_at_startup(matcher) -> bool:
    """그룹의 matcher 가 세션 시작(SessionStart 의 "startup")에도 걸리는가.

    Claude Code 2.1.281(바이너리 안의 함수) 실측을 그대로 거울에 비춘다 —
    Codex 의 정확한 의미론은 확인된 바 없고, 지금은 같은 스키마를 쓴다고
    가정한다: 비었거나 "*" 면 전부 매칭. `^[a-zA-Z0-9_|]+$` 를 만족하는
    "단순" matcher(예: `startup|resume|clear|compact`)는 정규식으로 쓰지
    않고 "|" 로 쪼개 "startup" 과 정확히 같은 항목이 있는지만 본다 — 그래서
    "start" 하나만 있는 단순 matcher는 "startup" 과 문자열이 달라 안 걸린다.
    그 외는 `new RegExp(m).test(source)`(고정 없는 부분 검색)이므로
    `re.search`(fullmatch 아님)로 흉내 낸다. 정규식으로 못 읽는 문자열은
    안 도는 쪽(False)이다.
    """
    if matcher is None or matcher == "" or matcher == "*":
        return True
    if not isinstance(matcher, str):
        return False
    if _SIMPLE_MATCHER_RE.match(matcher):
        return "startup" in matcher.split("|")
    try:
        pattern = re.compile(matcher)
    except re.error:
        return False
    return pattern.search("startup") is not None


def _runnable_at_startup(entry: "_Entry") -> bool:
    return entry.type == "command" and _matcher_runs_at_startup(entry.matcher)


def _not_runnable_reason(entry: "_Entry") -> str:
    if entry.type != "command":
        return "type {!r}".format(entry.type)
    return "matcher {!r}".format(entry.matcher)


def _parse_call(command: str) -> Optional[OmhcCall]:
    """command 문자열을 argv 로 쪼개고, omhc mark/brief 호출이면 구조를 돌려준다.

    argv[0] 의 basename 이 정확히 "omhc"여야 한다 — 절대경로/`~`/`${HOME}`/
    PATH 상의 bare `omhc` 는 전부 이 조건을 만족하지만, `echo 'omhc brief'`
    처럼 명령어 자체가 다른 것(첫 토큰이 omhc 가 아닌 것)은 걸리지 않는다.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return None  # 따옴표가 안 맞는 등 셸로도 못 쪼개는 문자열은 omhc 명령이 아니다.
    if not argv:
        return None
    if os.path.basename(argv[0]) != "omhc":
        return None
    if len(argv) < 2 or argv[1] not in ("mark", "brief"):
        return None
    flags: Dict[str, str] = {}
    rest = argv[2:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--") and "=" in tok:
            # argparse 가 받는 --harness=claude-code 형태. 값은 첫 = 뒤 전부다.
            key, value = tok.split("=", 1)
            flags[key] = value
            i += 1
        elif tok.startswith("--") and i + 1 < len(rest) and not rest[i + 1].startswith("--"):
            flags[tok] = rest[i + 1]
            i += 2
        else:
            # 값 없는 플래그(다음 토큰도 "--"로 시작하거나 마지막 토큰이다)/
            # 잉여 토큰은 조용히 건너뛴다 — 비교는 아는 키만 본다. 다음 토큰을
            # 무조건 값으로 삼으면 `--text --harness codex-cli` 에서 "--harness"
            # 가 --text 의 값으로 먹혀 진짜 --harness 를 잃어버린다.
            i += 1
    return OmhcCall(argv=tuple(argv), sub=argv[1], flags=flags)


def _omhc_calls(entries: List["_Entry"], *, runnable_only: bool = False) -> List[OmhcCall]:
    out = []
    for entry in entries:
        if runnable_only and not _runnable_at_startup(entry):
            continue
        call = _parse_call(entry.command)
        if call is not None:
            out.append(call)
    return out


def _resolve_binary(argv0: str, home: str) -> Optional[str]:
    """argv[0] 을 실행 가능한 절대경로로 푼다. 못 찾으면 None.

    `$HOME`/`${HOME}`/선두 `~` 는 모두 (실행 시점 os.environ 이 아니라) 주어진
    `home` 인자로 치환한다 — 이 검사가 실제 사용자 $HOME 과 다른 홈(테스트의
    임시 홈 등)을 흉내 낼 수 있어야 한다. `~otheruser` 같은 드문 꼴만
    os.path.expanduser(실행 프로세스의 실제 환경을 본다)에 맡긴다. 슬래시가
    없는 bare 이름은 PATH 조회(shutil.which, 실행 시점과 같은 실제 PATH)로
    찾는다 — 그건 이 검사가 흉내 낼 대상이 아니라 실제로 있어야 하는 것이다.
    """
    token = _HOME_RE.sub(lambda _m: home, argv0)  # home 을 치환 템플릿으로 읽지 않게
    if token == "~" or token.startswith("~/"):
        # os.path.expanduser 는 실행 프로세스의 실제 $HOME(os.environ)을 본다 —
        # 그러면 이 함수가 흉내 내려는 `home` 인자와 갈라진다. `~` 는 직접
        # 치환한다. `~otheruser` 같은 드문 꼴만 expanduser 에 맡긴다.
        token = home.rstrip("/") + token[1:]
    else:
        token = os.path.expanduser(token)
    if "/" not in token:
        return shutil.which(token)
    return token


def _diff_reason(shipped: List[OmhcCall], installed: List[OmhcCall]) -> str:
    shipped_subs = [c.sub for c in shipped]
    installed_subs = [c.sub for c in installed]
    if installed_subs != shipped_subs:
        missing = [s for s in shipped_subs if s not in installed_subs]
        if missing:
            return "missing {}".format(" and ".join(missing))
        installed_counts = collections.Counter(installed_subs)
        shipped_counts = collections.Counter(shipped_subs)
        if any(installed_counts[s] > shipped_counts[s] for s in installed_counts):
            return "duplicate omhc hooks (found {})".format(", ".join(installed_subs))
        return "wrong order (found {})".format(", ".join(installed_subs) or "nothing")
    for want, got in zip(shipped, installed):
        if want.flags != got.flags:
            keys = sorted(set(want.flags) | set(got.flags))
            parts = [
                "{} expected {!r} found {!r}".format(k, want.flags.get(k), got.flags.get(k))
                for k in keys if want.flags.get(k) != got.flags.get(k)
            ]
            return "{}: {}".format(want.sub, "; ".join(parts))
    return "commands differ"  # 도달하면 안 되지만(위에서 다 같으면 애초에 안 불린다) 방어적으로 둔다.


def inspect(config_path: str, fragment: Dict[str, list], home: str) -> Tuple[bool, str]:
    """설치 상태를 판정한다. 절대 던지지 않는다 — 호출자(cmd_status)가 감싸지만
    이 함수 자체도 진단 도구이므로 스스로 fail-closed 하게 짠다.

    `fragment` 는 `load_fragment()` 가 돌려주는, 그 하네스가 *지금* 배포하는
    조각의 `hooks` 값이다.
    """
    try:
        with open(config_path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return False, "not installed in {} — run `omhc hooks install`".format(config_path)
    except UnicodeDecodeError:
        return False, "cannot parse {}".format(config_path)

    try:
        conf = json.loads(raw)
    except ValueError:
        return False, "cannot parse {}".format(config_path)
    if not isinstance(conf, dict):
        return False, "cannot parse {}".format(config_path)

    installed_entries = _extract_entries(conf.get("hooks"))
    installed_omhc_any = _omhc_calls(installed_entries)
    if not installed_omhc_any:
        return False, "not installed in {} — run `omhc hooks install`".format(config_path)

    installed_omhc = _omhc_calls(installed_entries, runnable_only=True)
    if not installed_omhc:
        # omhc 호출은 있는데(위에서 확인) 세션 시작 시점에는 하나도 안 돈다 —
        # matcher 가 좁혀놨거나 command 가 아닌 type 이어서다. "PASS 인데 안
        # 도는" 설치가 merge() 의 "이미 PASS 면 손대지 않는다" 경로에 걸려
        # 영영 고쳐지지 않는 걸 막는다.
        reason = None
        for entry in installed_entries:
            if _parse_call(entry.command) is not None and not _runnable_at_startup(entry):
                reason = _not_runnable_reason(entry)
                break
        return False, ("omhc hooks never run at session start ({}) — "
                        "run `omhc hooks install`").format(reason or "not runnable")

    shipped_entries = _extract_entries(fragment)
    shipped_any = _omhc_calls(shipped_entries)
    # runnable 호출만 비교하면, 실제로는 안 도는 여분의 omhc 그룹(예: matcher
    # "resume" 에 낀 두 번째 brief)이 있어도 PASS 로 보일 수 있다 — merge()
    # 의 "PASS 면 이미 중복이 없다" 는 전제가 깨진다. 그래서 전체(안 도는
    # 것까지) 개수도 shipped 를 넘지 않는지 따로 본다(#20 리뷰).
    installed_counts = collections.Counter(c.sub for c in installed_omhc_any)
    shipped_counts = collections.Counter(c.sub for c in shipped_any)
    if any(installed_counts[s] > shipped_counts[s] for s in installed_counts):
        return False, "differs from shipped fragment ({}) — run `omhc hooks install`".format(
            _diff_reason(shipped_any, installed_omhc_any))

    shipped = _omhc_calls(shipped_entries, runnable_only=True)
    if [c.sub for c in installed_omhc] != [c.sub for c in shipped] or \
            any(w.flags != g.flags for w, g in zip(shipped, installed_omhc)):
        return False, "differs from shipped fragment ({}) — run `omhc hooks install`".format(
            _diff_reason(shipped, installed_omhc))

    for call in installed_omhc:
        binpath = _resolve_binary(call.argv[0], home)
        if binpath is None:
            return False, "cannot verify {} on PATH".format(call.argv[0])
        if not (os.path.isfile(binpath) and os.access(binpath, os.X_OK)):
            return False, "{} not executable".format(binpath)

    return True, "installed"


def has_runnable_call(config_path: str, sub: str, flags: Optional[Dict[str, str]] = None) -> bool:
    """`config_path` 의 SessionStart 에 `sub`(예: "brief")를 부르는, 세션 시작
    시점에 실제로 도는 omhc 호출이 있는가. `flags` 가 주어지면 그 키=값도
    맞아야 한다(예: `{"--harness": "codex-cli"}`).

    훅 경로(install_handoff → hook_is_installed)에서 쓴다 — 절대 던지지
    않는다: 실패는 전부 False 다(설치 안 됨과 구분 못 하지만, 구분해서 얻는
    이득보다 훅 경로가 절대 안 죽어야 한다는 쪽이 우선이다, 불변식 2).
    """
    try:
        with open(config_path, encoding="utf-8") as fh:
            conf = json.loads(fh.read())
        if not isinstance(conf, dict):
            return False
        for call in _omhc_calls(_extract_entries(conf.get("hooks")), runnable_only=True):
            if call.sub != sub:
                continue
            if flags and any(call.flags.get(k) != v for k, v in flags.items()):
                continue
            return True
        return False
    except Exception:
        return False


# --- install/uninstall (`omhc hooks install|uninstall`) ---------------------


def _load(config_path: str) -> dict:
    """설정 객체를 읽는다. 파일이 없으면 빈 dict(``{}``) — merge 가 그 위에
    새로 만들 수 있어야 한다(Codex 는 원래 hooks.json 이 없다). 그 밖의
    모든 실패는 fail-closed 하게 던진다 — 절반만 읽고 계속 진행하지 않는다."""
    try:
        with open(config_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HookConfigError("cannot parse {}: {}".format(config_path, exc))
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise HookConfigError("cannot parse {}: {}".format(config_path, exc))
    if not isinstance(obj, dict):
        raise HookConfigError("{}: top-level is not an object".format(config_path))
    return obj


def _strip_hooks(conf: dict) -> dict:
    """`conf`(전체 설정 객체)의 사본에서 hooks.SessionStart 의 omhc mark/brief
    훅만 지운다. install.sh 의 문자열 정규식과 달리 `_parse_call` 로 구조적으로
    판정한다 — 지우는 쪽 실수(진짜 도는 훅을 못 지우거나, `echo 'omhc brief'`
    같은 남의 훅을 지우는 것)가 여기서도 여전히 위험하기 때문이다.

    모양이 기대를 벗어나면(hooks 가 객체가 아니다 등) install.sh 의 uninstall
    경로와 같은 원칙으로 fail-closed 한다 — 못 알아보는 기계적 형태를 그대로
    두거나 고쳐 쓰지 않는다.
    """
    conf = copy.deepcopy(conf)
    hooks = conf.get("hooks")
    if hooks is None:
        return conf
    if not isinstance(hooks, dict):
        raise HookConfigError("hooks is not an object")
    groups = hooks.get("SessionStart")
    if groups is None:
        return conf
    if not isinstance(groups, list):
        raise HookConfigError("hooks.SessionStart is not an array")

    kept_groups = []
    removed_any = False
    for group in groups:
        if not isinstance(group, dict):
            raise HookConfigError("a SessionStart group is not an object")
        if not isinstance(group.get("hooks"), list):
            raise HookConfigError("a SessionStart group's hooks is not an array")
        original_hooks = group["hooks"]
        kept_hooks = [
            h for h in original_hooks
            if not (isinstance(h, dict) and isinstance(h.get("command"), str)
                    and _parse_call(h["command"]) is not None)
        ]
        if len(kept_hooks) == len(original_hooks):
            # 이 그룹에서는 아무것도 지우지 않았다 — 원래 비어 있던 그룹이라도
            # (install.sh 와 마찬가지로) 손대지 않고 그대로 둔다. 건드리지
            # 않은 빈 그룹까지 드롭하면, omhc 훅이 하나도 없는 설정에서도
            # strip() 이 "바뀌었다"고 잘못 보고한다(#7 리뷰 4).
            kept_groups.append(group)
            continue
        removed_any = True
        if kept_hooks:
            new_group = dict(group)
            new_group["hooks"] = kept_hooks
            kept_groups.append(new_group)
        # else: 이 그룹은 omhc 훅만 있었고 지워서 비었다 — 그룹째 드롭한다.

    if not removed_any:
        return conf  # SessionStart 자체도 원래 모습 그대로(빈 배열이었어도) 남긴다.

    if kept_groups:
        hooks["SessionStart"] = kept_groups
    else:
        del hooks["SessionStart"]
    if not hooks:
        # hooks 가 SessionStart 하나만 들고 있었다면 이제 빈 객체다 — 남겨두면
        # 아무 것도 설치한 적 없는 설정에 `{"hooks": {}}` 만 흔적으로 남는다.
        del conf["hooks"]
    return conf


def _write_if_changed(config_path: str, original: dict, updated: dict) -> bool:
    """`updated` 가 `original` 과 같으면(idempotent) 아무것도 안 쓴다 — Codex
    health 행이 hooks.json 의 mtime 에 기대므로 그 보장이 여기서 무너지면
    안 된다. 다르면 기존 파일을 `.omhc-bak` 로 백업(있을 때만)하고 원자적으로
    갈아끼운다."""
    if updated == original:
        return False
    real_target = os.path.realpath(config_path)
    if os.path.exists(real_target):
        if not os.access(real_target, os.W_OK):
            # 실측: 0444 설정 파일은 그냥 os.replace 로도 갈아끼워질 수 있다
            # (디렉터리 쓰기 권한만 있으면 rename 은 파일 자체의 모드를 안 본다)
            # — 하지만 사람이 일부러 잠가 둔 파일을 조용히 덮어쓰면 안 되므로
            # 여기서 명시적으로 거부한다(#7 리뷰 5).
            raise HookConfigError(
                "{} is read-only; refusing to overwrite it silently — "
                "chmod it writable first if you want omhc to manage it".format(real_target))
        backup = config_path + ".omhc-bak"
        # 이전 실행이 남긴 백업이 0444 로 남아 있으면(원본이 그 시점에
        # 읽기 전용이었다면) copy2 의 open(dst, "wb") 이 EACCES 로 터진다 —
        # 백업은 매번 최신 원본을 가리키면 되므로 먼저 지운다(#7 리뷰 5).
        fsio.unlink_quiet(backup)
        shutil.copy2(config_path, backup)  # 권한 비트도 원본과 같게
    try:
        text = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(updated, indent=2, ensure_ascii=True) + "\n"
    fsio.replace_preserving(config_path, text)
    return True


def strip(config_path: str) -> bool:
    """`config_path` 의 hooks.SessionStart 에서 omhc 자신의 훅만 제거한다.
    바뀐 게 있으면 True, 없으면(원래 omhc 훅이 없었다) False."""
    original = _load(config_path)
    updated = _strip_hooks(original)
    return _write_if_changed(config_path, original, updated)


def merge(config_path: str, fragment: Dict[str, list], home: str) -> bool:
    """`config_path` 에 `fragment`(=`load_fragment()` 가 돌려주는 `hooks` 값)를
    병합한다. 먼저 `strip` 해 중복을 막은 뒤 fragment 의 SessionStart 그룹을
    이어 붙인다 — `hooks/*.json` 의 명령이 바뀌었을 때(예: `--wire sdk` →
    `claude`) 옛 명령 옆에 새 명령이 덧붙어 brief 가 두 번 돌지 않게 한다.

    이미 `inspect()` 가 PASS 하는 설치는 손대지 않는다 — 사람이 손으로
    병합하면서 omhc 그룹 앞뒤에 자기 그룹을 두거나, omhc 훅에 `timeout`/
    `matcher` 같은 필드를 얹거나, 순서를 바꿔도 여전히 유효한 설치일 수
    있다. 여기서 다시 쓰면 구조가 재배치되고 그런 필드가 지워지며, 무엇보다
    `hooks.json` 의 mtime 이 바뀐다 — omhc 의 codex hook 판정이 그 mtime 을
    기준으로 삼아 "아직 판정 불가" 로 돌아가고, 내용이 바뀐 hooks.json 을
    Codex 가 다시 신뢰하게 할 수도 있다(미검증). 이미 돌고 있는 설치를
    재정렬만으로 그렇게 만들면 안 된다(#7 리뷰 3). `inspect()` 는 모든 SessionStart 그룹의 omhc
    호출을 순서·개수·플래그까지 배포된 조각과 정확히 비교하므로, PASS 라면
    중복 omhc 호출도 이미 없다는 뜻이다."""
    ok, _detail = inspect(config_path, fragment, home)
    if ok:
        return False
    original = _load(config_path)
    updated = _strip_hooks(original)
    frag_groups = fragment.get("SessionStart") or []
    if frag_groups:
        hooks = updated.setdefault("hooks", {})
        hooks["SessionStart"] = list(hooks.get("SessionStart") or []) + copy.deepcopy(frag_groups)
    return _write_if_changed(config_path, original, updated)
