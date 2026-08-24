"""The README's flow chart, checked against the flow it claims to draw.

DOCUMENTATION ROTS SILENTLY. A diagram is worse than no diagram once it is wrong, because a
reader has no way to tell -- and this one already went stale once: the README showed an
`identify` node collecting the caller's name for weeks after that step was deleted.

So the chart is not decoration. It is an assertion about `build_flow()`, and this is the test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dialtone.agents.support import build_flow

README = Path(__file__).resolve().parents[3] / "README.md"


def flowchart() -> str:
    """The mermaid block that draws the conversation flow."""
    blocks = re.findall(r"```mermaid\n(.*?)```", README.read_text(encoding="utf-8"), re.S)
    charts = [b for b in blocks if "greet" in b and "goodbye" in b]
    assert charts, "the README has no conversation flow chart"
    return charts[0]


@pytest.mark.skipif(not README.exists(), reason="running outside the repository")
class TestTheFlowChart:
    def test_it_draws_every_node(self):
        chart = flowchart()
        for node_id in build_flow().nodes:
            # `preferred_day` is drawn as `day` for width; the label carries the real name.
            assert node_id in chart or node_id.split("_")[0] in chart, (
                f"the README's flow chart is missing {node_id!r}"
            )

    def test_it_draws_no_node_that_does_not_exist(self):
        """The failure that actually happened. `identify` was deleted from the flow and stayed
        in the README, so the chart documented a step that could not run."""
        chart = flowchart()
        real = set(build_flow().nodes)
        drawn = {
            match.group(1)
            for match in re.finditer(r"^\s{4}(\w+)[\[({]", chart, re.M)
        }
        aliases = {"day": "preferred_day", "slots": "offer_slots"}
        for name in drawn:
            resolved = aliases.get(name, name)
            assert resolved in real, (
                f"the README's flow chart shows {name!r}, which is not in the flow"
            )

    def test_it_has_an_edge_for_every_edge(self):
        chart = flowchart()
        aliases = {"preferred_day": "day", "offer_slots": "slots"}
        flow = build_flow()
        for node in flow.nodes.values():
            source = aliases.get(node.id, node.id)
            for edge in node.edges:
                target = aliases.get(edge.to, edge.to)
                # Mermaid writes an edge as `a -->|label| b`, one per line.
                line = rf"^\s*{source}\s*-->\s*(?:\|[^|]*\|)?\s*{target}\s*$"
                assert re.search(line, chart, re.M), (
                    f"the README's flow chart is missing {node.id} -> {edge.to}"
                )

    def test_the_tools_named_in_the_chart_are_real(self):
        chart = flowchart()
        from dialtone.agents.support import build_registry

        registry = build_registry()
        for tool in re.findall(r"tool · (\w+)", chart):
            assert registry.spec(tool) is not None, (
                f"the README's flow chart names {tool!r}, which is not a registered tool"
            )


@pytest.mark.skipif(not README.exists(), reason="running outside the repository")
def test_the_readme_states_the_real_test_count():
    """A README that claims more tests than exist is the cheapest possible lie to tell, and the
    easiest one to leave behind."""
    text = README.read_text(encoding="utf-8")
    claimed = re.search(r"(\d+) in the gateway", text)
    assert claimed, "the README no longer says how many tests there are"

    counted = sum(
        len(re.findall(r"^\s*(?:async )?def test_", path.read_text(encoding="utf-8"), re.M))
        for path in Path(__file__).parent.glob("test_*.py")
    )
    # Parametrised cases multiply, so the file count is a floor rather than the exact number.
    # The check that matters is that the README is not claiming a number pulled from the air.
    assert counted <= int(claimed.group(1)), (
        f"the README claims {claimed.group(1)} gateway tests; there are at least {counted} "
        f"test functions, so the figure is stale"
    )
