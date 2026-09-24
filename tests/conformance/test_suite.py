"""모든 어댑터가 지켜야 하는 불변식.

REGISTRY 위에 파라미터화되므로 **어댑터를 추가하면 테스트가 저절로 늘어난다**.
"붙인 것 같다"가 아니라 증명이 된다. 읽기 전용 어댑터는 쓰기 절반을 가짜로
채우지 않고도 통과한다 — 읽기와 쓰기는 독립 capability 다.

새 어댑터를 붙이는 사람이 해야 할 일:
  1. omhc/adapters/<harness>.py 에 메서드 5개를 구현하고 @_register 를 붙인다
  2. omhc/adapters/__init__.py 맨 아래에 import 한 줄을 추가한다
  3. tests/fixtures/<harness>/ 에 실물 세션 하나를 얼린다
그러면 이 파일의 불변식이 자동으로 그 어댑터에 적용된다.
"""
from __future__ import annotations

import inspect
import os
import tempfile
import unittest

from omhc import adapter as A
from omhc import adapters, guard
from omhc.event import AUTHORS, VERBS

import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from _repo import REPO  # noqa: E402

# 계약 클래스에서 유도한다. 손으로 적으면 계약과 검증 대상이 조용히 갈라진다.
REQUIRED_METHODS = tuple(
    name for name, value in vars(A.HarnessAdapter).items()
    if not name.startswith("_") and callable(value)
)



def adapter_ids():
    return sorted(adapters.REGISTRY)


def sessions_or_skip(case, adapter_id):
    """세션을 하나도 못 찾으면 **건너뛴다**. 조용히 통과시키지 않는다.

    0건 순회는 단정을 하나도 실행하지 않은 채 PASS 가 된다 — "외래 물질이 새지
    않는다" 는 보장이 검증됐다고 보고되면서 실제로는 아무것도 검사되지 않는,
    가장 위험한 종류의 통과다.
    """
    refs = adapters.get(adapter_id).list_sessions(REPO)
    if not refs:
        case.skipTest(
            "{}: {} 에 세션이 없다 — 이 머신에서 그 하네스를 쓴 적이 없거나 "
            "다른 체크아웃이다. 순회 불변식은 검증되지 않았다.".format(adapter_id, REPO)
        )
    return refs


class AdapterContract(unittest.TestCase):
    """계약 불변식. 어댑터마다 subTest 로 개별 보고된다."""

    def test_01_registry_holds_classes(self):
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                self.assertTrue(isinstance(adapters.REGISTRY[adapter_id], type))

    def test_02_adapter_id_matches_the_registry_key(self):
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                self.assertEqual(adapters.REGISTRY[adapter_id].adapter_id, adapter_id)

    def test_03_adapter_id_is_a_slug(self):
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                self.assertRegex(adapter_id, r"^[a-z0-9-]+$")

    def test_04_capabilities_are_declared_and_non_empty(self):
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                caps = adapters.REGISTRY[adapter_id].capabilities
                self.assertTrue(caps, "capability 를 하나도 선언하지 않았다")
                for cap in caps:
                    self.assertIsInstance(cap, A.Capability)

    def test_05_all_five_methods_exist(self):
        for adapter_id in adapter_ids():
            for name in REQUIRED_METHODS:
                with self.subTest(adapter=adapter_id, method=name):
                    self.assertTrue(
                        callable(getattr(adapters.REGISTRY[adapter_id], name, None))
                    )

    def test_06_init_accepts_home_and_now_as_keywords(self):
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                sig = inspect.signature(adapters.REGISTRY[adapter_id].__init__)
                for name in ("home", "now"):
                    self.assertIn(name, sig.parameters)
                    self.assertEqual(
                        sig.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY
                    )

    def test_07_init_does_no_io(self):
        """존재하지 않는 home 으로 생성해도 터지지 않아야 한다."""
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                adapters.get(adapter_id, home="/proc/omhc-nonexistent")

    def test_08_detect_never_raises(self):
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home:
                    got = adapters.get(adapter_id, home=home).detect()
                self.assertIsInstance(got, A.HarnessPresence)
                self.assertIsInstance(got.present, bool)

    def test_09_detect_reports_a_note_when_absent(self):
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home:
                    got = adapters.get(adapter_id, home=home).detect()
                if not got.present:
                    self.assertTrue(got.note, "없으면 왜 없는지 말해야 한다")

    def test_10_list_sessions_returns_empty_for_an_unknown_repo(self):
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home, \
                        tempfile.TemporaryDirectory() as repo:
                    got = adapters.get(adapter_id, home=home).list_sessions(repo)
                self.assertEqual(list(got), [])

    def test_11_list_sessions_accepts_none_repo_root(self):
        """작업 디렉터리 개념이 없는 하네스를 위해 None 을 받아야 한다."""
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home:
                    adapters.get(adapter_id, home=home).list_sessions(None)

    def test_12_session_refs_are_well_formed(self):
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                for ref in sessions_or_skip(self, adapter_id):
                    self.assertEqual(ref.adapter_id, adapter_id)
                    self.assertTrue(ref.source_path)
                    self.assertTrue(os.path.exists(ref.source_path))
                    self.assertIsInstance(ref.size, int)

    def test_13_cwdless_refs_only_appear_when_repo_root_is_none(self):
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                for ref in sessions_or_skip(self, adapter_id):
                    self.assertIsNotNone(ref.cwd)

    def test_14_read_session_never_raises_on_a_missing_file(self):
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                ref = A.SessionRef(adapter_id=adapter_id, session_id="gone",
                                   source_path="/nope/missing.jsonl", cwd=REPO,
                                   epoch=0.0, size=0)
                got = adapters.get(adapter_id).read_session(ref)
                self.assertIsInstance(got, A.SessionRead)
                self.assertEqual(len(got.events), 0)

    def test_15_read_session_never_raises_on_garbage(self):
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                with tempfile.NamedTemporaryFile("wb", suffix=".jsonl",
                                                 delete=False) as fh:
                    fh.write(b"\x00\xff{not json\n\n\x80\x81")
                    path = fh.name
                try:
                    got = adapters.get(adapter_id).read_session(
                        A.SessionRef(adapter_id=adapter_id, session_id="junk",
                                     source_path=path, cwd=REPO, epoch=0.0,
                                     size=os.path.getsize(path))
                    )
                    self.assertIsInstance(got, A.SessionRead)
                finally:
                    os.unlink(path)

    def test_16_events_use_only_closed_verbs_and_authors(self):
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                adapter = adapters.get(adapter_id)
                for ref in sessions_or_skip(self, adapter_id)[:1]:
                    for ev in adapter.read_session(ref).events:
                        self.assertIn(ev.verb, VERBS)
                        self.assertIn(ev.author, AUTHORS)

    def test_17_no_foreign_machinery_reaches_any_event_text(self):
        """가장 중요한 불변식. 남의 하네스 지시가 중계되면 안 된다."""
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                adapter = adapters.get(adapter_id)
                for ref in sessions_or_skip(self, adapter_id)[:1]:
                    for ev in adapter.read_session(ref).events:
                        if ev.author == "human":
                            # 사람의 문장은 그 사람의 권위다(사용자 결정 (a)).
                            continue
                        for marker in guard.FOREIGN_MARKERS:
                            self.assertNotIn(marker, ev.text)

    def test_18_offsets_stay_inside_the_source_file(self):
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                adapter = adapters.get(adapter_id)
                for ref in sessions_or_skip(self, adapter_id)[:1]:
                    size = os.path.getsize(ref.source_path)
                    for ev in adapter.read_session(ref).events:
                        self.assertGreaterEqual(ev.offset, 0)
                        self.assertLessEqual(ev.offset + ev.length, size)

    def test_19_sequence_numbers_are_monotonic(self):
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                adapter = adapters.get(adapter_id)
                for ref in sessions_or_skip(self, adapter_id)[:1]:
                    seqs = [e.seq for e in adapter.read_session(ref).events]
                    self.assertEqual(seqs, sorted(seqs))

    def test_20_unknown_record_types_are_reported_not_swallowed(self):
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                adapter = adapters.get(adapter_id)
                for ref in sessions_or_skip(self, adapter_id)[:1]:
                    read = adapter.read_session(ref)
                    self.assertIsInstance(read.dropped, dict)
                    self.assertIsInstance(read.unparsed, int)

    def test_21_native_resume_hint_is_a_string_or_none(self):
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                ref = A.SessionRef(adapter_id=adapter_id, session_id="abc",
                                   source_path="/x.jsonl", cwd=REPO, epoch=0.0, size=0)
                hint = adapters.get(adapter_id).native_resume_hint(ref)
                self.assertTrue(hint is None or isinstance(hint, str))

    def test_23_every_adapter_declares_exactly_one_wire_field(self):
        """주입 페이로드에 컨텍스트 필드가 둘이면 Claude Code 가 둘 다 읽어
        핸드오프가 두 번 주입된다. 어댑터가 추가되면 이 불변식이 자동으로 적용된다."""
        from omhc import brief

        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                wire = getattr(adapters.REGISTRY[adapter_id], "wire", None)
                self.assertIn(wire, ("claude", "cursor", "sdk"))
                import json as _json

                self.assertEqual(len(_json.loads(brief.hook_wire("x", wire))), 1)

    def test_24_fallback_channels_are_callables(self):
        """폴백 채널은 어댑터의 속성이다 — 라우터에 벤더 문자열이 있으면
        어댑터 추가가 코어 수정을 요구한다."""
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home:
                    channels = adapters.get(adapter_id, home=home).fallback_channels()
                self.assertIsInstance(tuple(channels), tuple)
                for channel in channels:
                    self.assertTrue(callable(channel))

    def test_22_write_capable_adapters_return_a_receipt(self):
        for adapter_id in adapter_ids():
            caps = adapters.REGISTRY[adapter_id].capabilities
            with self.subTest(adapter=adapter_id):
                bundle = A.HandoffBundle(body_md="[omhc] x\n", repo_root=REPO,
                                         to_adapter_id=adapter_id)
                if A.Capability.WRITE not in caps:
                    # 읽기 전용은 쓰기 절반을 가짜로 채우지 않는다.
                    # continue 여야 한다 — return 이면 첫 읽기 전용 어댑터에서
                    # 테스트가 끝나고 나머지 어댑터는 검증되지 않은 채 PASS 가 된다.
                    with self.assertRaises((A.NoInjectionChannel, NotImplementedError)):
                        adapters.get(adapter_id).install_handoff(bundle)
                    continue
                # WRITE 어댑터는 receipt 를 주거나, 그 채널이 지금 성립하지 않는다면
                # NoInjectionChannel 을 던진다 — 둘 다 선언된 정상 결과다. 전달이
                # 아닌 것을 성공으로 보고하면 폴백이 영원히 발동하지 않는다.
                with tempfile.TemporaryDirectory() as home:
                    try:
                        receipt = adapters.get(
                            adapter_id, home=home).install_handoff(bundle)
                    except A.NoInjectionChannel:
                        continue
                self.assertIsInstance(receipt, A.InstallReceipt)
                self.assertTrue(receipt.channel)
                self.assertTrue(receipt.paths_written)

    def test_25_deliver_always_produces_a_receipt(self):
        """어떤 어댑터로 보내도 라우터는 receipt 로 끝난다 — 조용히 사라지지 않는다."""
        from omhc import deliver

        for adapter_id in adapter_ids() + ["definitely-not-an-adapter"]:
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home, \
                        tempfile.TemporaryDirectory() as repo:
                    bundle = A.HandoffBundle(body_md="[omhc] x\n", repo_root=repo,
                                             to_adapter_id=adapter_id)
                    receipt = deliver.deliver(bundle, home=home, now=1758500000.0)
                self.assertIsInstance(receipt, A.InstallReceipt)
                self.assertTrue(receipt.channel)

    def test_26_classify_never_raises_on_garbage(self):
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                with tempfile.NamedTemporaryFile("wb", suffix=".jsonl",
                                                 delete=False) as fh:
                    fh.write(b"\x00\xff{not json\n\n\x80\x81")
                    path = fh.name
                try:
                    got = adapters.get(adapter_id).classify(path)
                    self.assertIsInstance(got, bool)
                finally:
                    os.unlink(path)
                # 없는 파일도 던지지 않는다(반환값은 fail-open 정책이라 어댑터마다
                # 다를 수 있다 — 여기서는 예외가 없다는 것만 본다).
                adapters.get(adapter_id).classify("/nope/missing.jsonl")

    def test_27_health_is_a_tuple_of_3_tuples_and_never_raises(self):
        """health 는 선택 메서드다(fallback_channels 와 같은 패턴). 빈 홈에서도
        절대 던지지 않고, 준 게 있다면 (label, ok, detail) 모양이어야 한다.

        빈 홈에서는 대부분의 어댑터가 (정당하게) 빈 튜플을 준다 — codex-cli 는
        훅이 설치돼 있지 않으면 행 자체를 생략한다. 여기서 벤더 지식 없이 훅
        설치 상태를 흉내 낼 방법이 없으므로, 실제로 행이 나오는 경로의 모양
        검증은 해당 어댑터의 전용 테스트(tests/test_codex_cli.py::TestHealth)가
        맡는다 — 이 테스트는 "던지지 않는다" 와 "나온 게 있다면 모양이 맞다"
        만 모든 어댑터에 대해 증명한다.
        """
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home:
                    rows = adapters.get(adapter_id, home=home).health(REPO, [])
                rows = tuple(rows)
                for row in rows:
                    self.assertEqual(len(row), 3)
                    label, ok, detail = row
                    self.assertIsInstance(label, str)
                    # ok 는 True/False/None 이다 — None 은 아직 판단할 근거가
                    # 없는 정보성 진단(status 의 `----`, 게이팅 안 함).
                    self.assertTrue(ok is None or isinstance(ok, bool))
                    self.assertIsInstance(detail, str)

    def test_29_hook_config_is_none_or_a_shipped_fragment(self):
        """hook_config 는 선택 메서드다(health/fallback_channels 와 같은 패턴).
        None 이 아니면 그 fragment_name 이 배포되는 hooks/ 아래 실재하고 JSON 으로
        파싱돼야 한다 — 코어(hookconf)와 어댑터가 같은 파일을 가리켜야 한다."""
        from omhc import hookconf

        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home:
                    hc = adapters.get(adapter_id, home=home).hook_config()
                if hc is None:
                    continue
                self.assertTrue(hc.config_path)
                fragment = hookconf.load_fragment(hc.fragment_name)
                self.assertIsInstance(fragment, dict)
                self.assertIsInstance(hc.post_write_note, str)

    def test_28_discover_is_an_iterable_of_session_refs_and_never_raises(self):
        """discover 는 mark 백필 전용 선택 메서드다(fallback_channels/health 와
        같은 패턴). 빈 홈에서도 절대 던지지 않고, 준 게 있다면 SessionRef 여야
        한다 — Claude 는 (일부러) 항상 빈 튜플이다(discover 의 docstring 참고).

        `deadline` 은 키워드 전용이 아니라 위치로도 받아들여야 cmd_mark 의
        호출(`discover(root, deadline=deadline)`)이 모든 어댑터에서 통한다 —
        이미 지난 deadline 을 줘도 던지지 않아야 한다."""
        import time

        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home:
                    inst = adapters.get(adapter_id, home=home)
                    refs = tuple(inst.discover(REPO))
                    expired = tuple(inst.discover(REPO, deadline=time.time() - 1))
                for got in (refs, expired):
                    for ref in got:
                        self.assertIsInstance(ref, A.SessionRef)

    def test_30_read_session_since_matches_read_session_restricted_to_the_offset(self):
        """선택 메서드(discover/health 와 같은 패턴) — 구현하는 어댑터만 본다.
        기본(None) 인 어댑터는 건너뛴다(#22)."""
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            adapter = adapters.get(adapter_id)
            with self.subTest(adapter=adapter_id):
                # 가비지에서 절대 던지지 않는다.
                with tempfile.NamedTemporaryFile("wb", suffix=".jsonl",
                                                 delete=False) as fh:
                    fh.write(b"\x00\xff{not json\n\n\x80\x81")
                    junk_path = fh.name
                try:
                    junk_ref = A.SessionRef(adapter_id=adapter_id, session_id="junk",
                                            source_path=junk_path, cwd=REPO, epoch=0.0,
                                            size=os.path.getsize(junk_path))
                    got = adapter.read_session_since(junk_ref, 0)
                    if got is None:
                        continue  # 선택 메서드 미구현 — 나머지도 볼 필요 없다.
                    self.assertIsInstance(got, A.SessionSince)
                    self.assertIsInstance(got.end_offset, int)
                finally:
                    os.unlink(junk_path)

                for ref in sessions_or_skip(self, adapter_id)[:1]:
                    full = adapter.read_session(ref)
                    if not full.events:
                        continue
                    # 줄 경계(어떤 이벤트의 offset)에서 자른다 — line-aligned start.
                    # seq 는 이 부분 읽기가 처음부터 다시 매기므로(구현 자유
                    # — 색인은 read_session_since 를 쓰지 않는다) 비교에서
                    # 뺀다; 나머지 필드는 read_session 과 완전히 같아야 한다.
                    mid = full.events[len(full.events) // 2]
                    since = adapter.read_session_since(ref, mid.offset)
                    self.assertIsNotNone(since)
                    expected = tuple(e._replace(seq=0) for e in full.events
                                     if e.offset >= mid.offset)
                    got = tuple(e._replace(seq=0) for e in since.events)
                    self.assertEqual(got, expected)
                    # end_offset 은 항상 줄 경계다(리뷰: 개행 없이 끝나는
                    # 마지막 줄은 아직 "안전히 다 읽은" 것이 아니다) — 실물
                    # 픽스처는 마지막 줄에 개행이 없을 수도 있으므로 EOF 와
                    # 같다고 단정하지 않고, 직전 바이트가 개행인지로 본다.
                    size = os.path.getsize(ref.source_path)
                    self._assert_line_aligned(ref.source_path, since.end_offset, size)

                    # EOF 에서는 빈 이벤트, end_offset 은 여전히 줄 경계 이하.
                    eof = adapter.read_session_since(ref, size)
                    self.assertIsNotNone(eof)
                    self.assertEqual(eof.events, ())
                    self._assert_line_aligned(ref.source_path, eof.end_offset, size)

    def _assert_line_aligned(self, path, end_offset, size):
        self.assertGreaterEqual(end_offset, 0)
        self.assertLessEqual(end_offset, size)
        if end_offset == 0:
            return
        with open(path, "rb") as fh:
            fh.seek(end_offset - 1)
            self.assertEqual(fh.read(1), b"\n",
                             "end_offset {} is not right after a newline".format(end_offset))


if __name__ == "__main__":
    unittest.main()
