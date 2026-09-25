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

git 이 아닌 프로젝트라면 최상위 디렉터리에서 `touch .omhc-root` 를 한 번
해두세요 — `.git` 도 `.omhc-root` 도 없으면 omhc 를 돌린 모든 서브디렉터리가
각각 별개 프로젝트가 됩니다.

> [!IMPORTANT]
> 실측(codex-cli 0.155.1): `.omhc-root` 프로젝트에서 서브폴더에 들어가 시작한
> Codex 는 기본 `project_root_markers = [".git"]` 로는 조상 `AGENTS.md` 를
> 읽지 **않습니다** — Path B(AGENTS.md managed block)가 조용히 무력해집니다.
> `~/.codex/config.toml` 의 이 설정에 `.omhc-root` 를 더하세요(`.git` 은
> 그대로 두고):
> ```toml
> project_root_markers = [".git", ".omhc-root"]
> ```
> `omhc status`의 `codex root markers` 행(아래 참고)이 이걸 대신 확인해
> 줍니다.

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

훅 배선은:

```bash
omhc hooks install
```

`--harness` 없이 부르면 이미 감지됐거나(`~/.claude/projects`,
`~/.codex/sessions` 가 있다) 설정 *디렉터리* 자체가 있는(`~/.claude`,
`~/.codex`) 등록된 하네스 전부가 대상입니다 — 후자는 어느 하네스도 아직
한 번도 세션을 시작하지 않아 `projects`/`sessions` 디렉터리가 없는, 설치
직후의 흔한 상태를 덮습니다. 둘 다 없으면(예: Codex 자체를 안 깔았다면)
기본 대상이 아닙니다 — `omhc hooks install --harness claude-code`(또는
`--harness codex-cli`)로 직접 지정하십시오. 아무것도 못 찾으면 등록된
하네스 id 목록을 보여주고 조용히 아무 일도 안 하는 대신 exit 1 로 끝납니다.

조각을 그 하네스 자신의 설정(Claude Code 는 `~/.claude/settings.json`,
Codex 는 `~/.codex/hooks.json`)에 병합합니다 — 파일을 덮어쓰지 않고, 먼저
기존 omhc 훅만 지운 뒤 다시 붙이므로(`hooks/*.json` 이 바뀐 뒤 등) 다시
실행해도 중복되지 않고, 이미 통과하는 설치(`omhc status` 의 `<adapter-id>
hooks` 행이 PASS)는 손으로 병합하며 필드를 더 얹었거나 그룹 순서가 달라도
건드리지 않습니다. 멱등적입니다 — 바꿀 게 없으면 아무것도 쓰지 않고 백업도
만들지 않습니다. 실제로 바뀌어 처음 쓸 때 JSON 형식(2칸 들여쓰기)도
정규화됩니다. `omhc hooks uninstall [--harness ID]` 는 같은 방식으로,
구조적으로(문자열 정규식이 아니라 argv 로 판정) omhc 자신의 `SessionStart`
훅만 제거합니다 — `install.sh --uninstall` 과 대체로 같지만 드문 명령
꼴에서는 갈릴 수 있습니다(실측 사례는 `omhc/hookconf.py` 상단 주석 참고).
두 명령 모두 기존 파일을 실제로 바꾸기 직전에 `<파일>.omhc-bak` 로 먼저
백업합니다.

<details>
<summary>조각 파일을 손으로 병합하려면</summary>

`curl \| sh` 로 설치했다면 `~/.local/share/omhc/current/hooks/` 아래,
git 체크아웃이라면 레포의 `hooks/` 아래에 있습니다. 조각의 `hooks` 키를
각자 설정에 병합하십시오(덮어쓰지 말 것).

| 하네스 | 파일 | 대상 |
|---|---|---|
| Claude Code | `claude-settings.fragment.json` | `~/.claude/settings.json` 의 `hooks` |
| Codex CLI | `codex-hooks.json` | `~/.codex/hooks.json` |

</details>

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

Codex 는 `config.toml` 의 인라인 `[hooks]` 테이블에서도 훅을 읽습니다(공식
[config-advanced 문서](https://developers.openai.com/codex/config-advanced#hooks)
참고 — `hooks.json` 과 같은 `hooks.<Event>[].hooks[].command` 구조를 TOML
array-of-tables 로 적었을 뿐입니다). `omhc hooks install` 은 여전히
`hooks.json` 에만 씁니다. 하지만 `omhc status` 의 `codex-cli hooks` 행과 훅
경로의 `install_handoff` 는 `~/.codex/config.toml` 에 손으로 적은 omhc 설치도
그대로 인식합니다 — 거기에 이미 적었다면 `hooks.json` 이 없어도 됩니다. 둘 다
있고 둘 다 omhc `SessionStart` 훅을 정의하면 Codex 는 문서대로 둘 다 읽고
경고합니다 — `status` 는 이걸 PASS 대신 미판정 `----` 행으로 보여주며 두
파일 이름을 모두 적습니다. 프로젝트 쪽(`<repo>/.codex/hooks.json` /
`<repo>/.codex/config.toml`)은 그 레포의 `.codex/` 레이어가 신뢰된 경우에만
셉니다(`~/.codex/config.toml` 의 `[projects."<path>"] trust_level =
"trusted"`) — 그렇지 않으면 omhc 는 무시합니다.

`omhc status`의 모든 행은 세 라벨 중 하나입니다: 실제로 판정되어 exit code 를
게이팅할 수 있는 **PASS**/**FAIL**(adapters, archive, instruction files,
`ledger rejects`, `codex hook` 같은 어댑터 health 행), 그리고 정보성이거나
아직 판단할 근거가 없는 행을 위한 **`----`**(ledger, off switch, pull rate,
watcher) — `----` 는 게이팅하지 않습니다.

원장은 세션 시작마다 JSON 한 줄을 덧붙이고, 그 한 줄은 반드시 `write(2)`
호출 한 번 안에 들어가야 합니다(append-only 라 잠금이 필요 없다 — `O_APPEND`
fd 에 대한 단일 write(2) 는 크기와 무관하게 POSIX 상 원자적이고, 이건
`PIPE_BUF` 와는 무관하다 — `PIPE_BUF` 는 파이프 전용이다). 그 상한을 넘는
행은 잘라내지 않고 통째로 버립니다(`path`/`session` 을 자르면 아무것도
가리키지 않는 줄이 조용히 남기 때문) — 대신 버렸다는 사실 자체를 남겨
안 보이게 사라지지 않게 합니다; 같은 세션이 재시도돼도 `mark` 마다 또
남기지 않고 한 번만 기록합니다. `ledger rejects` 는 이 레포에서 최근 7일
안에 버려졌고 지금 상한으로도 여전히 못 들어가는 행이 있으면 FAIL, 없으면
`----` 입니다; `omhc clear` 로 이 레포의 기록을 지울 수 있습니다(예: 상한을
올려 고친 뒤). omhc Codex 훅이
설치돼 있으면 `codex hook` 행이
붙습니다. `hooks.json`이 마지막으로 바뀐 뒤 이 레포의 가장 최근 대화형 Codex
세션이 훅을 돌리지 않았으면(신뢰되지 않은 훅) FAIL 이고, 그 세션의 originator 를
함께 보여줍니다. 이때 Claude→Codex 는 전달되지 않지만 Codex→Claude 는 Claude 쪽
`mark` 의 채우기로 계속 동작합니다. 아직 판정할 수 없으면 `----` 입니다:
`hooks.json`이 바뀐 뒤 이 레포에 대화형 Codex 세션이 없을 때(날짜 표시), 헤드리스
`codex exec` 세션만 있을 때(판정에 세지 않습니다 — 여기서 대화형 `codex` 를 한 번
여십시오), 알 수 없는 오류일 때.

레포 루트가 `.omhc-root` 로 정해진 경우(`.git` 이 아니고, 조상 디렉터리에도
`.git` 이 전혀 없는 경우 — 있다면 Codex 기본값이 거기서부터 이미 내려오며
읽습니다), `codex root markers` 행이 `~/.codex/config.toml`의
`project_root_markers` 에 `.omhc-root` 가 있는지 봅니다(위 박스 참고). 이
행은 절대 게이팅하지 않습니다 — 이 설정은 Path B(AGENTS.md 폴백)에만
영향을 주므로, 마커가 있으면 PASS, 없으면 넣을 정확한 줄과 함께 `----`
입니다 — 그리고 omhc Codex 훅이 설치돼 있지 않으면 "지금 Path B 가 Codex 로
가는 유일한 채널"이라는 사실을 명시적으로 덧붙입니다. 설정 파일이 없거나,
못 읽거나, 파싱할 수 없을 때(omhc 가 직접 고치는 일은 없습니다), 그리고
키가 `[section]` 안에서만 보일 때(TOML 테이블은 키의 스코프를 바꾼다 —
최상위에 있어야 합니다)도 모두 `----` 입니다.

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
| `omhc log [--last N] [--grep P] [--verb V] [--file P]` | 색인된 이벤트를 한 줄씩. 각 줄은 `<session>#N` 참조로 시작하며 그대로 `show`에 넘길 수 있음 |
| `omhc show <E1\|#137\|abcdef01#137> [--full]` | **원본 바이트를 오프셋으로 조회** (tier (b) 진입점). 맨 `#N`은 가장 최근 전달된 세션 기준(`log`의 인출률 회계와 같은 규칙)이고, `<prefix>#N`은 세션을 직접 지정함 — 접두사가 모호하면 후보를 나열함 |
| `omhc note "<text>"` | 메모. 두 하네스의 에이전트가 맨 명령줄로 호출 가능 |

**인출률**("pulled X of N recent injections")은 omhc 의 부담이 값을 하는지
판단할 유일한 숫자입니다. N 은 이 레포에 전달된 가장 최근 `PULL_RATE_WINDOW`
(20)개 세션, X 는 그중 `omhc show`나 `omhc log`로 실제로 파본 세션 수입니다
(같은 세션을 여러 번 파봐도 한 번만 셉니다) — 사람이 손으로 `omhc log`를
돌려도 셈에 들어갑니다, 에이전트뿐 아니라. 창은 전달된 전체 역사가 아니라
가장 최근 전달들(append 순서, 세션 id 로 매칭)만 봅니다 — 안 그러면 오래 쓴
레포에서 더 이상 안 파보는 옛 전달들이 분모에 계속 쌓여 인출률이 끝없이
낮아 보입니다.

끄기: `OMHC_OFF=1` 또는 `~/.omhc/<repo-key>/off` 파일.

기본적으로 헤드리스 세션(`claude -p`, `codex exec`, 앱서버 클라이언트)과 Codex
서브에이전트 스레드는 핸드오프 원천이 되지 않습니다. 샌드박스에서 헤드리스 세션을
실제 세션처럼 쓰려면 받는 쪽 실행에 `OMHC_ALLOW_HEADLESS=1`을 export 하십시오.
적격 여부는 받는 세션이 시작할 때 판정하므로, 이 값을 켜기 전에 돌았던 헤드리스
세션도 받습니다. 실행 전체에 한 번 export 해 두는 것이 가장 간단합니다.
서브에이전트·사이드체인은 이것으로도 풀리지 않습니다.

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
bash tests/smoke.sh                             # 적대적 입력 8종
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
  가려집니다. **알려진 구멍(#22, 기본 설정에서는 무해함):** 그 5개를 넘는
  초과분(또는 `discover()` 자신의 훅 예산에 밀려 못 본 나머지)은 이후 어떤
  `mark` 도 다시 채우지 않습니다 — 다음 호출의 watermark 가 이미 이번에 고른
  것 중 가장 최근 것이라, 그보다 오래된 미채움 세션은 "원장에 있는 것보다
  최신이어야 한다" 판정에 영영 걸립니다. 이게 무해한 이유는 due() 가 가장
  최근의 **자격 있는** 외래 세션 하나만 필요로 하고, discover() 가 brief 의
  자격 판정과 **같은** 헤드리스 필터(`allow_headless()`)를 쓰기 때문입니다
  — 그래서 보통 백필되는 집합과 due() 가 원하는 집합이 같습니다. 유일하게
  깨지는 경우는 백필을 돌린 `mark` 와 이후 `brief` 사이에
  `OMHC_ALLOW_HEADLESS` 값이 달라지는 것뿐입니다 — 그러면 헤드리스 세션 5개
  보다 더 뒤에 있는 대화형 세션이 원장에서 통째로 빠질 수 있습니다. 흔치 않은
  설정 변경이라 고치지 않았습니다.

  `session_meta.timestamp`는 롤아웃 첫 줄에서 한 번만 읽고 다시 갱신하지
  않습니다 — `codex exec resume`(실측: 같은 롤아웃에 이어 쓰고 새
  `session_meta` 는 안 남깁니다)이 이 값을 움직이지 않으므로, 재개된 세션의
  시작 epoch 는 그대로고 첫 줄 시각으로도 `already_delivered` 의 세션 id
  로도 재개가 일어났다는 걸 백필 경로가 알 수 없었습니다. Codex 훅이
  신뢰돼 있으면 문제없습니다 — SessionStart 가 `source:"resume"` 으로
  발화하고, `mark` 가 새 start 행과 `delivered.tsv` 의 `reopen` 줄을
  남깁니다(`source:"compact"` 는 다시 열지 않습니다). 훅이 신뢰**되지
  않은**(기본값, 미검증 Codex 설정) 경우엔 세션 시작 epoch 이 아닌 다른
  변화 감지 신호가 필요했습니다 — invariant 6 은 여전히 마지막 레코드
  타임스탬프나 mtime 을 **순서** 기준으로 쓰는 것은 금지하지만, 파일
  **크기**는 "언제"가 아니라 "이 파일이 자랐다"만 알려줍니다: 이제 `mark`
  의 백필이 원장에 남은 각 외래 세션의 마지막 크기도 stat 해 비교하고(다시
  스캔하지 않고, 날짜 디렉터리 창도 안 쓰므로 14일짜리 `discover()` 창
  밖의 세션도 여전히 잡습니다), 자랐으면 어댑터의 선택 메서드
  `read_session_since(ref, offset)` 로 늘어난 꼬리만 읽어(Codex: 다음 줄
  경계로 스냅) 그 꼬리에 진짜 사람의 새 턴이 있는지만 확인합니다 —
  에이전트 혼자 움직인 성장(도구 호출, `turn_aborted`, `task_complete`)은
  크기 기준만 갱신하고 세션을 다시 띄우지 않습니다. 꼬리를 읽는 비용은
  여전히 바이트에 비례하므로(실측 ~22µs/KB) 최대한 일찍 멈춥니다 —
  `stop_at_human_turn` 은 사람의 턴을 찾는 즉시 돌아오고(실측: 45MB
  롤아웃에서도 턴이 읽기 시작 지점 근처면 0.18ms), `max_bytes` 캡이 최악의
  경우(사람 턴이 **전혀** 없는 큰 성장)를 실제 꼬리 크기와 무관하게 고정
  상한으로 묶습니다(실측: 1MB 캡에서 약 17ms) — 훅 예산 안에 넉넉히
  들어옵니다. 캡에 걸려 다 못 읽었으면 실제로 읽은 데까지만 크기 기준을
  올립니다 — 안 읽은 나머지는 다음 `mark` 가 이어서 보므로 조용히
  건너뛰지 않습니다. 다음 baseline 으로 쓰는 `end_offset`(raw stat 크기가
  아니라)은 언제나 실제로 다 읽은 마지막 완전한 줄 끝에 스냅됩니다 — 바로
  그 첫 baseline 도 마찬가지입니다: 파일을 쓰는 도중에 stat 하면 레코드
  중간을 잡을 수 있는데, 그 바이트 수를 그대로 경계로 쓰면 나중에 그
  레코드부터 읽을 때 마저 쓰인 그 레코드 전체를 건너뛰어, 그 순간 쓰이고
  있던 사람의 턴을 영영 잃습니다. 이 스냅은 개행을 최대 64KB 만 거꾸로
  찾습니다 — 그보다 긴 JSONL 레코드 하나(실측 안 됨, 드물 것으로 봅니다)는
  이전에 알던 baseline 이 있으면 그 값으로 대체해, 최악의 경우도 그 구간을
  다시 읽는 정도로 그칩니다. **알려진 한계:** 이전 baseline 이 없으면 원래
  크기를 그대로 씁니다 — 0 으로 대체하면 다음 판정이 처음부터 읽어 그 세션의
  **원래** 사람 턴을 찾고 옛 내용을 다시 넘깁니다(리뷰에서 재현). 그래서 첫
  관측 순간 64KB 넘는 사람 레코드가 쓰이던 중이면 그 턴을 놓칠 수 있습니다.
  `stop_at_human_turn` 도 같은 원칙을 씁니다: 이미 완전한 JSON 으로
  파싱되는 사람의 턴이라도 아직 끝에 개행이 안 붙었으면 매치로 세지
  않습니다 — 세면 baseline 이 그 줄 바로 앞(그 줄을 포함하지 않는 지점)
  까지만 전진하고, 다음 `mark` 가 개행이 마저 붙은 뒤 같은(아직 안 읽은)
  줄에서 "새" 성장을 또 찾아 같은 턴을 두 번 전달하게 됩니다.

  이렇게 찾은 재개도 여전히 **원장 append 순서**(새 `start` 행, `grew:1`)로
  자리를 잡습니다 — due() 가 늘 쓰던 그 순서 규칙 그대로입니다; 행의
  epoch 는 이 `mark` 자신의 현재 시각이라 `due.MAX_AGE_SECONDS` 가
  오래됐다고 걸러내지 않습니다. 한 전환 구간에서 더 최신 세션에 밀린
  세션도 영원히 막히지는 않습니다 — 그 더 최신 세션의 start 행이 원장에
  **어떻게** 들어왔든 마찬가지입니다: 백필 스캔으로 들어왔다면 밀린
  세션의 baseline 을 **같은** `mark` 호출 안에서(아직 아무것도 안 자랐을
  가능성이 가장 높은 시점에) 그 뒤로 바로 옮기고, 그 세션 **자신의**
  신뢰된 훅이 직접 start 행을 남겼다면(백필 스캔에는 안 보입니다 — 이미
  원장이 아는 세션이라서요) 다음 재판정 라운드가 자랐는지 여부와 무관하게
  같은 재기준점 이동을 지연(lazy) 적용합니다. 어느 경로든 그 새 start 행
  **뒤로** 또 자라면 새 판정으로 다시 잡힙니다, 다만 이미 진 그 구간
  자체를 되돌리지는 않습니다. 이 방식은 지금까지 손도 못 댔던 경우도
  함께 잡습니다: 이미 전달된 Codex 세션 A 에 사람이 계속 타이핑하다가
  Codex 쪽 SessionStart 없이 곧장 새 Claude 세션으로 넘어가는 경우 —
  성장 확인은 A 를 받는 쪽을 포함해 **어떤** `mark` 에서도 돌므로 A 쪽에
  훅이 필요 없습니다.

  **남은 한계:** 순서 근거 자체는 여전히 원장 append 순서라, 더 최신
  세션의 start 행이 들어온 시점과 밀린 세션이 재기준점을 얻는 시점(늦어도
  다음 `mark` 한 번) **사이**에 밀린 세션이 이미 자랐다면, 그 성장이
  그 사이 어느 쪽에서 일어났는지는 size 만으로 가릴 수 없어 그대로
  흡수됩니다 — 그 구간은 더 최신 **start** 가 이깁니다(그 구간 뒤에
  다시 자란 나머지 한쪽은 새로 판정됩니다). 그리고 이건 Codex→Claude
  방향뿐입니다 — 성장 확인은 `read_session_since` 를 구현한 어댑터에서만
  도는데 Claude 어댑터는 아직 구현하지 않았으므로, Claude 쪽
  live-continue(이미 전달된 Claude 세션에 계속 타이핑하다 새 Codex
  세션으로 옮기는 경우)는 그게 구현될 때까지 감지되지 않습니다.

  **`reopen` 은 힌트일 뿐, 보장이 아닙니다(#27).** `codex exec resume <id>
  ""` 는 `source:"resume"` 으로 발화해 `reopen` 줄을 남기지만, 롤아웃에는
  `"text": ""` 인 user 레코드만 붙습니다 — 사람의 턴이 없습니다. 게다가
  Codex 는 SessionStart 훅을 동시에 돌리므로 `mark` 와 `brief` 가 경합할 수
  있습니다 — `brief` 가 세션을 전달한 바로 그 초에 `mark` 의 성장 판정이
  이미 `brief` 가 읽은 그 턴을 보고 `reopen` 을 전달 **뒤에** 붙일 수
  있습니다. 어느 쪽이든 `due()` 가 그 세션을 다시 `brief` 에 넘기면 보낼
  새 내용이 없습니다. 그래서 `brief.compute` 는 전달할 때마다 실제로 어디까지
  읽었는지(바이트 오프셋)를 `delivered.tsv` 의 5번째 열로 남깁니다(탭으로
  구분 — 앞 두 열만 보는 리더는 영향이 없고, 이 열이 없는 옛 줄은 오늘까지의
  조건 없는 동작으로 그대로 남습니다). 다시 열린 세션을 재전달하기 전에, 그
  오프셋 이후에 `author=="human"`, `verb=="said"` 이고 텍스트가 비어 있지
  않은 이벤트가 최소 하나 있어야 합니다 — 없으면 게이트도 `delivered.tsv` 도
  건드리지 않고 "보낼 것 없음" 경로와 똑같이 빈 문자열을 돌려줍니다.
  `--dry-run` 도 같은 검사를 거칩니다. `mark` 의 `reopen` 기록은 그대로입니다
  — `due()` 가 전달된 세션을 다시 후보로 보게 하는 유일한 신호는 여전히
  그것이고, 실제로 새로 보낼 게 있는지는 `brief` 가 정합니다.

  </details>

- **원본 포맷은 공식 계약이 아닙니다.**
  <details>
  <summary>자세히</summary>

  Claude Code의 on-disk 스키마는 문서화되지 않았고 2026년 내내 파괴적으로
  변했습니다(공식 `SessionStore`조차 엔트리를 "opaque"로 선언합니다).
  화이트리스트 + fail-open + `status`의 열화 보고로 완화하지만, 깨질 것을
  전제로 설계했습니다. 아카이브가 포인터인 이유가 이것입니다.

  </details>

- **git 이 아닌 프로젝트는 마커가 필요합니다.** 레포 루트는 `.git` 또는
  `.omhc-root` 를 가진 가장 가까운 조상입니다. 둘 다 없으면 omhc 를 돌린 모든
  서브디렉터리가 각각 별개 프로젝트가 됩니다(키도, `~/.omhc/` 아래 상태도
  따로). git 레포가 아니라면 최상위에서 한 번 `touch .omhc-root` 하세요.
  omhc 는 `/` 에서는 아예 거부합니다(`status` 는 `FAIL root`, 훅 경로는
  invariant 2 대로 조용히 아무것도 안 합니다) — `$HOME` 은 정상 루트입니다.

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
| `omhc hooks install\|uninstall [--harness ID]` | omhc 자신의 `SessionStart` 훅을 병합/제거 (설치 항목 참고) |

</details>
