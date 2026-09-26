[English](status.md) · **한국어** · [← README](../README.ko.md)

# `omhc status`가 확인하는 것

`omhc status`의 모든 행은 세 라벨 중 하나입니다. 실제로 판정되어 exit
code를 게이팅할 수 있는 **PASS**/**FAIL**(adapters, archive, instruction
files, `ledger rejects`, `codex hook` 같은 어댑터 health 행), 그리고
정보성이거나 아직 판단할 근거가 없는 행을 위한 **`----`**(ledger, off
switch, pull rate, watcher)입니다. `----`는 게이팅하지 않습니다. SKIP은
없습니다.

| 행 | 보여 주는 것 |
|---|---|
| `adapters` | 이 머신에서 감지된 하네스. 하나도 없으면 FAIL |
| `ledger` | 이 레포에 기록된 세션 시작 수(`----`) |
| `ledger rejects` | 너무 길어서 버려진 ledger 행. [아래](#ledger-rejects) 참고 |
| `archive` | 아카이브한 세션마다 아직 따라잡지 못한 꼬리(`tail=…B`). 핸드오프는 전달됐는데 아카이브된 것이 없으면 FAIL |
| `off switch` | `OMHC_OFF`나 `off` 파일로 omhc가 꺼져 있는지(`----`) |
| `instruction files` | `AGENTS.md`를 `CLAUDE.md`와 공유하는지(공유하면 Path B 대신 outbox로 떨어짐). 오래된 omhc block이 Claude Code로 샐 상황이면 FAIL |
| `codex hook`, `codex root markers`, `codex agents.md budget` | Codex 상태 행. 아래 참고 |
| `<adapter-id> hooks` | omhc 훅이 그 하네스 설정에 병합돼 있는지. [아래](#adapter-id-hooks) 참고 |
| `pull rate` | 최근 핸드오프 중 실제로 파본 것의 수. [아래](#pull-rate) 참고 |
| `watcher (optional)` | `omhc watch` 데몬이 도는지(`----`) |

행 다음의 `events`는 인덱싱된 이벤트를 verb별로 세고, `artifact`는 마지막으로
만든 핸드오프(상태 디렉터리의 `omhc.txt`) 크기를 보여 줍니다. `--json`은 같은 행과 원래 숫자를 함께
출력합니다.

- [ledger rejects](#ledger-rejects)
- [codex hook](#codex-hook)
- [codex root markers](#codex-root-markers)
- [codex agents.md budget](#codex-agentsmd-budget)
- [`<adapter-id>` hooks](#adapter-id-hooks)
- [pull rate](#pull-rate)

## ledger rejects

ledger는 세션 시작마다 JSON 한 줄을 덧붙이고, 그 한 줄은 반드시
`write(2)` 호출 한 번 안에 들어가야 합니다(append-only라 잠금이 필요
없습니다. `O_APPEND` fd에 대한 단일 `write(2)`는 크기와 무관하게 POSIX
상 원자적이고, 이건 `PIPE_BUF`와는 무관합니다. `PIPE_BUF`는 파이프
전용입니다). 그 상한을 넘는 행은 잘라내지 않고 통째로 버립니다
(`path`/`session`을 자르면 아무것도 가리키지 않는 줄이 조용히 남기
때문입니다). 대신 버렸다는 사실 자체를 남겨 안 보이게 사라지지 않게
합니다. 같은 세션이 재시도돼도 `mark`마다 또 남기지 않고 한 번만
기록합니다. `ledger rejects`는 이 레포에서 최근 7일 안에 버려졌고
지금 상한으로도 여전히 못 들어가는 행이 있으면 FAIL, 없으면 `----`
입니다. `omhc clear`로 이 레포의 기록을 지울 수 있습니다(예: 상한을
올려 고친 뒤).

## codex hook

omhc Codex 훅이 설치돼 있으면 붙는 행입니다. `hooks.json`이 마지막으로
바뀐 뒤 이 레포의 가장 최근 대화형 Codex 세션이 훅을 돌리지
않았으면(신뢰되지 않은 훅) FAIL이고, 그 세션의 originator를 함께
보여줍니다. 이때 Claude→Codex는 전달되지 않지만 Codex→Claude는 Claude
쪽 `mark`의 backfill로 계속 동작합니다. 아직 판정할 수 없으면 `----`
입니다. `hooks.json`이 바뀐 뒤 이 레포에 대화형 Codex 세션이 없을
때(날짜 표시), 헤드리스 `codex exec` 세션만 있을 때(판정에 세지
않습니다. 여기서 대화형 `codex`를 한 번 여십시오), 또는 알 수 없는
오류일 때가 그렇습니다.

## codex root markers

레포 루트가 `.omhc-root`로 정해진 경우(`.git`이 아니고, 조상
디렉터리에도 `.git`이 전혀 없는 경우. 있다면 Codex 기본값이 거기서부터
이미 내려오며 읽습니다), 이 행이 `~/.codex/config.toml`의
`project_root_markers`에 `.omhc-root`가 있는지 봅니다([Codex: git이
아닌 프로젝트](install.ko.md#codex-git이-아닌-프로젝트) 참고). 이 행은
절대 게이팅하지 않습니다. 이 설정은 Path B(AGENTS.md 폴백)에만 영향을
주므로, 마커가 있으면 PASS, 없으면 넣을 정확한 줄과 함께 `----`입니다.
omhc Codex 훅이 설치돼 있지 않으면 "지금 Path B가 Codex로 가는 유일한
채널"이라는 사실도 명시적으로 덧붙입니다. 설정 파일이 없거나, 못
읽거나, 파싱할 수 없을 때(omhc가 직접 고치는 일은 없습니다), 그리고
키가 `[section]` 안에서만 보일 때(TOML 테이블은 키의 범위를 바꿉니다.
최상위에 있어야 합니다)도 모두 `----`입니다.

## codex agents.md budget

omhc managed block이 `AGENTS.md`에 지금 설치돼 있으면 이 행이 그 block이
실제로 끝나는 바이트 오프셋을 `~/.codex/config.toml`의
`project_doc_max_bytes`(설정이 없거나 못 읽으면 기본값 32768)와
비교합니다. block이 그 한도를 넘겨 끝나면 FAIL(게이팅)이고 오프셋과
한도를 함께 보여줍니다. Codex가 그 block을 못 볼 것이기 때문입니다.
설치된 block이 없으면 `----`입니다. Path B 자신도 이 검사를 통과하지
못할 걸 미리 아는 block은 아예 쓰지 않습니다. 성공을 주장하지 않고
outbox로 떨어지며, 그 사유를 `guard.log`에 남깁니다([Codex: AGENTS.md
크기 한도](install.ko.md#codex-agentsmd-크기-한도) 참고).

## `<adapter-id>` hooks

감지된 하네스마다 `<adapter-id> hooks` 행(예: `claude-code hooks`,
`codex-cli hooks`)도 붙습니다. 하네스 디렉터리가 존재한다는 것만이
아니라 omhc의 SessionStart 훅이 그 하네스 자신의 설정에 실제로
병합돼 있는지를 봅니다. 판정은 구조적입니다(바이트 단위 문자열 비교가
아니라 argv로 쪼개 비교합니다). 절대경로, `~`, `${HOME}`, 따옴표로
감싼 명령, `PATH` 상의 bare `omhc` 모두 설치된 것으로 인정됩니다.
설정 파일이 없으면(`omhc hooks install`을 처방으로 보여줍니다) 또는
파싱이 안 되면, `mark`/`brief` 명령이 배포된 fragment가 기대하는
순서·플래그대로 있지 않으면(낡은 `--wire sdk`, `mark` 누락, `mark`
보다 앞선 `brief` 등. 이때도 `omhc hooks install`을 처방으로
보여줍니다), 또는 훅의 바이너리를 찾을 수 없거나 실행 가능하지 않으면
FAIL(게이팅)입니다. 설치된 명령이 배포된 fragment와 구조적으로 일치하고
바이너리가 실행 가능하면 PASS입니다. `config.toml`에 적은 Codex 설치도
인정합니다([Codex: config.toml의 훅](install.ko.md#codex-configtoml의-훅)
참고).

## pull rate

**pull rate**("pulled X of N recent injections")는 omhc의 부담이 값을
하는지 판단할 유일한 숫자입니다. N은 이 레포에 전달된 가장 최근
`PULL_RATE_WINDOW`(20)개 세션이고, X는 그중 `omhc show`, `omhc log`,
`omhc trace`로 실제로 파본 세션 수입니다(같은 세션을 여러 번 파봐도
한 번만 셉니다). 사람이 손으로 `omhc log`를 돌려도 셈에 들어갑니다.
에이전트뿐 아니라 사람의 조회도 pull로 칩니다. 창은 전달된 전체 역사가
아니라 가장 최근 전달들(append 순서, 세션 id로 매칭)만 봅니다. 그러지
않으면 오래 쓴 레포에서 더 이상 안 파보는 옛 전달들이 분모에 계속
쌓여 pull rate가 끝없이 낮아 보입니다.
