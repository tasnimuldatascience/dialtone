"""How many callers this machine will take, and what happens to the next one.

THE MEASUREMENT THAT SET THE NUMBER. Concurrent turns on the reference laptop, Qwen2.5-1.5B on
CPU:

    1 caller    first token 1114ms   whole turn 3091ms
    2 callers   first token 1054ms   whole turn 2699ms
    4 callers   first token 3427ms   whole turn 5929ms
    8 callers   first token 5287ms   whole turn 8100ms

NOTHING FAILED AT EIGHT, and that is the whole problem. The system did not refuse anybody; it
degraded everybody. A voice agent that answers and then leaves you in silence for five seconds is
worse than one that never answered — the caller is already committed, and the entire argument of
this project is about the first few hundred milliseconds.

So the limit is enforced at the door. Every commercial platform in this category publishes a
concurrency number and refuses past it; this one had neither, which is indistinguishable from
having no limit right up until the day it is hit.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from dialtone.platform import DEFAULT_MAX_CALLS, AtCapacity, Platform


def platform(max_calls: int | None = None) -> Platform:
    """A scratch platform with one agent. Seeding only runs on a warm start, and these tests do
    not warm anything, so the agent is created directly."""
    p = Platform(Path(tempfile.mkdtemp()) / "c.db", use_local_model=False, max_calls=max_calls)
    if not p.store.list_agents():
        p.store.create_agent(name="Reception", business="Northgate Dental")
    return p


class TestTheLimit:
    def test_a_call_is_refused_once_the_machine_is_full(self):
        p = platform(max_calls=2)
        agent = p.store.list_agents()[0]["id"]

        p.start_call(agent)
        p.start_call(agent)
        with pytest.raises(AtCapacity):
            p.start_call(agent)

    def test_the_refusal_says_what_to_do_about_it(self):
        """A 503 with no reason is a wall. The operator can raise this if the hardware allows."""
        p = platform(max_calls=1)
        agent = p.store.list_agents()[0]["id"]
        p.start_call(agent)
        with pytest.raises(AtCapacity, match="DIALTONE_MAX_CALLS"):
            p.start_call(agent)

    def test_hanging_up_frees_the_slot(self):
        p = platform(max_calls=1)
        agent = p.store.list_agents()[0]["id"]

        call_id, _ = p.start_call(agent)
        with pytest.raises(AtCapacity):
            p.start_call(agent)

        p.end_call(call_id)
        p.start_call(agent)                    # the next caller gets through

    def test_calls_already_running_are_never_disturbed(self):
        """Admission control, not eviction. Refusing the fourth caller is a bad moment for one
        person; degrading the three already talking is a bad moment for four."""
        p = platform(max_calls=3)
        agent = p.store.list_agents()[0]["id"]
        live = [p.start_call(agent)[0] for _ in range(3)]

        with pytest.raises(AtCapacity):
            p.start_call(agent)

        for call_id in live:
            assert p.live_call(call_id) is not None


class TestItIsVisible:
    def test_capacity_is_reported(self):
        p = platform(max_calls=4)
        assert p.capacity == {
            "live": 0, "limit": 4, "available": 4, "measured_on": "Qwen2.5-1.5B, CPU",
        }

    def test_it_tracks_as_calls_come_and_go(self):
        p = platform(max_calls=3)
        agent = p.store.list_agents()[0]["id"]

        call_id, _ = p.start_call(agent)
        assert p.capacity["live"] == 1
        assert p.capacity["available"] == 2

        p.end_call(call_id)
        assert p.capacity["live"] == 0
        assert p.capacity["available"] == 3

    def test_the_default_is_where_the_measurements_put_it(self):
        """Two concurrent callers cost nothing; four doubles the turn time. Three is the last
        number that is still a phone call."""
        assert DEFAULT_MAX_CALLS == 3
        assert platform().capacity["limit"] == DEFAULT_MAX_CALLS

    def test_the_operator_can_raise_it(self, monkeypatch):
        """The number belongs to the hardware, not the code. A GPU or a smaller model moves it
        and nothing else has to change."""
        monkeypatch.setenv("DIALTONE_MAX_CALLS", "16")
        assert platform().capacity["limit"] == 16
