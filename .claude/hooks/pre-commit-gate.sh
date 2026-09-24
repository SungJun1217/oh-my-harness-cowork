#!/usr/bin/env bash
# PreToolUse(Bash): git commit 직전에 unittest + smoke 를 돌리고, 실패하면 커밋을 막는다(exit 2).
cmd="$(jq -r '.tool_input.command // empty')"
printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit([[:space:]]|$)' || exit 0
cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}" || exit 0
if ! out="$(python3 -m unittest discover -s tests -t . -q 2>&1)"; then
  echo "Commit blocked: unit tests failed." >&2
  echo "$out" | tail -40 >&2
  exit 2
fi
if ! out="$(bash tests/smoke.sh 2>&1)"; then
  echo "Commit blocked: tests/smoke.sh failed." >&2
  echo "$out" | tail -40 >&2
  exit 2
fi
exit 0
