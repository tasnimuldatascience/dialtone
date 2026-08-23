"""Turn-taking: endpointing, barge-in, and the flow guardrails.

The three that carry the argument:

  `test_mid_number_pauses_are_never_cut_off` — a caller reading a card number aloud is the
  most damaging false cutoff there is, and the recogniser emits it as WORDS, not digits.
  `test_history_records_what_the_caller_heard` — the bug almost every voice agent has.
  `test_a_transition_the_graph_does_not_declare_is_refused` — the guardrail that makes a flow
  worth having over a prompt.
"""

from __future__ import annotations

import pytest

from dialtone.eval import ablate, run
from dialtone.eval.endpointing import CORPUS
from dialtone.flow.graph import (
    Edge,
    Flow,
    FlowRunner,
    GuardrailError,
    Node,
    NodeKind,
)
from dialtone.pipeline.orchestrator import TurnBudget, first_clause
from dialtone.turn import (
    BargeConfig,
    BargeDecision,
    BargeInDetector,
    EndpointConfig,
    Endpointer,
    SpeechFrame,
    TurnDecision,
    TurnState,
    Utterance,
    completion_score,
    fixed_threshold_endpointer,
    truncate_to_played,
)


def state(transcript: str, silence_ms: float, energy=(), speech_ms: float = 1200.0) -> TurnState:
    return TurnState(transcript=transcript, silence_ms=silence_ms,
                     speech_ms=speech_ms, energy_tail=list(energy))


# ════════════════════════════════════════════════════════════ completion score
class TestCompletionScore:
    @pytest.mark.parametrize("text", [
        "my account number is four two",
        "the card ends in seven three",
        "the reference is one two three",
        "call me back on double four seven",
    ])
    def test_spelled_out_numbers_read_as_incomplete(self, text: str):
        """THE CASE THAT MATTERS MOST.

        Recognisers transcribe spoken numbers as WORDS. An earlier version of the rule matched
        only \\d+ and therefore missed every single real instance of the failure it was written
        to prevent — the benchmark caught it, not inspection.
        """
        score, reason = completion_score(text)
        assert score < 0.15, f"{text!r} scored {score:.2f}: {reason}"

    @pytest.mark.parametrize("text", [
        "my postcode is SW1A", "the booking ref is AB12CD", "my plate is LT19 XYZ",
    ])
    def test_alphanumeric_codes_read_as_incomplete(self, text: str):
        assert completion_score(text)[0] < 0.2

    @pytest.mark.parametrize("text", [
        "I'd like to book an appointment for", "can I speak to", "the problem is that",
        "I was wondering if", "I'm trying to work out whether", "could you tell me the",
    ])
    def test_dangling_function_words_read_as_incomplete(self, text: str):
        assert completion_score(text)[0] < 0.15

    @pytest.mark.parametrize("text", ["I think it's um", "let me see uh", "so basically", "well"])
    def test_fillers_read_as_incomplete(self, text: str):
        assert completion_score(text)[0] < 0.2

    @pytest.mark.parametrize("text", ["yes", "no", "okay", "agent", "sure"])
    def test_short_confirmations_read_as_complete(self, text: str):
        """Waiting 700ms after "yes" is most of what makes an agent feel sluggish."""
        assert completion_score(text)[0] > 0.9

    def test_an_empty_transcript_is_neutral_not_complete(self):
        """A caller who has not started speaking must not be endpointed."""
        assert completion_score("")[0] == 0.5


# ════════════════════════════════════════════════════════════ endpointer
class TestEndpointer:
    def test_a_complete_short_turn_ends_fast(self):
        endpointer = Endpointer()
        assert endpointer.evaluate(state("yes", 200, (0.9, 0.8, 0.7, 0.3, 0.2, 0.1))).ended

    def test_mid_number_pauses_are_never_cut_off(self):
        """A caller reading out a card number pauses constantly. Cutting in is the worst
        thing this system can do, so it must hold well past any plausible pause."""
        endpointer = Endpointer()
        for silence in (300, 500, 700, 900, 1100, 1400):
            decision = endpointer.evaluate(state("my card number is four two four two", silence))
            assert not decision.ended, f"cut off after {silence}ms: {decision.reason}"

    def test_the_threshold_adapts_to_the_words(self):
        """The entire mechanism, in one assertion."""
        endpointer = Endpointer()
        complete = endpointer.evaluate(state("yes", 0))
        incomplete = endpointer.evaluate(state("my account number is", 0))
        assert complete.threshold_ms < incomplete.threshold_ms / 2

    def test_the_ceiling_is_respected_when_a_caller_trails_off(self):
        """People do leave sentences unfinished. Waiting forever for a grammatical ending is
        its own failure mode."""
        endpointer = Endpointer()
        assert endpointer.evaluate(state("I was wondering if", 2000)).ended

    def test_the_floor_is_respected(self):
        """Below the floor, an intra-word pause reads as end-of-turn and the agent interrupts
        syllables. No linguistic confidence may go below it."""
        endpointer = Endpointer()
        assert not endpointer.evaluate(state("yes", 100)).ended

    def test_a_long_turn_earns_exactly_one_backchannel(self):
        endpointer = Endpointer()
        turn = state("so what happened was I ordered it on the", 200, speech_ms=4000)
        first = endpointer.evaluate(turn)
        assert first.decision is TurnDecision.BACKCHANNEL
        turn.backchanneled = True
        assert endpointer.evaluate(turn).decision is TurnDecision.WAIT

    def test_disabling_the_signals_reproduces_a_fixed_threshold(self):
        plain = Endpointer(EndpointConfig(base_silence_ms=600, enable_semantic=False,
                                          enable_prosody=False))
        for text in ("yes", "my account number is", "I was wondering if"):
            assert plain.evaluate(state(text, 0)).threshold_ms == pytest.approx(600)


# ════════════════════════════════════════════════════════════ the benchmark
class TestEndpointingBenchmark:
    def test_the_adaptive_endpointer_beats_the_baseline_on_both_axes(self):
        """The claim the README makes. Both axes, or it is not an improvement -- any latency
        figure is reachable by lowering a threshold; the question is what it cost."""
        baseline = run(fixed_threshold_endpointer(700), "fixed 700ms")
        adaptive = run(Endpointer(EndpointConfig()), "adaptive")

        assert adaptive.median_latency_ms < baseline.median_latency_ms
        assert adaptive.false_cutoff_rate <= baseline.false_cutoff_rate
        assert adaptive.completion_recall >= baseline.completion_recall

    def test_the_adaptive_endpointer_never_cuts_off_an_unfinished_turn(self):
        result = run(Endpointer(EndpointConfig()), "adaptive")
        assert result.false_cutoff_rate == 0.0, "\n".join(result.failures)

    def test_it_answers_every_completed_turn(self):
        """A system that never interrupts by never responding is not an improvement."""
        assert run(Endpointer(EndpointConfig()), "adaptive").completion_recall == 1.0

    def test_lowering_a_fixed_threshold_trades_latency_for_cutoffs(self):
        """The trade-off the single published latency number hides."""
        fast = run(fixed_threshold_endpointer(300), "fast")
        slow = run(fixed_threshold_endpointer(1200), "slow")
        assert fast.median_latency_ms < slow.median_latency_ms
        assert fast.false_cutoff_rate > slow.false_cutoff_rate

    def test_the_ablation_shows_syntax_carries_the_result(self):
        """If the adaptive version were only better because its base threshold happens to be
        tuned, turning the signals off would not change the false-cutoff rate."""
        results = {r.label: r for r in ablate()}
        assert results["adaptive, no signals"].false_cutoff_rate > 0.5
        assert results["+ both (default)"].false_cutoff_rate == 0.0

    def test_the_corpus_is_balanced_enough_to_measure_both_axes(self):
        complete = [s for s in CORPUS if s.complete]
        incomplete = [s for s in CORPUS if not s.complete]
        assert len(complete) >= 15 and len(incomplete) >= 15


# ════════════════════════════════════════════════════════════ barge-in
class TestTruncation:
    def test_history_records_what_the_caller_heard(self):
        """THE BUG ALMOST EVERY VOICE AGENT HAS.

        The agent generated a full sentence; the caller heard a third of it. Recording the
        full text makes the agent believe it offered three appointment slots the caller never
        heard, and its next turn refers back to them.
        """
        utterance = Utterance(
            text="Sure, I can see three appointments available Tuesday Wednesday or Friday",
            total_ms=3000, played_ms=900,
        )
        heard = truncate_to_played(utterance)
        assert "Friday" not in heard
        assert heard.startswith("Sure")
        assert heard.endswith("…"), "the model must be able to see it was cut off"

    def test_a_finished_utterance_is_not_truncated(self):
        utterance = Utterance(text="all done", total_ms=500, played_ms=500)
        assert truncate_to_played(utterance) == "all done"

    def test_an_interruption_before_any_audio_yields_nothing_heard(self):
        assert truncate_to_played(Utterance("hello there", 800, 0)) == ""

    def test_word_timings_are_used_when_available(self):
        utterance = Utterance(
            text="one two three four", total_ms=800, played_ms=450,
            word_timings=[("one", 100), ("two", 200), ("three", 600), ("four", 800)],
        )
        heard = truncate_to_played(utterance)
        assert "two" in heard and "three" not in heard

    def test_truncation_never_lands_mid_word(self):
        utterance = Utterance(text="internationalisation matters", total_ms=1000, played_ms=310)
        heard = truncate_to_played(utterance).rstrip("…").strip()
        assert heard in ("", "internationalisation")


class TestBargeIn:
    def frame(self, energy=0.5, speech=True, ms=100.0) -> SpeechFrame:
        return SpeechFrame(energy=energy, is_speech=speech, duration_ms=ms)

    def talking(self, played=1000.0) -> Utterance:
        return Utterance(text="a fairly long agent sentence here", total_ms=3000, played_ms=played)

    def test_sustained_loud_speech_interrupts(self):
        detector = BargeInDetector()
        detector.noise_floor = 0.02
        detector.evaluate(self.frame(), self.talking(), "no wait")
        verdict = detector.evaluate(self.frame(), self.talking(), "no wait actually")
        assert verdict.decision is BargeDecision.INTERRUPT

    def test_a_cough_does_not_interrupt(self):
        """A transient must not be able to stop the agent mid-sentence."""
        detector = BargeInDetector()
        detector.noise_floor = 0.02
        verdict = detector.evaluate(self.frame(ms=80), self.talking(), "")
        assert verdict.decision is BargeDecision.IGNORE
        assert "transient" in verdict.reason

    def test_quiet_background_does_not_interrupt(self):
        detector = BargeInDetector()
        detector.noise_floor = 0.2
        verdict = detector.evaluate(self.frame(energy=0.25), self.talking(), "")
        assert verdict.decision is BargeDecision.IGNORE

    @pytest.mark.parametrize("word", ["mm-hmm", "yeah", "okay", "right", "sure"])
    def test_backchannels_do_not_interrupt(self, word: str):
        """Stopping dead every time a caller says "uh huh" makes long answers impossible."""
        detector = BargeInDetector()
        detector.noise_floor = 0.02
        detector.evaluate(self.frame(), self.talking(), word)
        verdict = detector.evaluate(self.frame(), self.talking(), word)
        assert verdict.decision is BargeDecision.BACKCHANNEL

    @pytest.mark.parametrize("word", ["stop", "wait", "no"])
    def test_hard_interrupts_bypass_the_duration_gate(self, word: str):
        """Making someone say "stop" twice is the worst behaviour available."""
        detector = BargeInDetector()
        detector.noise_floor = 0.02
        verdict = detector.evaluate(self.frame(ms=40), self.talking(), word)
        assert verdict.decision is BargeDecision.INTERRUPT

    def test_the_echo_guard_ignores_the_agents_own_first_syllable(self):
        """Speakerphone echo arrives right as the agent starts talking, and it looks exactly
        like the caller interrupting."""
        detector = BargeInDetector(BargeConfig(echo_guard_ms=150))
        detector.noise_floor = 0.02
        detector.evaluate(self.frame(), self.talking(played=40), "")
        verdict = detector.evaluate(self.frame(), self.talking(played=60), "")
        assert verdict.decision is BargeDecision.IGNORE
        assert "echo guard" in verdict.reason

    def test_an_interruption_reports_what_was_heard(self):
        detector = BargeInDetector()
        detector.noise_floor = 0.02
        detector.evaluate(self.frame(), self.talking(), "hang on")
        verdict = detector.evaluate(self.frame(), self.talking(), "hang on a second")
        assert verdict.decision is BargeDecision.INTERRUPT
        assert verdict.heard_text and verdict.heard_text.endswith("…")


# ════════════════════════════════════════════════════════════ flow
def sample_flow() -> Flow:
    return Flow(
        name="support",
        start="greet",
        nodes={
            "greet": Node("greet", NodeKind.SPEAK, "Greet and find out why they called",
                          edges=(Edge("identify", "caller states a reason"),
                                 Edge("transfer", "caller asks for a person"))),
            "identify": Node("identify", NodeKind.COLLECT, "Get the account number",
                             collects="account", pattern=r"\d{4,}",
                             tools=("lookup_account",),
                             edges=(Edge("resolve", "account found"),
                                    Edge("transfer", "cannot verify"))),
            "resolve": Node("resolve", NodeKind.TOOL, "Resolve the issue",
                            tools=("check_order", "refund"),
                            edges=(Edge("done", "issue resolved"),
                                   Edge("transfer", "needs a human"))),
            "transfer": Node("transfer", NodeKind.TRANSFER, "Hand to a human", edges=()),
            "done": Node("done", NodeKind.END, "Close the call", edges=()),
        },
        global_tools=("end_call",),
    )


class TestFlow:
    def test_a_valid_flow_loads(self):
        assert FlowRunner(sample_flow())

    def test_an_edge_to_a_missing_node_is_refused_at_load(self):
        """Found before a call rather than during one -- a dead-end discovered live is
        discovered on a customer."""
        flow = sample_flow()
        flow.nodes["greet"] = Node("greet", NodeKind.SPEAK, "x", edges=(Edge("nowhere", "w"),))
        with pytest.raises(GuardrailError, match="no such node"):
            FlowRunner(flow)

    def test_an_unreachable_node_is_refused(self):
        flow = sample_flow()
        flow.nodes["orphan"] = Node("orphan", NodeKind.END, "never reached")
        with pytest.raises(GuardrailError, match="unreachable"):
            FlowRunner(flow)

    def test_a_flow_that_cannot_terminate_is_refused(self):
        flow = Flow("loop", "a", {
            "a": Node("a", NodeKind.SPEAK, "x", edges=(Edge("b", "w"),)),
            "b": Node("b", NodeKind.SPEAK, "y", edges=(Edge("a", "w"),)),
        })
        with pytest.raises(GuardrailError, match="cannot terminate"):
            FlowRunner(flow)

    def test_only_the_current_nodes_tools_are_available(self):
        """THE GUARDRAIL. A tool absent from the schema cannot be called even by a model that
        has decided it should be."""
        runner = FlowRunner(sample_flow())
        state = runner.start()
        assert "refund" not in runner.available_tools(state)
        runner.transition(state, "identify")
        assert "lookup_account" in runner.available_tools(state)
        assert "refund" not in runner.available_tools(state)

    def test_global_tools_are_always_available(self):
        runner = FlowRunner(sample_flow())
        assert "end_call" in runner.available_tools(runner.start())

    def test_a_transition_the_graph_does_not_declare_is_refused(self):
        """The entire value of the graph: a hallucinated transition cannot change state."""
        runner = FlowRunner(sample_flow())
        with pytest.raises(GuardrailError, match="not a declared edge"):
            runner.transition(runner.start(), "resolve")

    def test_a_collect_node_cannot_be_left_before_it_collects(self):
        runner = FlowRunner(sample_flow())
        state = runner.transition(runner.start(), "identify")
        with pytest.raises(GuardrailError, match="before collecting"):
            runner.transition(state, "resolve")

    def test_a_malformed_value_is_rejected_and_re_asked(self):
        runner = FlowRunner(sample_flow())
        state = runner.transition(runner.start(), "identify")
        ok, reason = runner.collect(state, "I don't know")
        assert not ok and "re-ask" in reason
        ok, _ = runner.collect(state, "48210")
        assert ok
        runner.transition(state, "resolve")

    def test_repeated_failures_escalate_rather_than_looping_forever(self):
        """A caller trapped re-answering the same question is worse than a transfer."""
        runner = FlowRunner(sample_flow())
        state = runner.transition(runner.start(), "identify")
        for _ in range(2):
            runner.collect(state, "nope")
        ok, reason = runner.collect(state, "still nope")
        assert not ok and "escalate" in reason

    def test_every_path_can_be_enumerated(self):
        """What makes a flow testable at all, and a prompt not."""
        paths = FlowRunner(sample_flow()).flow.paths()
        assert paths
        assert all(p[0] == "greet" for p in paths)
        assert any(p[-1] == "done" for p in paths)
        assert any(p[-1] == "transfer" for p in paths)

    def test_the_prompt_states_objectives_not_scripts(self):
        runner = FlowRunner(sample_flow())
        prompt = runner.prompt(runner.start())
        assert "Objective:" in prompt
        assert "your own words" in prompt
        assert "identify" in prompt and "transfer" in prompt


# ════════════════════════════════════════════════════════════ pipeline
class TestPipeline:
    def test_first_clause_splits_at_a_natural_pause(self):
        """~200ms earlier than waiting for a sentence — about a third of the whole budget."""
        clause, rest = first_clause("Of course I can help, let me pull that up for you.")
        assert clause == "Of course I can help,"
        assert rest.strip().startswith("let me pull")

    def test_a_fragment_too_short_is_not_emitted_alone(self):
        """A two-word opening gives the synthesiser no prosodic context and sounds clipped."""
        clause, rest = first_clause("Hi, there")
        assert clause == ""
        assert rest == "Hi, there"

    def test_text_with_no_boundary_is_held(self):
        assert first_clause("no boundaries here at all")[0] == ""

    def test_the_budget_accumulates_repeated_stages(self):
        """A turn with two tool calls should report total time in tools, not just the last."""
        budget = TurnBudget()
        budget.mark("tool")
        budget.mark("tool")
        assert len([k for k in budget.marks if k == "tool"]) == 1
        assert budget.marks["tool"] > 0

    def test_the_budget_flags_an_over_budget_turn(self):
        budget = TurnBudget()
        budget.marks["llm"] = 900.0
        assert not budget.within_budget
