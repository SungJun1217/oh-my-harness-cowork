#!/usr/bin/env bash
# PostToolUse(Edit|Write): 고친 .py 파일을 이 머신의 python3(3.9) 문법으로 파싱한다.
# 3.10+ 전용 문법(match 등)이 여기서 걸린다. 바이트코드를 쓰지 않도록 py_compile 대신 ast 를 쓴다.
f="$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')"
case "$f" in *.py) ;; *) exit 0 ;; esac
[ -f "$f" ] || exit 0
if ! out="$(python3 -c 'import ast,sys; p=sys.argv[1]; ast.parse(open(p,encoding="utf-8").read(), p)' "$f" 2>&1)"; then
  echo "Python 3.9 syntax check failed for $f:" >&2
  echo "$out" | tail -5 >&2
  exit 2
fi
exit 0
