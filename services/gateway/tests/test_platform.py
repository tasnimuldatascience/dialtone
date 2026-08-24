"""Tests for the tool layer, telephony boundary, compliance and simulated calls.

Each test names the failure it prevents rather than the function it calls. A test called
`test_invoke_returns_result` tells a future reader nothing; `test_a_dropped_line_does_not_double
_charge_the_caller` tells them why the code is shaped the way it is, and what breaks if they
simplify it.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from dialtone.agents.support import build_flow, build_registry
from dialtone.compliance.redact import (
    Sensitivity,
    StreamingRedactor,
    luhn,
    redact,
)
from dialtone.flow.graph import FlowRunner
from dialtone.sim.call import CANNED_CALLS, replay
from dialtone.telephony.provider import (
    FRAME_MS,
    ScriptedTurn,
    SimulatedCall,
    frame_energy,
    pcm16_to_ulaw,
    ulaw_to_pcm16,
)
from dialtone.tools.registry import (
    DEADLINE_MS,
    Latency,
    ToolCall,
    ToolRegistry,
    ToolSpec,
    ToolTrace,
)


# ── tools ────────────────────────────────────────────────────────────────────
class TestToolRegistry:
    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="lookup", description="fast lookup",
                parameters={"type": "object", "properties": {"q": {"type": "string"}},
                            "required": ["q"]},
                latency=Latency.FAST,
            ),
            lambda q: {"found": q},
        )
        return registry

    @pytest.mark.asyncio
    async def test_a_tool_outside_the_current_node_is_refused(self):
        """The guardrail. A model that decides it should charge a card at the greeting cannot."""
        registry = self.registry()
        result = await registry.invoke(ToolCall("lookup", {"q": "x"}), allowed=("other",))
        assert not result.ok
        assert "not available at this step" in result.error

    @pytest.mark.asyncio
    async def test_a_missing_argument_is_a_result_not_an_exception(self):
        """The model can supply it. Raising would abort a recoverable turn on a live call."""
        result = await self.registry().invoke(ToolCall("lookup", {}))
        assert not result.ok
        assert "q" in result.error

    @pytest.mark.asyncio
    async def test_a_failing_tool_never_leaks_its_exception_to_the_caller(self):
        """A caller must not hear a stack trace, and the operator still needs the detail."""
        registry = ToolRegistry()
        def explode() -> None:
            raise RuntimeError("postgres: connection refused at 10.0.3.11:5432")

        registry.register(
            ToolSpec(name="boom", description="", parameters={"type": "object"},
                     latency=Latency.FAST, on_error="I couldn't reach the booking system."),
            explode,
        )
        result = await registry.invoke(ToolCall("boom"))
        assert not result.ok
        assert result.error == "I couldn't reach the booking system."
        assert "postgres" not in result.error
        assert "10.0.3.11" not in result.error

    @pytest.mark.asyncio
    async def test_a_dropped_line_does_not_double_charge_the_caller(self):
        """The failure that costs money. Retrying a booking after a drop must not book twice."""
        registry = ToolRegistry()
        calls: list[int] = []
        registry.register(
            ToolSpec(name="charge", description="", parameters={"type": "object"},
                     latency=Latency.SLOW, idempotent=False),
            lambda: calls.append(1) or {"charged": True},
        )
        call = ToolCall("charge", {}, idempotency_key="call-7:charge:1")

        first = await registry.invoke(call)
        second = await registry.invoke(call)

        assert first.ok and second.ok
        assert len(calls) == 1, "the work ran twice"
        assert second.deduplicated and not first.deduplicated

    @pytest.mark.asyncio
    async def test_without_a_key_the_retry_does_run_twice(self):
        """Proves the previous test measures the guard, not an accident of the handler."""
        registry = ToolRegistry()
        calls: list[int] = []
        registry.register(
            ToolSpec(name="charge", description="", parameters={"type": "object"},
                     latency=Latency.SLOW, idempotent=False),
            lambda: calls.append(1) or {"charged": True},
        )
        await registry.invoke(ToolCall("charge"))
        await registry.invoke(ToolCall("charge"))
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_tool_that_overruns_its_deadline_is_abandoned(self):
        """Silence is indistinguishable from a dropped line. Waiting forever is not an option."""
        registry = ToolRegistry()

        async def slow() -> None:
            await asyncio.sleep(DEADLINE_MS[Latency.INSTANT] / 1000 * 3)

        registry.register(
            ToolSpec(name="slow", description="", parameters={"type": "object"},
                     latency=Latency.INSTANT),
            slow,
        )
        result = await registry.invoke(ToolCall("slow"))
        assert result.timed_out and not result.ok

    def test_only_slow_tools_get_a_cover_phrase(self):
        """Announcing "let me check" before a 40ms lookup makes the agent sound slower."""
        registry = build_registry()
        assert registry.cover_for("check_availability") is None    # FAST
        assert registry.cover_for("book_appointment")               # SLOW

    def test_the_model_only_ever_sees_reachable_tools(self):
        """Stronger than instructing it not to call one: it is absent from the schema."""
        registry = build_registry()
        schemas = registry.schemas(allowed=("lookup_patient",))
        assert [s["name"] for s in schemas] == ["lookup_patient"]

    def test_a_trace_totals_the_time_spent_in_tools(self):
        trace = ToolTrace()
        trace.record(ToolCall("a"), type("R", (), {
            "as_dict": lambda self: {"name": "a", "ok": True, "value": 1, "error": "",
                                     "duration_ms": 12.5, "deduplicated": False,
                                     "timed_out": False}})())
        assert trace.total_ms == 12.5
        assert trace.failures == []


# ── the worked agent ─────────────────────────────────────────────────────────
class TestSupportAgent:
    def test_the_flow_is_structurally_valid(self):
        assert build_flow().validate() == []

    def test_every_path_reaches_a_terminal_node(self):
        flow = build_flow()
        for path in flow.paths():
            assert flow.nodes[path[-1]].kind.value in ("end", "transfer"), path

    def test_a_human_is_always_reachable(self):
        """An agent that will not transfer is the fastest way to make a caller hate a line."""
        flow = build_flow()
        assert any("handoff" in path for path in flow.paths())

    @pytest.mark.asyncio
    async def test_booking_into_the_past_is_refused(self):
        """A caller says "Tuesday" on a Wednesday and means NEXT Tuesday. Every demo skips this."""
        registry = build_registry(clock=date(2026, 3, 2))
        result = await registry.invoke(ToolCall("check_availability", {"date": "2026-02-20"}))
        assert result.ok
        assert result.value["slots"] == []
        assert "passed" in result.value["error"]

    @pytest.mark.asyncio
    async def test_an_unparseable_date_is_data_not_a_crash(self):
        registry = build_registry(clock=date(2026, 3, 2))
        result = await registry.invoke(ToolCall("check_availability", {"date": "next tuesday"}))
        assert result.ok and "not an ISO date" in result.value["error"]

    @pytest.mark.asyncio
    async def test_booking_cannot_be_called_from_the_greeting(self):
        registry = build_registry(clock=date(2026, 3, 2))
        runner = FlowRunner(build_flow())
        state = runner.start()
        result = await registry.invoke(
            ToolCall("book_appointment", {"date": "2026-03-04", "time": "09:00", "name": "S H"}),
            allowed=runner.available_tools(state),
        )
        assert not result.ok


# ── μ-law and framing ────────────────────────────────────────────────────────
class TestTelephony:
    @pytest.mark.parametrize("sample", [0, 1, -1, 100, -100, 8000, -8000, 32000, -32000])
    def test_mulaw_round_trips_within_its_quantisation_error(self, sample: int):
        """G.711 is 8-bit logarithmic, so it is lossy by design — but bounded."""
        back = ulaw_to_pcm16(pcm16_to_ulaw(sample))
        assert abs(back - sample) <= max(64, abs(sample) * 0.05)

    def test_energy_is_computed_on_decoded_samples(self):
        """μ-law is logarithmic, so RMS of the raw bytes is not proportional to loudness.

        Computing energy without decoding yields a VAD that works on loud speech and fails on
        quiet speech — the hardest kind of bug to notice and the most annoying to experience.
        """
        quiet = bytes(pcm16_to_ulaw(200) for _ in range(160))
        loud = bytes(pcm16_to_ulaw(20000) for _ in range(160))
        assert frame_energy(quiet) < frame_energy(loud)
        assert frame_energy(b"") == 0.0

    @pytest.mark.asyncio
    async def test_the_simulator_honours_the_declared_speaking_rate(self):
        """A caller who speaks at 20ms/char is speaking at ~3000wpm, and every latency number
        measured against them is meaningless."""
        turn = ScriptedTurn(text="hello there", trailing_silence_ms=0.0, ms_per_char=55.0)
        call = SimulatedCall(turns=(turn,))
        speech = [f async for f in call.inbound() if f.is_speech]
        expected = len(turn.text) * 55.0 / FRAME_MS
        assert abs(len(speech) - expected) <= 2

    @pytest.mark.asyncio
    async def test_partials_are_revealed_word_by_word_not_all_at_once(self):
        """The endpointer must never see text the caller has not said yet.

        Revealing the whole turn up front is the obvious shortcut and it makes every result
        measured on the simulator fiction.
        """
        call = SimulatedCall(turns=(ScriptedTurn("book me an appointment",
                                                 trailing_silence_ms=0.0),))
        seen: list[str] = []
        async for _ in call.inbound():
            if call.partial and (not seen or call.partial != seen[-1]):
                seen.append(call.partial)
        assert len(seen) > 3
        assert seen[-1] == "book me an appointment"
        assert all(
            "book me an appointment".startswith(p) for p in seen
        ), "a partial was not a prefix of the final transcript"

    @pytest.mark.asyncio
    async def test_packet_loss_is_deterministic_for_a_seed(self):
        """A simulator whose loss pattern varies between runs cannot prove a regression fixed."""
        def build() -> SimulatedCall:
            return SimulatedCall(
                turns=(ScriptedTurn("testing one two three", trailing_silence_ms=100.0),),
                packet_loss=0.2, seed=42,
            )

        first = [f.energy async for f in build().inbound()]
        second = [f.energy async for f in build().inbound()]
        assert first == second

    @pytest.mark.asyncio
    async def test_packet_loss_does_not_speed_the_caller_up(self):
        """A dropped frame still cost wall-clock time — the audio was sent, it just didn't land."""
        turn = ScriptedTurn("testing one two three", trailing_silence_ms=0.0)
        clean = SimulatedCall(turns=(turn,), packet_loss=0.0)
        lossy = SimulatedCall(turns=(turn,), packet_loss=0.3, seed=3)
        async for _ in clean.inbound():
            pass
        async for _ in lossy.inbound():
            pass
        assert clean._clock_ms == lossy._clock_ms


# ── compliance ───────────────────────────────────────────────────────────────
class TestRedaction:
    @pytest.mark.parametrize("number,valid", [
        ("4539148803436467", True),
        ("4242424242424242", True),
        ("4242424242424241", False),
        ("12345", False),
    ])
    def test_luhn_separates_cards_from_order_numbers(self, number: str, valid: bool):
        """Without this the redactor strips order numbers, and the agent cannot do its job."""
        assert luhn(number) is valid

    def test_a_spoken_card_number_is_redacted(self):
        """Nobody says "4242424242424242". Every card regex ever written fails on speech."""
        spoken = "my card is four two four two four two four two four two four two four two four two"
        result = redact(spoken)
        assert "[CARD]" in result.text
        assert "four two" not in result.text
        assert not result.clean

    def test_a_spoken_card_does_not_destroy_the_following_word_boundary(self):
        """THE BUG THIS FILE IS WRITTEN AROUND.

        Collapsing whitespace after every number word turns "...four two and my name" into
        "...42and my name", killing the \\b that every later pattern needs. Cards then pass
        through unredacted while the compliance log reports a clean run.
        """
        text = ("my card is four two four two four two four two four two four two four two "
                "four two and my name is Sam")
        result = redact(text)
        assert "[CARD] and my name is Sam" in result.text

    def test_an_order_number_survives(self):
        """It fails Luhn, so it is not a card, so the agent can still read it back."""
        result = redact("my order number is 4242 4242 4242 4241")
        assert "4242 4242 4242 4241" in result.text
        assert result.clean

    def test_a_findings_record_never_carries_the_pan(self):
        """The `phone` rule matches inside a card number. Without overlap resolution the
        findings list holds the last four digits — a second copy of the breach."""
        result = redact("card four two four two four two four two four two four two four two four two")
        assert not result.clean
        assert all(f.preview == "" for f in result.stripped)
        assert not any(
            f.sensitivity is Sensitivity.TAG and "4" in f.preview for f in result.findings
        )

    def test_a_cvv_needs_its_cue_word(self):
        """Three digits are otherwise unremarkable; stripping every one destroys dates."""
        assert "[CVV]" in redact("the cvv is 737").text
        assert redact("there were 737 charges").clean

    def test_addresses_are_kept_because_the_agent_needs_them(self):
        result = redact("my postcode is SW1A 1AA")
        assert "SW1A 1AA" in result.text
        assert result.clean                       # TAG, not STRIP

    def test_a_prefix_already_emitted_can_become_sensitive_in_hindsight(self):
        """Four digits are not a card. Sixteen are — and the first four are already downstream."""
        redactor = StreamingRedactor()
        early = redactor.feed("my card is four two four two")
        assert early.clean and not redactor.dirty

        full = redactor.feed(
            "my card is four two four two four two four two four two four two four two four two"
        )
        assert not full.clean
        assert redactor.dirty, "downstream was never told to retract the earlier partial"

    def test_the_model_only_ever_receives_the_redacted_text(self):
        """A model that never receives a PAN cannot leak one."""
        redactor = StreamingRedactor()
        redactor.feed("card four two four two four two four two four two four two four two four two")
        assert "[CARD]" in redactor.safe_for_model


# ── end-to-end simulated calls ───────────────────────────────────────────────
class TestSimulatedCalls:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario_id", list(CANNED_CALLS))
    async def test_every_scenario_runs_and_answers_every_turn(self, scenario_id: str):
        result = await replay(CANNED_CALLS[scenario_id])
        assert result["summary"]["turns"] >= 1
        assert result["events"]

    @pytest.mark.asyncio
    async def test_a_caller_reading_a_number_is_never_cut_off(self):
        """The most damaging false cutoff there is. Two pauses over 700ms, mid-number."""
        result = await replay(CANNED_CALLS["account-number"])
        assert result["summary"]["false_cutoffs"] == 0
        assert result["summary"]["turns"] == 2

    @pytest.mark.asyncio
    async def test_holding_through_a_number_costs_latency_and_that_is_the_point(self):
        """Not a regression. The agent waits longer BECAUSE it refuses to interrupt, and a
        benchmark that hid this cost would be dishonest."""
        numbers = await replay(CANNED_CALLS["account-number"])
        ordinary = await replay(CANNED_CALLS["booking"])
        assert numbers["summary"]["median_endpoint_ms"] > ordinary["summary"]["median_endpoint_ms"]

    @pytest.mark.asyncio
    async def test_history_records_what_the_caller_heard_not_what_was_generated(self):
        """THE CENTRAL CLAIM. An agent that believes it said things the caller never heard
        produces answers that make no sense two turns later."""
        result = await replay(CANNED_CALLS["barge-in"])
        cuts = [e for e in result["events"] if e["kind"] == "barge_in"]
        assert cuts, "the scenario exercised no barge-in"

        cut = cuts[0]
        assert cut["fraction_played"] < 0.9, "interrupted after the whole utterance had played"
        assert len(cut["heard"]) < len(cut["generated"])
        assert cut["heard"].endswith("…")
        # And the truncated version is what the model will see next turn.
        assert any(m["content"] == cut["heard"] for m in result["transcript"])

    @pytest.mark.asyncio
    async def test_a_backchannel_does_not_stop_the_agent(self):
        """An agent that stops on "mm-hmm" cannot deliver a sentence longer than a few words."""
        result = await replay(CANNED_CALLS["backchannel"])
        assert result["summary"]["backchannels"] >= 1
        assert result["summary"]["interruptions"] == 0

    @pytest.mark.asyncio
    async def test_a_long_pause_earns_exactly_one_acknowledgement(self):
        """`evaluate` runs every 20ms. An unlatched backchannel fires sixty times in a pause."""
        result = await replay(CANNED_CALLS["card-number"])
        assert result["summary"]["backchannels"] <= 2

    @pytest.mark.asyncio
    async def test_a_card_read_aloud_never_reaches_the_transcript(self):
        result = await replay(CANNED_CALLS["card-number"])
        assert result["redactions"], "the card was not detected"
        assert all("[CARD]" in r["safe_text"] for r in result["redactions"])
        for message in result["transcript"]:
            assert "four five three nine" not in message["content"]

    @pytest.mark.asyncio
    async def test_the_endpointer_holds_on_a_lossy_line(self):
        result = await replay(CANNED_CALLS["packet-loss"])
        assert result["summary"]["false_cutoffs"] == 0

    @pytest.mark.asyncio
    async def test_replaying_a_scenario_twice_exercises_it_twice(self):
        """Replay consumes the script — a fired interruption is cleared so it cannot re-fire.

        If that mutation lands on the shared `CANNED_CALLS` rather than a copy, the second
        replay silently exercises less than the first, and every test that runs after another
        test touching the same scenario quietly stops testing anything.
        """
        first = await replay(CANNED_CALLS["barge-in"])
        second = await replay(CANNED_CALLS["barge-in"])
        assert [e["kind"] for e in first["events"]] == [e["kind"] for e in second["events"]]
        assert second["summary"]["interruptions"] == 1

    @pytest.mark.asyncio
    async def test_a_run_is_reproducible(self):
        """A simulator that answers differently each time cannot prove anything."""
        first = await replay(CANNED_CALLS["booking"])
        second = await replay(CANNED_CALLS["booking"])
        assert first["summary"] == second["summary"]
        assert first["events"] == second["events"]


class TestRetrievalRelevance:
    """The gate between "the agent knows this" and "the agent is guessing".

    Both directions are tested because both are real failures and they pull against each other.
    A live call retrieved the emergency-treatment page for "hi, how are you doing?" -- harmless
    that time, and the same leak on a question about prices would have the agent answering from
    an unrelated document with complete confidence.
    """

    def base(self):
        from dialtone.brain.knowledge import KnowledgeBase
        from dialtone.platform import SEED_DOCUMENTS

        kb = KnowledgeBase()
        for index, (title, body) in enumerate(SEED_DOCUMENTS.items()):
            kb.add_document(f"d{index}", title, body)
        return kb

    @pytest.mark.parametrize("phrase", [
        "hi", "hello", "hi how are you doing", "Hi, how are you doing?",
        "thanks, bye", "ok great", "sorry can you repeat that",
    ])
    def test_small_talk_retrieves_nothing(self, phrase: str):
        """Greetings are not questions. Handing the agent a document for "hello" invites it to
        answer one that was never asked."""
        assert self.base().search(phrase) == []

    @pytest.mark.parametrize("question,expected", [
        ("how much is a check-up", "Prices"),
        ("my tooth broke", "Emergencies"),
        ("are you open on saturday", "Opening hours"),
        ("do you have parking", "Parking and access"),
        ("is there disabled access", "Parking and access"),
        ("can I cancel my appointment", "Appointments and cancellations"),
    ])
    def test_a_real_question_finds_the_right_document(self, question: str, expected: str):
        """The costlier error of the two. An agent that says "let me check" about something it
        was explicitly told is the reason a caller asks for a person."""
        hits = self.base().search(question)
        assert hits, f"{question!r} retrieved nothing"
        assert hits[0].chunk.document_title == expected

    def test_a_question_about_something_else_entirely_retrieves_nothing(self):
        assert self.base().search("do you sell dog food") == []
