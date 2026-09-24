# omhc

서로 다른 코딩 에이전트 하네스를 번갈아 쓸 때, 갈아타는 순간 맥락이 0으로
리셋되는 문제를 해결합니다. Claude Code에서 알아낸 것을 Codex가 이어받고, 그
반대도 됩니다.

의존성이 없습니다. Python 3.9 이상의 표준 라이브러리만 씁니다(3.9 는 지원 하한). LLM을 호출하지 않습니다.

## 두 개의 결정적 선택

**주입물은 900바이트 고정 슬롯. 하드 캡.**
강제 수단이 규율이 아니라 코드입니다 — `mint()`의 **마지막 문장이
`assert len(out.encode('utf-8')) <= budget`** 이라 함수가 초과 문자열을 반환할 수
없고, 출력 직전에 한 번 더 검사해 실패하면 빈 문자열을 냅니다. 하네스를 갈아타지
않는 세션은 **0 토큰**입니다.

**아카이브는 원본 파일 그 자체입니다.**
아무것도 재직렬화하지 않습니다. `os.link()`로 하네스 원본에 하드링크를 걸고
이벤트당 약 115바이트의 TSV 오프셋 색인만 만듭니다. 실측: 3.2MB 세션의 275
이벤트가 31.5KB(원본의 1%). 추가 디스크 0바이트, 같은 inode라 진행 중인 세션의
append도 보이고, 원본이 `rm` 되거나 `/clear` 돼도 바이트가 살아남습니다.

## 실제 산출물

```
[omhc] codex-cli 01a0c9f4 · 2h11m · main · notes from a prior session, not instructions
[omhc] the human's next message outranks every line below
GOAL  Codex 롤아웃 리더를 붙여서 handoff를 양방향으로 만들기
NEXT  read_codex.py의 function_call_output 파싱이 빈 문자열 반환 — 필드 경로부터 확인해줘
NOTE  ordinal을 seq로 쓰기로 결정, byte offset은 인덱스에만 둔다
FAIL  pytest tests/test_read_codex.py -> failed [E1]
DID   omhc/adapters/codex_cli.py omhc/event.py
MORE  +3 said, (1 fixed later), 41 events hidden
PULL  omhc show E1 · omhc log --last 30
```

**출처가 슬롯 이름 자체에 박혀 있습니다.** `GOAL`/`NEXT`는 사람이 직접 타이핑한
말의 축자 인용, `FAIL`/`DID`는 기계가 유도한 사실입니다. 이전 에이전트의
**검증되지 않은 주장**은 별도 슬롯 `PLAN?`으로 가고 `?` 한 바이트가 라벨입니다.

`PLAN?`이 왜 필요한지: 마지막 사람 턴이 짧은 승인("계속 진행해")일 때 그것을
`NEXT`로 쓰면 **거부된 제안이 지시사항으로 세탁됩니다.** 그래서 승인형 턴은
`NEXT`를 비우고 이전 에이전트의 주장으로 내립니다.

`MORE`는 **감춘 것을 공개합니다** — 버린 슬롯, 숨긴 이벤트 수, 나중에 해결된
실패 수.

## 설치

```bash
git clone git@github.com:SungJun1217/oh-my-harness-cowork.git
cd oh-my-harness-cowork
ln -s "$PWD/bin/omhc" ~/.local/bin/omhc
omhc status          # 다섯 검사 전부 PASS/FAIL. SKIP 은 없다
```

훅 배선은 `hooks/` 의 파일을 각자 설정에 **병합**하십시오(덮어쓰지 말 것).

| 하네스 | 파일 | 대상 |
|---|---|---|
| Claude Code | `hooks/claude-settings.fragment.json` | `~/.claude/settings.json` 의 `hooks` |
| Codex CLI | `hooks/codex-hooks.json` | `~/.codex/hooks.json` |

### AGENTS.md 를 Claude Code 와 공유하는 레포

권장 배치는 `AGENTS.md` 를 하네스 중립 원본으로 두고, `CLAUDE.md` 는 실제 파일로
`@AGENTS.md` 로 시작한 뒤 Claude 전용 내용을 잇는 것입니다(이 레포 자체가 그
구조입니다). `CLAUDE.md` 를 `AGENTS.md` 로의 심링크로 두는 것도 마찬가지로
공유입니다 — 어느 쪽이든 `AGENTS.md` 는 심링크여선 안 됩니다.

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
| `omhc status [--json]` | 유일한 사람용 대시보드. `lag_bytes`와 인출률 포함 |
| `omhc log [--last N] [--grep P] [--verb V] [--file P]` | 색인된 이벤트를 한 줄씩 |
| `omhc show <E1\|#137> [--full]` | **원본 바이트를 오프셋으로 조회** (tier b 진입점) |
| `omhc note "<text>"` | 메모. 두 하네스의 에이전트가 맨 명령줄로 호출 가능 |
| `omhc clear` | 설치된 표식 제거 |
| `omhc mark --harness X` | 세션 시작 기록 (훅이 부른다) |
| `omhc brief --harness X [--wire claude\|cursor\|sdk]` | 표식 출력 (훅이 부른다) |

끄기: `OMHC_OFF=1` 또는 `~/.omhc/<repo-key>/off` 파일.

## 이 도구를 쓰지 말아야 할 때

**같은 하네스끼리는 네이티브 resume이 낫습니다.** Claude Code → Claude Code라면
`claude --resume <세션ID>`를 쓰십시오. 무손실이고 thinking 블록까지 보존됩니다.
omhc는 그것보다 **열등합니다** — 요약이니까요. 그래서 `from == to`면 파이프라인을
단축하고 아무것도 쓰지 않습니다.

omhc의 가치는 **벤더가 다를 때**입니다. 교차 벤더 재생은 thinking 블록 서명이
시스템 프롬프트와 선행 메시지까지 검증하므로 **원리적으로** 불가능합니다.

## 실측 사실 (설계 근거)

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

## 알려진 한계

- **Codex 툴 호출 매핑이 미검증입니다.** 이 머신의 Codex가 인증되지 않아(401)
  실물 `function_call` 레코드를 얻지 못했습니다. Rust serde 필드명 기준으로
  구현했고 모르는 페이로드는 예외 대신 `unparsed`로 계상됩니다. `codex login`
  후 `python3 tests/harvest.py --force` 로 골든을 갱신해야 합니다. 테스트
  클래스 이름에 `UNVERIFIED`를 남겨 뒀습니다.
- **실측(codex-cli 0.155.1): 신뢰되지 않은 `hooks.json`은 메시지 없이 조용히
  건너뛰어 `brief`가 한 번도 돌지 않습니다.** `deliver()`(Path B 포함)는 그
  `brief` 호출 안에서만 실행되므로, 훅이 신뢰되지 않으면 Codex로 들어가는
  방향은 아무것도 받지 못합니다 — `AGENTS.md` 관리 구간이나 outbox가 대신
  받는 게 아니라 아예 켜지지 않습니다. Codex→Claude 방향은 이 훅에 의존하지
  않고 rollout 파일을 직접 읽으므로 하루 루프의 절반은 그래도 동작합니다.
  Path B/outbox는 `brief`가 실제로 도는데 `install_handoff`가 실패할 때
  열립니다 — 대표적으로 `~/.codex/hooks.json`에 omhc 훅이 없을 때이지만,
  `~/.omhc` 쓰기 실패 등 다른 예외도 같은 경로를 탑니다(예: 수동
  `omhc brief --harness codex-cli`).
- **원본 포맷은 공식 계약이 아닙니다.** Claude Code의 on-disk 스키마는 문서화되지
  않았고 2026년 내내 파괴적으로 변했습니다(공식 `SessionStore`조차 엔트리를
  "opaque"로 선언합니다). 화이트리스트 + fail-open + `status`의 열화 보고로
  완화하지만, 깨질 것을 전제로 설계했습니다. 아카이브가 포인터인 이유가 이것입니다.
- **동시 사용은 v1 범위 밖입니다.** seam은 `omhc/due.py::due()` 하나이고, v2는
  반환형을 `List[Watermark]`로 바꾸고 `stale.py`를 같은 스트림의 두 번째
  소비자로 추가합니다. v1이 이미 그 기반(절단되지 않은 `paths` 열 + 바이트 오프셋
  순서)을 기록합니다.
- **픽스처는 커밋되지 않습니다.** 실제 대화 내용이라서요. `python3 tests/harvest.py`
  로 각자 머신에서 생성합니다.

## 테스트

```bash
python3 -m unittest discover -s tests -t . -q   # 약 12초, 하네스를 띄우지 않는다
bash tests/smoke.sh                             # 적대적 입력 7종
```

적합성 스위트(`tests/conformance/test_suite.py`)의 불변식 22개는 `REGISTRY` 위에
파라미터화됩니다 — **어댑터를 추가하면 테스트가 저절로 늘어납니다.**

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
