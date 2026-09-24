#!/bin/sh
# omhc 설치 스크립트. GitHub 릴리스 소스를 ~/.local/share/omhc/<버전> 에 풀고
# ~/.local/bin/omhc 로 심링크한다. hooks/ 조각이 가리키는 경로와 같다.
# pip 을 쓰지 않는다 — 의존성이 0 이라 소스 트리가 곧 설치물이다.
#
#   curl -fsSL https://raw.githubusercontent.com/SungJun1217/oh-my-harness-cowork/main/install.sh | sh
#   curl -fsSL .../install.sh | OMHC_VERSION=v0.1.0 sh      # 특정 버전
#
# 다시 실행하면 업데이트다. 제거: curl -fsSL .../install.sh | sh -s -- --uninstall
set -eu

repo=SungJun1217/oh-my-harness-cowork
share=$HOME/.local/share/omhc
bin=$HOME/.local/bin

die() { echo "omhc install: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 가 없다 (3.9 이상 필요)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || die "python3 $(python3 -c 'import platform; print(platform.python_version())') — 3.9 이상이 필요하다"

# --uninstall: 훅 병합을 되돌리고 설치물을 지운다. ~/.omhc(아카이브)는
# OMHC_PURGE=1 이 아니면 남긴다 — 실수로 --uninstall 한 뒤 기록을 잃지 않게.
# omhc 가 설치돼 있지 않은 상태(또는 그 전)에서도 돌아야 하므로 omhc 자체에
# 의존하지 않는다 — 그래서 hookconf.strip() 을 부르지 못하고 같은 로직을
# 셸 안에 다시 python 으로 심는다. 두 구현이 갈라지지 않게 tests/test_hooks_cmd.py
# 의 TestInstallShParity 가 같은 입력에서 같은 출력을 내는지 지킨다.
if [ "${1:-}" = "--uninstall" ]; then
  claude_settings=$HOME/.claude/settings.json
  codex_hooks=$HOME/.codex/hooks.json

  strip_omhc_hooks() {
    # omhc/hookconf.py 의 strip()/_strip_hooks() 를 그대로 거울에 비춘 것 —
    # hooks.SessionStart 만 본다. 다른 이벤트(Stop 등)는 아예 건드리지 않아
    # "omhc done" 같은 사용자 명령이 우연히 걸리는 일이 없다. 훅 하나 단위로
    # 걸러 그룹/이벤트는 비었을 때만 지운다 — 손으로 병합해 같은 그룹에 낀
    # 사용자 훅을 함께 지우지 않는다. 명령 판정은 하네스 조각이 실제로 쓰는
    # 두 명령(mark/brief)에만 좁게 맞춘다. hookconf 는 argv 를 구조적으로
    # 판정하고(_parse_call) 여기는 문자열 정규식이다 — 지우는 쪽 실수는
    # 넓게 잡아도 안전한 삭제 전용 작업이라 용인하지만, 문자열만 닮은 훅에서는
    # 두 판정이 갈라진다. 실측한 세 경우(`echo 'omhc brief'`, `cd ~ && omhc
    # brief …`·`/usr/bin/env omhc mark …`, `'omhc' 'mark' …`)는 omhc/hookconf.py
    # 의 모듈 주석에 있고, 패리티 테스트가 그 경우들을 의도적으로 제외한다.
    #
    # 종료 코드: 0=변경해 씀, 2=오류(die, 바이너리 제거 전에 중단), 3=바꿀 게 없음.
    python3 - "$1" <<'PY'
import json, os, re, shutil, sys, tempfile

OMHC_CMD = re.compile(r'''(^|[/\s"'])omhc["']?\s+(mark|brief)(\s|$|["'])''')

def is_omhc_hook(h):
    return isinstance(h, dict) and isinstance(h.get("command"), str) \
        and OMHC_CMD.search(h["command"]) is not None

def fail(msg):
    print("omhc uninstall: %s" % msg, file=sys.stderr)
    sys.exit(2)

def main():
    target = sys.argv[1]
    try:
        with open(target, encoding="utf-8") as fh:
            conf = json.load(fh)
    except (OSError, ValueError) as e:
        fail("%s 파싱 실패, 건드리지 않음: %s" % (target, e))

    if not isinstance(conf, dict):
        fail("%s 최상위가 객체가 아님, 건드리지 않음" % target)

    hooks = conf.get("hooks")
    if hooks is None:
        sys.exit(3)
    if not isinstance(hooks, dict):
        fail("%s 의 hooks 가 객체가 아님, 건드리지 않음" % target)

    groups = hooks.get("SessionStart")
    if groups is None:
        sys.exit(3)
    if not isinstance(groups, list):
        fail("%s 의 hooks.SessionStart 가 배열이 아님, 건드리지 않음" % target)

    changed = False
    kept_groups = []
    for g in groups:
        # 모양이 기대(그룹은 객체, hooks 는 배열)를 벗어나면 안전하게 건너뛰지
        # 않고 fail-closed 한다 — guard.py 와 같은 원칙: 못 알아보는 기계적
        # 형태를 함부로 그대로 두거나 고쳐 쓰지 않는다.
        if not isinstance(g, dict):
            fail("%s 의 SessionStart 그룹이 객체가 아님, 건드리지 않음" % target)
        if not isinstance(g.get("hooks"), list):
            fail("%s 의 SessionStart 그룹의 hooks 가 배열이 아님, 건드리지 않음" % target)
        kept_hooks = [h for h in g["hooks"] if not is_omhc_hook(h)]
        if len(kept_hooks) != len(g["hooks"]):
            changed = True
        if kept_hooks:
            ng = dict(g)
            ng["hooks"] = kept_hooks
            kept_groups.append(ng)
        # else: 그룹 전체가 omhc 훅뿐이었다 — 그룹째 드롭

    if not changed:
        sys.exit(3)  # 바꿀 게 없었다 — 백업/재기록 생략

    if kept_groups:
        hooks["SessionStart"] = kept_groups
    else:
        del hooks["SessionStart"]
    if not hooks:
        # hooks 가 SessionStart 하나만 들고 있었다면 이제 빈 객체다 — 남겨두면
        # 아무 것도 설치한 적 없는 설정에 {"hooks": {}} 만 흔적으로 남는다.
        del conf["hooks"]

    shutil.copy2(target, target + ".omhc-bak")  # 권한 비트도 원본과 같게

    # 원자적 교체: 서로게이트 등 인코딩 실패로 파일이 잘려나가지 않도록 먼저
    # 메모리에서 완전히 인코딩한 뒤 같은 디렉터리의 임시 파일에 쓰고
    # os.replace 한다. target 이 심링크면 os.path.realpath 로 그 대상 파일을
    # 바꿔치기해 심링크 자체는 유지한다.
    real_target = os.path.realpath(target)
    try:
        text = json.dumps(conf, indent=2, ensure_ascii=False) + "\n"
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(conf, indent=2, ensure_ascii=True) + "\n"
        data = text.encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(real_target) or ".", prefix=".omhc-tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp_path, os.stat(real_target).st_mode & 0o777)  # mkstemp 는 0600 — 원래 권한을 잃지 않는다
        except OSError:
            pass
        os.replace(tmp_path, real_target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    sys.exit(0)

try:
    main()
except SystemExit:
    raise
except Exception as e:
    fail("예상치 못한 오류, 건드리지 않았을 수 있다: %s" % e)
PY
  }

  for f in "$claude_settings" "$codex_hooks"; do
    if [ ! -f "$f" ]; then
      echo "omhc uninstall: $f 없음, 건너뜀"
      continue
    fi
    set +e
    strip_omhc_hooks "$f"
    rc=$?
    set -e
    case $rc in
      0) echo "omhc uninstall: $f 에서 omhc 의 SessionStart 훅 제거, 백업 $f.omhc-bak" ;;
      3) echo "omhc uninstall: $f 에 omhc 훅 없음" ;;
      *) echo "omhc uninstall: $f 처리 실패, 손대지 않았다 (바이너리도 지우지 않는다)" >&2; exit 2 ;;
    esac
  done

  if [ -L "$bin/omhc" ]; then
    target=$(readlink "$bin/omhc" 2>/dev/null || true)
    case "$target" in
      "$share"/*) rm -f "$bin/omhc"; echo "omhc uninstall: $bin/omhc 제거" ;;
      *) echo "omhc uninstall: $bin/omhc 은 omhc 가 만든 심링크가 아니라 건너뜀" ;;
    esac
  else
    echo "omhc uninstall: $bin/omhc 없음, 건너뜀"
  fi

  if [ -d "$share" ]; then
    rm -rf "$share"
    echo "omhc uninstall: $share 제거"
  else
    echo "omhc uninstall: $share 없음, 건너뜀"
  fi

  if [ "${OMHC_PURGE:-}" = "1" ]; then
    rm -rf "$HOME/.omhc"
    echo "omhc uninstall: OMHC_PURGE=1 — ~/.omhc(아카이브) 도 제거"
  else
    echo "omhc uninstall: ~/.omhc(아카이브) 는 남겨둠 (지우려면 OMHC_PURGE=1)"
  fi

  exit 0
fi

ver=${OMHC_VERSION:-}
if [ -z "$ver" ]; then
  ver=$(curl -fsSL "https://api.github.com/repos/$repo/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1) || true
  [ -n "$ver" ] || die "최신 릴리스를 찾지 못했다. OMHC_VERSION=v0.1.0 처럼 지정하라"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# 파이프로 바로 풀지 않는다: sh 에는 pipefail 이 없어 curl 실패가 가려진다.
curl -fsSL -o "$tmp/src.tar.gz" "https://github.com/$repo/archive/refs/tags/$ver.tar.gz" \
  || die "$ver 를 받지 못했다 (그런 태그가 있는지 확인하라)"
mkdir "$tmp/src"
tar -xzf "$tmp/src.tar.gz" -C "$tmp/src" || die "$ver 압축을 풀지 못했다"
set -- "$tmp/src"/*
[ -x "$1/bin/omhc" ] || die "받은 소스에 bin/omhc 가 없다"

mkdir -p "$share" "$bin"
rm -rf "$share/$ver"
mv "$1" "$share/$ver"
ln -sfn "$ver" "$share/current"
ln -sfn "$share/current/bin/omhc" "$bin/omhc"

# 구버전 정리: current 가 가리키는 것만 남긴다. v[0-9]* 패턴의 실제 디렉터리만
# 지우고 current 심링크 자신은 절대 건드리지 않는다 (#15).
for d in "${share:?}"/v[0-9]*; do
  [ -d "$d" ] || continue
  [ "$(basename "$d")" = "$ver" ] && continue
  rm -rf "$d"
done

echo "omhc $ver 설치됨 → $bin/omhc"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "주의: $bin 이 PATH 에 없다. 셸 설정에 export PATH=\"\$HOME/.local/bin:\$PATH\" 를 추가하라" ;;
esac
echo "다음: omhc hooks install 로 훅을 설치한 뒤 Codex 에서 그 훅을 신뢰 승인하고 omhc status"
# install.sh 는 main 브랜치에서 서빙되지만 $ver 는 더 오래된 릴리스일 수
# 있다 — hooks install 이 아직 없는 버전을 받았을 수 있으므로 손 병합
# 경로도 함께 보여준다(#7 리뷰 7).
echo "      (구버전이라 'hooks install' 명령이 없다면 $share/current/hooks/*.json 을 각자 설정에 손으로 병합하라)"
