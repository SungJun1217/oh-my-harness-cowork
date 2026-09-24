"""훅 설치 상태 판정. 벤더 이름을 모른다 — 두 하네스가 같은 스키마를 쓴다:
`hooks.<Event>[].hooks[].command`.

`omhc hooks install`(다음 유닛)과 `omhc status` 의 `<adapter-id> hooks` 행이
공유한다. 여기서는 판정만 한다 — 실제로 파일을 쓰는 것은 다음 유닛의 몫이다.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from typing import Dict, List, NamedTuple, Optional, Tuple

# install.sh(install.sh:39, OMHC_CMD)는 **문자열** 정규식으로 omhc 훅을
# 찾는다 — 사람이 손으로 병합한 hooks.SessionStart 그룹에서 "지울 대상"을
# 넓게 잡아도 안전한 삭제 전용 작업이기 때문이다. 여기(설치 *판정*)는 반대
# 방향의 실수(진짜로 도는 훅을 아니라고 하거나, 우연히 "omhc brief"라는
# 글자가 들어간 사용자 훅을 설치로 착각하는 것)가 더 위험해서 argv 를
# 구조적으로 판정한다 — 절대경로/`~`/`${HOME}`/따옴표/PATH 상의 `omhc` 처럼
# 문자열은 다양해도 여전히 "그 명령"인 경우를 다 잡고, `echo 'run omhc brief
# later'` 처럼 문자열만 닮은 남의 훅은 걸러낸다. 두 정규식/판정이 다른 목적을
# 가지므로 하나로 합치지 않는다.
_HOME_RE = re.compile(r"\$\{HOME\}|\$HOME\b")


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


def _extract_commands(hooks_by_event, event: str = "SessionStart") -> List[str]:
    """`event` 아래 그룹들의 command 문자열을 순서대로 펼친다. 다른 이벤트는
    본 적도 없다는 듯 무시한다 — "omhc done" 같은 사용자 훅이 다른 이벤트에
    있어도 이 판정에 걸리면 안 된다."""
    out: List[str] = []
    if not isinstance(hooks_by_event, dict):
        return out
    for group in hooks_by_event.get(event) or []:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks") or []:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                out.append(h["command"])
    return out


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
        elif tok.startswith("--") and i + 1 < len(rest):
            flags[tok] = rest[i + 1]
            i += 2
        else:
            i += 1  # 값 없는 플래그/잉여 토큰은 조용히 건너뛴다 — 비교는 아는 키만 본다.
    return OmhcCall(argv=tuple(argv), sub=argv[1], flags=flags)


def _omhc_calls(commands: List[str]) -> List[OmhcCall]:
    out = []
    for c in commands:
        call = _parse_call(c)
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

    installed_omhc = _omhc_calls(_extract_commands(conf.get("hooks")))
    if not installed_omhc:
        return False, "not installed in {} — run `omhc hooks install`".format(config_path)

    shipped = _omhc_calls(_extract_commands(fragment))
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
