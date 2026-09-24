<div align="center">

[English](README.md) · **한국어**

# omhc

**Claude Code ↔ Codex CLI, 갈아타도 맥락이 0으로 돌아가지 않게.**

<sub>SessionStart 훅 하나 · 900바이트 핸드오프 · 원본 하드링크 아카이브</sub>

![python 3.9+](https://img.shields.io/badge/python-3.9%2B-A3968C?style=flat-square)
![dependencies 0](https://img.shields.io/badge/dependencies-0-3F8F6E?style=flat-square)
![LLM calls 0](https://img.shields.io/badge/LLM%20calls-0-3F8F6E?style=flat-square)
![handoff ≤900 bytes](https://img.shields.io/badge/handoff-%E2%89%A4900%20bytes-F0A45C?style=flat-square)
[![tests](https://github.com/SungJun1217/oh-my-harness-cowork/actions/workflows/test.yml/badge.svg)](https://github.com/SungJun1217/oh-my-harness-cowork/actions/workflows/test.yml)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/handoff-dark.svg">
  <img src="assets/handoff-light.svg" width="100%" alt="Claude Code 터미널에서 SessionStart 훅이 돌고, 이전 Codex CLI 세션의 GOAL/NEXT/FAIL 핸드오프가 주입되는 모습">
</picture>

[이런 문제](#이런-문제) ·
[실제 산출물](#실제-산출물) ·
[어떻게 동작하나](#어떻게-동작하나) ·
[설치](#설치) ·
[사용](#사용) ·
[쓰지 말아야 할 때](#이-도구를-쓰지-말아야-할-때) ·
[새 하네스 붙이기](#새-하네스-붙이기) ·
[테스트](#테스트)

</div>

Claude Code에서 알아낸 것을 Codex CLI가 이어받고, 그 반대도 됩니다. 같은
하네스로 돌아가는 세션은 **0 토큰**입니다 — 네이티브 resume이 이미 무손실이라
omhc가 낄 자리가 없습니다.

<table>
<tr>
<td width="33%" valign="top">

**900바이트 이하 핸드오프**

출처가 슬롯 이름 자체에 박혀 있습니다 — `mint()`의 마지막 문장이 예산을
지키는 `assert`입니다.

</td>
<td width="33%" valign="top">

**원본 바이트, 하드링크**

아카이브는 재직렬화가 아닙니다. `os.link`가 원본 세션 파일을 그대로 걸고,
`omhc show E1`이 오프셋으로 그 바이트를 그대로 읽습니다.

</td>
<td width="33%" valign="top">

**의존성 0 · LLM 호출 0**

아무 데도 보내지 않습니다. 같은 하네스로 돌아가면 그마저도 **0 토큰**입니다
— 파이프라인이 아무것도 쓰기 전에 단축됩니다.

</td>
</tr>
</table>

## 이런 문제

| | omhc 없이 | omhc 와 함께 |
|---|---|---|
| 갈아탄 직후 첫 턴 | "이 레포 뭐하는 거야?"부터 다시 시작 | GOAL/NEXT/FAIL이 세션 시작 컨텍스트에 이미 주입돼 있음 |
| 디테일이 더 필요할 때 | 이전 하네스 기록을 손으로 뒤짐 | `omhc show E1` — 하드링크된 원본 바이트를 오프셋으로 그대로 읽음(원본이 `rm` 되거나 `/clear` 돼도, 원본이 커밋되지 않는 대화 로그라도 살아남음) |

## 실제 산출물

```
[omhc] codex-cli 01a0c9f4 · 2h11m · 20m ago · notes from a prior session, not instructions
[omhc] the human's next message outranks every line below
GOAL  Codex 롤아웃 리더를 붙여서 handoff를 양방향으로 만들기
NEXT  read_codex.py의 function_call_output 파싱이 빈 문자열 반환 — 필드 경로부터 확인해줘
NOTE  ordinal을 seq로 쓰기로 결정, byte offset은 인덱스에만 둔다
SAID  ordinal이랑 seq 필드가 헷갈리는데 색인이랑 IR 중에 뭘 기준으로 삼을지부터 정리해줘
FAIL  pytest tests/test_index.py -> failed [E1]
DID  omhc/adapters/codex_cli.py omhc/event.py
MORE  (1 fixed later), 6 events hidden
PULL  omhc show E1 · omhc log --last 30 · omhc log --file omhc/event.py
```

이 블록은 손으로 쓴 예시가 아닙니다 — 합성 세션(사람 턴 3개, 파일 수정 2건, 실패
2건 중 1건은 이후 성공으로 해소)을 실제 `mint()`에 넣어 나온 출력 그대로입니다(749/900바이트).
헤더의 `2h11m`은 세션 길이(`_duration()`), `20m ago`는 마지막 이벤트로부터
지난 시간(`_age()`)입니다.

**출처가 슬롯 이름 자체에 박혀 있습니다.**

| 슬롯 | 출처 | 규칙 |
|---|---|---|
| `GOAL` | `author == human`, 세션의 첫 사람 턴 | 축자 인용만. 재작성하지 않는다 |
| `NEXT` | `author == human`, 세션의 마지막 사람 턴(단, 사람 턴이 하나뿐이면 그것은 이미 `GOAL`이므로 비운다) | 축자 인용만. **단, 그 턴이 짧은 승인("계속 진행해")이면 비운다** — 그것을 `NEXT`로 쓰면 이전 에이전트의 제안이 사람의 지시로 세탁된다 |
| `PLAN?` | 이전 에이전트의 마지막 발화 | `NEXT`가 비었을 때만 채운다. `?` 한 바이트가 "검증되지 않은 주장" 라벨 |
| `FAIL` | 기계가 관측한 실패(`ok=False`) | **"해소됨" 판정은 "같은 인자"가 아니라 인자 앞 40자가 같은 이후 성공**이다 — 그러면 보고하지 않는다. 리포트 대상은 최대 2개, 태그 `[E1]` `[E2]`로 `omhc show`와 연결 |
| `DID` | 기계가 관측한 수정 경로 | 레포 루트 상대경로, 최대 4개 |
| `NOTE` | `omhc note "<text>"` 호출 — 사람과 양쪽 하네스의 에이전트 모두 명령줄로 부를 수 있고 저자를 구분해 기록하지 않는다 | **검증되지 않은 자유 텍스트.** `~/.omhc/<repo-key>/notes.txt`에서 최근 2개 |
| `SAID` | `author == human`, 중간 사람 턴 | 최근순이 아니라 긴 문장 우선(최대 3개) — "어디까지 됐어?" 같은 질문보다 요구사항 문장이 쓸모 있다 |
| `MORE` | 버려진 슬롯·해소된 실패·숨겨진 이벤트의 집계 | **감춘 것을 공개한다.** 버린 슬롯 수는 900바이트 예산 때문이지만, 숨긴 이벤트 수는 예산과 무관하다 — 사람 턴도 아니고 `FAIL`로 리포트되지도 않은 이벤트 수(DID 등으로 요약됐어도 낱개로는 안 보인다)를 그냥 센 것이다 |
| `PULL` | omhc 생성(항상 포함) | `omhc log --last 30`은 늘 있고, 미해소 실패가 있으면 `omhc show E1`, 수정한 파일 중 가장 짧은 경로가 32자 이하면 `omhc log --file …`이 붙습니다. 절대 버리지 않는 슬롯 |

## 어떻게 동작하나

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/flow-dark.svg">
  <img src="assets/flow-light.svg" width="100%" alt="SessionStart 훅이 mark 와 brief 를 부르고, due() 가 상대 하네스의 최신 세션을 고르고, 화이트리스트 파서가 Event 를 만들고, mint() 가 900바이트 이하 핸드오프를 렌더링하고, gate() 가 세션당 한 번만 통과시키고, 아카이브가 원본을 하드링크하며 오프셋 색인을 남기는 그림">
</picture>

**Claude→Codex 는 여전히 Codex의 SessionStart 훅이 신뢰되어 돌아야
합니다** — `brief`는 그 훅 안에서만 돌고, 훅이 안 돌면 Codex 쪽은 애초에
주입할 기회 자체가 없습니다. **Codex→Claude 는 더 이상 거기 매이지
않습니다.** `due()`가 상대 세션을 고르려면 그 세션의 시작이 원장에 있어야
하고, 보통은 그 하네스 자신의 훅만 그 행을 남기지만, 이제 Claude 의
`mark`가 다른 어댑터의 `discover()`도 함께 불러 롤아웃 파일에서 바로 Codex
세션을 찾아 원장에 채워 넣습니다(원장에 `via:"scan"`으로 표시). 그래서
Codex 자신의 훅이 신뢰된 적이 없어도 그 세션이 원장에 남습니다. 두 훅이
여전히 다른 점은 **신뢰 절차의 유무**입니다 — Claude Code 훅은 설정에
넣으면 그대로 돌지만, Codex 훅은 Codex 자신의 절차로 한 번 승인해야
합니다(아래 설치 절 경고 참고) — 그리고 그 승인은 여전히 Claude→Codex
방향을 살리는 유일한 방법입니다.

**두 개의 결정적 선택:**

- **주입물은 900바이트 고정 슬롯. 하드 캡.** 강제 수단이 규율이 아니라
  코드입니다 — `mint()`의 **마지막 문장이
  `assert len(out.encode('utf-8')) <= budget`**이라 함수가 초과 문자열을 반환할
  수 없고, 출력 직전에 한 번 더 검사해 실패하면 빈 문자열을 냅니다.
- **아카이브는 원본 파일 그 자체입니다.** 아무것도 재직렬화하지 않습니다.
  `os.link()`로 하네스 원본에 하드링크를 걸고 이벤트당 약 115바이트의 TSV
  오프셋 색인만 만듭니다. 실측: 3.2MB 세션의 275 이벤트가 31.5KB(원본의 1%).
  추가 디스크 0바이트, 같은 inode라 진행 중인 세션의 append도 보이고, 원본이
  `rm` 되거나 `/clear` 돼도 바이트가 살아남습니다.

<details>
<summary>전달 경로가 막히면</summary>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/delivery-dark.svg">
  <img src="assets/delivery-light.svg" width="100%" alt="전달 경로: SessionStart 훅이 신뢰되지 않으면 아무것도 전달되지 않고, 신뢰되면 Path A(install_handoff)를 시도한 뒤 Path B(AGENTS.md 관리 구간, Codex 전용, Claude Code 와 공유되는 레포에서는 끔), 마지막으로 자동으로 읽히지 않는 outbox 바닥으로 떨어지는 그림">
</picture>

</details>

## 설치

```bash
curl -fsSL https://raw.githubusercontent.com/SungJun1217/oh-my-harness-cowork/main/install.sh | sh
omhc status          # 모든 행이 PASS/FAIL/---- 중 하나(+ codex hook, 감지 시 <adapter-id> hooks). SKIP 은 없다
```

최신 릴리스를 `~/.local/share/omhc/<버전>` 에 풀고 `~/.local/bin/omhc` 로
심링크합니다. pip·pipx 를 쓰지 않습니다(의존성이 0 이라 소스 트리가 곧
설치물입니다). 다시 실행하면 업데이트(`~/.local/share/omhc` 아래 구버전은
`current` 가 가리키는 것만 남기고 자동 정리됩니다), 버전 고정은
`| OMHC_VERSION=v0.1.0 sh`.

제거는:

```bash
curl -fsSL https://raw.githubusercontent.com/SungJun1217/oh-my-harness-cowork/main/install.sh | sh -s -- --uninstall
```

omhc 자신의 `SessionStart` 훅만 `~/.claude/settings.json` 과
`~/.codex/hooks.json` 에서 제거합니다 — 같은 파일, 심지어 같은 훅 그룹
안의 다른 훅도 그대로 남습니다; JSON 은 재직렬화(2칸 들여쓰기)만 됩니다.
먼저 `<파일>.omhc-bak` 백업을 만듭니다. 이어서 `~/.local/bin/omhc` 와
`~/.local/share/omhc` 를 지웁니다. `~/.omhc`(아카이브·원장)는 남겨둡니다 —
이것까지 지우려면 `OMHC_PURGE=1`(파이프로도 가능:
`curl -fsSL .../install.sh | OMHC_PURGE=1 sh -s -- --uninstall`). 아무것도
설치되지 않았을 때도, 두 번 실행해도 안전합니다.

손대지 않는 것들:
- **Codex 훅 신뢰.** 신뢰 항목은 그룹/훅 인덱스로 키가 매겨져서, omhc 의
  그룹을 지우면 그 외 Codex `SessionStart` 훅들의 인덱스가 밀릴 수
  있습니다 — 제거 후 Codex 자신의 신뢰 절차로 다시 승인해야 할 수 있습니다.
- **레포별 잔여물.** 레포의 `AGENTS.md` 안 omhc 관리 구간과
  `<레포>/.omhc/outbox/`. 이것도 정리하려면 제거하기 *전에* 각 레포에서
  `omhc clear` 를 실행하십시오.

<details>
<summary>git 체크아웃에서 직접 쓰려면</summary>

```bash
git clone git@github.com:SungJun1217/oh-my-harness-cowork.git
cd oh-my-harness-cowork
ln -s "$PWD/bin/omhc" ~/.local/bin/omhc
```

</details>

훅 배선은 조각 파일을 각자 설정에 **병합**하십시오(덮어쓰지 말 것).
`curl \| sh` 로 설치했다면 `~/.local/share/omhc/current/hooks/` 아래,
git 체크아웃이라면 레포의 `hooks/` 아래에 있습니다.

| 하네스 | 파일 | 대상 |
|---|---|---|
| Claude Code | `claude-settings.fragment.json` | `~/.claude/settings.json` 의 `hooks` |
| Codex CLI | `codex-hooks.json` | `~/.codex/hooks.json` |

> [!WARNING]
> 실측(codex-cli 0.155.1): 손으로 떨어뜨린 `hooks.json`은 기본적으로
> 신뢰되지 않고, **신뢰되지 않은 훅은 메시지 없이 조용히 건너뛰어** Codex
> 쪽의 `mark`도 `brief`도 한 번도 돌지 않아 그 방향(**Claude→Codex**)으로는
> Codex 세션에 아무것도 주입되지 않습니다 — 그 방향을 고치려면 Codex 자신의
> 훅 신뢰 절차로 한 번 승인해야 합니다. **Codex→Claude** 는 이것 없이도
> 됩니다 — Claude 자신의 `mark`가 롤아웃 파일에서 바로 Codex 세션을 원장에
> 채워 넣습니다.
> 두 조각 모두 `--wire claude`를 씁니다 — `--wire sdk`(최상위
> `additionalContext`)는 codex-cli 0.155.1에서 `hook: SessionStart Failed`로
> 거부되고 아무것도 주입되지 않습니다.

`omhc status`의 모든 행은 세 라벨 중 하나입니다: 실제로 판정되어 exit code 를
게이팅할 수 있는 **PASS**/**FAIL**(adapters, archive, instruction files,
`codex hook` 같은 어댑터 health 행), 그리고 정보성이거나 아직 판단할 근거가
없는 행을 위한 **`----`**(ledger, off switch, pull rate, watcher) — `----`
는 게이팅하지 않습니다. omhc Codex 훅이 설치돼 있으면 `codex hook` 행이
붙습니다. `hooks.json`이 마지막으로 바뀐 뒤 이 레포의 가장 최근 대화형 Codex
세션이 훅을 돌리지 않았으면(신뢰되지 않은 훅) FAIL 이고, 그 세션의 originator 를
함께 보여줍니다. 이때 Claude→Codex 는 전달되지 않지만 Codex→Claude 는 Claude 쪽
`mark` 의 채우기로 계속 동작합니다. 아직 판정할 수 없으면 `----` 입니다:
`hooks.json`이 바뀐 뒤 이 레포에 대화형 Codex 세션이 없을 때(날짜 표시), 헤드리스
`codex exec` 세션만 있을 때(판정에 세지 않습니다 — 여기서 대화형 `codex` 를 한 번
여십시오), 알 수 없는 오류일 때.

감지된 하네스마다 `<adapter-id> hooks` 행(예: `claude-code hooks`,
`codex-cli hooks`)도 붙습니다 — 하네스 디렉터리가 존재한다는 것만이 아니라
omhc 의 SessionStart 훅이 그 하네스 자신의 설정에 실제로 병합돼 있는지를
봅니다. 판정은 구조적입니다(바이트 단위 문자열 비교가 아니라 argv 로 쪼개
비교합니다) — 절대경로, `~`, `${HOME}`, 따옴표로 감싼 명령, `PATH` 상의
bare `omhc` 모두 설치된 것으로 인정됩니다. 설정 파일이 없으면(`omhc hooks
install` 을 처방으로 보여줍니다) 또는 파싱이 안 되면, `mark`/`brief` 명령이
배포된 조각이 기대하는 순서·플래그대로 있지 않으면(낡은 `--wire sdk`,
`mark` 누락, `mark` 보다 앞선 `brief` 등 — 역시 `omhc hooks install` 을
처방으로 보여줍니다), 또는 훅의 바이너리를 찾을 수 없거나 실행 가능하지
않으면 FAIL(게이팅)입니다. 설치된 명령이 배포된 조각과 구조적으로 일치하고
바이너리가 실행 가능하면 PASS 입니다.

### AGENTS.md 를 Claude Code 와 공유하는 레포

> [!IMPORTANT]
> 권장 배치는 `AGENTS.md` 를 하네스 중립 원본으로 두고, `CLAUDE.md` 는 실제
> 파일로 `@AGENTS.md` 로 시작한 뒤 Claude 전용 내용을 잇는 것입니다(이 레포
> 자체가 그 구조입니다). `CLAUDE.md` 를 `AGENTS.md` 로의 심링크로 두는 것도
> 마찬가지로 공유입니다 — 어느 쪽이든 `AGENTS.md` 는 심링크여선 안 됩니다.

이런 레포에서는 omhc 가 `AGENTS.md` 에 절대 쓰지 않습니다. Codex 용 관리 구간
(Path B)이 Claude Code 세션에도 그대로 읽혀 핸드오프가 새고, 심링크를 통해 쓰면
공유·추적 중인 원본 파일이 바뀌기 때문입니다. Codex 로의 핸드오프는 Codex의
SessionStart 훅(Path A)으로 전달됩니다 — 이 훅이 신뢰되어 돌지 않으면 Path B/
outbox 가 대신 받는 게 아니라 Codex 쪽 `brief` 호출 자체가 없어 아무것도
전달되지 않으므로, **Codex 는 outbox 디렉터리를 자동으로 읽지도 않을뿐더러**
위 표의 Codex 훅을 반드시 설치해야 합니다(그리고 그 훅을 Codex 자신의 절차로
신뢰해야 합니다). `omhc status` 의 `instruction files` 행이 이 레이아웃을
보여줍니다.

## 사용

| 명령 | 역할 |
|---|---|
| `omhc status [--json]` | 유일한 사람용 대시보드. 아카이브 지연(`lag_bytes`, `tail=…B`)과 인출률 포함 |
| `omhc log [--last N] [--grep P] [--verb V] [--file P]` | 색인된 이벤트를 한 줄씩 |
| `omhc show <E1\|#137> [--full]` | **원본 바이트를 오프셋으로 조회** (tier (b) 진입점) |
| `omhc note "<text>"` | 메모. 두 하네스의 에이전트가 맨 명령줄로 호출 가능 |

**인출률**("pulled X of N injections")은 omhc 의 부담이 값을 하는지 판단할
유일한 숫자입니다. N 은 전달된 세션 수, X 는 그중 `omhc show`나 `omhc log`로
실제로 파본 세션 수입니다(같은 세션을 여러 번 파봐도 한 번만 셉니다) — 사람이
손으로 `omhc log`를 돌려도 셈에 들어갑니다, 에이전트뿐 아니라.

끄기: `OMHC_OFF=1` 또는 `~/.omhc/<repo-key>/off` 파일.

기본적으로 헤드리스 세션(`claude -p`, `codex exec`, 앱서버 클라이언트)과 Codex
서브에이전트 스레드는 핸드오프 원천이 되지 않습니다. 샌드박스에서 헤드리스 세션을
실제 세션처럼 쓰려면 `OMHC_ALLOW_HEADLESS=1`을 원천과 수신 양쪽 실행에 같은 값으로
export 하십시오(mark 와 brief 가 둘 다 읽습니다). 서브에이전트·사이드체인은 이것으로도
풀리지 않습니다.

## 이 도구를 쓰지 말아야 할 때

> [!TIP]
> **같은 하네스끼리는 네이티브 resume이 낫습니다.** Claude Code → Claude
> Code라면 `claude --resume <세션ID>`를 쓰십시오. 무손실이고 thinking 블록까지
> 보존됩니다.

omhc는 그것보다 **열등합니다** — 요약이기 때문입니다. 그래서 `from == to`면
파이프라인을 단축하고 아무것도 쓰지 않습니다.

omhc의 가치는 **벤더가 다를 때**입니다. 교차 벤더 재생은 thinking 블록 서명이
시스템 프롬프트와 선행 메시지까지 검증하므로 **원리적으로** 불가능합니다.

## 새 하네스 붙이기

v1은 어댑터 2개만 구현합니다. 3번째를 붙이는 비용은 **파일 하나 + 픽스처 하나**:

1. `omhc/adapters/<harness>.py` 에 메서드 5개(`detect`, `list_sessions`,
   `read_session`, `native_resume_hint`, `install_handoff`)를 구현하고
   `@_register` 를 붙인다
2. `omhc/adapters/__init__.py` 맨 아래에 `from . import <harness>` 한 줄
3. `tests/fixtures/<harness>/` 에 실물 세션 하나를 얼린다

코어 수정은 없습니다. 읽기와 쓰기는 독립 capability라서, 세션 훅이 없는 하네스는
**읽기 전용 어댑터가 정상 상태**이고 결함이 아닙니다. 그 하네스로의 `brief`가
실행됐는데 주입 경로가 없으면(또는 실패하면) `<repo>/.omhc/outbox/` 로 떨어지는
보편 바닥이 받습니다 — `brief` 자체가 안 도는 경우(예: 훅이 없거나 신뢰되지
않아 세션 시작 때 불리지 않음)는 outbox도 받지 못합니다.

## 테스트

```bash
python3 -m unittest discover -s tests -t . -q   # 약 12초, 하네스를 띄우지 않는다
bash tests/smoke.sh                             # 적대적 입력 7종
```

적합성 스위트(`tests/conformance/test_suite.py`)의 불변식 22개는 `REGISTRY` 위에
파라미터화됩니다 — **어댑터를 추가하면 테스트가 저절로 늘어납니다.**

~60개 테스트는 픽스처(`tests/fixtures/`, 커밋되지 않음)가 없으면 건너뜁니다.
각자 머신에서 `python3 tests/harvest.py [--force]` 로 실제 세션에서 생성하십시오.

<details>
<summary>더 보기: 알려진 한계, 실측 사실, 전달 경로 다이어그램, 덜 쓰는 명령</summary>

### 알려진 한계

- **Codex 0.144–0.148 세션에는 명령 사실이 없습니다.**
  <details>
  <summary>자세히</summary>

  Codex 매핑은 실제 rollout 194개(codex-cli 0.141–0.155.1)로 실측했고, 세 시기로
  나뉩니다. 0.141–0.142 는 셸 실행을 `exec_command` 함수 호출과 평문 종료 코드로,
  0.149 이후는 `CommandExecution` 항목으로 남깁니다. 0.144–0.148 은 셸 호출이
  어댑터가 일부러 파싱하지 않는 JavaScript 소스 안에만 있어서(화이트리스트,
  fail-closed), 그 세션은 수정 사항만 있고 `ran` 이벤트 없이 읽힙니다. 모르는
  레코드 종류는 여전히 `unparsed`로 계상됩니다.

  </details>

- **신뢰되지 않은 Codex 훅은 여전히 Claude→Codex 방향을 통째로 끕니다.**
  <details>
  <summary>자세히</summary>

  실측(codex-cli 0.155.1): 신뢰되지 않은 `hooks.json`은 메시지 없이 조용히
  건너뛰어 Codex 쪽의 `mark`도 `brief`도 한 번도 돌지 않습니다.
  `deliver()`(Path B 포함)는 `brief` 호출 안에서만 실행되므로, 훅이
  신뢰되지 않으면 Claude→Codex 방향은 `AGENTS.md` 관리 구간이나 outbox가
  대신 받는 게 아니라 아예 켜지지 않습니다. Path B/outbox는 `brief`가 실제로
  도는데 `install_handoff`가 실패할 때 열립니다 — 대표적으로
  `~/.codex/hooks.json`에 omhc 훅이 없을 때이지만, `~/.omhc` 쓰기 실패 등
  다른 예외도 같은 경로를 탑니다(예: 수동 `omhc brief --harness codex-cli`).

  Codex→Claude 는 더 이상 그 훅에 매이지 않습니다 — Claude 자신의 `mark`가
  Codex 어댑터의 `discover()`를 불러, 이 레포에서 이미 원장에 있는 그 하네스
  행보다 더 최근인 Codex 세션의 원장 행을 롤아웃 파일에서 바로 채워 넣습니다
  (`via:"scan"`, `mark` 호출당 최대 5개). `omhc status`의 `codex hook` 행은
  일부러 이 `scan` 행을 세지 않습니다 — 세면 훅 자체가 안 돈 사실이
  가려집니다. **알려진 구멍(미검증):** `codex resume`으로 옛 롤아웃을 이어가면
  그 롤아웃의 원래 시작 시각이 그대로 남으므로, "더 최근이어야 한다" 검사에
  걸려 재개된 옛 세션이 누락될 수 있습니다 — 그리고 그 원래 시작 시각이
  7일(`due.MAX_AGE_SECONDS`)보다 오래됐으면, 재개 여부와 무관하게 백필 자체의
  나이 검사에서도 걸러집니다.

  </details>

- **원본 포맷은 공식 계약이 아닙니다.**
  <details>
  <summary>자세히</summary>

  Claude Code의 on-disk 스키마는 문서화되지 않았고 2026년 내내 파괴적으로
  변했습니다(공식 `SessionStore`조차 엔트리를 "opaque"로 선언합니다).
  화이트리스트 + fail-open + `status`의 열화 보고로 완화하지만, 깨질 것을
  전제로 설계했습니다. 아카이브가 포인터인 이유가 이것입니다.

  </details>

- **동시 사용은 v1 범위 밖입니다.**
  <details>
  <summary>자세히</summary>

  seam은 `omhc/due.py::due()` 하나이고, v2는 반환형을 `List[Watermark]`로
  바꾸고 `stale.py`를 같은 스트림의 두 번째 소비자로 추가합니다. v1이 이미 그
  기반(절단되지 않은 `paths` 열 + 바이트 오프셋 순서)을 기록합니다.

  </details>

- **픽스처는 커밋되지 않습니다.**
  <details>
  <summary>자세히</summary>

  실제 대화 내용이기 때문입니다. `python3 tests/harvest.py` 로 각자 머신에서
  생성합니다.

  </details>

### 실측 사실 (설계 근거)

| 사실 | 값 |
|---|---|
| 세션 파일에서 실제 대화가 차지하는 비중 | Claude Code 8%, Codex 0.15% |
| 최상위 세션 31개 중 대화형(`entrypoint=cli`) | **1개** (나머지 30개는 `sdk-py`) |
| 798 레코드에서 추출되는 진짜 사람 턴 | **11개** (`user` 95개 중 67개가 `tool_result`, 7개가 슬래시 명령 봉투) |
| `SessionStart` 훅이 한 세션에서 발동한 횟수 | **6회** → 세션당 1회 게이트가 필수 |
| `cwd`가 처음 등장하는 레코드 인덱스 | **3** (0이 아니고, 798개 중 222개엔 아예 없음) |
| 타임스탬프가 뒤로 가는 지점 | **254개** (최대 52ms) → 순서 근거로 쓸 수 없음 |
| `skill_listing` 본문 크기 / 포함된 마커 수 | 29,958자 / **0개** → 마커 탐지만으로는 못 잡음 |
| 툴 어휘 교집합 | **공집합** (`Read/Edit/Bash` vs `shell/apply_patch`) |

### 전달 경로 다이어그램

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/delivery-dark.svg">
  <img src="assets/delivery-light.svg" width="100%" alt="전달 경로: SessionStart 훅이 신뢰되지 않으면 아무것도 전달되지 않고, 신뢰되면 Path A(install_handoff)를 시도한 뒤 Path B(AGENTS.md 관리 구간, Codex 전용, Claude Code 와 공유되는 레포에서는 끔), 마지막으로 자동으로 읽히지 않는 outbox 바닥으로 떨어지는 그림">
</picture>

### 덜 쓰는 명령

| 명령 | 역할 |
|---|---|
| `omhc mark --harness X` | 세션 시작 기록 (훅이 부른다) |
| `omhc brief --harness X [--wire claude\|cursor\|sdk]` | 표식 출력 (훅이 부른다) |
| `omhc clear` | 설치된 표식 제거 |
| `omhc watch [--stop\|--once]` | 가속기 데몬(선택, 없어도 결과는 같다) |

</details>
