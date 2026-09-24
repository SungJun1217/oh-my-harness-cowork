# 보안 정책

## 취약점 제보

공개 이슈로 올리지 말고 GitHub 의 **비공개 취약점 제보**를 써 주십시오:
[Security → Report a vulnerability](https://github.com/SungJun1217/oh-my-harness-cowork/security/advisories/new).

재현 절차, 영향받는 버전(`omhc/__init__.py` 의 `__version__` 또는 커밋 해시),
하네스 버전(Claude Code / Codex CLI)을 같이 적어 주시면 빠릅니다.

## omhc 가 읽고 쓰는 것

omhc 는 코딩 에이전트의 **세션 기록(실제 대화 내용)** 을 다룹니다. 무엇이 어디에
남는지 알고 쓰십시오.

| 무엇 | 어디 | 비고 |
|---|---|---|
| 하네스 원본 세션 파일 | `~/.omhc/<repo-key>/` | 복사가 아니라 **하드링크**. 원본을 지워도 바이트가 여기 남습니다 |
| 이벤트 오프셋 색인 (TSV) | `~/.omhc/<repo-key>/` | 이벤트당 약 115바이트 |
| `omhc note` 메모, 원장 | `~/.omhc/<repo-key>/` | |
| 핸드오프 (≤900바이트) | 다음 세션 컨텍스트, 또는 `AGENTS.md` 관리 구간, 또는 `<repo>/.omhc/outbox/` | `.omhc/` 는 gitignore 대상 |

- 네트워크 호출과 LLM 호출이 **없습니다.** 아무것도 외부로 보내지 않습니다.
- 아카이브를 지우려면 `~/.omhc/<repo-key>/` 디렉터리를 삭제하십시오. 하드링크라서
  하네스 원본까지 지워야 디스크에서 바이트가 사라집니다.
- 테스트 픽스처도 실제 대화라서 커밋하지 않습니다(`tests/fixtures/` 는 gitignore).
  이슈나 PR 에 세션 원문을 붙이지 마십시오.
