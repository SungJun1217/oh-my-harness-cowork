"""Invariants every adapter must hold.

Parameterized over REGISTRY, so **adding an adapter grows the test suite for
free**. It's proof, not "looks wired up". A read-only adapter still passes
without faking the write half — read and write are independent capabilities.

What someone adding a new adapter has to do:
  1. implement the 5 methods in omhc/adapters/<harness>.py and add @_register
  2. add one import line at the bottom of omhc/adapters/__init__.py
  3. freeze one real session under tests/fixtures/<harness>/
Then this file's invariants apply to that adapter automatically.
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

# Derived from the contract class. Hand-writing this lets the contract and
# what's actually verified drift apart silently.
REQUIRED_METHODS = tuple(
    name for name, value in vars(A.HarnessAdapter).items()
    if not name.startswith("_") and callable(value)
)



def adapter_ids():
    return sorted(adapters.REGISTRY)


def sessions_or_skip(case, adapter_id):
    """**Skip** if no sessions are found at all. Never pass silently.

    A zero-item loop passes with no assertions ever run — the most dangerous
    kind of pass, one that reports "no foreign matter leaks" as verified
    while nothing was actually checked.
    """
    refs = adapters.get(adapter_id).list_sessions(REPO)
    if not refs:
        case.skipTest(
            "{}: no sessions under {} — either this harness was never used "
            "on this machine or this is a different checkout. The iteration "
            "invariant was not verified.".format(adapter_id, REPO)
        )
    return refs


class AdapterContract(unittest.TestCase):
    """Contract invariants. Reported per-adapter via subTest."""

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
                self.assertTrue(caps, "declared no capability at all")
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
        """Must not raise even when constructed with a nonexistent home."""
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
                    self.assertTrue(got.note, "must say why it's absent")

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
        """Must accept None for harnesses with no notion of a working directory."""
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
        """The most important invariant. A foreign harness's instructions must never be relayed."""
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            with self.subTest(adapter=adapter_id):
                adapter = adapters.get(adapter_id)
                for ref in sessions_or_skip(self, adapter_id)[:1]:
                    for ev in adapter.read_session(ref).events:
                        if ev.author == "human":
                            # A human's own sentence carries their own authority
                            # (user decision (a)).
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
        """If the injected payload carries two context fields, Claude Code reads
        both and the handoff gets injected twice. This invariant applies
        automatically to any adapter that's added."""
        from omhc import brief

        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                wire = getattr(adapters.REGISTRY[adapter_id], "wire", None)
                self.assertIn(wire, ("claude", "cursor", "sdk"))
                import json as _json

                self.assertEqual(len(_json.loads(brief.hook_wire("x", wire))), 1)

    def test_24_fallback_channels_are_callables(self):
        """Fallback channels are the adapter's own property — a vendor string
        in the router would mean adding an adapter requires a core change."""
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
                    # Read-only adapters don't fake the write half.
                    # Must be `continue`, not `return` — `return` would end
                    # the test at the first read-only adapter and leave the
                    # rest unverified yet PASS.
                    with self.assertRaises((A.NoInjectionChannel, NotImplementedError)):
                        adapters.get(adapter_id).install_handoff(bundle)
                    continue
                # A WRITE adapter either gives a receipt, or raises
                # NoInjectionChannel if that channel doesn't currently hold —
                # both are declared normal outcomes. Reporting a non-delivery
                # as success would mean the fallback never fires.
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
        """Whichever adapter it's sent to, the router ends in a receipt — nothing vanishes silently."""
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
                # A missing file must not raise either (the return value is
                # a fail-open policy that can differ per adapter — here we
                # only check that no exception is raised).
                adapters.get(adapter_id).classify("/nope/missing.jsonl")

    def test_27_health_is_a_tuple_of_3_tuples_and_never_raises(self):
        """health is an optional method (same pattern as fallback_channels).
        Must never raise even on an empty home, and whatever it returns must
        be shaped (label, ok, detail).

        Most adapters (legitimately) return an empty tuple on an empty home
        — codex-cli omits the row entirely when its hook isn't installed.
        There's no way here to fake an installed-hook state without vendor
        knowledge, so verifying the shape of a row that actually appears is
        left to that adapter's own test (tests/test_codex_cli.py::TestHealth)
        — this test only proves "never raises" and "if something comes back,
        it's shaped right", across every adapter.
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
                    # ok is True/False/None — None is an informational
                    # diagnosis with no basis to judge yet (status's `----`,
                    # not gated on).
                    self.assertTrue(ok is None or isinstance(ok, bool))
                    self.assertIsInstance(detail, str)

    def test_30_on_session_start_mark_never_raises(self):
        """on_session_start_mark is an optional method (same pattern as
        discover/health). It's called from the hook path (cmd_mark), so it
        must never raise for any source/repo combination — the default is a
        no-op, and any real implementation must still guard against this."""
        for adapter_id in adapter_ids():
            with self.subTest(adapter=adapter_id):
                with tempfile.TemporaryDirectory() as home:
                    inst = adapters.get(adapter_id, home=home)
                    for source in ("startup", "resume", "compact", ""):
                        self.assertIsNone(
                            inst.on_session_start_mark(REPO, source=source, epoch=1.0))

    def test_29_hook_config_is_none_or_a_shipped_fragment(self):
        """hook_config is an optional method (same pattern as
        health/fallback_channels). If not None, its fragment_name must
        actually exist under the shipped hooks/ and parse as JSON — the
        core (hookconf) and the adapter must point at the same file."""
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
        """discover is an optional method dedicated to mark's backfill (same
        pattern as fallback_channels/health). Must never raise even on an
        empty home, and whatever it returns must be a SessionRef — Claude
        (deliberately) always returns an empty tuple (see discover's
        docstring).

        `deadline` must be accepted positionally as well as by keyword so
        cmd_mark's call (`discover(root, deadline=deadline)`) works across
        every adapter — must not raise even given an already-past deadline."""
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
        """Optional method (same pattern as discover/health) — only checked
        for adapters that implement it. Adapters that default to None are
        skipped (#22)."""
        for adapter_id in adapter_ids():
            if A.Capability.READ not in adapters.REGISTRY[adapter_id].capabilities:
                continue
            adapter = adapters.get(adapter_id)
            with self.subTest(adapter=adapter_id):
                # Must never raise on garbage.
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
                        continue  # optional method not implemented — nothing more to check.
                    self.assertIsInstance(got, A.SessionSince)
                    self.assertIsInstance(got.end_offset, int)
                finally:
                    os.unlink(junk_path)

                for ref in sessions_or_skip(self, adapter_id)[:1]:
                    full = adapter.read_session(ref)
                    if not full.events:
                        continue
                    # Cut at a line boundary (some event's offset) — a
                    # line-aligned start. seq is renumbered from scratch by
                    # this partial read (implementation's own choice — the
                    # index never uses read_session_since), so it's excluded
                    # from the comparison; every other field must match
                    # read_session exactly.
                    mid = full.events[len(full.events) // 2]
                    since = adapter.read_session_since(ref, mid.offset)
                    self.assertIsNotNone(since)
                    expected = tuple(e._replace(seq=0) for e in full.events
                                     if e.offset >= mid.offset)
                    got = tuple(e._replace(seq=0) for e in since.events)
                    self.assertEqual(got, expected)
                    # end_offset is always a line boundary (review: a last
                    # line with no trailing newline isn't "safely fully read"
                    # yet) — a real fixture's last line may lack a newline,
                    # so instead of asserting it equals EOF, check whether
                    # the preceding byte is a newline.
                    size = os.path.getsize(ref.source_path)
                    self._assert_line_aligned(ref.source_path, since.end_offset, size)

                    # At EOF, events are empty and end_offset is still at or before a line boundary.
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
