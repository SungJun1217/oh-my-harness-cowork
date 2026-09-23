#!/usr/bin/env bash
# 적대적 입력에서 훅 경로가 조용히 exit 0 으로 끝나는지 본다.
#
# 세션 시작을 깨뜨리는 것이 이 도구의 최악 결과다. 아무것도 주입하지 못하는 것은
# 그에 비해 아무 일도 아니다. 그래서 여기서 요구하는 것은 "성공"이 아니라
# "빈 stdout + exit 0" 이다.
#
# 사용: bash tests/smoke.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OMHC="$ROOT/bin/omhc"
PASS=0
FAIL=0

setup() {
  WORK="$(mktemp -d)"
  export HOME="$WORK/home"
  mkdir -p "$HOME" "$WORK/repo"
  git -C "$WORK/repo" init -q
  unset OMHC_OFF
  REPO="$(cd "$WORK/repo" && pwd -P)"
  PAYLOAD="{\"cwd\":\"$REPO\",\"session_id\":\"smoke-1\"}"
}

teardown() { rm -rf "$WORK"; }

check() {
  local name="$1" stdin="$2"
  local out rc
  out="$(cd "$REPO" && printf '%s' "$stdin" | "$OMHC" brief --harness claude-code 2>/dev/null)"
  rc=$?
  if [ "$rc" -eq 0 ] && [ -z "$out" ]; then
    printf 'PASS  %s\n' "$name"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %s (rc=%s, stdout=%dB)\n' "$name" "$rc" "${#out}"
    FAIL=$((FAIL + 1))
  fi
}

# 1) 원장 없음
setup
check "ledger missing" "$PAYLOAD"
teardown

# 2) 원장에 깨진 반줄만 있음
setup
mkdir -p "$HOME/.omhc"
printf '{"repo":"x","harn\n' > "$HOME/.omhc/ledger.jsonl"
check "ledger with a corrupt half-line" "$PAYLOAD"
teardown

# 3) transcript_path 가 존재하지 않음
setup
mkdir -p "$HOME/.omhc"
KEY="$("$OMHC" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["repo_key"])')"
printf '{"repo":"%s","harness":"codex-cli","session":"gone","event":"start","epoch":1,"path":"/nope/missing.jsonl","cwd":"%s"}\n' \
  "$KEY" "$REPO" > "$HOME/.omhc/ledger.jsonl"
check "transcript_path does not exist" "$PAYLOAD"
teardown

# 4) transcript_path 가 /dev/null
setup
mkdir -p "$HOME/.omhc"
KEY="$("$OMHC" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["repo_key"])')"
printf '{"repo":"%s","harness":"codex-cli","session":"null","event":"start","epoch":1,"path":"/dev/null","cwd":"%s"}\n' \
  "$KEY" "$REPO" > "$HOME/.omhc/ledger.jsonl"
check "transcript_path is /dev/null" "$PAYLOAD"
teardown

# 5) HOME 이 쓰기 불가
setup
export HOME=/proc/omhc-nonexistent
check "HOME is unwritable" "$PAYLOAD"
teardown

# 6) stdin 이 깨진 JSON
setup
check "stdin is broken json" "{not json at all"
teardown

# 7) stdin 이 비어 있음
setup
check "stdin is empty" ""
teardown

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
