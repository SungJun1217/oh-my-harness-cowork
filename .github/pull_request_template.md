## 무엇을, 왜

<!-- 커밋 규칙과 같게: type(scope): 한국어 요약. 본문은 *왜* 를 설명 -->

## 확인

- [ ] `python3 -m unittest discover -s tests -t . -q` 통과
- [ ] `bash tests/smoke.sh` 통과
- [ ] `AGENTS.md` 의 불변식을 깨지 않음 (특히 900바이트 캡, 훅 경로 무예외)
- [ ] 세션 원문·픽스처를 커밋하지 않음
- [ ] 대상 브랜치가 `develop`
