from __future__ import annotations

import unittest

from omhc import adapter as A
from omhc import mint
from omhc.event import Event


def ev(seq, author="human", verb="said", text="", arg="", ok=True, paths=(), epoch=None):
    return Event(
        seq=seq, epoch=1700000000.0 + (seq if epoch is None else epoch),
        author=author, verb=verb, ok=ok, text=text, arg=arg, paths=tuple(paths),
        offset=seq * 10, length=10,
    )


def read_of(events, adapter_id="codex-cli", dropped=None, unparsed=0):
    ref = A.SessionRef(
        adapter_id=adapter_id, session_id="01a0c9f4-06aa-72d0", source_path="/x.jsonl",
        cwd="/repo", epoch=1700000000.0, size=100,
    )
    return A.SessionRead(ref=ref, events=tuple(events), unparsed=unparsed,
                         dropped=dropped or {})


def slots_of(text):
    out = {}
    for line in text.splitlines():
        if line.startswith("[omhc]"):
            out.setdefault("HEADER", []).append(line)
            continue
        key, _, value = line.partition("  ")
        out.setdefault(key.strip(), []).append(value.strip())
    return out


NOW = 1700000900.0


class TestBudget(unittest.TestCase):
    def test_output_never_exceeds_the_budget(self):
        events = [ev(i, text="사람의 긴 문장 " * 20) for i in range(1, 40)]
        for budget in (300, 900, 4000):
            out = mint.mint(read_of(events), to_adapter_id="claude-code",
                            budget=budget, now=NOW)
            self.assertLessEqual(len(out.encode("utf-8")), budget, budget)

    def test_header_and_pull_survive_every_budget(self):
        events = [ev(i, text="긴 문장 " * 30) for i in range(1, 30)]
        for budget in (300, 900, 4000):
            out = mint.mint(read_of(events), to_adapter_id="claude-code",
                            budget=budget, now=NOW)
            self.assertIn("[omhc]", out)
            self.assertIn("PULL", out)

    def test_budget_below_floor_raises_rather_than_emitting_a_useless_marker(self):
        with self.assertRaises(ValueError):
            mint.mint(read_of([ev(1, text="hi")]), to_adapter_id="claude-code",
                      budget=50, now=NOW)

    def test_default_budget_is_900(self):
        self.assertEqual(mint.BUDGET, 900)

    def test_clipping_is_byte_based_not_character_based(self):
        """Korean is 3 bytes/char in UTF-8.

        Clipping by character count lets a single 200-char slot eat 600
        bytes and crowd out every other slot — measured: a 749-byte output
        with 151 bytes of headroom still dropped 5 slots.
        """
        korean = "가" * 500
        out = mint.mint(read_of([ev(1, text=korean), ev(2, text=korean)]),
                        to_adapter_id="claude-code", budget=900, now=NOW)
        self.assertLessEqual(len(out.encode("utf-8")), 900)
        for line in out.splitlines():
            self.assertLessEqual(len(line.encode("utf-8")), 400, line[:40])

    def test_no_line_is_truncated_mid_command(self):
        """A truncated command is worse than no command. Only trim whole lines."""
        events = [ev(1, text="목표 " * 60)] + [
            ev(i, author="agent", verb="modified", arg="x",
               paths=("/repo/a/very/long/path/number{}.py".format(i),))
            for i in range(2, 10)
        ]
        for budget in (200, 250, 300, 500, 900):
            out = mint.mint(read_of(events), to_adapter_id="claude-code",
                            budget=budget, now=NOW)
            for line in out.splitlines():
                if line.startswith("PULL"):
                    self.assertTrue(line.rstrip().endswith(("30", ".py", "E1")),
                                    "PULL was cut off mid-line: {!r}".format(line))

    def test_small_budget_still_says_one_substantive_thing(self):
        out = mint.mint(read_of([
            ev(1, text="Codex 롤아웃 리더를 붙여서 handoff 를 양방향으로 만든다"),
            ev(2, author="agent", verb="ran", arg="pytest", ok=False),
        ]), to_adapter_id="claude-code", budget=420, now=NOW)
        slots = slots_of(out)
        self.assertTrue(
            "GOAL" in slots or "NEXT" in slots or "FAIL" in slots,
            "a handoff with no content at all is useless: {!r}".format(out),
        )

    def test_budget_pressure_keeps_verified_facts_over_an_unverified_claim(self):
        """PLAN? is a prior agent's claim, so it must be dropped before GOAL.

        Asserting on the private constant (_PRIORITY) breaks the test on any
        refactor of the drop mechanism with no behavior change, and even if
        the numbers match, the loop could still be dropping the wrong thing
        — that wouldn't prove the guarantee. Assert on the observable result
        instead.
        """
        events = [
            ev(1, text="목표를 세운다 " * 12),
            ev(2, author="agent", text="이전 에이전트의 계획 주장 " * 12),
            ev(3, text="응"),
        ]
        roomy = slots_of(mint.mint(read_of(events), to_adapter_id="claude-code",
                                   budget=900, now=NOW))
        self.assertIn("GOAL", roomy)
        self.assertIn("PLAN?", roomy)

        tight = slots_of(mint.mint(read_of(events), to_adapter_id="claude-code",
                                   budget=480, now=NOW))
        self.assertIn("GOAL", tight, "a verified goal was dropped before a claim")
        self.assertNotIn("PLAN?", tight)
        self.assertIn("plan?", " ".join(tight.get("MORE", [])).lower())


class TestProvenanceSlots(unittest.TestCase):
    def test_first_human_turn_becomes_goal(self):
        out = mint.mint(read_of([
            ev(1, text="Codex 롤아웃 리더를 붙여서 handoff를 양방향으로 만들기"),
            ev(2, author="agent", text="알겠습니다"),
            ev(3, text="필드 경로부터 다시 확인해줘"),
        ]), to_adapter_id="claude-code", now=NOW)
        self.assertIn("Codex 롤아웃 리더", slots_of(out)["GOAL"][0])

    def test_last_human_turn_becomes_next(self):
        out = mint.mint(read_of([
            ev(1, text="목표를 세운다"),
            ev(3, text="read_codex.py의 파싱이 빈 문자열 반환 — 필드 경로부터 확인해줘"),
        ]), to_adapter_id="claude-code", now=NOW)
        self.assertIn("필드 경로", slots_of(out)["NEXT"][0])

    def test_ack_shaped_last_turn_yields_plan_question_instead_of_next(self):
        """Using an approval-shaped turn like '계속 진행해' (go ahead) as NEXT launders a rejected proposal into an instruction."""
        out = mint.mint(read_of([
            ev(1, text="목표를 세운다"),
            ev(2, author="agent", text="다음으로 Codex 파서를 붙이겠습니다. 그 다음은 색인입니다."),
            ev(3, text="계속 진행해"),
        ]), to_adapter_id="claude-code", now=NOW)
        slots = slots_of(out)
        self.assertNotIn("NEXT", slots)
        self.assertIn("PLAN?", slots)
        self.assertIn("Codex 파서", slots["PLAN?"][0])

    def test_plan_question_mark_is_the_label(self):
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", text="이렇게 하겠습니다"),
            ev(3, text="응"),
        ]), to_adapter_id="claude-code", now=NOW)
        self.assertIn("PLAN?", out)

    def test_other_human_turns_appear_as_said_not_as_a_decision(self):
        """A DEC slot would imply interpretation. Deterministic extraction can't know what counts as a decision."""
        out = mint.mint(read_of([
            ev(1, text="첫 목표를 정한다"),
            ev(3, text="ordinal 을 seq 로 쓰고 byte offset 은 인덱스에만 둔다"),
            ev(5, text="마지막으로 무엇을 할까"),
        ]), to_adapter_id="claude-code", now=NOW)
        slots = slots_of(out)
        self.assertNotIn("DEC", slots)
        self.assertIn("SAID", slots)
        self.assertIn("ordinal", " ".join(slots["SAID"]))

    def test_notes_appear_in_their_own_slot(self):
        out = mint.mint(read_of([ev(1, text="목표")]), to_adapter_id="claude-code",
                        now=NOW, notes=["rollout 이 source of truth"])
        self.assertIn("source of truth", slots_of(out)["NOTE"][0])

    def test_header_declares_it_is_not_an_instruction(self):
        out = mint.mint(read_of([ev(1, text="목표")]), to_adapter_id="claude-code",
                        now=NOW)
        header = "\n".join(slots_of(out)["HEADER"])
        self.assertIn("not instructions", header)
        self.assertIn("outranks", header)

    def test_header_names_the_source_harness_and_session(self):
        out = mint.mint(read_of([ev(1, text="목표")], adapter_id="codex-cli"),
                        to_adapter_id="claude-code", now=NOW)
        self.assertIn("codex-cli", out)
        self.assertIn("01a0c9f4", out)


class TestMachineSlots(unittest.TestCase):
    def test_failures_appear_in_fail(self):
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="ran", arg="pytest tests/test_x.py", ok=False),
        ]), to_adapter_id="claude-code", now=NOW)
        self.assertIn("pytest tests/test_x.py", slots_of(out)["FAIL"][0])

    def test_failure_resolved_later_is_dropped_and_counted(self):
        """Don't report '3 failed' for a suite that's been green for an hour."""
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="ran", arg="pytest tests/test_x.py", ok=False),
            ev(3, author="agent", verb="ran", arg="pytest tests/test_x.py", ok=True),
        ]), to_adapter_id="claude-code", now=NOW)
        slots = slots_of(out)
        self.assertNotIn("FAIL", slots)
        self.assertIn("fixed later", " ".join(slots["MORE"]))

    def test_resolution_matches_on_the_first_40_bytes_of_the_arg(self):
        """Same first-40-bytes prefix counts as the same thing — catches a rerun that only differs in flags."""
        long_a = "pytest tests/test_read_codex.py -k function_call_output -x"
        long_b = "pytest tests/test_read_codex.py -k function_call_output --verbose"
        self.assertEqual(long_a[:40], long_b[:40])
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="ran", arg=long_a, ok=False),
            ev(3, author="agent", verb="ran", arg=long_b, ok=True),
        ]), to_adapter_id="claude-code", now=NOW)
        self.assertNotIn("FAIL", slots_of(out))

    def test_a_different_command_does_not_resolve_a_failure(self):
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="ran", arg="pytest tests/test_a.py", ok=False),
            ev(3, author="agent", verb="ran", arg="ruff check .", ok=True),
        ]), to_adapter_id="claude-code", now=NOW)
        self.assertIn("FAIL", slots_of(out))

    def test_modified_paths_appear_in_did(self):
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="modified", arg="/repo/omhc/mint.py",
               paths=("/repo/omhc/mint.py",)),
        ]), to_adapter_id="claude-code", now=NOW)
        self.assertIn("mint.py", slots_of(out)["DID"][0])

    def test_did_paths_are_repo_relative(self):
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="modified", arg="x",
               paths=("/repo/omhc/mint.py",)),
        ]), to_adapter_id="claude-code", now=NOW)
        self.assertIn("omhc/mint.py", slots_of(out)["DID"][0])
        self.assertNotIn("/repo/omhc", slots_of(out)["DID"][0])

    def test_pull_line_is_prefilled_with_commands(self):
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="ran", arg="pytest", ok=False),
        ]), to_adapter_id="claude-code", now=NOW)
        pull = slots_of(out)["PULL"][0]
        self.assertIn("omhc show", pull)
        self.assertIn("omhc log", pull)

    def test_failures_get_a_tag_that_show_can_resolve(self):
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="ran", arg="pytest", ok=False),
        ]), to_adapter_id="claude-code", now=NOW)
        self.assertRegex(out, r"\[E\d+\]")


class TestDisclosure(unittest.TestCase):
    def test_more_reports_hidden_event_count(self):
        events = [ev(1, text="목표")] + [
            ev(i, author="agent", verb="ran", arg="cmd {}".format(i))
            for i in range(2, 60)
        ]
        out = mint.mint(read_of(events), to_adapter_id="claude-code", now=NOW)
        self.assertIn("hidden", " ".join(slots_of(out)["MORE"]))

    def test_more_uses_singular_for_exactly_one_hidden_event(self):
        """#15b: "1 events hidden" reads wrong — no plural for exactly one."""
        events = [ev(1, text="목표"),
                  ev(2, author="agent", verb="ran", arg="pytest")]
        out = mint.mint(read_of(events), to_adapter_id="claude-code", now=NOW)
        more = " ".join(slots_of(out)["MORE"])
        self.assertIn("1 event hidden", more)
        self.assertNotIn("events hidden", more)

    def test_more_reports_dropped_slots(self):
        events = [ev(1, text="목표")] + [
            ev(i, text="사람의 말 {} ".format(i) * 5) for i in range(2, 12)
        ]
        out = mint.mint(read_of(events), to_adapter_id="claude-code", budget=400,
                        now=NOW)
        self.assertIn("MORE", slots_of(out))

    def test_more_is_absent_when_nothing_was_hidden(self):
        out = mint.mint(read_of([ev(1, text="목표"), ev(2, text="다음 할 일")]),
                        to_adapter_id="claude-code", now=NOW)
        self.assertNotIn("MORE", slots_of(out))


class TestSameVendorShortCircuit(unittest.TestCase):
    def test_minting_for_the_same_harness_returns_empty(self):
        """Same-vendor native resume is lossless and strictly better."""
        out = mint.mint(read_of([ev(1, text="목표")], adapter_id="claude-code"),
                        to_adapter_id="claude-code", now=NOW)
        self.assertEqual(out, "")

    def test_no_human_and_no_machine_events_returns_empty(self):
        self.assertEqual(
            mint.mint(read_of([]), to_adapter_id="claude-code", now=NOW), ""
        )


class TestShape(unittest.TestCase):
    def test_every_line_uses_the_two_space_separator(self):
        out = mint.mint(read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="modified", arg="x", paths=("/repo/a.py",)),
        ]), to_adapter_id="claude-code", now=NOW)
        for line in out.splitlines():
            if line.startswith("[omhc]"):
                continue
            self.assertIn("  ", line, line)

    def test_no_slot_value_contains_a_newline(self):
        out = mint.mint(read_of([ev(1, text="여러\n줄\n짜리 사람 말")]),
                        to_adapter_id="claude-code", now=NOW)
        for line in out.splitlines():
            self.assertNotIn("\n", line)

    def test_output_ends_with_a_single_newline(self):
        out = mint.mint(read_of([ev(1, text="목표")]), to_adapter_id="claude-code",
                        now=NOW)
        self.assertTrue(out.endswith("\n"))
        self.assertFalse(out.endswith("\n\n"))


class TestAlsoLines(unittest.TestCase):
    """v2 phase 1 (#41): older undelivered sessions become ALSO lines."""

    def _also_read(self, session_id, human, adapter_id="codex-cli", fail=False):
        events = [ev(1, text=human, epoch=100)]
        if fail:
            events.append(ev(2, author="agent", verb="ran", arg="pytest", ok=False, epoch=101))
        ref = A.SessionRef(adapter_id=adapter_id, session_id=session_id,
                           source_path="/{}.jsonl".format(session_id), cwd="/repo",
                           epoch=1700000000.0, size=100)
        return A.SessionRead(ref=ref, events=tuple(events), unparsed=0, dropped={})

    def test_also_line_shows_harness_id_age_and_verbatim_goal(self):
        also = self._also_read("cx-old", "옛 세션의 사람 말")
        out = mint.mint(read_of([ev(1, text="목표")]), to_adapter_id="claude-code",
                        now=NOW, also=[also])
        slots = slots_of(out)
        self.assertIn("ALSO", slots)
        line = slots["ALSO"][0]
        self.assertIn("codex-cli", line)
        self.assertIn("cx-old"[:8], line)
        self.assertIn("옛 세션의 사람 말", line)

    def test_also_line_omits_fail_when_no_unresolved_failures(self):
        also = self._also_read("cx-old", "옛 세션의 사람 말", fail=False)
        out = mint.mint(read_of([ev(1, text="목표")]), to_adapter_id="claude-code",
                        now=NOW, also=[also])
        line = slots_of(out)["ALSO"][0]
        self.assertNotIn("FAIL", line)

    def test_also_line_carries_a_failure_tag_numbered_after_the_main_session(self):
        main = read_of([
            ev(1, text="목표"),
            ev(2, author="agent", verb="ran", arg="pytest tests/test_a.py", ok=False),
        ])
        also = self._also_read("cx-old", "옛 세션의 사람 말", fail=True)
        out = mint.mint(main, to_adapter_id="claude-code", now=NOW, also=[also])
        self.assertIn("[E1]", out)  # main session's own failure
        self.assertIn("[E2]", out)  # also session's failure, numbered after E1

    def test_a_no_human_also_session_does_not_consume_a_tag_number(self):
        """Review finding 3: a no-human ALSO session must not be numbered at
        all — the surviving session's tag stays contiguous (E1, not E2)."""
        main = read_of([ev(1, text="목표")])
        no_human = A.SessionRead(
            ref=A.SessionRef(adapter_id="codex-cli", session_id="cx-m",
                             source_path="/m.jsonl", cwd="/repo",
                             epoch=1700000000.0, size=100),
            events=(ev(1, author="agent", verb="ran", arg="pytest", ok=False, epoch=100),),
            unparsed=0, dropped={},
        )
        also = self._also_read("cx-o", "옛 세션의 사람 말", fail=True)
        out = mint.mint(main, to_adapter_id="claude-code", now=NOW, also=[no_human, also])
        self.assertIn("[E1]", out)
        self.assertNotIn("[E2]", out)
        self.assertEqual(mint.all_tags(main, also=[no_human, also]),
                         [[], [("E1", also.ref, also.events[1])]])

    def test_also_goal_is_verbatim_human_text_never_agent_text(self):
        """Invariant 3: never fall back to agent text for an ALSO line's GOAL."""
        events = [ev(1, author="agent", text="에이전트의 주장")]
        ref = A.SessionRef(adapter_id="codex-cli", session_id="cx-old",
                           source_path="/x.jsonl", cwd="/repo", epoch=1700000000.0, size=100)
        also = A.SessionRead(ref=ref, events=tuple(events), unparsed=0, dropped={})
        out = mint.mint(read_of([ev(1, text="목표")]), to_adapter_id="claude-code",
                        now=NOW, also=[also])
        self.assertNotIn("ALSO", slots_of(out))
        self.assertNotIn("에이전트의 주장", out)

    def test_also_lines_drop_first_and_more_says_sessions(self):
        """Under budget pressure ALSO is the lowest priority — the oldest one
        (last add()-ed) drops first, and MORE reads '+N sessions'."""
        main = read_of([ev(1, text="목표를 세운다 " * 20)])
        alsos = [self._also_read("cx-{}".format(i), "옛 세션 {} 의 사람 말".format(i) * 5)
                for i in range(3)]
        out = mint.mint(main, to_adapter_id="claude-code", budget=mint.MIN_BUDGET + 40,
                        now=NOW, also=alsos)
        self.assertLessEqual(len(out.encode("utf-8")), mint.MIN_BUDGET + 40)
        self.assertNotIn("also", " ".join(slots_of(out).get("MORE", [])).lower())
        more = " ".join(slots_of(out).get("MORE", []))
        if "ALSO" not in slots_of(out):
            self.assertIn("session", more)

    def test_unread_count_is_disclosed_in_more(self):
        out = mint.mint(read_of([ev(1, text="목표")]), to_adapter_id="claude-code",
                        now=NOW, unread=2)
        more = " ".join(slots_of(out).get("MORE", []))
        self.assertIn("2 sessions unread", more)

    def test_zero_unread_says_nothing(self):
        out = mint.mint(read_of([ev(1, text="목표"), ev(2, text="다음 할 일")]),
                        to_adapter_id="claude-code", now=NOW, unread=0)
        self.assertNotIn("MORE", slots_of(out))

    def test_budget_holds_with_three_korean_also_sessions_at_min_and_full_budget(self):
        main = read_of([ev(1, text="목표를 세운다")])
        alsos = [self._also_read("cx-{}".format(i), "한국어로 된 이전 세션의 목표 문장 " * 6,
                                 fail=True)
                for i in range(3)]
        for budget in (mint.MIN_BUDGET, 900):
            out = mint.mint(main, to_adapter_id="claude-code", budget=budget,
                            now=NOW, also=alsos)
            self.assertLessEqual(len(out.encode("utf-8")), budget, budget)


if __name__ == "__main__":
    unittest.main()
