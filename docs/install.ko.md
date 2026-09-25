[English](install.md) · **한국어** · [← README](../README.ko.md)

# 설치, 설정, 제거

- [설치 스크립트](#설치-스크립트)
- [훅 연결하기](#훅-연결하기)
- [Codex: 훅 신뢰하기](#codex-훅-신뢰하기)
- [Codex: config.toml의 훅](#codex-configtoml의-훅)
- [Codex: git이 아닌 프로젝트](#codex-git이-아닌-프로젝트)
- [Codex: AGENTS.md 크기 한도](#codex-agentsmd-크기-한도)
- [전달 경로가 막히면](#전달-경로가-막히면)
- [AGENTS.md를 Claude Code와 공유하는 레포](#agentsmd를-claude-code와-공유하는-레포)
- [헤드리스 세션](#헤드리스-세션)
- [제거](#제거)

## 설치 스크립트

```bash
curl -fsSL https://raw.githubusercontent.com/SungJun1217/oh-my-harness-cowork/main/install.sh | sh
```

최신 릴리스를 `~/.local/share/omhc/<버전>`에 풀고 `~/.local/bin/omhc`로
심링크합니다. pip·pipx를 쓰지 않습니다(의존성이 0이라 소스 트리가 곧
설치물입니다). 다시 실행하면 업데이트됩니다(`~/.local/share/omhc` 아래
구버전은 `current`가 가리키는 것만 남기고 자동으로 정리됩니다). 버전
고정은 `| OMHC_VERSION=v0.1.0 sh`로 합니다.

git이 아닌 프로젝트라면 최상위 디렉터리에서 `touch .omhc-root`를 한 번
해 두세요. `.git`도 `.omhc-root`도 없으면 omhc를 돌린 모든 서브디렉터리가
각각 별개 프로젝트가 됩니다. 이런 프로젝트에서는 Codex 설정도 하나
더 필요합니다. [Codex: git이 아닌 프로젝트](#codex-git이-아닌-프로젝트)를
보십시오.

### git 체크아웃에서 직접 쓰려면

```bash
git clone git@github.com:SungJun1217/oh-my-harness-cowork.git
cd oh-my-harness-cowork
ln -s "$PWD/bin/omhc" ~/.local/bin/omhc
```

## 훅 연결하기

```bash
omhc hooks install
```

`--harness` 없이 부르면 이미 감지됐거나(`~/.claude/projects`,
`~/.codex/sessions`가 있다) 설정 *디렉터리* 자체가 있는(`~/.claude`,
`~/.codex`) 등록된 하네스 전부가 대상입니다. 후자는 어느 하네스도 아직
한 번도 세션을 시작하지 않아 `projects`/`sessions` 디렉터리가 없는,
설치 직후의 흔한 상태를 덮습니다. 둘 다 없으면(예: Codex 자체를 안
깔았다면) 기본 대상이 아닙니다. `omhc hooks install --harness
claude-code`(또는 `--harness codex-cli`)로 직접 지정하십시오. 아무것도
못 찾으면 등록된 하네스 id 목록을 보여주고 조용히 아무 일도 안 하는
대신 exit 1로 끝납니다.

조각을 그 하네스 자신의 설정(Claude Code는 `~/.claude/settings.json`,
Codex는 `~/.codex/hooks.json`)에 병합합니다. 파일을 덮어쓰지 않고, 먼저
기존 omhc 훅만 지운 뒤 다시 붙입니다. 그래서 `hooks/*.json`이 바뀐 뒤
등 다시 실행해도 중복되지 않고, 이미 통과하는 설치(`omhc status`의
`<adapter-id> hooks` 행이 PASS)는 손으로 병합하며 필드를 더 얹었거나
그룹 순서가 달라도 건드리지 않습니다. 멱등적입니다. 바꿀 게 없으면
아무것도 쓰지 않고 백업도 만들지 않습니다. 실제로 바뀌어 처음 쓸 때
JSON 형식(2칸 들여쓰기)도 정규화됩니다. `omhc hooks uninstall
[--harness ID]`는 같은 방식으로, 구조적으로(문자열 정규식이 아니라
argv로 판정) omhc 자신의 `SessionStart` 훅만 제거합니다. `install.sh
--uninstall`과 대체로 같지만 드문 명령 꼴에서는 갈릴 수 있습니다(실측
사례는 `omhc/hookconf.py` 상단 주석 참고). 두 명령 모두 기존 파일을
실제로 바꾸기 직전에 `<파일>.omhc-bak`로 먼저 백업합니다.

### 조각 파일을 손으로 병합하려면

`curl | sh`로 설치했다면 `~/.local/share/omhc/current/hooks/` 아래,
git 체크아웃이라면 레포의 `hooks/` 아래에 있습니다. 조각의 `hooks` 키를
각자 설정에 병합하십시오(덮어쓰지 말 것).

| 하네스 | 파일 | 대상 |
|---|---|---|
| Claude Code | `claude-settings.fragment.json` | `~/.claude/settings.json`의 `hooks` |
| Codex CLI | `codex-hooks.json` | `~/.codex/hooks.json` |

## Codex: 훅 신뢰하기

> [!WARNING]
> 실측(codex-cli 0.155.1): 손으로 떨어뜨린 `hooks.json`은 기본적으로
> 신뢰되지 않고, **신뢰되지 않은 훅은 메시지 없이 조용히 건너뜁니다.**
> Codex 쪽의 `mark`도 `brief`도 한 번도 돌지 않아 그 방향
> (**Claude→Codex**)으로는 Codex 세션에 아무것도 주입되지 않습니다. 그
> 방향을 고치려면 Codex 자신의 훅 신뢰 절차로 한 번 승인해야 합니다.
> **Codex→Claude**는 이것 없이도 됩니다. Claude 자신의 `mark`가 롤아웃
> 파일에서 바로 Codex 세션을 원장에 채워 넣습니다.

두 조각 모두 `--wire claude`를 씁니다. `--wire sdk`(최상위
`additionalContext`)는 codex-cli 0.155.1에서 `hook: SessionStart
Failed`로 거부되고 아무것도 주입되지 않습니다. 훅이 한 번도 안 돌았으면
`omhc status`의 [`codex hook` 행](status.ko.md#codex-hook)이 알려 줍니다.

## Codex: config.toml의 훅

Codex는 `config.toml`의 인라인 `[hooks]` 테이블에서도 훅을 읽습니다
(공식 [config-advanced 문서](https://developers.openai.com/codex/config-advanced#hooks)
참고. `hooks.json`과 같은 `hooks.<Event>[].hooks[].command` 구조를
TOML array-of-tables로 적었을 뿐입니다). `omhc hooks install`은 여전히
`hooks.json`에만 씁니다. 하지만 `omhc status`의 `codex-cli hooks` 행과
훅 경로의 `install_handoff`는 `~/.codex/config.toml`에 손으로 적은 omhc
설치도 그대로 인식합니다. 거기에 이미 적었다면 `hooks.json`이 없어도
됩니다. 둘 다 있고 둘 다 omhc `SessionStart` 훅을 정의하면 Codex는
문서대로 둘 다 읽고 경고합니다. `status`는 이걸 PASS 대신 미판정
`----` 행으로 보여주며 두 파일 이름을 모두 적습니다. 프로젝트 쪽
(`<repo>/.codex/hooks.json` / `<repo>/.codex/config.toml`)은 그 레포의
`.codex/` 레이어가 신뢰된 경우에만 셉니다(`~/.codex/config.toml`의
`[projects."<path>"] trust_level = "trusted"`). 그렇지 않으면 omhc는
무시합니다.

## Codex: git이 아닌 프로젝트

> [!IMPORTANT]
> 실측(codex-cli 0.155.1): `.omhc-root` 프로젝트에서 서브폴더에 들어가
> 시작한 Codex는 기본 `project_root_markers = [".git"]`로는 조상
> `AGENTS.md`를 읽지 **않습니다**. 그러면 Path B(AGENTS.md managed
> block)가 조용히 무력해집니다. `~/.codex/config.toml`의 이 설정에
> `.omhc-root`를 더하세요(`.git`은 그대로 두고):
> ```toml
> project_root_markers = [".git", ".omhc-root"]
> ```

`omhc status`의 [`codex root markers` 행](status.ko.md#codex-root-markers)이
이걸 대신 확인해 줍니다.

## Codex: AGENTS.md 크기 한도

> [!IMPORTANT]
> 실측(codex-cli 0.156.1): Codex는 `AGENTS.md`를 머리부터
> `project_doc_max_bytes`(기본 32768, 레포 루트부터 cwd까지 체인 전체에
> 대한 총 예산 하나)만큼만 읽고 그 지점에서 예고 없이 자릅니다.

Path B는 관리 구간을 언제나 `AGENTS.md` **맨 앞**에 씁니다(예전에 끝에
있던 구간도 다음 쓰기에서 앞으로 옮겨집니다). 그래서 파일이 커도 그
잘림을 피합니다. 그래도 구간 자체가 설정된 `project_doc_max_bytes`를
넘겨 끝난다면, omhc는 Path B 설치를 성공으로 주장하지 않고 outbox로
대신 떨어집니다. Codex가 못 볼 것을 쓰지 않기 위해서입니다. `omhc
status`의 [`codex agents.md budget` 행](status.ko.md#codex-agentsmd-budget)이
이걸 알려줍니다.

## 전달 경로가 막히면

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/delivery-dark.svg">
  <img src="../assets/delivery-light.svg" width="100%" alt="전달 경로: SessionStart 훅이 신뢰되지 않으면 아무것도 전달되지 않고, 신뢰되면 Path A(install_handoff)를 시도한 뒤 Path B(AGENTS.md 관리 구간, Codex 전용, Claude Code 와 공유되는 레포에서는 끔), 마지막으로 자동으로 읽히지 않는 outbox 바닥으로 떨어지는 그림">
</picture>

`deliver()`는 어댑터 자신의 `install_handoff`(Path A, 훅의 stdout)를
먼저 시도하고, 다음으로 어댑터의 폴백 채널(Codex: `AGENTS.md` 관리
구간, Path B), 마지막으로 보편 바닥인 `<repo>/.omhc/outbox/`로
떨어집니다. 이 모든 과정이 `brief` 안에서 돌기 때문에, 훅이 아예 안
돌면 아무것도 전달되지 않습니다.

`<repo>/.omhc/outbox/`의 파일은 오래 두지 않습니다. `omhc mark`
(SessionStart 훅이 부름)는 omhc 자신이 쓴 outbox 파일 중 24시간 넘은
것만 지우고, `omhc clear`는 이 레포의 것을 즉시 전부 지웁니다. omhc의
이름 규칙·헤더와 맞지 않는 파일은 절대 건드리지 않습니다. 파일을 하나
떨굴 때마다 `.omhc/`를 `.git/info/exclude`에 등재하려 시도합니다(이미
등재됐거나 다른 방식으로 무시 중이면 건너뜁니다). AGENTS.md 관리
구간이 쓰는 것과 같은, 클론마다 한 번만 해당하는 등재 방식입니다.

Codex 자신의 SessionStart 훅이 성공하면(Path A), 그 옆에 남아 있던
AGENTS.md 관리 구간(Path B)도 평소의 24시간을 기다리지 않고 그 자리에서
붕괴시킵니다. 새 세션이 신선한 훅 핸드오프와 낡은 구간을 동시에 읽지
않게 하기 위해서입니다.

> [!NOTE]
> 실측(codex-cli 0.156.1): Codex는 자기 SessionStart 훅보다 **먼저**
> `AGENTS.md`를 읽습니다. 세션의 첫 턴은 훅이 그 파일을 뭘 하든 세션이
> 시작할 때 디스크에 있던 그대로를 읽습니다. **같은** 세션의 이후 턴만
> (resume으로 확인) `AGENTS.md`를 다시 확인하고, 바뀐 부분만 알려줍니다.
> 내용이 바뀌었으면 "These AGENTS.md instructions replace all previously
> provided AGENTS.md instructions."와 새 본문을, 블록이 없어졌으면 "The
> previously provided AGENTS.md instructions no longer apply."를, 그대로면
> 아무것도 넣지 않습니다.

즉 어떤 Codex 세션이 시작하기 전부터 있던 구간은 그 세션의 `startup`
턴에만 읽히고(아직 안 지워졌다면 그 세션의 이후 턴에 diff로 다시 보일
수 있습니다), **다음** Codex 세션이 그 낡은 구간을 또 읽는 일만
막습니다. `omhc mark`가 그 세션 자신의 `startup`(`resume`은 제외.
"훅보다 먼저 읽는다"는 순서는 startup에서만 실측했고, 같은 세션이
이어지는 것뿐인 `compact`도 제외)에서, 이 mark 호출보다 이미 먼저 적힌
구간을 평소의 24시간을 기다리지 않고 그 자리에서 붕괴시킵니다. 이는
Path A의 같은 세션 붕괴, 24시간 노후화 정리와 별개로 더해지는
동작입니다. 같은 SessionStart 안에서 다른 훅이 병렬로(Codex는
SessionStart 훅을 병렬로 돌립니다, 실측) 방금 쓴 구간은 캡처 시각에
작은 여유를 둬 건드리지 않습니다. 그 여유가 "낡았다고 판정한 시점"과
"실제로 지우는 시점" 사이의 창을 완전히 닫지는 못하므로, 지우는 동작
자체도 판정에 쓴 캡처 시각이 그대로인 경우에만 실행됩니다. 그 사이에
새로 쓰인 구간은 잃지 않습니다.

이 정리는 `AGENTS.md`가 Claude Code와 공유되는 레포(아래 참고)에서는
하나도 하지 않습니다. omhc는 공유된 `AGENTS.md`를 설치할 때든 붕괴시킬
때든 아예 건드리지 않습니다.

## AGENTS.md를 Claude Code와 공유하는 레포

> [!IMPORTANT]
> 권장 배치는 `AGENTS.md`를 하네스 중립 원본으로 두고, `CLAUDE.md`는
> 실제 파일로 `@AGENTS.md`로 시작한 뒤 Claude 전용 내용을 잇는
> 것입니다(이 레포 자체가 그 구조입니다). `CLAUDE.md`를 `AGENTS.md`로의
> 심링크로 두는 것도 마찬가지로 공유입니다. 어느 쪽이든 `AGENTS.md`는
> 심링크여서는 안 됩니다.

이런 레포에서는 omhc가 `AGENTS.md`에 절대 쓰지 않습니다. Codex용 관리
구간(Path B)이 Claude Code 세션에도 그대로 읽혀 핸드오프가 새고,
심링크를 통해 쓰면 공유·추적 중인 원본 파일이 바뀌기 때문입니다.
Codex로의 핸드오프는 Codex의 SessionStart 훅(Path A)으로 전달됩니다. 이
훅이 신뢰되어 돌지 않으면 Path B나 outbox가 대신 받는 게 아니라 Codex
쪽 `brief` 호출 자체가 없어 아무것도 전달되지 않습니다. 그래서
**Codex는 outbox 디렉터리를 자동으로 읽지도 않으며**, Codex 훅을 반드시
설치해야 합니다(그리고 그 훅을 Codex 자신의 절차로 신뢰해야 합니다).
`omhc status`의 `instruction files` 행이 이 레이아웃을 보여줍니다.

## 헤드리스 세션

기본적으로 헤드리스 세션(`claude -p`, `codex exec`, 앱서버 클라이언트)과
Codex 서브에이전트 스레드는 핸드오프 원천이 되지 않습니다. 샌드박스에서
헤드리스 세션을 실제 세션처럼 쓰려면 받는 쪽 실행에
`OMHC_ALLOW_HEADLESS=1`을 export 하십시오. 적격 여부는 받는 세션이
시작할 때 판정하므로, 이 값을 켜기 전에 돌았던 헤드리스 세션도
받습니다. 실행 전체에 한 번 export 해 두는 것이 가장 간단합니다.
서브에이전트·사이드체인은 이것으로도 풀리지 않습니다.

## 제거

```bash
curl -fsSL https://raw.githubusercontent.com/SungJun1217/oh-my-harness-cowork/main/install.sh | sh -s -- --uninstall
```

omhc 자신의 `SessionStart` 훅만 `~/.claude/settings.json`과
`~/.codex/hooks.json`에서 제거합니다. 같은 파일, 심지어 같은 훅 그룹
안의 다른 훅도 그대로 남습니다. JSON은 재직렬화(2칸 들여쓰기)만 됩니다.
먼저 `<파일>.omhc-bak` 백업을 만듭니다. 이어서 `~/.local/bin/omhc`와
`~/.local/share/omhc`를 지웁니다. `~/.omhc`(아카이브·원장)는 남겨
둡니다. 이것까지 지우려면 `OMHC_PURGE=1`을 씁니다(파이프로도 가능:
`curl -fsSL .../install.sh | OMHC_PURGE=1 sh -s -- --uninstall`). 아무것도
설치되지 않았을 때도, 두 번 실행해도 안전합니다.

손대지 않는 것들:
- **Codex 훅 신뢰.** 신뢰 항목은 그룹/훅 인덱스로 키가 매겨져서, omhc의
  그룹을 지우면 그 외 Codex `SessionStart` 훅들의 인덱스가 밀릴 수
  있습니다. 제거 후 Codex 자신의 신뢰 절차로 다시 승인해야 할 수
  있습니다.
- **레포별 잔여물.** 레포의 `AGENTS.md` 안 omhc 관리 구간과
  `<레포>/.omhc/outbox/`가 남습니다. 이것도 정리하려면 제거하기 *전에*
  각 레포에서 `omhc clear`를 실행하십시오.
