#!/bin/sh
# omhc 설치 스크립트. GitHub 릴리스 소스를 ~/.local/share/omhc/<버전> 에 풀고
# ~/.local/bin/omhc 로 심링크한다. hooks/ 조각이 가리키는 경로와 같다.
# pip 을 쓰지 않는다 — 의존성이 0 이라 소스 트리가 곧 설치물이다.
#
#   curl -fsSL https://raw.githubusercontent.com/SungJun1217/oh-my-harness-cowork/main/install.sh | sh
#   curl -fsSL .../install.sh | OMHC_VERSION=v0.1.0 sh      # 특정 버전
#
# 다시 실행하면 업데이트다. 제거: rm -rf ~/.local/share/omhc ~/.local/bin/omhc
set -eu

repo=SungJun1217/oh-my-harness-cowork
share=$HOME/.local/share/omhc
bin=$HOME/.local/bin

die() { echo "omhc install: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 가 없다 (3.9 이상 필요)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || die "python3 $(python3 -c 'import platform; print(platform.python_version())') — 3.9 이상이 필요하다"

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

echo "omhc $ver 설치됨 → $bin/omhc"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "주의: $bin 이 PATH 에 없다. 셸 설정에 export PATH=\"\$HOME/.local/bin:\$PATH\" 를 추가하라" ;;
esac
echo "다음: hooks/ 의 조각을 각 하네스 설정에 병합한 뒤 omhc status"
