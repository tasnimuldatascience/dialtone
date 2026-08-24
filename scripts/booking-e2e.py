"""An end-to-end booking, against the running gateway, over the real API.

WHAT THIS PROVES that a unit test cannot. Every part of the booking path has tests, and all of
them pass against fakes. This drives the HTTP and WebSocket surface a browser drives, with the
real model generating the replies and the real SQLite file underneath, and then goes and looks in
the database. Two questions only this can answer:

  DOES THE WIRING HOLD?      The model, the memory, the calendar, the store and the socket are
                             each fine alone. A real call is the only thing that runs them in
                             one process in the right order.
  DID ANYTHING PERSIST?      "The call went well" is not a result. A row with a reference the
                             caller could quote back tomorrow is.

Run the gateway first, then:  python scripts/booking-e2e.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

PORT = os.environ.get("DIALTONE_PORT", "8071")
BASE = f"http://127.0.0.1:{PORT}"
DB = Path(__file__).resolve().parent.parent / "services" / "gateway" / "dialtone.db"

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def call(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read())


def check(label: str, passed: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{OFF}" if passed else f"{RED}FAIL{OFF}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{OFF}" if detail else ""))
    return passed


def main() -> int:
    try:
        health = call("GET", "/api/health")
    except urllib.error.URLError:
        print(f"{RED}The gateway is not running.{OFF}  Start it with:")
        print("  cd services/gateway && python -m dialtone.cli serve")
        return 2

    if health["status"] != "ready":
        print(f"{RED}The model is still loading.{OFF} Try again in a few seconds.")
        return 2

    agent_id = call("GET", "/api/agents")["agents"][0]["id"]
    ok = True

    print(f"\n{BOLD}The diary before the call{OFF}")
    before = call("GET", f"/api/agents/{agent_id}/availability")
    ok &= check("availability is served", before["total_open"] > 0,
                f"{before['total_open']} slots free")
    target = before["open"][0]
    print(f"  {DIM}first free: {target['spoken']}{OFF}")

    print(f"\n{BOLD}The call{OFF}")
    started = call("POST", "/api/calls", {"agent_id": agent_id, "channel": "text"})
    call_id = started["call_id"]
    print(f"  {DIM}agent  {started['greeting']}{OFF}")

    # The socket carries the conversation. urllib cannot speak WebSocket, so the turns go over
    # the REST surface the studio also has -- the same Conversation object either way.
    import asyncio

    import websockets

    async def converse() -> list[dict]:
        events: list[dict] = []
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/call/{call_id}") as socket:
            await socket.recv()  # ready
            for line in [
                "hi, I need an appointment, my tooth is hurting",
                f"can I come {target['spoken'].split(' at ')[0]}?",
                # The time as the practice actually offers it. Asking for a rounded hour is a
                # different test -- that the agent REFUSES a slot that does not exist -- and it
                # has one of its own.
                f"how about {target['spoken'].split(' at ', 1)[1]}",
            ]:
                print(f"  {DIM}caller {line}{OFF}")
                await socket.send(json.dumps({"type": "say", "text": line}))
                while True:
                    event = json.loads(await socket.recv())
                    events.append(event)
                    if event["type"] == "done":
                        print(f"  {DIM}agent  {event['agent']}{OFF}")
                        break

            # The details are typed, not spoken. That is the product decision this whole path
            # exists to serve, so the test takes the same route a caller does.
            result = call("PATCH", f"/api/calls/{call_id}/details", {
                "name": "Tasnimul Hasan",
                "phone": "(212) 555-0142",
                "email": "tasnimul@example.com",
            })
            print(f"  {DIM}[form]  name, phone and email typed{OFF}")
            events.append({"type": "details", **result})

            print(f"  {DIM}caller yes, that works{OFF}")
            await socket.send(json.dumps({"type": "say", "text": "yes, that works"}))
            while True:
                event = json.loads(await socket.recv())
                events.append(event)
                if event["type"] == "done":
                    print(f"  {DIM}agent  {event['agent']}{OFF}")
                    break
            await socket.send(json.dumps({"type": "hangup"}))
        return events

    events = asyncio.run(converse())

    print(f"\n{BOLD}What the agent ended up knowing{OFF}")
    memory = next(e["memory"] for e in reversed(events) if e.get("memory"))
    for name, fact in memory["facts"].items():
        print(f"  {name:<8} {fact['value']}  {DIM}({fact['source']}){OFF}")

    ok &= check("the typed name survived, not the heard one",
                memory["facts"].get("name", {}).get("value") == "Tasnimul Hasan")
    ok &= check("the typed values are marked confirmed",
                all(memory["facts"][f]["confirmed"] for f in ("name", "phone", "email")))
    ok &= check("a slot was put on the table", bool(memory["proposed_slot"]),
                memory["proposed_slot"])
    ok &= check("nothing is still missing", not memory["missing"],
                ", ".join(memory["missing"]) or "—")

    print(f"\n{BOLD}The booking{OFF}")
    booked = next((e for e in events if e.get("type") == "booked"), None) or next(
        (e["booked"] for e in events if e.get("type") == "details" and e.get("booked")), None
    )
    ok &= check("an appointment was made", booked is not None)
    if booked:
        print(f"  {DIM}{booked['reference']} — {booked['spoken']}{OFF}")

        rows = sqlite3.connect(DB).execute(
            "SELECT reference, starts_at, patient_name, phone, email, reason "
            "FROM appointments WHERE reference = ?", (booked["reference"],),
        ).fetchall()
        ok &= check("it is in the database", len(rows) == 1)
        if rows:
            reference, starts_at, name, phone, email, reason = rows[0]
            print(f"  {DIM}row: {starts_at}  {name}  {phone}  {email}  {reason}{OFF}")
            ok &= check("the row carries the typed name", name == "Tasnimul Hasan")
            ok &= check("the row carries the typed phone", phone == "(212) 555-0142")
            ok &= check("the row carries the typed email", email == "tasnimul@example.com")

        after = call("GET", f"/api/agents/{agent_id}/availability")
        ok &= check("the slot has left the calendar",
                    booked["starts_at"] not in {s["iso"] for s in after["open"]})
        ok &= check("the diary lists it",
                    any(a["reference"] == booked["reference"]
                        for a in call("GET", "/api/appointments")["appointments"]))

        # The guarantee the whole thing rests on: a second caller cannot have the same slot.
        second = call("POST", "/api/calls", {"agent_id": agent_id, "channel": "text"})
        try:
            call("PATCH", f"/api/calls/{second['call_id']}/details", {"name": "Someone Else"})
        finally:
            call("POST", f"/api/calls/{second['call_id']}/end")
        ok &= check("that slot cannot be taken twice",
                    booked["starts_at"] not in {
                        s["iso"] for s in call(
                            "GET", f"/api/agents/{agent_id}/availability")["open"]
                    })

    print()
    print(f"{GREEN}{BOLD}Everything held.{OFF}" if ok else f"{RED}{BOLD}Something broke.{OFF}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
