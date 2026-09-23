from __future__ import annotations

import unittest

from omhc import adapter as A
from omhc import adapters


class FakeAdapter:
    adapter_id = "fake"
    capabilities = frozenset({A.Capability.READ})

    def __init__(self, *, home=None, now=None):
        self.home = home
        self.now = now

    def detect(self):
        return A.HarnessPresence(present=True, note="fake")

    def list_sessions(self, repo_root):
        return []

    def read_session(self, ref):
        return A.SessionRead(ref=ref, events=(), unparsed=0, dropped={})

    def native_resume_hint(self, ref):
        return None

    def install_handoff(self, bundle):
        raise A.NoInjectionChannel("fake is read-only")


class TestCapability(unittest.TestCase):
    def test_exactly_two_capabilities(self):
        self.assertEqual({c.name for c in A.Capability}, {"READ", "WRITE"})


class TestRecords(unittest.TestCase):
    def test_records_are_immutable(self):
        ref = A.SessionRef(
            adapter_id="fake", session_id="s", source_path="/p", cwd="/c",
            epoch=1.0, size=2,
        )
        with self.assertRaises(AttributeError):
            ref.session_id = "x"
        with self.assertRaises(AttributeError):
            ref.smuggled = "x"

    def test_session_ref_cwd_may_be_none_for_cwdless_harnesses(self):
        ref = A.SessionRef(
            adapter_id="fake", session_id="s", source_path="/p", cwd=None,
            epoch=1.0, size=2,
        )
        self.assertIsNone(ref.cwd)

    def test_no_record_has_a_field_that_could_hold_foreign_material(self):
        for cls in (A.SessionRef, A.SessionRead, A.HandoffBundle,
                    A.InstallReceipt, A.HarnessPresence):
            for forbidden in ("raw", "extra", "metadata", "payload", "tool"):
                self.assertNotIn(forbidden, cls._fields,
                                 "{}.{}".format(cls.__name__, forbidden))


class TestRegistry(unittest.TestCase):
    def test_registry_holds_classes_not_instances(self):
        for key, value in adapters.REGISTRY.items():
            self.assertTrue(isinstance(value, type), "{} 은 클래스여야 한다".format(key))

    def test_adapter_ids_match_module_stem_convention(self):
        for key in adapters.REGISTRY:
            self.assertRegex(key, r"^[a-z0-9-]+$")

    def test_get_unknown_id_raises_adapter_unavailable(self):
        with self.assertRaises(A.AdapterUnavailable):
            adapters.get("definitely-not-an-adapter")

    def test_get_passes_home_and_now_as_keywords(self):
        adapters.REGISTRY["fake"] = FakeAdapter
        try:
            inst = adapters.get("fake", home="/h", now=lambda: 7.0)
            self.assertEqual(inst.home, "/h")
            self.assertEqual(inst.now(), 7.0)
        finally:
            del adapters.REGISTRY["fake"]

    def test_present_is_deterministic_and_survives_a_bad_adapter(self):
        class Exploding:
            adapter_id = "boom"
            capabilities = frozenset({A.Capability.READ})

            def __init__(self, *, home=None, now=None):
                pass

            def detect(self):
                raise RuntimeError("한 어댑터의 나쁜 하루가 전체를 죽이면 안 된다")

        adapters.REGISTRY["fake"] = FakeAdapter
        adapters.REGISTRY["boom"] = Exploding
        try:
            first = adapters.present()
            second = adapters.present()
            self.assertEqual(first, second)
            self.assertIn("fake", first)
            self.assertNotIn("boom", first)
        finally:
            del adapters.REGISTRY["fake"]
            del adapters.REGISTRY["boom"]

    # v1 어댑터 2개가 실제로 등재됐는지는 tests/test_registry_v1.py 가 본다
    # (어댑터 구현 태스크에서 추가된다). 태스크마다 스위트가 그린이어야 하므로
    # 여기서는 계약만 검증한다.


class TestExceptions(unittest.TestCase):
    def test_three_exception_types_exist_and_share_a_base(self):
        for exc in (A.AdapterUnavailable, A.UnsupportedFormat, A.NoInjectionChannel):
            self.assertTrue(issubclass(exc, A.OmhcAdapterError))


if __name__ == "__main__":
    unittest.main()
