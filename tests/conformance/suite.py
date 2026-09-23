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

REQUIRED_METHODS = (
    "detect",
    "list_sessions",
    "read_session",
    "native_resume_hint",
    "install_handoff",
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
                with tempfile.TemporaryDirectory() as home:
                    receipt = adapters.get(adapter_id, home=home).install_handoff(bundle)
                self.assertIsInstance(receipt, A.InstallReceipt)
                self.assertTrue(receipt.channel)
                self.assertTrue(receipt.paths_written)


if __name__ == "__main__":
    unittest.main()
