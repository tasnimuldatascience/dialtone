"""Talking over the agent, against the running gateway.

The two halves of barge-in are unit-tested apart: `apps/studio/src/bargein.ts` decides WHEN, and
`Conversation.interrupted` decides what the agent is left believing. This drives the seam between
them — the real socket, the real model — and checks the thing that actually matters:

    AFTER BEING CUT OFF, DOES THE AGENT KNOW WHAT IT DID NOT MANAGE TO SAY?

That is the failure worth testing. Stopping the audio is easy. The damage comes two turns later,
when an agent that still believes it read out four appointment times says "as I mentioned,
Wednesday at noon" to a caller who never heard it.

Run the gateway first, then:  python scripts/interrupt-e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("DIALTONE_PORT", "8071")
BASE = f"http://127.0.0.1:{PORT}"

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

ok = True


def check(label: str, passed: bool, detail: str = "") -> None:
    global ok
    ok = ok and passed
    mark = f"{GREEN}PASS{OFF}" if passed else f"{RED}FAIL{OFF}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{OFF}" if detail else ""))


def call(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read())


async def main() -> int:
    import websockets

    try:
        if call("GET", "/api/health")["status"] != "ready":
            print(f"{RED}The model is still loading.{OFF}")
            return 2
    except urllib.error.URLError:
        print(f"{RED}The gateway is not running.{OFF}")
        return 2

    agent = call("GET", "/api/agents")["agents"][0]["id"]
    call_id = call("POST", "/api/calls", {"agent_id": agent, "channel": "text"})["call_id"]

    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/call/{call_id}") as socket:
        await socket.recv()

        print(f"\n{BOLD}The agent starts answering{OFF}")
        await socket.send(json.dumps({"type": "say", "text": "when are you open this week?"}))
        written = ""
        while True:
            event = json.loads(await socket.recv())
            if event["type"] == "done":
                written = event["agent"]
                break
        print(f"  {DIM}wrote  {written}{OFF}")

        # The caller cuts in a third of the way through. On a real call this comes from the audio
        # clock; here it is simulated at a fixed point so the assertion is deterministic.
        words = written.split()
        heard = " ".join(words[: max(1, len(words) // 3)])
        print(f"  {DIM}heard  {heard}…{OFF}")
        print(f"  {DIM}caller [talks over it]{OFF}")

        await socket.send(json.dumps({"type": "interrupt", "heard": heard}))
        reply = json.loads(await socket.recv())

        print(f"\n{BOLD}What the gateway did{OFF}")
        check("the interruption is acknowledged", reply.get("type") == "interrupted")
        check("it trimmed the reply", bool(reply.get("trimmed")))

        memory = call("GET", f"/api/calls/{call_id}/memory")
        check("the call is still live afterwards", isinstance(memory, dict))

        # The real question: does the next turn reference something the caller never heard?
        print(f"\n{BOLD}The next turn{OFF}")
        await socket.send(json.dumps({"type": "say", "text": "sorry, say that again?"}))
        while True:
            event = json.loads(await socket.recv())
            if event["type"] == "done":
                again = event["agent"]
                break
        print(f"  {DIM}agent  {again}{OFF}")

        # Nothing the caller never heard may be referred to as already said. This is a weak
        # check by nature -- it is a language model -- so it looks for the specific phrasing
        # that gives the game away rather than trying to judge the whole reply.
        bad = [p for p in ("as i mentioned", "as i said", "like i said", "as mentioned")
               if p in again.lower()]
        check("it does not claim to have already said it", not bad, ", ".join(bad) or "—")

        await socket.send(json.dumps({"type": "hangup"}))

    # And the transcript keeps the record straight.
    record = call("GET", f"/api/calls/{call_id}")
    turns = record.get("turns", [])
    check("the call record survived", bool(turns), f"{len(turns)} turns")

    print()
    print(f"{GREEN}{BOLD}Barge-in holds.{OFF}" if ok else f"{RED}{BOLD}Something broke.{OFF}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
