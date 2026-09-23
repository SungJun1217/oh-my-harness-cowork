# omhc — 하네스 간 작업 이어가기 도구 (v1 설계)

- 작성일: 2026-09-22
- 상태: 승인됨, 구현 계획 대기
- 레포: `/home/ec2-user/capstone/oh-my-harness-cowork`
- 근거 자료: `~/.claude/projects/-home-ec2-user-capstone-oh-my-harness-cowork/<session>/design-artifacts/` 의 `v1-design.json`, `adapter-contract.json`, `deep-research.json`

---

## 1. 문제

서로 다른 에이전트 하네스를 번갈아 쓸 때, 도구를 갈아타는 순간 맥락이 0으로 리셋된다. 현재는 사람이 메모리 파일을 손으로 쓰고 다음 에이전트에게 "저걸 읽어라"고 지시하는 방식이며, (ㄱ) 손이 가고 (ㄴ) 요약이 전부를 담지 못하고 (ㄷ) 세션이 비정상 종료되면 아무것도 남지 않는다.

v1 대상은 **Claude Code ↔ Codex CLI 양방향**, 같은 머신·같은 레포·**순차 사용**이다.

## 2. 확정 결정 (재논의 대상 아님)

1. **실전 개인 도구.** 포트폴리오도 오픈소스 제품도 아니다. 거친 UX와 하드코딩 경로를 허용한다. 성공 기준은 "내 하루가 실제로 편해지는가" 하나.
2. **2계층 충실도.** (a) 세션 시작에 자동 주입되는 작은 구조화 핸드오프, (b) 필요할 때 깊이 조회할 수 있는 원본 아카이브. (a) 단독은 기존 불만을 재생산하므로 불가.
3. **상시 자동 캡처.** 저장 명령을 사람이 기억할 필요가 없어야 하고, Ctrl-C·자동 압축·크래시에서 살아남아야 한다.
4. **v1은 순차 사용만.** 충돌 처리 없음. v2에서 동시 사용으로 확장하며 그때 stale-read 무효화가 핵심이 된다. v1은 v2가 재작성이 아닌 확장이 되도록 seam을 명시한다.
5. **어댑터 확장성은 요구사항이되, 지금 엮지는 않는다.** v1은 어댑터 2개만 구현한다. 3번째를 붙이는 비용이 "파일 하나 + 픽스처 하나"가 되도록 계약만 진짜로 존재해야 한다.
6. **1차 프로토타입(`/home/ec2-user/claude-lab/oh-my-agent-cowork`)은 아키텍처를 승계하지 않는다.** 실측 사실만 자산으로 가져온다.
7. **데몬은 가속기로만 존재한다.** 정확성을 담당하지 않는다.
8. **이름은 `omhc`.** 명령·상태 디렉터리·산출물 헤더가 모두 같은 단어를 쓴다.

## 3. 실측 사실 (설계 제약)

### 3.1 1차 프로토타입이 확보한 것

| # | 사실 | 설계 귀결 |
|---|---|---|
| F1 | 세션 파일에서 실제 대화가 차지하는 비중 — Claude Code **8%**, Codex **0.15%**. 나머지는 해당 하네스의 시스템 프롬프트 기계장치 | 화이트리스트 파싱만 한다. 기계장치는 파싱 자체를 하지 않으므로 누출될 수 없다 |
| F2 | 그 기계장치는 노이즈가 아니라 **주입 위험** — 다른 벤더 에이전트에게 존재하지 않는 툴·스킬을 쓰라는 명령형 지시가 된다 | 가드가 유도 텍스트에 대해 fail-closed. 재작성하지 않고 드롭만 한다 |
| F3 | 툴 어휘 교집합이 **공집합** — CC는 `Read/Edit/Bash/Task`, Codex는 `shell/apply_patch/update_plan` | IR 스키마에 툴 이름이 들어갈 필드를 두지 않는다. 중립 동사로만 축약한다 |
| F4 | 훅 비대칭 — CC에는 `PreCompact`/`SessionEnd`가 있으나 **Codex에는 종료·압축 훅이 없다**(6종 모두 "시작" 훅) | 캡처를 종료 시점이 아니라 **다음 세션 시작의 지연 소급 수집**으로 한다. 크래시 복구가 공짜로 따라온다 |
| F5 | 같은 벤더끼리는 `claude --resume`이 무손실이며 우월하다. 교차 벤더 재생은 thinking 블록 서명 때문에 **원리적으로 불가능** | `from == to`면 파이프라인을 단축하고 native resume 명령만 출력한다 |
| F6 | `claude -p --bare`는 구독 인증만 있으면 **에러 없이 멈춘다**(rc=124, stdout·stderr 빈 상태). 또 매 호출이 자기 세션 로그를 남겨 자기 참조 루프를 만든다 | **LLM 호출을 어디에도 두지 않는다.** F6이 구조적으로 발생 불가능해진다 |
| F7 | 프로토타입의 주입 상황판이 **15KB(약 4~5천 토큰), 상한 없음, 세션마다 증가**. 갈아타지 않는 세션에도 고정 과세. 또 `tool_result`를 버려 (b)계층이 사실상 부재 | **재설계가 존재하는 이유.** 하드 바이트 캡 + "아카이브는 원본 그 자체" |

### 3.2 이 머신 정찰로 확인한 것

- Python **3.9.25**. `@dataclass(slots=True)`는 **존재하지 않는다**(TypeError). `frozen=True` 단독으로 미선언 속성 할당까지 막히는 것을 실측 확인했다 → 불변 보장은 유지된다.
- Claude 프로젝트 디렉터리 슬러그는 `re.sub(r'[^a-zA-Z0-9]', '-', path)`이며 200자 초과 시 절단 + base36 해시 분기가 있다. 실측 **27/27 일치**.
- `cwd`는 **첫 레코드에 없다.** 최초 등장은 index 3이고, 490개 레코드 중 **124개에는 아예 없다** → "첫 줄에서 cwd 읽기" 금지.
- **마커 기반 탐지만으로는 부족하다(2026-09-23 실측).** 가장 큰 기계장치 덩어리인 `skill_listing` 본문은 **29,958자인데 태그가 하나도 없는 평범한 불릿 목록**이다. 즉 어떤 차단목록도 이것을 잡지 못한다. 1차 방어는 파서가 `attachment` 레코드를 **아예 파싱하지 않는 것**이고, 가드에는 기계 유도 텍스트 **2000자 상한**을 백스톱으로 둔다 — 산출물 전체 예산이 900바이트이므로 한 슬롯 값이 그보다 길 수 없다는 구조적 근거다.
- 키워드 스캔이 거짓 양성을 낸다는 것도 실측됐다: `<system-reminder>`가 이 레포 트랜스크립트에 **146회** 등장하는데 상당수가 **그 마커를 논의하는 산문**이다. 그래서 가드는 출처로 범위를 한정하고, 사람이 쓴 문장에는 마커·툴이름 금지를 적용하지 않는다.
- `<system-reminder>`는 초기 측정 시점에 **79회** 등장했다. 후보 설계들이 넣은 백슬래시 섞인 리터럴은 **0회 매칭** → 차단목록 항목마다 "실물에서 목격됨"을 메타테스트로 강제하고, 항목에 백슬래시를 금지한다.
- 서브에이전트 워크플로우 파일 **87개**에 `type:"user"` 레코드 **1766개**가 있고 `isSidechain: True`다. 역할 기반 허용목록은 이것을 **사람의 말로 판정해 축자 중계**한다 → `author`를 3값(`human|agent|harness`)으로 두고 **`human`만 축자 중계**한다.
- `SessionStart` 훅이 한 세션 안에서 **6회 발동**했다(PreToolUse 39, PostToolUse 39, Stop 18, UserPromptSubmit 11) → 세션당 1회 게이트가 필수다.
- "세션"은 파일이 아니라 **디렉터리**다. 사이드카 38MB(`tool-results/`, `subagents/`)에 본문은 1.1MB. 큰 `tool_result`는 `<persisted-output>` 스텁으로 치환되고 내용이 외부 파일로 빠진다.
- 레코드별 타임스탬프는 **단조가 아니다** — 최대 파일에 역행 지점 254개, 최대 52ms 역행 → 순서 기준으로 쓸 수 없다.
- Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`이 source of truth이고 sqlite들(`thread_history_1`, `state_5`, `memories_1`, `goals_1`, `queue_1`, `logs_2`)은 그 투영이다. `~/.codex/history.jsonl`은 이 머신에 없다.
- 이 머신의 유일한 Codex rollout에는 **어시스턴트 메시지 0개, 툴 호출 0개**다 → Codex 동사 매핑은 아직 실물로 검증되지 않았다. **최대 미지**.
- Codex `role=="user"` 레코드가 2개이고 **하나가 `<environment_context>…</environment_context>` 봉투**, 하나가 진짜 사람의 프롬프트다 → 역할 기반 필터만으로는 Codex 환경 프롬프트를 중계한다.
  - **확정(2026-09-23, 실물 확인 2회):** 정찰이 보고한 `content_item_kinds` 는 **실재한다.** 단 `payload` 최상위가 아니라 `payload.internal_chat_message_metadata_passthrough.content_item_kinds` 에 중첩돼 있다. (중간에 "그 필드는 존재하지 않는다"고 적었던 것은 최상위 키만 확인한 오판이었고 여기서 정정한다.) 실측값:
    - `['user.text']` — 진짜 사람의 프롬프트
    - `['environments.environment_context']` — `role=user` 인데 환경 프롬프트
    - `['host_skills.instructions', 'permissions.instructions', 'collaboration_mode.instructions']`, `['multi_agent.role_instructions']`, `['multi_agent.mode_instructions']` — `role=developer`
  - 따라서 **판별자를 둘 다 쓴다.** 메타데이터 kind 는 정확하지만 하네스별이므로 `user.` 접두 **허용**으로 판정한다(새 `user.*` kind 가 생겨도 사람의 말을 잃지 않는다). 봉투 구조는 덜 정확하지만 하네스와 무관하게 동작하며 Claude Code와 같은 코드를 쓴다. 하나가 실패해도 다른 하나가 받는다.
  - `role=="developer"` 레코드는 `<skills_instructions>`(2484자) · `<permissions instructions>`(341) · `<collaboration_mode>`(1328) · `<multi_agent_role>`(2429) · `<multi_agent_mode>`(271) 로 순수 기계장치다. 역할 화이트리스트에서 제외한다.
- Codex 샌드박스가 `{"type":"read-only"}`로 관측된 사례가 있다 → 레포 안으로 쓰는 경로를 전제하면 안 된다.
- `~/.claude`와 `~/` 는 **같은 장치**다 → 하드링크 가능.

### 3.3 웹 조사로 확인한 것 (검증된 finding 11개 중 설계 관련)

- 선행 사례 생태계는 **넓지만 얇다.** 2026-09 기준 유지보수되는 것은 `casr`와 `cli-continues` 둘뿐. 이 문제는 아직 해결되지 않았다.
- 선행 사례는 두 아키텍처로 갈린다: **(A)** 손실 요약 → 마크다운 문서 → 프롬프트 주입, **(B)** 대상 하네스 세션 저장소에 **합성 세션 파일을 위조**해 넣고 native resume 호출. v1은 (A) 계열이며 **(B)는 채택하지 않는다**(§11 참조).
- 업계가 **flat canonical IR + 허브앤스포크(2N 변환기)** 로 수렴했다. 독립 프로젝트 9개 이상에서 동일 패턴이며 키 리네임 방식은 하나도 없다 → 어댑터 계약의 방향이 외부 확증됨.
- 충실도 상한은 구조적이다: 메시지 순서·역할 의도·텍스트·타임스탬프만 보존되고 tool 페어링·tool-call id·thinking 블록·세션 id·프로바이더 메타데이터는 버려진다.
- Claude Code 스키마는 **비문서이며 2026년 내내 파괴적으로 변했다.** 유일한 공식 표면인 Agent SDK `SessionStore`조차 엔트리를 "opaque"로 선언하며 스키마 문서화를 거부한다. Codex는 오픈소스라 소스 검증이 가능하다 — **비대칭이 존재한다.**
- 핸드오프에 가장 필요한 레코드인 **compaction 경계**(Claude `summary`, Codex `compacted`)를 가장 성숙한 리더(`agent-history`)조차 "Not handled"로 명시한다.
- 상호운용 **표준은 검증을 통과한 것이 0건**이다(AGENTS.md·MCP 메모리·ACP·AG-UI 모두). 표준이 해결해 줄 것을 기다릴 대상이 아니다.

## 4. 아키텍처: 두 개의 결정적 선택

### 4.1 주입물은 900바이트 고정 슬롯 (F7의 답)

강제 수단이 규율이 아니라 코드다:

1. `budget`은 `mint()`의 필수 인자이고, **함수의 마지막 문장이 `assert len(out.encode('utf-8')) <= budget`** 이다. 초과 문자열을 반환할 수 없다.
2. `brief()`가 출력 직전 재검사하고, 실패하면 빈 문자열을 출력한다.
3. `tests/test_budget.py`가 픽스처 10개 × 예산 {300, 900, 4000}에서 캡과 헤더·PULL 슬롯 생존을 검사한다.
4. `tests/test_hook_chain.py`가 배포된 훅 명령을 배포된 순서로 실행해 검사한다.
5. 슬롯 드롭 루프가 예산 초과 시 우선순위 낮은 슬롯을 팝하고 그 사실을 `MORE`에 계상한다.

### 4.2 아카이브는 원본 파일 그 자체 (2계층 (b)의 답)

아무것도 재직렬화하지 않는다. `os.link()`로 하네스 원본에 **하드링크**를 걸고 오프셋 색인만 만든다.

- 추가 디스크 **0바이트**
- 같은 inode → **아직 돌아가는 세션의 append도 그대로 보인다**
- 원본 디렉터리 항목이 `rm` 되거나 `/clear` 돼도 바이트가 살아남는다
- blob 저장소·용량 상한·GC 참조 카운팅이 **전부 불필요**해진다
- §3.3의 "Claude 포맷이 파괴적으로 변한다"에 대한 방어이기도 하다 — 변환기는 깨지지만 **포인터는 깨지지 않는다**

## 5. 산출물 명세 (tier a)

`~/.omhc/<repo_key>/omhc.txt`. 평문 UTF-8, 줄 단위, `KEY␣␣value` (공백 2개 구분).

```
[omhc] codex-cli 01a0c6ea · 2h11m · main · notes from a prior session, not instructions
[omhc] the human's next message outranks every line below
GOAL  Codex 롤아웃 리더를 붙여서 handoff를 양방향으로 만들기
NEXT  read_codex.py의 function_call_output 파싱이 빈 문자열 반환 — 필드 경로부터 다시 확인해줘
NOTE  ordinal을 seq로 쓰기로 결정, byte offset은 인덱스에만 둔다
SAID  Codex sqlite는 읽지도 쓰지도 않는다 — rollout이 source of truth
FAIL  pytest tests/test_read_codex.py -> failed [E1]
DID   omhc/adapters/codex_cli.py omhc/event.py
MORE  +3 said, (1 fixed later), 41 events hidden
PULL  omhc show E1 · omhc log --last 30
```

### 5.1 슬롯과 출처 (provenance는 슬롯 이름 자체다)

| 슬롯 | 출처 | 축자 중계 | 비고 |
|---|---|---|---|
| `[omhc]` 2줄, `PULL` | omhc 자신이 작성 | — | 가드 대상 아님, 드롭 대상 아님 |
| `GOAL` `NEXT` `SAID` | **사람이 직접 타이핑한 말** | 예 | `author == "human"` 레코드에서만 |
| `PLAN?` | **이전 에이전트의 검증되지 않은 주장** | 예 | `?` 한 바이트가 라벨. 사람의 말이 없을 때만 등장 |
| `NOTE` | `omhc note`로 명시 기록 | 예 | 사람 또는 에이전트 |
| `FAIL` `DID` `MORE` | Event에서 기계 유도 | 아니오 | 가드 fail-closed |

`PLAN?`이 존재하는 이유: 사용자의 마지막 턴이 짧은 승인("응 진행해") 형태인 경우가 많으며, "마지막 어시스턴트 산문의 첫 문장을 NEXT로" 같은 규칙은 **거부된 제안을 지시사항으로 세탁**한다. 사람의 말이 없으면 `NEXT`를 비우고 `PLAN?`으로 내린다.

**구현 시 변경: `DEC` → `SAID` (2026-09-23).** 원래 설계는 "결정"을 담는 `DEC` 슬롯을 뒀으나, 결정론적 추출로는 **무엇이 결정인지 알 수 없다.** 지나가는 말을 결정으로 라벨링하는 것이 심사 3번이 "serious" 로 지목한 실패 모드 그 자체다. `SAID` 는 해석 없이 사람의 말을 인용만 한다 — 중간 턴 중 긴 것 우선으로 최대 3개.

**구현 시 추가: 슬롯 우선순위와 바이트 상한.** 실물 산출물을 눈으로 본 뒤 두 가지를 고쳤다.
- 우선순위: `NEXT`(60) > `GOAL`(50) > `FAIL`(45) > `NOTE`(40) > `DID`(38) > `PLAN?`(35) > `SAID`(10). 초기 구현에서 `PLAN?`(이전 에이전트의 검증되지 않은 주장)이 `GOAL` 을 밀어내고 살아남는 일이 실제로 벌어졌다. 검증된 사실이 주장보다 낮으면 안 된다.
- 상한은 **바이트 기준**이다. 한글은 UTF-8 에서 글자당 3바이트이므로 글자 수로 자르면 200자 슬롯 하나가 600바이트를 먹는다 — 749바이트 산출물에 151바이트 여유가 남았는데도 슬롯 5개가 버려지는 것을 실측했다.
- 예산이 작아 슬롯이 하나도 남지 않으면 최우선 슬롯을 잘라서라도 한 가지는 말한다. 내용 없는 표식은 쓸모가 없다. 그리고 줄 단위로만 줄인다 — `PULL` 이 `omhc log --file .git` 처럼 중간에서 끊기면 없는 명령보다 나쁘다.

### 5.2 실패 해소 패스

`FAIL` 슬롯을 내보내기 전에, 같은 인자의 앞 40바이트를 공유하고 `ok=True`인 **이후 이벤트가 있는지 전방 탐색**한다. 해소된 실패는 `FAIL`에서 빼고 `MORE`에 `(1 fixed later)`로 계상한다. 없으면 아침 핸드오프가 한 시간 전부터 그린인 스위트에 대해 "pytest: 3 failed"를 말한다.

### 5.3 공개 의무

`MORE` 줄은 감춘 것을 밝힌다 — 드롭된 슬롯 수, 숨긴 이벤트 수, 가드가 withheld한 줄 수.

## 6. 아카이브 계층 명세 (tier b)

`~/.omhc/<repo_key>/` 아래:

| 경로 | 내용 |
|---|---|
| `pinned/<session-id>/source.jsonl` | 하네스 원본에 대한 `os.link()` 하드링크 |
| `pinned/<session-id>/tool-results/*.txt` | 사이드카 각각에 대한 하드링크 — `<persisted-output>` 스텁을 해소 가능하게 유지 |
| `pinned/<session-id>/rescued/<agentId>.output` | **유일한 실제 복사본.** `/tmp` 참조물은 다른 파일시스템이고 재부팅에 죽는다. 5MB 상한 |
| `index/<session-id>.idx` | TSV, Event당 1행: `seq epoch author verb ok offset length paths arg120`. **실측 115 B/행** — 3.2MB 세션의 275 Event가 31.5KB(원본의 약 1%). append-only, 마지막 행의 `offset+length`에서 재개 가능. 잘린 마지막 행은 건너뛴다 |
| `refs.tsv` | `tag session_id source_path offset length`. 매 mint가 재작성. 900바이트 안에 세션 id 없이도 `omhc show E1`이 풀리는 근거 |
| `ledger.jsonl` (전역, `~/.omhc/`) | 세션 생명주기 및 인출 이벤트. §7.1 |

**받는 에이전트가 도달하는 방법**: 산출물의 `PULL` 줄이 미리 채워진 명령줄이다. 맨 명령줄은 두 하네스에서 교집합이 비어 있지 않은 유일한 메커니즘이고(Claude `Bash`, Codex `shell`) 어느 벤더의 툴 이름도 부르지 않는다. `omhc`는 `~/.local/bin/omhc` 심링크로 둔다(`claude`·`codex`가 이미 그 디렉터리의 심링크이며 두 하네스의 PATH에 있음을 확인). `mint()` 시점에 `shutil.which('omhc')`로 확인하고 풀리지 않으면 절대경로를 대신 넣는다.

## 7. 캡처와 주입

### 7.1 캡처 — 지연 소급 수집이 유일한 경로

훅으로 "지금 저장"하지 않는다. `omhc mark`가 세션 시작에 원장에 한 줄(약 220바이트, `O_APPEND` 단일 `write(2)`, `PIPE_BUF` 이하라 원자적)을 남기고, 실제 읽기는 **다음 세션 시작 때** 일어난다.

- 원장 시작 줄: `{repo, harness, session, path, cwd, event:"start", epoch, ms}`
- 원장 인출 줄: `{repo, harness, session, event:"pull", tag, epoch}`

Ctrl-C·자동 압축·크래시가 **정의상 문제가 되지 않는다** — 디스크 로그는 이미 존재한다. F4의 우회책을 정식 경로로 승격한 것이다.

### 7.2 주입 — Claude Code

`SessionStart` 훅이 `omhc brief --harness claude`를 호출하고, stdout으로 `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"…"}}`를 낸다. 핸드오프가 없으면 **빈 문자열**을 출력한다.

세션당 1회 게이트: `~/.omhc/<repo_key>/gate/<session_id>`를 `O_CREAT|O_EXCL`로 선점한다. 실패하면 빈 문자열. §3.2의 "SessionStart 6회 발동"에 대한 방어다.

### 7.3 주입 — Codex (Path A → Path B)

- **Path A**: `~/.codex/hooks.json`의 `SessionStart`. 단 훅 신뢰(`HookStateToml{enabled, trusted_hash}`)가 손으로 떨어뜨린 파일을 거부할 수 있다.
- **Path B (fallback)**: `<repo>/AGENTS.md`의 `<!-- omhc:begin -->` / `<!-- omhc:end -->` 관리 구간에 산출물 텍스트를 밀어넣는다. 같은 900바이트 상한, 24시간 만료, 어떤 `omhc` 호출에서든 붕괴. 원자적 `tmp + fsync + os.replace`.
  - **사용자 승인 사항**: `AGENTS.md`를 `.git/info/exclude`에 넣어 `git status`에 보이지 않고 이 클론 밖으로 나가지 않게 한다.
  - 훅 신뢰도 모델 협조도 필요 없는 유일한 Codex 방향 경로다.

Codex→Claude 방향은 **훅에 의존하지 않는다**: 어댑터가 최근 14개 날짜 디렉터리를 글롭하고 각 rollout의 **첫 줄만** 읽어 `payload.cwd`를 확인한다. 즉 Path A/B가 둘 다 실패해도 하루 루프의 절반은 확실히 동작한다.

### 7.4 보편 바닥

주입 경로가 전부 실패하면 `<repo>/.omhc/outbox/<iso-ts>-to-<adapter_id>.md`로 떨어진다. **모든 경로가 receipt로 끝나며 조용히 실패하지 않는다.**

## 8. 가속기 데몬

`omhc watch` — 활성 트랜스크립트를 tail하며 `index/<sid>.idx`를 증분 확장하고 핸드오프를 미리 발행한다.

- **효과는 전환하는 순간에 나타난다**: 1MB 트랜스크립트여도 세션 시작이 150ms를 넘지 않는다. 데몬이 없으면 그 파싱이 훅 안에서 일어나고 큰 파일에서는 초 단위가 된다.
- **정확성 의존 0**: 죽어 있으면 `brief`가 인라인 파싱으로 떨어진다. 산출물은 동일하고 느려질 뿐이다.
- 단일 인스턴스 락파일, **유휴 30분 후 자동 종료**, `omhc status`가 생존과 `lag_bytes`를 보고한다.
- v2의 stale-read 감시가 붙을 자리다.

## 9. 관측 가능성과 끄기

- `omhc status`: repo key / 원장 꼬리 / 고정된 세션별 **`lag_bytes`**(색인 워터마크 대 현재 `st_size`) / `brief p95` / Codex `[unparsed]` 비율 / 데몬 생존. 다섯 검사 모두 **PASS 또는 FAIL을 출력하고 SKIP은 없다**.
- **인출률 회계**: `omhc show`/`log`가 원장에 `{"event":"pull"}`을 남겨 `status`가 `pulled 3 of last 4 injections`를 출력한다. 6주 뒤 "이 세금이 값을 하는가"를 판단할 유일한 숫자다.
- **끄기**: 환경변수 `OMHC_OFF=1` 또는 `~/.omhc/<repo_key>/off` 마커 파일 → `brief`가 빈 문자열을 내고 exit 0.

## 10. 두 개의 seam

### 10.1 하네스 어댑터 seam

`omhc/adapter.py`의 Protocol — **메서드 5개, capability 플래그 2개**가 전부다.

```python
class HarnessAdapter(Protocol):
    adapter_id: str                       # ^[a-z0-9-]+$, 모듈 stem과 일치
    capabilities: FrozenSet[Capability]    # {READ} / {WRITE} / 둘 다

    def __init__(self, *, home: Optional[Path] = None, now=time.time) -> None: ...
    def detect(self) -> HarnessPresence: ...                    # 싸야 하고 예외 금지
    def list_sessions(self, repo_root: Optional[str]) -> List[SessionRef]: ...   # READ
    def read_session(self, ref: SessionRef) -> SessionRead: ...  # 파일 하나 → 중립 Event
    def native_resume_hint(self, ref: SessionRef) -> Optional[str]: ...
    def install_handoff(self, bundle: HandoffBundle) -> InstallReceipt: ...      # WRITE
```

부수 규칙: `__init__`에서 I/O 금지. `home=`/`now=`를 키워드로 받아야 한다. 모르는 레코드 타입은 **기본 DROP이며 보고된다**. `author=="human"`은 **최상위 세션 파일에서만** 나온다. 글롭은 **깊이 1만**.

- 예외 3종: `AdapterUnavailable`, `UnsupportedFormat`, `NoInjectionChannel`
- 레지스트리는 **클래스의 literal dict**. entry_points도 스캐닝도 자동 등록도 없다
- 새 하네스 비용 = 5개 구현 + 플래그 선언 + 픽스처 1개. **코어 수정 0**
- **적합성 스위트**: 불변식 22개가 `REGISTRY` 위에서 자동 파라미터화된다. 어댑터를 추가하면 테스트가 저절로 늘어나고 "붙인 것 같다"가 아니라 **증명**된다. 읽기 전용 어댑터는 쓰기 절반을 가짜로 채우지 않고 통과한다

### 10.2 동시성 seam

**seam은 `omhc/due.py::due(repo_key, my_harness, my_session_id, now) -> Optional[Watermark]` 함수 하나**이며, `Watermark = namedtuple('Watermark', 'repo_key harness session_id path event epoch')`과 색인 TSV의 `paths` 열이 함께 seam을 구성한다.

v1의 순차 가정 전체가 이 함수 안에 있다. 상류(ledger·locate·어댑터·index·pin·guard)와 하류(mint·brief·agents_md)는 이미 순서 무관이며, 리더가 append-only 로그를 바이트 0부터 EOF까지 스트리밍하므로 **여전히 자라는 파일을 이미 견딘다**(양쪽 하네스에서 실측 확인).

v2는 반환형을 `List[Watermark]`로 바꾸고 `omhc/stale.py::check(...) -> List[StaleRead]`를 **같은 스트림의 두 번째 소비자로 추가**한다(기존 코드 수정이 아니라 추가). `StaleRead = namedtuple('StaleRead', 'path read_by read_seq written_by written_seq')`. v1이 이미 쓰는 데이터만 읽는다 — 절단되지 않은 `paths` 열, `verb`(inspected 대 modified), 순서 키.

**순서 키는 파일 mtime도 레코드별 타임스탬프도 아니다.** (원장의 세션 시작 epoch, 소스 파일 내 바이트 오프셋)이다. §3.2의 역행 254건이 근거이며, 데몬 안이 바로 이 지점에서 탈락했다.

## 11. 채택하지 않은 대안과 이유

| 대안 | 기각 이유 |
|---|---|
| **상시 테일러 데몬이 정확성을 담당** | 제안 저자 스스로가 데몬이 "정확성에 아무것도 기여하지 않는다"고 인정. v2 기반(`Event.mt`)이 증명 가능하게 틀림 — "순수·I/O 없음"으로 선언한 정규화기가 파일을 stat할 수 없고 테일러는 1~5초 늦게 stat하므로 staleness 판정이 **정확히 동시 사용 상황에서 거짓**이 된다. 가속기로만 승격해 채택(§8) |
| **나가는 에이전트가 자기 핸드오프를 직접 작성** | 가치 전체가 검증되지 않은 게이트 3개에 걸려 있고, 그중 하나(`~/.omhc` 쓰기 가능)가 read-only로 관측된 Codex 샌드박스에서 실패하면 **Codex 양방향이 모두 죽는다**(주 경로로 AGENTS.md를 이미 소진한 상태). 단, `omhc note`로 에이전트가 자발적으로 기여하는 경로는 남긴다 |
| **합성 세션 파일 위조 + native resume (선행 사례 (B)형)** | (ⅰ) 위조된 세션은 받는 모델이 **자기 기억으로 착각**한다 — 울타리를 칠 자리가 없다. (ⅱ) 사람이 다른 명령을 타이핑해야 하므로 일상 마찰 렌즈가 기각. (ⅲ) Claude 포맷이 비문서·파괴적 변경 중이라 쓰기가 특히 위험하다. **Path A·B가 모두 실패했을 때의 최후 수단으로만 기록** |
| **LLM distiller (`claude -p` 등)** | F6. 조용히 멈추는 실패 + 자기 참조 코퍼스. LLM 호출을 두지 않으면 구조적으로 발생하지 않는다 |
| **Codex sqlite 읽기/쓰기, `memories_1`/`goals_1` 주입** | 투영이며 source of truth가 아니다. `memories` 스토어에 thread별 `raw_memory`·`rollout_summary`가 있는 것은 확인했으나 경로·계약이 미검증. **v1 범위 밖, 후속 조사 대상** |
| **PreCompact / PostCompact 훅** | 압축은 로그가 아니라 컨텍스트를 버린다. 로그는 그대로 남으므로 지연 수집으로 충분하고, `PreCompact`는 구조적으로 압축된 컨텍스트에 되쓸 수 없다 |
| **키 리네임식 포맷 변환** | §3.3. 레코드 레벨 비호환이며 재분할 + id 재페어링이 필요하다. 독립 프로젝트 9개 이상이 전부 중립 IR로 갔고 키 리네임은 하나도 없었다 |

## 12. 컴포넌트와 파일 레이아웃

두 워크플로우가 독립적으로 산출한 명명을 다음과 같이 **통일**했다(이 문서가 기준):

- 중립 레코드 이름은 `Event`(색인·동사 어휘와 일관), 화자 필드는 `author`(3값)
- 주입 텍스트 생성은 `mint.py` **한 곳**에 둔다. 두 설계가 각각 `mint()`와 `render()`를 "유일한 생성지점"으로 선언했으므로 후자를 없앤다 — 생성지점이 둘이면 §16-4의 외래 provenance 단정이 한쪽만 덮는다
- 하네스별 지식은 **어댑터 모듈 안에** 둔다. `locate.py`는 레포 수준 식별만 담당한다
- 상태 루트는 `~/.omhc/`, 레포 내 바닥은 `<repo>/.omhc/outbox/`

```
bin/omhc                      # ~/.local/bin/omhc 심링크 대상
omhc/
  cli.py                      # 7개 서브커맨드 디스패치, 훅 stdin 읽기, argparse, 3.9
  locate.py                   # repo_key / resolve_repo_root / is_within / relativize
  ledger.py                   # append-only 전역 원장. 이것만 원장을 쓴다
  event.py                    # Event + 닫힌 동사 어휘. 툴 이름이 들어갈 필드가 없다
  guard.py                    # 외래 provenance 판정. 유도 텍스트에 fail-closed, 드롭만
  index.py                    # 오프셋 색인 append/lookup. 재개 가능, grep 가능
  pin.py                      # 하드링크로 원본 바이트 생존시키기
  due.py                      # 어느 외래 세션을 알릴지 결정 ← 동시성 SEAM
  mint.py                     # Event → ≤900바이트. 주입 텍스트를 만드는 유일한 코드.
                              #   예산·슬롯 우선순위·실패 해소 패스를 소유
  managed_block.py            # 마커 구간 splice/strip. 원자적
  agents_md.py                # Codex Path B
  brief.py                    # 훅 와이어 셰이프. 절대 예외를 던지지 않는다
  gate.py                     # O_CREAT|O_EXCL 세션당 1회
  watch.py                    # 가속기 데몬
  deliver.py                  # 라우팅 + resume_instead 단축 + 보편 바닥
  adapter.py                  # HarnessAdapter Protocol, Capability, 예외 3종
  adapters/
    __init__.py               # REGISTRY = 클래스 literal dict
    claude_code.py            # {READ, WRITE}
    codex_cli.py              # {READ, WRITE}
  known_non_harnesses.py      # 모델/엔드포인트 → 실제 호스트 하네스 매핑
hooks/
  claude-settings.fragment.json
  codex-hooks.json
tests/
  conformance/suite.py        # 불변식 22개, REGISTRY 위에 파라미터화
  conformance/denylist.py     # 항목마다 목격 증거 필요, 백슬래시 금지
  fixtures/                   # 실물 10개 + 위조 compaction 2개 + 위조 Codex 툴호출 3개
  golden/*.omhc               # 바이트 일치 골든
  smoke.sh
  harvest.py                  # 픽스처 수확 스크립트
docs/superpowers/specs/2026-09-22-omhc-design.md
```

## 13. 명령 표면 (7개)

| 명령 | 역할 |
|---|---|
| `omhc brief --harness claude\|codex [--budget 900] [--force] [--dry-run]` | 훅 경로. 훅 와이어 JSON 또는 빈 문자열 |
| `omhc mark --harness claude\|codex [--event start\|end]` | 원장에 약 220바이트 한 줄 |
| `omhc show <TAG\|#SEQ> [--full]` | tier (b) 진입점. `refs.tsv`로 태그를 풀고 원본 바이트를 낸다 |
| `omhc log [--last 30] [--grep PAT] [--verb …] [--file PATH]` | Event당 가드 통과 한 줄 |
| `omhc note "<text>"` | `notes.txt`에 추가. 두 하네스의 에이전트가 맨 명령줄로 호출 가능 |
| `omhc status [--json]` | 유일한 사람용 대시보드. doctor 검사 포함, SKIP 없음 |
| `omhc watch [--stop]` | 가속기 데몬 |

## 14. v1 범위 밖

데몬이 정확성을 담당하는 구조 / 동시 사용과 모든 충돌 처리 / stale-read 무효화(기반만 기록) / **LLM 호출 일체** / Codex SQLite 쓰기 / Codex 메모리 루트 쓰기 / `codex resume`로 재개 가능하게 만들기 / PreCompact·PostCompact / watcher·inotify·소켓·MCP·TUI·상태줄·Claude 스킬·플러그인 래퍼 / Claude Code·Codex 외 하네스 / 이미지·스크린샷·thinking 텍스트(thinking은 이 머신에서 4,970개 블록 중 4,961개가 빈 문자열) / 패키징·pip·venv·CI·버저닝·다중 머신 동기화 / 가드의 base64 규칙을 넘는 비밀 삭제(트랜스크립트는 이미 평문으로 존재하며 omhc가 노출을 늘리지 않는다).

## 15. 빌드 순서 (각 단계가 관찰 가능한 것으로 끝난다)

1. **골격·식별·원장·픽스처.** `bin/omhc`, `cli.py`(mark + status만), `locate.py`, `ledger.py`, `tests/harvest.py`. 픽스처 10개 수확 — 최대 파일, 실행 중 파일, 압축 이어짐 파일, Codex rollout, **위조 compaction 2개**, **위조 Codex 툴호출 3개**. 관찰: `omhc mark` 후 `omhc status`가 원장 꼬리와 repo key를 출력.
2. **가드 뒤의 두 리더.** `event.py`, `guard.py`, `adapters/claude_code.py`, `adapters/codex_cli.py`, `omhc log --file <fixture>`. 관찰: 중립 동사 줄이 출력되고 기계장치가 한 줄도 새지 않음.
3. **아카이브.** `index.py`, `pin.py`. 관찰: `ls -li`로 inode 공유 확인, 원본에 append하면 고정 경로로 보임, `omhc show #137`이 색인을 통해 seek.
4. **산출물과 판단.** `mint.py`, `render.py`, `due.py`. 관찰: 바이트 일치 골든 3개, 예산 테스트 그린, 실패 해소 테스트 그린.
5. **양쪽 훅 배선과 훅 체인 증명.** `brief.py`, `gate.py`, `managed_block.py`, `agents_md.py`, `deliver.py`, 훅 파일 2개, `smoke.sh`, `test_hook_chain.py`. 관찰: 배포된 훅 명령을 배포된 순서로 실행한 결과가 단정과 일치.
6. **실제 종단 수락 1회.** Codex에서 10분 작업(의도적 실패 `pytest` 포함) → 종료 → Claude Code 시작 → 새 트랜스크립트의 첫 attachment가 `hook_additional_context`이며 900바이트 이하임을 확인.

가속기 데몬(`watch.py`)은 **5단계 이후 선택 단계**로 붙인다. 정확성 의존이 없으므로 종단 수락의 전제가 아니다.

## 16. 완료 기준 (반증 가능)

**실측 결과 (2026-09-23):** 순수 단위 스위트 **227개 / 0.978초** — 기준 충족. 전체 스위트(적합성 22개 + 훅 체인 14개 포함) **298개 / 8.5초** — 훅 체인이 배포된 명령을 실제 프로세스로 60여 회 띄우므로 느린 것이 정상이다. 아래 8개 중 **6개 충족, 2개는 Codex 인증 부재로 차단**.

1. ✅ `python3 -m unittest` 가 **1초 이내** 그린(순수 단위 227개). **어떤 단위 테스트도 하네스를 띄우지 않는다**. 테스트 러너는 stdlib `unittest` 다(의존성 0 원칙).
2. ✅ `test_hook_chain.py`가 훅 파일의 명령을 **배포된 순서 그대로** 임시 `$HOME`에서 실행하고, `(codex@t1, claude start)` → 유효 JSON, 재발동 → 빈 출력을 단정.
3. ✅ `test_budget.py`가 픽스처 10개 × 예산 {300, 900, 4000}에서 캡을 단정하고, 예산 300 이상에서 헤더·PULL 슬롯 생존을 단정.
4. ✅ `test_no_foreign_provenance.py`가 픽스처 10개에서 mint해 **교차 벤더 툴 이름 40개가 기계·에이전트 슬롯에 0회**, 기계장치 태그 8종이 **어디에도 0회**임을 단정.
5. ✅ `smoke.sh`가 (7종, 전부 빈 stdout + exit 0) 적대적 입력 5종(원장 없음 / 원장에 깨진 반줄 / `transcript_path` 없음 / `transcript_path=/dev/null` / `$HOME` 쓰기 불가)에서 **빈 stdout과 exit 0**.
6. ⛔ **차단됨 (Codex 인증 부재, 401)** — 수동 수락: 실제 Codex 세션 후 실제 Claude Code 세션에서 새 트랜스크립트에 `hook_additional_context`가 존재.
7. ✅ **Claude 단독 세션 10회 연속에서 주입 0회**이고 `omhc status`가 `injected 1 time in 7 days`를 보고 — F7의 0 케이스를 주장이 아니라 **관측**으로 확인.
8. 🟡 **부분 충족** — `omhc status`가 여섯 검사 모두 PASS 또는 FAIL을 출력하고 SKIP이 없다(충족). 다만 Codex `[unparsed]` 비율을 **실제 툴 호출을 담은 rollout**에 대해 측정하는 것은 인증 부재로 차단됐다.

**차단 해제 방법:** `codex login` → `codex exec -s read-only 'run ls and tell me the first file'` → `python3 tests/harvest.py --force` → `python3 -m unittest tests.test_codex_cli`. 그러면 §17-4와 위 6·8번이 함께 풀린다. 그때까지 Codex 툴 호출 매핑은 Rust serde 필드명 기준 추정이며 테스트 클래스 이름에 `UNVERIFIED`로 표시돼 있다.

## 17. 구현 첫 10분에 실물로 확인할 것

| # | 확인 | 방법 | 시간 |
|---|---|---|---|
| 1 | Codex 훅 신뢰 통과 여부 | `~/.codex/hooks.json`에 `sh -c 'date >> /tmp/omhc-probe; cat > /tmp/omhc-hookin.json'` 심고 세션 시작 | 4분 |
| 2 | Codex 훅 stdin 필드 | 위 결과를 `json.tool`로 확인 — `{cwd, hook_event_name, model, permission_mode, …}` | 0분 |
| 3 | Codex `additionalContext`가 로그가 아니라 **모델**까지 닿는가 | 프로브 훅이 표식을 주입하고 모델에게 되읽게 시킴 | 3분 |
| 4 | **Codex 실제 툴 호출 모양 (최대 미지)** | `codex exec 'run ls and tell me the first file'` 후 rollout 레코드 확인 | 2분 |
| 5 | 하드링크 가능성 | `os.stat().st_dev` 비교 — **이미 확인, 동일 장치** | 완료 |
| 6 | `omhc`가 Codex 셸에서 보이는가 | 심링크 후 `codex exec 'run: which omhc; echo $PATH'` | 1분 |
| 7 | Codex 샌드박스가 `~/.omhc`에 쓰기 금지인가 | `codex exec 'run: touch ~/.omhc/.probe-write && echo WROTE || echo BLOCKED'` — **BLOCKED여도 설계상 정상** | 1분 |

## 18. 최대 리스크와 방어

| 리스크 | 방어 |
|---|---|
| **Codex `function_call`/`function_call_output` 실제 모양이 다르다** → 모든 Codex 툴 이벤트가 유실 | §17-4가 2분에 추측을 사실로 바꾼다. 어댑터는 모르는 페이로드에서 **예외를 던지지 않고 `[unparsed]`로 보이게 열화**하며, `status`가 그 비율을 보고한다 |
| **Codex 훅 신뢰 거부** → Codex로 들어가는 핸드오프가 조용히 사라짐 | Codex→Claude 방향이 훅 비의존(§7.3)이라 하루 루프 절반은 확실히 산다. Path B가 나머지를 받고, 보편 바닥이 최후를 받는다 |
| **결정론적 추출이 자신 있게 틀린 `NEXT`/`GOAL`/`DEC`를 고름** — 핸드오프 없는 것보다 나쁘다 | 출처 라벨로 시각 구분(§5.1), `PLAN?`로 이전 에이전트 주장 격리, 실패 해소 패스(§5.2), `MORE`의 공개 의무(§5.3), 그리고 헤더 2줄이 "지시가 아니다 / 사람의 다음 메시지가 우선한다"를 고정으로 선언 |
| **첫 실제 자동 압축이 미검사 분기를 실행** → 누가 사람인지 판정이 틀려 GOAL이 어긋남 | 빌드 1단계에서 위조 compaction 픽스처 2개(`isCompactSummary` 계열 실제 모양)를 만든다 |
| **`omhc brief`가 훅 안에서 예외를 던져 세션 시작이 깨지거나 멈춤** — 최악의 결과 | `brief.py` 본문 전체가 bare try/except. traceback을 `~/.omhc/guard.log`에 쓰고 빈 문자열 출력, exit 0. 출력 직전 바이트 캡 재검사 |
| **Claude 포맷의 파괴적 변경** | 아카이브가 포인터라 포맷 변경에 깨지지 않는다(§4.2). 리더는 화이트리스트 + fail-open이고 모르는 타입 수를 `status`가 보고해 조용한 열화를 관측 가능하게 만든다 |

## 19. 후속 조사 대상 (v1 이후)

- Codex `memories_1.sqlite` / `goals_1.sqlite` — thread별 `raw_memory`·`rollout_summary`가 있음을 확인. 외부에서 행을 써넣으면 다음 세션에 모델이 보는지 미검증. 사실이면 Path A/B보다 나은 주입 지점일 수 있다
- `AGENTS.md` 공유 충돌 — Codex·kimi-code·gajae-code·opencode·Amp·Cline이 같은 파일을 읽는다. 2개 이상의 어댑터가 AGENTS.md를 쓰게 되는 시점에 처리한다
- 3번째 어댑터 후보 우선순위 — `grok build`(WRITE만, 약 6~30 LOC), `kimi-code`(약 200 LOC, 가장 어댑터 친화적), Cursor(READ 먼저, 주입은 파일 규약)
- `~/.claude/projects/<slug>/memory/` 를 Claude 측 쓰기 채널로 쓸 수 있는지
