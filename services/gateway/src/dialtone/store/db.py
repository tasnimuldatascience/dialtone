"""Persistence for the whole platform: agents, knowledge, numbers, calls, campaigns.

SQLITE, DELIBERATELY. One contact centre's data is agents (tens), documents (hundreds), and
calls (thousands per day). SQLite in WAL mode handles that on a laptop with room to spare, needs
no service to run, and makes the whole product a `git clone` away from working. The moment this
is genuinely too small the answer is Postgres with the same schema, not a bigger SQLite.

WHAT IS AND IS NOT STORED. Transcripts are stored REDACTED — the redaction happens in the
conversation engine before anything reaches here, so a card number cannot be in the database even
if someone later queries it directly. That ordering is the point: a system that stores raw text
and redacts on the way out has already lost, because the raw text is on disk and in the backups.

TIMINGS ARE STORED PER TURN. Not aggregated, not averaged. An operator asking "why did that call
feel slow" needs the turn, and an average cannot answer it. Aggregation happens at read time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("dialtone.store")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agents (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    business      TEXT NOT NULL,
    persona       TEXT NOT NULL,
    greeting      TEXT NOT NULL,
    voice         TEXT NOT NULL DEFAULT 'female-warm',
    temperature   REAL NOT NULL DEFAULT 0.4,
    use_knowledge INTEGER NOT NULL DEFAULT 1,
    flow_json     TEXT,
    intake_json   TEXT,                            -- what this agent asks callers for
    status        TEXT NOT NULL DEFAULT 'draft',   -- draft | live | paused
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'upload',
    chunks     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_agent ON documents(agent_id);

CREATE TABLE IF NOT EXISTS numbers (
    id         TEXT PRIMARY KEY,
    e164       TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL DEFAULT '',
    agent_id   TEXT REFERENCES agents(id) ON DELETE SET NULL,
    provider   TEXT NOT NULL DEFAULT 'simulator',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    number_id     TEXT REFERENCES numbers(id) ON DELETE SET NULL,
    direction     TEXT NOT NULL DEFAULT 'inbound',
    from_number   TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    -- completed | abandoned | transferred | failed. Kept distinct because they mean different
    -- things to an operator: abandoned is a staffing problem, transferred is a coverage gap.
    outcome       TEXT NOT NULL DEFAULT 'in_progress',
    resolved      INTEGER NOT NULL DEFAULT 0,
    escalated     INTEGER NOT NULL DEFAULT 0,
    sentiment     TEXT NOT NULL DEFAULT 'neutral',
    summary       TEXT NOT NULL DEFAULT '',
    channel       TEXT NOT NULL DEFAULT 'text',    -- text | voice | simulated
    campaign_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_agent   ON calls(agent_id);
CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at);

CREATE TABLE IF NOT EXISTS turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    caller       TEXT NOT NULL DEFAULT '',
    agent        TEXT NOT NULL DEFAULT '',
    spoken       TEXT NOT NULL DEFAULT '',
    node         TEXT NOT NULL DEFAULT '',
    moved_to     TEXT NOT NULL DEFAULT '',
    timing_json  TEXT NOT NULL DEFAULT '{}',
    citations    TEXT NOT NULL DEFAULT '[]',
    tools        TEXT NOT NULL DEFAULT '[]',
    redacted     TEXT NOT NULL DEFAULT '[]',
    refused      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_call ON turns(call_id);

CREATE TABLE IF NOT EXISTS appointments (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    call_id     TEXT REFERENCES calls(id) ON DELETE SET NULL,
    reference   TEXT NOT NULL UNIQUE,
    -- ISO start time. UNIQUE because it is the whole booking guarantee: two callers cannot be
    -- given the same slot, and enforcing that in the schema means a race between two live calls
    -- fails loudly at insert rather than quietly double-booking.
    starts_at   TEXT NOT NULL UNIQUE,
    duration_min INTEGER NOT NULL DEFAULT 30,
    patient_name TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'booked',   -- booked | cancelled | attended | no_show
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appt_starts ON appointments(starts_at);
CREATE INDEX IF NOT EXISTS idx_appt_agent  ON appointments(agent_id);

CREATE TABLE IF NOT EXISTS campaigns (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',     -- draft | running | paused | done
    script      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_contacts (
    id          TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name        TEXT NOT NULL DEFAULT '',
    e164        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | called | answered | failed
    call_id     TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_contacts_campaign ON campaign_contacts(campaign_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    """Everything the platform persists.

    One connection guarded by a lock rather than a pool. SQLite serialises writes anyway, and a
    pool over one file buys contention rather than throughput; the honest version is to say so
    and keep the code readable.
    """

    def __init__(self, path: str | Path = "dialtone.db") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Add columns to a database that predates them.

        `CREATE TABLE IF NOT EXISTS` covers a new table and does nothing at all for a new COLUMN,
        so an existing database silently keeps the old shape and every read of the new field comes
        back missing. Adding them here rather than shipping a migration tool: the set is small,
        each one is nullable with a sensible default, and a schema this size does not justify a
        framework.
        """
        for table, column, ddl in (
            ("agents", "intake_json", "ALTER TABLE agents ADD COLUMN intake_json TEXT"),
            ("calls", "wanted", "ALTER TABLE calls ADD COLUMN wanted TEXT"),
        ):
            have = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._db.execute(ddl)
                log.info("added %s.%s", table, column)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- agents ------------------------------------------------------------
    def create_agent(self, **fields: Any) -> dict[str, Any]:
        agent_id = fields.pop("id", None) or _uid("agt")
        now = _now()
        row = {
            "id": agent_id,
            "name": fields.get("name", "New agent"),
            "business": fields.get("business", "Acme"),
            "persona": fields.get("persona", "a warm, efficient receptionist"),
            "greeting": fields.get("greeting", "Hello, how can I help?"),
            "voice": fields.get("voice", "female-warm"),
            "temperature": float(fields.get("temperature", 0.4)),
            "use_knowledge": int(bool(fields.get("use_knowledge", True))),
            "flow_json": json.dumps(fields["flow"]) if fields.get("flow") else None,
            "intake_json": json.dumps(fields["intake"]) if fields.get("intake") else None,
            "status": fields.get("status", "draft"),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._db.execute(
                "INSERT INTO agents (id,name,business,persona,greeting,voice,temperature,"
                "use_knowledge,flow_json,intake_json,status,created_at,updated_at) VALUES "
                "(:id,:name,:business,:persona,:greeting,:voice,:temperature,:use_knowledge,"
                ":flow_json,:intake_json,:status,:created_at,:updated_at)", row,
            )
            self._db.commit()
        return self.get_agent(agent_id)  # type: ignore[return-value]

    def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"name", "business", "persona", "greeting", "voice", "temperature",
                   "use_knowledge", "status"}
        sets, values = [], {}
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = :{key}")
                values[key] = int(value) if key == "use_knowledge" else value
        if "flow" in fields:
            sets.append("flow_json = :flow_json")
            values["flow_json"] = json.dumps(fields["flow"])
        if "intake" in fields:
            sets.append("intake_json = :intake_json")
            values["intake_json"] = json.dumps(fields["intake"])
        if not sets:
            return self.get_agent(agent_id)

        values["id"] = agent_id
        values["updated_at"] = _now()
        with self._lock:
            self._db.execute(
                f"UPDATE agents SET {', '.join(sets)}, updated_at = :updated_at WHERE id = :id",
                values,
            )
            self._db.commit()
        return self.get_agent(agent_id)

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return _agent(row) if row else None

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT a.*, "
                "  (SELECT COUNT(*) FROM calls c WHERE c.agent_id = a.id) AS call_count, "
                "  (SELECT COUNT(*) FROM documents d WHERE d.agent_id = a.id) AS doc_count "
                "FROM agents a ORDER BY a.created_at"
            ).fetchall()
        return [_agent(r) for r in rows]

    def delete_agent(self, agent_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            self._db.commit()
        return cur.rowcount > 0

    # -- documents ---------------------------------------------------------
    def add_document(self, agent_id: str, title: str, body: str, *,
                     source: str = "upload", chunks: int = 0) -> dict[str, Any]:
        doc_id = _uid("doc")
        with self._lock:
            self._db.execute(
                "INSERT INTO documents (id,agent_id,title,body,source,chunks,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (doc_id, agent_id, title, body, source, chunks, _now()),
            )
            self._db.commit()
        return self.get_document(doc_id)  # type: ignore[return-value]

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def list_documents(self, agent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,agent_id,title,source,chunks,created_at,LENGTH(body) AS size "
                "FROM documents WHERE agent_id = ? ORDER BY created_at DESC", (agent_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_document(self, doc_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self._db.commit()
        return cur.rowcount > 0

    def set_document_chunks(self, doc_id: str, chunks: int) -> None:
        with self._lock:
            self._db.execute("UPDATE documents SET chunks = ? WHERE id = ?", (chunks, doc_id))
            self._db.commit()

    # -- numbers -----------------------------------------------------------
    def add_number(self, e164: str, *, label: str = "", agent_id: str | None = None,
                   provider: str = "simulator") -> dict[str, Any]:
        number_id = _uid("num")
        with self._lock:
            self._db.execute(
                "INSERT INTO numbers (id,e164,label,agent_id,provider,created_at) "
                "VALUES (?,?,?,?,?,?)", (number_id, e164, label, agent_id, provider, _now()),
            )
            self._db.commit()
        return {"id": number_id, "e164": e164, "label": label, "agent_id": agent_id,
                "provider": provider}

    def list_numbers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT n.*, a.name AS agent_name FROM numbers n "
                "LEFT JOIN agents a ON a.id = n.agent_id ORDER BY n.created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def assign_number(self, number_id: str, agent_id: str | None) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE numbers SET agent_id = ? WHERE id = ?", (agent_id, number_id)
            )
            self._db.commit()
        return cur.rowcount > 0

    # -- calls -------------------------------------------------------------
    def start_call(self, agent_id: str, *, from_number: str = "", direction: str = "inbound",
                   channel: str = "text", campaign_id: str | None = None) -> str:
        call_id = _uid("call")
        with self._lock:
            self._db.execute(
                "INSERT INTO calls (id,agent_id,direction,from_number,started_at,channel,"
                "campaign_id) VALUES (?,?,?,?,?,?,?)",
                (call_id, agent_id, direction, from_number, _now(), channel, campaign_id),
            )
            self._db.commit()
        return call_id

    def add_turn(self, call_id: str, ordinal: int, record: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO turns (call_id,ordinal,caller,agent,spoken,node,moved_to,"
                "timing_json,citations,tools,redacted,refused,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    call_id, ordinal,
                    record.get("caller", ""), record.get("agent", ""), record.get("spoken", ""),
                    record.get("node", ""), record.get("moved_to", ""),
                    json.dumps(record.get("timing", {})),
                    json.dumps(record.get("citations", [])),
                    json.dumps(record.get("tools", [])),
                    json.dumps(record.get("redacted", [])),
                    record.get("refused", ""), _now(),
                ),
            )
            self._db.commit()

    def end_call(self, call_id: str, *, outcome: str = "completed", resolved: bool = False,
                 escalated: bool = False, sentiment: str = "neutral", summary: str = "",
                 wanted: str = "", duration_ms: int = 0) -> None:
        """Close a call.

        `summary` is the caller's own opening words -- what the call SOUNDED like. `wanted` is
        what it was ABOUT, derived from what the agent had to look up and what it ended up doing.
        Two different questions, and one string was answering neither well.
        """
        with self._lock:
            self._db.execute(
                "UPDATE calls SET ended_at = ?, outcome = ?, resolved = ?, escalated = ?, "
                "sentiment = ?, summary = ?, wanted = ?, duration_ms = ? WHERE id = ?",
                (_now(), outcome, int(resolved), int(escalated), sentiment, summary, wanted,
                 duration_ms, call_id),
            )
            self._db.commit()

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT c.*, a.name AS agent_name FROM calls c "
                "LEFT JOIN agents a ON a.id = c.agent_id WHERE c.id = ?", (call_id,)
            ).fetchone()
            if not row:
                return None
            turns = self._db.execute(
                "SELECT * FROM turns WHERE call_id = ? ORDER BY ordinal", (call_id,)
            ).fetchall()
            # THE OUTCOME, not just the conversation. A call record that shows what was said and
            # not what was DONE makes an operator read the whole transcript to answer "did this
            # one book?" -- which is the first question anyone asks of a call list.
            booked = self._db.execute(
                "SELECT * FROM appointments WHERE call_id = ? AND status = 'booked' "
                "ORDER BY created_at LIMIT 1", (call_id,)
            ).fetchone()
        call = dict(row)
        call["turns"] = [_turn(t) for t in turns]
        call["appointment"] = dict(booked) if booked else None
        call["booked_reference"] = booked["reference"] if booked else None
        call["turn_count"] = len(turns)
        call["result"] = _result(call)
        return call

    def list_calls(self, *, agent_id: str | None = None, limit: int = 100,
                   outcome: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if agent_id:
            where.append("c.agent_id = ?")
            params.append(agent_id)
        if outcome:
            where.append("c.outcome = ?")
            params.append(outcome)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            # THE BOOKING REFERENCE COMES BACK WITH THE ROW. Without it the list could say a call
            # "completed" -- which was true of all sixteen calls on screen and told an operator
            # nothing -- while the question they are actually asking is "did this one book?".
            rows = self._db.execute(
                f"SELECT c.*, a.name AS agent_name, "
                f"  (SELECT COUNT(*) FROM turns t WHERE t.call_id = c.id) AS turn_count, "
                f"  (SELECT ap.reference FROM appointments ap "
                f"     WHERE ap.call_id = c.id AND ap.status = 'booked' "
                f"     ORDER BY ap.created_at LIMIT 1) AS booked_reference, "
                f"  (SELECT ap.starts_at FROM appointments ap "
                f"     WHERE ap.call_id = c.id AND ap.status = 'booked' "
                f"     ORDER BY ap.created_at LIMIT 1) AS booked_for "
                f"FROM calls c LEFT JOIN agents a ON a.id = c.agent_id {clause} "
                f"ORDER BY c.started_at DESC LIMIT ?", (*params, limit),
            ).fetchall()
        return [_call_row(dict(r)) for r in rows]

    # -- campaigns ---------------------------------------------------------
    def create_campaign(self, agent_id: str, name: str, script: str = "") -> dict[str, Any]:
        campaign_id = _uid("cmp")
        with self._lock:
            self._db.execute(
                "INSERT INTO campaigns (id,agent_id,name,script,created_at) VALUES (?,?,?,?,?)",
                (campaign_id, agent_id, name, script, _now()),
            )
            self._db.commit()
        return {"id": campaign_id, "agent_id": agent_id, "name": name, "status": "draft",
                "script": script, "contacts": 0}

    def add_contacts(self, campaign_id: str, contacts: list[dict[str, str]]) -> int:
        with self._lock:
            self._db.executemany(
                "INSERT INTO campaign_contacts (id,campaign_id,name,e164) VALUES (?,?,?,?)",
                [(_uid("ct"), campaign_id, c.get("name", ""), c["e164"]) for c in contacts],
            )
            self._db.commit()
        return len(contacts)

    def list_campaigns(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT c.*, a.name AS agent_name, "
                "  (SELECT COUNT(*) FROM campaign_contacts k WHERE k.campaign_id = c.id) "
                "    AS contacts, "
                "  (SELECT COUNT(*) FROM campaign_contacts k WHERE k.campaign_id = c.id "
                "    AND k.status <> 'pending') AS reached "
                "FROM campaigns c LEFT JOIN agents a ON a.id = c.agent_id "
                "ORDER BY c.created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def campaign_contacts(self, campaign_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM campaign_contacts WHERE campaign_id = ? ORDER BY rowid",
                (campaign_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def set_campaign_status(self, campaign_id: str, status: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id)
            )
            self._db.commit()

    def mark_contact(self, contact_id: str, status: str, call_id: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE campaign_contacts SET status = ?, call_id = ?, attempts = attempts + 1 "
                "WHERE id = ?", (status, call_id, contact_id),
            )
            self._db.commit()

    # -- appointments ------------------------------------------------------
    def taken_slots(self, *, from_iso: str = "") -> set[str]:
        """Start times that are already booked. The only thing the calendar needs from here."""
        with self._lock:
            rows = self._db.execute(
                "SELECT starts_at FROM appointments WHERE status = 'booked' AND starts_at >= ?",
                (from_iso,),
            ).fetchall()
        return {r["starts_at"] for r in rows}

    def book(self, agent_id: str, starts_at: str, **fields: Any) -> dict[str, Any] | None:
        """Reserve a slot. Returns None if somebody else already has it.

        The UNIQUE constraint on `starts_at` is what makes this safe rather than the check above
        it: two calls can both find a slot free and both try to take it, and only one insert can
        win. Returning None instead of raising lets the agent say "that just went" and offer
        another, which is what a receptionist would do.
        """
        appointment_id = _uid("apt")
        reference = f"NG{uuid.uuid4().hex[:6].upper()}"
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO appointments (id,agent_id,call_id,reference,starts_at,"
                    "duration_min,patient_name,phone,email,reason,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (appointment_id, agent_id, fields.get("call_id"), reference, starts_at,
                     int(fields.get("duration_min", 30)), fields.get("patient_name", ""),
                     fields.get("phone", ""), fields.get("email", ""), fields.get("reason", ""),
                     _now()),
                )
                self._db.commit()
        except sqlite3.IntegrityError:
            return None
        return self.appointment(appointment_id)

    def appointment(self, appointment_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_appointments(self, *, agent_id: str | None = None, upcoming: bool = False,
                          limit: int = 200) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        if agent_id:
            where.append("a.agent_id = ?")
            params.append(agent_id)
        if upcoming:
            where.append("a.starts_at >= ?")
            params.append(datetime.now(UTC).isoformat(timespec="minutes")[:16])
        with self._lock:
            rows = self._db.execute(
                f"SELECT a.*, g.name AS agent_name FROM appointments a "
                f"LEFT JOIN agents g ON g.id = a.agent_id "
                f"WHERE {' AND '.join(where)} ORDER BY a.starts_at LIMIT ?", (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def cancel_appointment(self, appointment_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment_id,)
            )
            self._db.commit()
        return cur.rowcount > 0

    # -- analytics ---------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        """Headline numbers for the dashboard.

        Computed with SQL rather than by loading calls into Python, because this runs on every
        dashboard poll and the whole point of a database is that it is better at this.
        """
        with self._lock:
            totals = self._db.execute(
                "SELECT COUNT(*) AS calls, "
                "  COALESCE(SUM(resolved),0) AS resolved, "
                "  COALESCE(SUM(escalated),0) AS escalated, "
                "  COALESCE(AVG(NULLIF(duration_ms,0)),0) AS avg_duration, "
                "  COALESCE(SUM(CASE WHEN outcome='abandoned' THEN 1 ELSE 0 END),0) AS abandoned "
                "FROM calls WHERE ended_at IS NOT NULL"
            ).fetchone()
            agents = self._db.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
            live = self._db.execute(
                "SELECT COUNT(*) AS n FROM calls WHERE ended_at IS NULL"
            ).fetchone()["n"]
            docs = self._db.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            booked = self._db.execute(
                "SELECT COUNT(*) AS n FROM appointments WHERE status = 'booked'"
            ).fetchone()["n"]

            # Median rather than mean for latency. One 9-second turn where a tool timed out
            # drags a mean far enough to hide that every other turn was fine.
            latencies = [
                json.loads(r["timing_json"]).get("total_ms", 0)
                for r in self._db.execute(
                    "SELECT timing_json FROM turns ORDER BY id DESC LIMIT 500"
                ).fetchall()
            ]
            by_day = self._db.execute(
                "SELECT substr(started_at,1,10) AS day, COUNT(*) AS calls, "
                "  COALESCE(SUM(resolved),0) AS resolved "
                "FROM calls GROUP BY day ORDER BY day DESC LIMIT 14"
            ).fetchall()
            sentiment = self._db.execute(
                "SELECT sentiment, COUNT(*) AS n FROM calls WHERE ended_at IS NOT NULL "
                "GROUP BY sentiment"
            ).fetchall()

        latencies = sorted(v for v in latencies if v)
        total = totals["calls"] or 0
        return {
            "calls": total,
            "live": live,
            "agents": agents,
            "documents": docs,
            "appointments": booked,
            "resolved": totals["resolved"],
            "escalated": totals["escalated"],
            "abandoned": totals["abandoned"],
            "containment": round(totals["resolved"] / total, 3) if total else 0.0,
            "escalation_rate": round(totals["escalated"] / total, 3) if total else 0.0,
            "avg_duration_ms": round(totals["avg_duration"] or 0),
            "median_turn_ms": _percentile(latencies, 0.5),
            "p90_turn_ms": _percentile(latencies, 0.9),
            # Densified. The query returns only days that HAVE calls, so a quiet week produced a
            # one-bar chart stretched across the whole panel -- which reads as "every call
            # happened at once" rather than "there was one day of calls". A day with no calls is
            # still a day, and the gap is the information.
            "by_day": _dense_days([dict(r) for r in by_day], days=14),
            "sentiment": {r["sentiment"]: r["n"] for r in sentiment},
        }



def _dense_days(rows: list[dict[str, Any]], *, days: int) -> list[dict[str, Any]]:
    """A contiguous daily series ending today, zero-filled where nothing happened."""
    from datetime import date, timedelta

    found = {r["day"]: r for r in rows}
    today = date.today()
    out: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        key = (today - timedelta(days=offset)).isoformat()
        out.append(found.get(key) or {"day": key, "calls": 0, "resolved": 0})
    return out

def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(q * len(values)) - 1))
    return round(values[index], 1)


#: What actually happened on a call, as a word an operator can filter on.
#:
#: NOT the `outcome` column, which records how the SOCKET ended and read "completed" for every
#: call ever placed. A column whose value is the same on every row is not a column; it is a
#: decoration that costs horizontal space and teaches the reader to stop looking at that part of
#: the screen.
def _result(row: dict[str, Any]) -> str:
    if row.get("booked_reference"):
        return "booked"
    if row.get("escalated"):
        return "passed on"
    if not row.get("turn_count"):
        # Connected and nobody said anything: a hang-up, a wrong number, or a test. Worth its own
        # word rather than being filed under the same label as a call that did something.
        return "no speech"
    if row.get("outcome") == "abandoned":
        return "abandoned"
    return "answered"


def _call_row(row: dict[str, Any]) -> dict[str, Any]:
    row["result"] = _result(row)
    # Rows written before the column existed have nothing in it, and a history that is blank for
    # everything older than a deploy is worse than one that is merely coarse.
    if not row.get("wanted"):
        row["wanted"] = _wanted_from_row(row)
    return row


def _wanted_from_row(row: dict[str, Any]) -> str:
    """A best guess for a call recorded before `wanted` was stored.

    Only what the row itself carries -- no turn lookup, because this runs once per row in a list
    of two hundred and a query per row is how a list gets slow.
    """
    if row.get("booked_reference"):
        return "Booked an appointment"
    if row.get("escalated"):
        return "Asked for a person"
    if not row.get("turn_count"):
        return "Nobody spoke"
    return ""


def _agent(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["use_knowledge"] = bool(out.get("use_knowledge", 1))
    flow = out.pop("flow_json", None)
    out["flow"] = json.loads(flow) if flow else None
    intake = out.pop("intake_json", None)
    out["intake"] = json.loads(intake) if intake else None
    return out


def _turn(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("timing_json", "citations", "tools", "redacted"):
        raw = out.pop(key) if key == "timing_json" else out.get(key)
        try:
            parsed = json.loads(raw) if raw else ({} if key == "timing_json" else [])
        except (TypeError, ValueError):
            parsed = {} if key == "timing_json" else []
        out["timing" if key == "timing_json" else key] = parsed
    return out
