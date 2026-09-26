#!/usr/bin/env bash
# Check that the hook path quietly exits 0 on adversarial input.
#
# Breaking session start is this tool's worst outcome. Failing to inject
# anything is nothing by comparison. So what's required here isn't
# "success" — it's "empty stdout + exit 0".
#
# Usage: bash tests/smoke.sh
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
  check_cmd "$name" "$stdin" brief --harness claude-code
}

# `omhc turn` (v2 phase 2, #42) is a second hook path with the same
# invariant 2 requirement (empty stdout + exit 0 on anything), routed
# through bin/omhc's own dedicated branch instead of omhc.cli — so it's
# exercised through the same adversarial inputs, not assumed safe by association.
check_turn() {
  local name="$1" stdin="$2"
  check_cmd "$name" "$stdin" turn --harness claude-code
}

check_cmd() {
  local name="$1" stdin="$2"; shift 2
  local out rc
  out="$(cd "$REPO" && printf '%s' "$stdin" | "$OMHC" "$@" 2>/dev/null)"
  rc=$?
  if [ "$rc" -eq 0 ] && [ -z "$out" ]; then
    printf 'PASS  %s\n' "$name"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %s (rc=%s, stdout=%dB)\n' "$name" "$rc" "${#out}"
    FAIL=$((FAIL + 1))
  fi
}

# 1) no ledger
setup
check "ledger missing" "$PAYLOAD"
teardown

# 2) ledger with only a corrupt half-line
setup
mkdir -p "$HOME/.omhc"
printf '{"repo":"x","harn\n' > "$HOME/.omhc/ledger.jsonl"
check "ledger with a corrupt half-line" "$PAYLOAD"
teardown

# 3) transcript_path does not exist
setup
mkdir -p "$HOME/.omhc"
KEY="$("$OMHC" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["repo_key"])')"
printf '{"repo":"%s","harness":"codex-cli","session":"gone","event":"start","epoch":1,"path":"/nope/missing.jsonl","cwd":"%s"}\n' \
  "$KEY" "$REPO" > "$HOME/.omhc/ledger.jsonl"
check "transcript_path does not exist" "$PAYLOAD"
teardown

# 4) transcript_path is /dev/null
setup
mkdir -p "$HOME/.omhc"
KEY="$("$OMHC" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["repo_key"])')"
printf '{"repo":"%s","harness":"codex-cli","session":"null","event":"start","epoch":1,"path":"/dev/null","cwd":"%s"}\n' \
  "$KEY" "$REPO" > "$HOME/.omhc/ledger.jsonl"
check "transcript_path is /dev/null" "$PAYLOAD"
teardown

# 5) HOME is unwritable
setup
export HOME=/proc/omhc-nonexistent
check "HOME is unwritable" "$PAYLOAD"
teardown

# 6) stdin is broken JSON
setup
check "stdin is broken json" "{not json at all"
teardown

# 7) stdin is empty
setup
check "stdin is empty" ""
teardown

# 8) cwd is "/" (rejected root)
setup
check "cwd is /" '{"cwd":"/","session_id":"smoke-1"}'
teardown

# --- omhc turn (v2 phase 2, #42) --------------------------------------------

# 9) turn: stdin is broken json
setup
check_turn "turn: stdin is broken json" "{not json at all"
teardown

# 10) turn: stdin is empty
setup
check_turn "turn: stdin is empty" ""
teardown

# 11) turn: cwd is "/" (rejected root)
setup
check_turn "turn: cwd is /" '{"cwd":"/","session_id":"smoke-1","transcript_path":"/x.jsonl"}'
teardown

# 12) turn: transcript_path does not exist
setup
check_turn "turn: transcript_path does not exist" \
  "{\"cwd\":\"$REPO\",\"session_id\":\"smoke-1\",\"transcript_path\":\"/nope/missing.jsonl\"}"
teardown

# 13) turn: ledger with only a corrupt half-line
setup
mkdir -p "$HOME/.omhc"
printf '{"repo":"x","harn\n' > "$HOME/.omhc/ledger.jsonl"
check_turn "turn: ledger with a corrupt half-line" \
  "{\"cwd\":\"$REPO\",\"session_id\":\"smoke-1\",\"transcript_path\":\"/dev/null\"}"
teardown

# 14) turn: HOME is unwritable
setup
export HOME=/proc/omhc-nonexistent
check_turn "turn: HOME is unwritable" \
  "{\"cwd\":\"$REPO\",\"session_id\":\"smoke-1\",\"transcript_path\":\"/dev/null\"}"
teardown

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
