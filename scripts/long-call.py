"""A call that goes on far longer than anyone plans for.

Qwen2.5-1.5B has a finite context. Every turn adds to it, and the interesting question is not
whether it eventually overflows but WHAT HAPPENS FIRST: does latency creep, does it forget the
name it was given on turn one, does it start repeating itself, does it crash.

WHAT THIS IS ACTUALLY FOR. The structural checks below all passed the first time it was run --
no crash, no empty reply, no repetition, 1.09x latency drift, and the name survived thirty
turns. Every bug it found was in the CONTENT, and only by reading the transcript:

  "one more thing - do you do whitening?"   parsed "one" as one o'clock and silently moved an
                                            appointment already agreed for nine thirty
  "located at [insert location]"            read a template placeholder out loud, because the
                                            knowledge base had no address in it
  "where are you exactly?" retrieved nothing, which is why the model had to invent something

So this prints the whole conversation rather than a pass/fail. The assertions catch the failures
that are mechanical; the transcript is where the real ones are.

Run the gateway first, then:  python scripts/long-call.py
"""

import asyncio
import json
import os
import statistics
import time
import urllib.request
from contextlib import suppress

PORT = os.environ.get("DIALTONE_PORT", "8071")
BASE = f"http://127.0.0.1:{PORT}"
DIM, BOLD, RED, GREEN, OFF = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[0m"

TURNS = [
    "hello, my name is Sam Hassan",
    "I need an appointment",
    "my tooth has been hurting for a few days",
    "how much is a check-up?",
    "what about a filling?",
    "do you take insurance?",
    "which ones?",
    "what are your opening hours?",
    "are you open on saturdays?",
    "how late on thursdays?",
    "where are you exactly?",
    "is there parking?",
    "how long does a check-up take?",
    "do I need to bring anything?",
    "can I come tomorrow?",
    "what times are free?",
    "how about the morning?",
    "is nine thirty free?",
    "actually, what about the afternoon?",
    "do you have anything at two?",
    "hmm, let me think",
    "what was the price again?",
    "and what's my name, do you still have it?",
    "ok let's do tomorrow morning",
    "nine thirty works",
    "yes that's fine",
    "how do I pay?",
    "can I pay on the day?",
    "great, thank you",
    "one more thing — do you do whitening?",
]


def call(method, path, body=None):
    r = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.loads(resp.read())


async def main():
    import websockets

    agent = call("GET", "/api/agents")["agents"][0]["id"]
    call_id = call("POST", "/api/calls", {"agent_id": agent, "channel": "text"})["call_id"]

    latencies, replies, problems = [], [], []
    print(f"{BOLD}{len(TURNS)} turns{OFF}\n")

    async with websockets.connect(
        f"ws://127.0.0.1:{PORT}/ws/call/{call_id}", max_size=None
    ) as socket:
        await socket.recv()
        for i, line in enumerate(TURNS, 1):
            started = time.perf_counter()
            await socket.send(json.dumps({"type": "say", "text": line}))
            reply = ""
            try:
                while True:
                    event = json.loads(await asyncio.wait_for(socket.recv(), timeout=180))
                    if event["type"] == "done":
                        reply = event["agent"]
                        break
                    if event["type"] == "error":
                        problems.append(f"turn {i}: {event.get('message')}")
                        break
            except TimeoutError:
                problems.append(f"turn {i}: no reply within 180s")
                break
            except Exception as exc:  # noqa: BLE001
                problems.append(f"turn {i}: {type(exc).__name__} {exc}")
                break

            elapsed = (time.perf_counter() - started) * 1000
            latencies.append(elapsed)
            replies.append(reply)
            flag = ""
            if not reply.strip():
                flag = f"  {RED}<- empty reply{OFF}"
                problems.append(f"turn {i}: empty reply")
            if reply and replies.count(reply) > 1:
                flag = f"  {RED}<- repeated verbatim{OFF}"
                problems.append(f"turn {i}: repeated a previous reply verbatim")
            print(f"  {i:>2}. {elapsed:>6.0f}ms  {DIM}{line[:38]:38}{OFF} {reply[:60]}{flag}")

        memory = call("GET", f"/api/calls/{call_id}/memory")
        with suppress(Exception):
            # The flow may have reached its end node and closed the call already, which is
            # the correct outcome rather than a failure to hang up.
            await socket.send(json.dumps({"type": "hangup"}))

    print(f"\n{BOLD}Latency{OFF}")
    first, last = latencies[:5], latencies[-5:]
    print(f"  median      {statistics.median(latencies):.0f}ms")
    print(f"  first five  {statistics.mean(first):.0f}ms")
    print(f"  last five   {statistics.mean(last):.0f}ms")
    drift = statistics.mean(last) / max(statistics.mean(first), 1)
    print(f"  drift       {drift:.2f}x")

    print(f"\n{BOLD}What it still knew at the end{OFF}")
    for name, fact in memory["facts"].items():
        print(f"  {name:<8} {fact['value']}")
    if memory.get("proposed_slot"):
        print(f"  slot     {memory['proposed_slot']}")

    kept_name = memory["facts"].get("name", {}).get("value") == "Sam Hassan"
    print(f"\n  name from turn 1 survived {len(TURNS)} turns: "
          f"{GREEN + 'yes' + OFF if kept_name else RED + 'NO' + OFF}")

    print(f"\n{BOLD}Problems{OFF}")
    if problems:
        for p in problems:
            print(f"  {RED}{p}{OFF}")
    else:
        print(f"  {GREEN}none{OFF}")
    return 1 if problems or not kept_name else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
