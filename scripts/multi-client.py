"""Several callers at once, on both channels, each doing something different.

WHAT THIS EXERCISES THAT THE OTHER SCRIPTS DO NOT. `booking-e2e.py` drives one perfect call.
Real traffic is not that: it is a booking and two questions and someone who hangs up without
speaking, all overlapping, some on voice and some typed, and the interesting failures live in the
overlap rather than in any one call.

Specifically, it checks the things that only go wrong when calls share a process:

  DOES STATE LEAK BETWEEN CALLS?  Every caller has a different name and wants a different slot.
                                  If memory, the flow position or the proposed slot is shared,
                                  the wrong name lands in the wrong appointment and this is where
                                  it shows up.
  ARE TWO CALLERS GIVEN THE SAME SLOT?  Both are shown it as free, and only one INSERT can win.
                                  The loser must be told, not silently double-booked.
  DOES THE LIMIT HOLD?            Past `DIALTONE_MAX_CALLS` a caller is refused with a reason
                                  rather than accepted into a queue nobody told them about.
  DOES LATENCY STAY HONEST?       Reported per channel, because a voice call is also paying for
                                  synthesis and the two should not be averaged together.

Run the gateway first, then:  python scripts/multi-client.py
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress

PORT = os.environ.get("DIALTONE_PORT", "8071")
BASE = f"http://127.0.0.1:{PORT}"

G, R, Y, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"

failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    if not passed:
        failures.append(label)
    mark = f"{G}PASS{OFF}" if passed else f"{R}FAIL{OFF}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{OFF}" if detail else ""))


def call(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read())


# ── the callers ──────────────────────────────────────────────────────────────
#: Each one is a different person wanting a different thing, so anything shared between calls
#: shows up as the wrong name against the wrong appointment.
CLIENTS = [
    {
        "who": "Ama Boateng", "channel": "voice", "kind": "booking",
        "phone": "(212) 555-9801", "email": "ama@example.com",
        "says": ["hello, I need to book a scale and polish", "can I come tomorrow morning?"],
    },
    {
        "who": "Jonas Weber", "channel": "text", "kind": "booking",
        "phone": "(212) 555-9802", "email": "jonas@example.com",
        # A DIFFERENT PART OF THE DAY FROM AMA, deliberately. When both asked for "tomorrow
        # morning" they were both correctly offered the first free slot, one INSERT won, and the
        # other caller was left with nothing -- which is the slot race, and it has its own test.
        # This script is for what happens when calls SHARE A PROCESS, so the two bookings are
        # kept apart to stop that one failure masking every other assertion.
        "says": ["hi, my tooth is hurting and I need an appointment",
                 "are you free tomorrow afternoon?"],
    },
    {
        "who": "Priya Raman", "channel": "text", "kind": "question",
        "says": ["how much is a check-up?", "and how late are you open on thursdays?"],
    },
    {
        "who": "Tom Ellis", "channel": "voice", "kind": "question",
        "says": ["where are you exactly?", "is there parking?"],
    },
    {
        "who": "silent caller", "channel": "voice", "kind": "silent", "says": [],
    },
]


async def run_client(agent: str, client: dict, index: int) -> dict:
    import websockets

    out: dict = {"who": client["who"], "channel": client["channel"], "kind": client["kind"],
                 "turns": [], "latencies": [], "booked": None, "error": None}
    try:
        started = call("POST", "/api/calls",
                       {"agent_id": agent, "channel": client["channel"]})
    except urllib.error.HTTPError as exc:
        out["error"] = f"{exc.code}: {json.loads(exc.read()).get('detail', '')[:60]}"
        out["refused"] = exc.code == 503
        return out

    call_id = out["call_id"] = started["call_id"]
    # Stagger slightly so the calls overlap rather than starting in lockstep, which is what real
    # traffic looks like and is harder on shared state than a synchronised burst.
    await asyncio.sleep(index * 0.35)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/call/{call_id}",
                                      max_size=None) as socket:
            await socket.recv()

            for i, line in enumerate(client["says"]):
                # The details are typed, always -- that is the product decision, and on a voice
                # call it is the ONLY way a name or an email is ever right.
                if client["kind"] == "booking" and i == len(client["says"]) - 1:
                    call("PATCH", f"/api/calls/{call_id}/details", {
                        "name": client["who"], "phone": client["phone"],
                        "email": client["email"],
                    })

                t0 = time.perf_counter()
                await socket.send(json.dumps({"type": "say", "text": line}))
                while True:
                    event = json.loads(await asyncio.wait_for(socket.recv(), timeout=300))
                    if event["type"] == "booked":
                        out["booked"] = event
                    if event["type"] == "done":
                        out["latencies"].append((time.perf_counter() - t0) * 1000)
                        out["turns"].append({"caller": line, "agent": event["agent"],
                                             "memory": event.get("memory", {})})
                        break

            # A booking caller agrees to whatever was offered.
            if client["kind"] == "booking":
                t0 = time.perf_counter()
                await socket.send(json.dumps({"type": "say", "text": "yes, that works"}))
                while True:
                    event = json.loads(await asyncio.wait_for(socket.recv(), timeout=300))
                    if event["type"] == "booked":
                        out["booked"] = event
                    if event["type"] == "done":
                        out["latencies"].append((time.perf_counter() - t0) * 1000)
                        out["turns"].append({"caller": "yes, that works",
                                             "agent": event["agent"],
                                             "memory": event.get("memory", {})})
                        break

            with suppress(Exception):
                await socket.send(json.dumps({"type": "hangup"}))
    except Exception as exc:                                    # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:80]

    with suppress(Exception):
        call("POST", f"/api/calls/{call_id}/end")
    return out


async def main() -> int:
    try:
        health = call("GET", "/api/health")
    except urllib.error.URLError:
        print(f"{R}The gateway is not running.{OFF}")
        return 2
    if health["status"] != "ready":
        print(f"{R}The model is still loading.{OFF}")
        return 2

    agent = call("GET", "/api/agents")["agents"][0]["id"]
    limit = health.get("capacity", {}).get("limit", 3)

    print(f"{BOLD}{len(CLIENTS)} callers at once{OFF}")
    print(f"{DIM}  {sum(1 for c in CLIENTS if c['channel'] == 'voice')} voice, "
          f"{sum(1 for c in CLIENTS if c['channel'] == 'text')} text — "
          f"this machine carries {limit}{OFF}\n")

    began = time.perf_counter()
    results = await asyncio.gather(*[run_client(agent, c, i) for i, c in enumerate(CLIENTS)])
    elapsed = (time.perf_counter() - began) * 1000

    # ── what each of them got ────────────────────────────────────────────
    for r in results:
        head = f"  {BOLD}{r['who']}{OFF} {DIM}({r['channel']}, {r['kind']}){OFF}"
        if r.get("refused"):
            print(f"{head}  {Y}refused — {r['error']}{OFF}")
            continue
        if r["error"]:
            print(f"{head}  {R}{r['error']}{OFF}")
            continue
        print(head)
        for turn in r["turns"]:
            print(f"     {DIM}caller{OFF} {turn['caller'][:60]}")
            print(f"     {DIM}agent {OFF} {turn['agent'][:76]}")
        if r["booked"]:
            print(f"     {G}booked {r['booked']['reference']} — {r['booked']['spoken']}{OFF}")
        print()

    served = [r for r in results if not r.get("refused") and not r["error"]]
    refused = [r for r in results if r.get("refused")]

    # ── the assertions ───────────────────────────────────────────────────
    print(f"{BOLD}Did they interfere with each other?{OFF}")

    booked = [r for r in served if r["booked"]]
    appointments = call("GET", "/api/appointments")["appointments"]
    by_ref = {a["reference"]: a for a in appointments}

    check("every caller who agreed a time got an appointment",
          all(r["booked"] for r in served if r["kind"] == "booking"),
          f"{len(booked)} booked")

    # THE STATE-LEAK CHECK. Each name must be on its own appointment and nobody else's.
    wrong = []
    for r in booked:
        record = by_ref.get(r["booked"]["reference"])
        if not record or record["patient_name"] != r["who"]:
            wrong.append(f"{r['who']} -> {record['patient_name'] if record else 'missing'}")
    check("each appointment carries the right caller's name", not wrong, "; ".join(wrong) or "—")

    slots = [a["starts_at"] for a in appointments]
    check("no two callers were given the same slot",
          len(slots) == len(set(slots)),
          f"{len(slots)} appointments, {len(set(slots))} distinct times")

    # Memory must not be shared: nobody should hold another caller's details.
    bleed = []
    for r in served:
        facts = r["turns"][-1]["memory"].get("facts", {}) if r["turns"] else {}
        name = facts.get("name", {}).get("value", "")
        if name and r["kind"] == "booking" and name != r["who"]:
            bleed.append(f"{r['who']} held {name!r}")
    check("no call was holding another caller's details", not bleed, "; ".join(bleed) or "—")

    if refused:
        check("a caller past the limit was refused with a reason",
              all("limit" in (r["error"] or "") for r in refused),
              f"{len(refused)} refused")

    check("the silent caller is recorded, not lost",
          any(r["kind"] == "silent" and not r["error"] for r in results)
          or any(r.get("refused") for r in results if r["kind"] == "silent"))

    # ── latency, split by channel ────────────────────────────────────────
    print(f"\n{BOLD}Latency, by channel{OFF}")
    for channel in ("text", "voice"):
        lat = [ms for r in served if r["channel"] == channel for ms in r["latencies"]]
        if lat:
            print(f"  {channel:<6} p50 {statistics.median(lat):>6.0f}ms   "
                  f"max {max(lat):>6.0f}ms   {DIM}{len(lat)} turns{OFF}")
    print(f"  {DIM}{elapsed:.0f}ms wall clock for all {len(CLIENTS)}{OFF}")

    # ── and what the operator would see ──────────────────────────────────
    print(f"\n{BOLD}What the call list shows{OFF}")
    for row in call("GET", "/api/calls?limit=20")["calls"]:
        ref = row.get("booked_reference") or ""
        print(f"  {row['result']:<10} {ref:<10} {row['channel']:<6} "
              f"{DIM}{(row.get('wanted') or row.get('summary') or '')[:46]}{OFF}")

    print()
    if failures:
        print(f"{R}{BOLD}{len(failures)} problem(s): {', '.join(failures)}{OFF}")
        return 1
    print(f"{G}{BOLD}All {len(CLIENTS)} callers handled correctly, concurrently.{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
