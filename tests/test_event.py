from __future__ import annotations

import unittest

from omhc.event import AUTHORS, VERBS, Event, tally


def mk(**kw):
    base = dict(
        seq=1,
        epoch=1.0,
        author="human",
        verb="said",
        ok=True,
        text="hi",
        arg="",
        paths=(),
        offset=0,
        length=2,
    )
    base.update(kw)
    return Event(**base)


class TestEvent(unittest.TestCase):
    def test_verbs_are_closed_and_neutral(self):
        self.assertEqual(
            VERBS,
            frozenset({"said", "inspected", "modified", "ran", "delegated", "researched"}),
        )

    def test_authors_are_exactly_three(self):
        self.assertEqual(AUTHORS, frozenset({"human", "agent", "harness"}))

    def test_vendor_tool_names_are_not_valid_verbs(self):
        for name in ("Bash", "Read", "Edit", "Task", "apply_patch", "shell", "update_plan"):
            with self.assertRaises(ValueError):
                mk(verb=name)

    def test_unknown_author_rejected(self):
        with self.assertRaises(ValueError):
            mk(author="user")

    def test_event_has_no_field_that_could_hold_a_tool_name(self):
        for forbidden in ("tool", "tool_name", "extra", "raw", "metadata", "payload"):
            self.assertNotIn(forbidden, Event._fields)

    def test_event_is_immutable_against_declared_and_undeclared_names(self):
        """namedtuple + __slots__ = () 이므로 어느 이름도 할당할 수 없다.

        dataclasses 대신 namedtuple 을 쓰는 이유는 import 비용이다 — dataclasses 는
        inspect·ast·dis·tokenize 를 끌어와 훅 경로 두 프로세스가 매번 8ms 씩
        지불했다. 불변 보장은 동일하다.
        """
        ev = mk()
        with self.assertRaises(AttributeError):
            ev.text = "x"
        with self.assertRaises(AttributeError):
            ev.smuggled = "x"

    def test_event_instances_have_no_dict(self):
        self.assertFalse(hasattr(mk(), "__dict__"))

    def test_tally_counts_by_verb_and_author(self):
        events = [
            mk(seq=1),
            mk(seq=2, author="agent", verb="ran", ok=False),
            mk(seq=3, author="agent", verb="modified"),
        ]
        got = tally(events)
        self.assertEqual(got["by_verb"]["ran"], 1)
        self.assertEqual(got["by_author"]["agent"], 2)
        self.assertEqual(got["failures"], 1)
        self.assertEqual(got["total"], 3)


if __name__ == "__main__":
    unittest.main()
